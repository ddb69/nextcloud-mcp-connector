"""What a flood of invalid credentials costs, measured in Nextcloud round trips (T-05-22).

**Why this file does not measure milliseconds.** The finding of the phase research that
decides this measurement comes out of the HaRP source and is written down in the module head
of ``src/mcp_connector/oauth/throttle.py``: HaRP asks Nextcloud who the caller is for *every*
request that carries an ``Authorization`` header, on PUBLIC routes as well, and it caches that
answer for cookie sessions only. On top of that
``src/mcp_connector/oauth/verifier.py`` caches positive results only, so an invalid bearer
reaches the store on every single attempt. Measuring the response times of our own container
would therefore find them fine and miss the whole point: the load lands on Nextcloud, not
here. The number that matters is Nextcloud requests per attacker request.

**What this file is not.** It is not a promise about a throttle on ``/mcp``. That route
carries none on purpose (D-37), and it carries no ``bruteforce_protection`` in the manifest on
purpose either (T-02-21), because the OAuth discovery flow begins with a rejected request by
specification. The result of this measurement is a number for
``docs/exapp-install.md`` and a recommendation to the administrator (a rate limit in the
reverse proxy), not an argument for a code change here. A finding that speaks against D-37
belongs in the summary with its number, not in a spontaneous fix.

**The two credential forms behave in opposite ways, so each gets its own run.**

* Run A, an invalid **bearer**: one Nextcloud PHP round trip per request, and no brute force
  entry at all, because ``Session::tryTokenLogin`` returns false without counting an attempt.
* Run B, an invalid **basic**: the same round trip, plus a brute force entry per source
  address as Nextcloud sees it, which throttles everybody who shares that address.

**Two properties of the throwaway topology the measurement has to handle.**

1. ``scripts/bootstrap_exapp.sh`` switches ``auth.bruteforce.protection.enabled`` off,
   because that guard is a property of an instance nobody can reach. With it off, run B would
   measure nothing and the empty counter would be an artifact of the fixture rather than a
   statement about Nextcloud. The measurement turns the guard on, and puts the previous value
   back when it is done.
2. Every ``docker compose`` call against ``compose.exapp.yml`` needs ``HP_SHARED_KEY`` in the
   environment or the file refuses to interpolate (WR-11), and the measuring process has no
   business knowing that key. The container names in that file are fixed
   (``container_name:``), so this file talks to ``docker exec`` and ``docker logs`` directly
   and names the compose equivalent in the log output for a reader who wants to repeat it.

Run it against the running HaRP topology::

    export HP_SHARED_KEY="$(openssl rand -hex 32)"
    docker compose -f compose.exapp.yml up -d --wait
    bash scripts/bootstrap_exapp.sh
    set -a && . ./.env.exapp && set +a
    uv run --no-sync pytest tests/integration/test_credential_flood.py -q -m integration
"""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx
import pytest
from topology import (
    CADDY_CONTAINER,
    COMPOSE_HINT,
    EXAPP_CONTAINER,
    HARP_CONTAINER,
    NC_CONTAINER,
)

pytestmark = [pytest.mark.integration]

# The app id is frozen (docs/app-id-freeze.md), so the HaRP route is a literal here, the
# same choice tests/integration/test_permission_fidelity_exapp.py makes.
EXAPP_MCP_PATH = "/exapps/mcp_connector/mcp"

# Two hundred requests per run. The number has to be far above the eight failed logins it
# takes Nextcloud to reach its maximum delay (measured below), so the run shows what happens
# after the guard has given up on delaying, and small enough that a run stays a few minutes
# on a development laptop. It is not a capacity benchmark: the interesting quantity is a
# ratio, and a ratio does not get truer with ten thousand requests.
FLOOD_SIZE = 200

# Twenty in flight at once. Higher numbers stop measuring Nextcloud and start measuring the
# file descriptor limit of the test machine and the socket queue of a single Caddy; twenty is
# well above what any real client opens and low enough that no request of the run is refused
# by our own operating system.
CONCURRENCY = 20

# The health check of the nextcloud service hits this path every five seconds. It is traffic
# of the fixture, not of the flood, so it is subtracted from every count.
HEALTHCHECK_PATH = "GET /status.php"

# The one request HaRP makes to Nextcloud to resolve the caller of an ExApp request. Counting
# it separately is what turns "the log grew" into "one PHP round trip per attacker request".
USER_INFO_PATH = "/index.php/apps/app_api/harp/user-info"

BRUTEFORCE_KEY = "auth.bruteforce.protection.enabled"

# A JSON-RPC request that would be legitimate if the credentials were. tools/list needs no
# session, so nothing but the credential decides the answer.
FLOOD_BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()

FLOOD_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _basic(user: str, secret: str) -> str:
    """A Basic header, the same shape a client hands to HaRP for a real app password."""
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


# --------------------------------------------------------------------------------------
# Talking to the topology
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Topology:
    """The handles of the running topology, resolved rather than assumed.

    ``candidates`` maps a label to an address the brute force counter could be keyed by. The
    measurement does not decide in advance which one Nextcloud uses: it reads all of them
    before and after each run and reports which one moved. In this topology Nextcloud trusts
    only the Caddy container as a proxy, so the address it sees for a HaRP request is HaRP's
    own, and that makes the counter shared by every user of every ExApp behind that proxy.
    """

    docker: str
    candidates: dict[str, str]


def _run(args: list[str], *, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, the executable comes from shutil.which
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _occ(topology: Topology, *args: str, timeout: float = 180.0) -> str:
    """One occ command inside the Nextcloud container, as www-data.

    The compose spelling of the same call is in :data:`COMPOSE_HINT`; this one avoids the
    ``HP_SHARED_KEY`` interpolation of the compose file (module head, point 2).
    """
    result = _run(
        [topology.docker, "exec", "-u", "www-data", NC_CONTAINER, "php", "occ", *args],
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"occ {' '.join(args)} failed with {result.returncode}: {result.stderr.strip()!r}"
    )
    return result.stdout


def _container_ip(docker: str, name: str) -> str | None:
    result = _run(
        [
            docker,
            "inspect",
            name,
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        ],
        timeout=60.0,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _attempts(topology: Topology) -> dict[str, int]:
    """The brute force counter of every candidate address, read one occ call at a time."""
    counters: dict[str, int] = {}
    for label, address in topology.candidates.items():
        raw = _occ(topology, "security:bruteforce:attempts", "--output=json", address)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        assert lines, f"occ answered nothing for {label} ({address})"
        payload = json.loads(lines[-1])
        counters[f"{label} ({address})"] = int(payload["attempts"])
    return counters


@dataclass(frozen=True)
class LogSnapshot:
    """The state of the Nextcloud access log at one moment, split into what matters."""

    total: int
    healthcheck: int
    user_info: int

    @property
    def requests(self) -> int:
        """Log lines that are not the five second health check of the compose file."""
        return self.total - self.healthcheck


def _log_snapshot(topology: Topology) -> LogSnapshot:
    result = _run([topology.docker, "logs", NC_CONTAINER], timeout=180.0)
    assert result.returncode == 0, f"docker logs {NC_CONTAINER} failed: {result.stderr.strip()!r}"
    lines = (result.stdout + result.stderr).splitlines()
    return LogSnapshot(
        total=len(lines),
        healthcheck=sum(1 for line in lines if HEALTHCHECK_PATH in line),
        user_info=sum(1 for line in lines if USER_INFO_PATH in line),
    )


# --------------------------------------------------------------------------------------
# The runs and their numbers
# --------------------------------------------------------------------------------------


@dataclass
class Run:
    """One flood of :data:`FLOOD_SIZE` requests with one shape of invalid credential."""

    name: str
    credential: str
    statuses: Counter[int] = field(default_factory=Counter)
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0
    nc_requests: int | None = None
    user_info: int | None = None
    attempts_before: dict[str, int] = field(default_factory=dict)
    attempts_after: dict[str, int] = field(default_factory=dict)

    @property
    def answered(self) -> int:
        return sum(self.statuses.values())

    @property
    def ratio(self) -> float | None:
        """Nextcloud requests per attacker request, the number this file exists for."""
        if self.nc_requests is None:
            return None
        return round(self.nc_requests / FLOOD_SIZE, 2)

    @property
    def user_info_ratio(self) -> float | None:
        if self.user_info is None:
            return None
        return round(self.user_info / FLOOD_SIZE, 2)

    @property
    def attempts_growth(self) -> dict[str, int]:
        return {
            key: self.attempts_after[key] - self.attempts_before.get(key, 0)
            for key in self.attempts_after
        }

    def report(self) -> str:
        lines = [
            (
                f"run {self.name} ({self.credential}), {FLOOD_SIZE} attacker requests,"
                f" {CONCURRENCY} in flight, {self.seconds:.1f} s"
            ),
            f"  status distribution: {dict(sorted(self.statuses.items()))}",
            f"  transport failures: {len(self.failures)}",
        ]
        if self.nc_requests is None:
            lines.append("  nextcloud requests: not measured (no docker)")
        else:
            lines.append(
                f"  nextcloud requests: {self.nc_requests}"
                f" (ratio {self.ratio}), of them {self.user_info} user-info"
                f" (ratio {self.user_info_ratio})"
            )
            lines.append(f"  bruteforce growth: {self.attempts_growth}")
        return "\n".join(lines)


@dataclass
class Measurement:
    """Both runs, the reset proof, and what had to be changed to make them meaningful."""

    bearer: Run
    basic: Run
    measured: bool
    date: str
    bruteforce_was: str | None = None
    reset_addresses: list[str] = field(default_factory=list)
    attempts_after_reset: dict[str, int] = field(default_factory=dict)

    def report(self) -> str:
        lines = [
            "",
            f"=== credential flood measurement, {self.date} ===",
            f"occ command shape: {COMPOSE_HINT} <command>",
            f"bruteforce guard before the measurement: {self.bruteforce_was}",
            self.bearer.report(),
            self.basic.report(),
        ]
        if self.measured:
            lines.append(f"  reset: {', '.join(self.reset_addresses) or 'nothing to reset'}")
            lines.append(f"  counters after reset: {self.attempts_after_reset}")
        return "\n".join(lines)


async def _flood(url: str, run: Run, authorization: list[str]) -> None:
    """Fire :data:`FLOOD_SIZE` requests at ``url``, at most :data:`CONCURRENCY` at once.

    ``asyncio.gather`` with ``return_exceptions=True``: a request that fails on the transport
    is a finding of its own (a hung or dropped connection is not a refusal), so it is
    collected instead of ending the run.
    """
    limit = asyncio.Semaphore(CONCURRENCY)
    started = time.monotonic()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=180.0)) as client:

        async def one(header: str) -> int:
            async with limit:
                response = await client.post(
                    url,
                    headers={**FLOOD_HEADERS, "Authorization": header},
                    content=FLOOD_BODY,
                )
                return response.status_code

        outcomes = await asyncio.gather(
            *(one(header) for header in authorization), return_exceptions=True
        )

    run.seconds = time.monotonic() - started
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            run.failures.append(f"{type(outcome).__name__}: {outcome}")
        else:
            run.statuses[outcome] += 1


def _bearer_headers() -> list[str]:
    """A distinct bearer per request, none of which this server ever issued."""
    return [f"Bearer {uuid.uuid4().hex}{uuid.uuid4().hex}" for _ in range(FLOOD_SIZE)]


def _basic_headers() -> list[str]:
    """A distinct non existent account per request, each with a wrong password."""
    return [_basic(f"ghost{uuid.uuid4().hex[:10]}", uuid.uuid4().hex) for _ in range(FLOOD_SIZE)]


async def _measure(base: str, topology: Topology | None) -> Measurement:
    url = base.rstrip("/") + EXAPP_MCP_PATH
    measurement = Measurement(
        bearer=Run("A", "invalid bearer"),
        basic=Run("B", "invalid basic"),
        measured=topology is not None,
        date=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    )

    if topology is None:
        # The answers are still worth asserting without docker; the numbers are not
        # obtainable, and the tests that need them skip with that reason.
        await _flood(url, measurement.bearer, _bearer_headers())
        await _flood(url, measurement.basic, _basic_headers())
        return measurement

    measurement.bruteforce_was = _occ(topology, "config:system:get", BRUTEFORCE_KEY).strip()
    guard_changed = measurement.bruteforce_was != "true"
    if guard_changed:
        _occ(topology, "config:system:set", BRUTEFORCE_KEY, "--value=true", "--type=boolean")

    try:
        # A clean baseline for run A: whatever a previous run or a mistyped password left
        # behind would otherwise show up as growth that this flood did not cause.
        for address in topology.candidates.values():
            _occ(topology, "security:bruteforce:reset", address)

        for run, headers in (
            (measurement.bearer, _bearer_headers()),
            (measurement.basic, _basic_headers()),
        ):
            run.attempts_before = _attempts(topology)
            before = _log_snapshot(topology)
            await _flood(url, run, headers)
            after = _log_snapshot(topology)
            run.attempts_after = _attempts(topology)
            run.nc_requests = after.requests - before.requests
            run.user_info = after.user_info - before.user_info

        # Mandatory, not tidiness: without the reset the instance stays throttled for the
        # address the flood filled, which is the address of HaRP and therefore of every
        # user of this app (T-05-24).
        for address in topology.candidates.values():
            _occ(topology, "security:bruteforce:reset", address)
            measurement.reset_addresses.append(address)
        measurement.attempts_after_reset = _attempts(topology)
    finally:
        if guard_changed:
            _occ(
                topology,
                "config:system:set",
                BRUTEFORCE_KEY,
                f"--value={measurement.bruteforce_was}",
                "--type=boolean",
            )

    return measurement


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def flood_base() -> str:
    """The reverse proxy front of the topology, from ``.env.exapp``.

    No app password is needed anywhere in this file: every credential it sends is invalid on
    purpose. When the topology is not configured the file skips, the shape the rest of the
    integration suite uses, so ``uv run --no-sync pytest -q`` stays untouched.
    """
    base = (os.environ.get("NC_MCP_URL") or "").strip()
    app_id = (os.environ.get("APP_ID") or "").strip()
    if not base or not app_id:
        pytest.skip("no ExApp topology configured (missing: NC_MCP_URL, APP_ID)")
    assert app_id == "mcp_connector", (
        f"the app id is frozen as mcp_connector but APP_ID is {app_id!r}"
    )
    return base


@pytest.fixture(scope="module")
def topology() -> Topology | None:
    """The docker handles, or ``None`` when this process cannot reach the daemon."""
    docker = shutil.which("docker")
    if docker is None:
        return None
    if _run([docker, "version", "--format", "{{.Server.Version}}"], timeout=60.0).returncode != 0:
        return None
    candidates: dict[str, str] = {}
    for label, container in (
        ("harp", HARP_CONTAINER),
        ("caddy", CADDY_CONTAINER),
        ("exapp", EXAPP_CONTAINER),
    ):
        address = _container_ip(docker, container)
        if address is not None:
            candidates[label] = address
    # The gateway of the compose network is the address a request from the host arrives
    # from, and loopback is what Nextcloud logs for its own health check.
    candidates["gateway"] = "172.29.42.1"
    candidates["loopback"] = "127.0.0.1"
    if not candidates:
        return None
    return Topology(docker=docker, candidates=candidates)


@pytest.fixture(scope="module")
def measurement(flood_base: str, topology: Topology | None) -> Measurement:
    """Both floods, run once for the whole module, with the numbers around them.

    The runs are expensive and change the state of the instance, so every assertion below
    reads the same measurement instead of producing its own. ``asyncio.run`` rather than the
    anyio plugin, because a module scoped async fixture would need a module scoped backend
    fixture, and the flood needs no pytest event loop of its own.
    """
    result = asyncio.run(_measure(flood_base, topology))
    # This print is the protocol of the measurement: `pytest -s` shows every number the
    # summary quotes, with the command that produced it in the first line.
    print(result.report())
    return result


def _require_numbers(measurement: Measurement) -> None:
    if not measurement.measured:
        pytest.skip("docker is not reachable from this process, so no counters were read")


# --------------------------------------------------------------------------------------
# Case 1: an invalid bearer is refused, every time, and never with a server error
# --------------------------------------------------------------------------------------


def test_bearer_flood_is_answered_with_a_clean_refusal(measurement: Measurement) -> None:
    """401 or 403 is a clean no; a 5xx would mean the flood broke something."""
    run = measurement.bearer
    assert run.failures == [], f"requests that never got an answer: {run.failures[:5]}"
    assert run.answered == FLOOD_SIZE, f"{run.answered} answers for {FLOOD_SIZE} requests"
    server_errors = {code: count for code, count in run.statuses.items() if code >= 500}
    assert server_errors == {}, f"the bearer flood produced server errors: {server_errors}"
    unexpected = {code: count for code, count in run.statuses.items() if code not in (401, 403)}
    assert unexpected == {}, f"unexpected statuses in the bearer flood: {unexpected}"


# --------------------------------------------------------------------------------------
# Case 2: an invalid basic is refused too
# --------------------------------------------------------------------------------------


def test_basic_flood_is_answered_with_a_refusal(measurement: Measurement) -> None:
    """Every answer is a 4xx. 429 is one of them: that is the guard doing its work."""
    run = measurement.basic
    assert run.failures == [], f"requests that never got an answer: {run.failures[:5]}"
    assert run.answered == FLOOD_SIZE, f"{run.answered} answers for {FLOOD_SIZE} requests"
    not_refusals = {code: count for code, count in run.statuses.items() if not 400 <= code < 500}
    assert not_refusals == {}, f"the basic flood was not refused cleanly: {not_refusals}"


# --------------------------------------------------------------------------------------
# Case 3: the price of a request, in Nextcloud round trips
# --------------------------------------------------------------------------------------


def test_every_attacker_request_costs_a_nextcloud_round_trip(measurement: Measurement) -> None:
    """The amplification of pitfall 5, as a number rather than a claim.

    HaRP resolves the caller for every request that carries an ``Authorization`` header, so
    the expected ratio is one user-info request per attacker request. A ratio far below one
    would mean something started caching refusals, which would be a new finding worth its own
    line in the summary.
    """
    _require_numbers(measurement)
    for run in (measurement.bearer, measurement.basic):
        assert run.user_info is not None, f"run {run.name} carries no user-info count"
        assert run.nc_requests is not None, f"run {run.name} carries no request count"
        assert run.user_info >= FLOOD_SIZE * 0.9, (
            f"run {run.name}: only {run.user_info} user-info requests for {FLOOD_SIZE}"
            f" attacker requests (ratio {run.user_info_ratio})"
        )
        assert run.nc_requests >= run.user_info, (
            f"run {run.name}: {run.nc_requests} nextcloud requests is less than the"
            f" {run.user_info} user-info requests counted inside it"
        )


# --------------------------------------------------------------------------------------
# Case 4: the two credential forms behave in opposite ways
# --------------------------------------------------------------------------------------


def test_only_the_basic_flood_fills_the_bruteforce_counter(measurement: Measurement) -> None:
    """A bearer costs a round trip and nothing else; a basic costs the instance its patience.

    If this ever fails the other way round, the assumption of the research is wrong and the
    result is a new finding for the summary, not a line to be relaxed away.
    """
    _require_numbers(measurement)
    bearer_growth = {
        key: value for key, value in measurement.bearer.attempts_growth.items() if value
    }
    assert bearer_growth == {}, f"the bearer flood produced bruteforce entries: {bearer_growth}"
    basic_growth = {key: value for key, value in measurement.basic.attempts_growth.items() if value}
    assert basic_growth, "the basic flood produced no bruteforce entry at all"


# --------------------------------------------------------------------------------------
# Case 5: the instance is not left throttled
# --------------------------------------------------------------------------------------


def test_the_counters_are_zero_after_the_run(measurement: Measurement) -> None:
    """T-05-24. A load test that leaves the instance throttled has broken the next one."""
    _require_numbers(measurement)
    assert measurement.attempts_after_reset, "no counter was read after the reset"
    remaining = {key: value for key, value in measurement.attempts_after_reset.items() if value}
    assert remaining == {}, f"bruteforce counters still standing after the reset: {remaining}"
