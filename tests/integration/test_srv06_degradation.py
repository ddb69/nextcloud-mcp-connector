"""SRV-06: all three new families disappear cleanly when their app is gone, measured.

There is no model for this file in the repository. No other test switches an app off and
restarts the instance, which is why the frame comes from ``test_exapp_mail_reach.py`` and
everything past the frame is new. Three things about it are load bearing, and the first of
them is the reason this file exists at all.

**Why the restart is mandatory.** Measured on 2026-08-24 against this topology: after
``occ app:disable mail`` the route ``core/navigation/apps`` still answers with a ``mail``
entry and ``account/list`` still answers 200. Only a restart of the Nextcloud makes both of
them go away. A degradation test without that restart claims the missing-app branch works and
has never once seen it: it is green because the app is still answering. The same finding has
been in ``STATE.md`` for the capabilities since phase 1 ("an app disabled with occ stays
visible in /cloud/capabilities until the Nextcloud restarts"), and the research of this phase
measured it for the navigation channel separately, because Mail is detected there and not in
the capabilities (trap 1).

**What this file costs and why that is accepted.** Three disable/restore cycles are six
restarts of a Nextcloud, which is minutes rather than seconds. It therefore runs under
``-m integration`` only and never in the default selection, and it is the one proof success
criterion 5 of this phase accepts. Each family is measured **once** per session and the rows
are memoised, the way ``test_exapp_mail_reach.py`` memoises its four ways: the assertions
below read recorded rows instead of each triggering a cycle of its own.

**The cleanup guarantee.** Every app that is switched off is switched back on and the instance
is restarted again, in a ``finally``, whether the measurement succeeded or not. The end state
is then **measured** rather than assumed: :func:`test_the_instance_is_back_the_way_it_was`
reads ``occ app:list`` and calls all three tools again. Leaving a throwaway topology in a half
state is the one side effect this file must not have, because everything else in the
integration suite runs against it.

On ``occ`` and Git Bash: this process talks to the daemon with a fixed argv
(``docker exec -u www-data ... php occ``), so no shell rewrites anything and the relative
``occ`` spelling is resolved inside the container. A reader who reproduces the same call by
hand from a Git Bash prompt with the absolute path needs ``MSYS_NO_PATHCONV=1``, otherwise the
shell turns ``/var/www/html/occ`` into a Windows path and php answers "Could not open input
file". The note is here because the reproduction line is the one people copy.

Run it against the running HaRP topology::

    docker compose -p nc-mcp-exapp -f compose.exapp.yml up -d --wait
    bash scripts/bootstrap_exapp.sh
    set -a && . ./.env.exapp && set +a
    uv run pytest tests/integration/test_srv06_degradation.py -m integration -s

The six rows it prints with ``-s`` are the deliverable: family, state, whether a restart lay
in between, and the first sentence of the answer.
"""

import json
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from topology import COMPOSE_HINT, NC_CONTAINER

from mcp_connector.config import normalize_base_url
from mcp_connector.errors import ToolError
from mcp_connector.nextcloud import NcClients, capabilities
from mcp_connector.nextcloud.clients import ocs
from mcp_connector.nextcloud.credentials import MODE_APPAPI, Credentials
from mcp_connector.tools import chatgpt
from mcp_connector.tools import mail as mail_tools
from mcp_connector.tools import tables as tables_tools
from mcp_connector.tools import talk as talk_tools

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

#: How long a restarted Nextcloud may take to answer ``occ status`` with ``installed: true``.
#: Generous on purpose: a timeout here would leave the instance in the disabled state, which
#: is the one outcome this file must not produce.
BOOT_TIMEOUT = 300.0
BOOT_POLL = 3.0

#: The three families, in the order this file walks them, with the app id each is detected by.
#: ``spreed`` and not ``talk``: the capabilities document names the section that way, and the
#: key of ``capabilities._MISSING`` is the key of that answer.
TALK = "spreed"
TABLES = "tables"
MAIL = "mail"
FAMILIES = (TALK, TABLES, MAIL)

#: The sentence each family answers with when its app is gone, pinned here as a literal and
#: additionally compared against ``capabilities.app_missing`` below. The literal is what a user
#: reads and therefore part of the contract; the comparison is what keeps this file from
#: drifting away from the module that owns the wording.
EXPECTED_MESSAGE = {
    TALK: "The Talk app is not available on this Nextcloud.",
    TABLES: "The Tables app is not enabled on this Nextcloud.",
    MAIL: "The Mail app is not available on this Nextcloud.",
}

#: The 404 branch of the shared status mapping. A missing app without a check up front would
#: land exactly there, and that branch tells a model to look for the object first, that is, to
#: search inside an app that is not installed. Its absence is asserted in every missing row.
FOUR_OH_FOUR_WORDING = "search for it first"

#: A message id that needs to exist nowhere: the full text path checks the app before it
#: builds a single request, which is the property this entry point exists to prove.
ANY_MAIL_ID = "mail:1"

#: The rows of one session, one entry per family. A memo and not a fixture: a fixture would
#: either run a disable/restore cycle per test or have to live for the whole module, and a
#: module scoped fixture that switches an app off would still hold it off while the end state
#: test ran.
_missing: dict[str, dict[str, Any]] = {}

#: What the enabled state answered, recorded by the control tests and printed by the protocol.
_present: dict[str, str] = {}


# --------------------------------------------------------------------------------------
# Talking to the topology
# --------------------------------------------------------------------------------------


def _run(args: list[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, the executable comes from shutil.which
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture(scope="module")
def docker() -> str:
    """The docker executable, or a skip. Without it nothing in this file can be measured."""
    found = shutil.which("docker")
    if not found:
        pytest.skip("no docker executable on PATH; this proof needs the running topology")
    result = _run([found, "inspect", NC_CONTAINER, "--format", "{{.State.Running}}"], timeout=60.0)
    if result.returncode != 0 or result.stdout.strip() != "true":
        pytest.skip(f"{NC_CONTAINER} is not running; start the topology first")
    return found


def occ(docker_path: str, *args: str, timeout: float = 300.0) -> str:
    """One occ command inside the Nextcloud container, as www-data.

    The compose spelling of the same call is :data:`COMPOSE_HINT`; this one avoids the
    ``HP_SHARED_KEY`` interpolation of the compose file. See the module docstring for why no
    ``MSYS_NO_PATHCONV`` is needed on this path and when a reader does need it.
    """
    result = _run([docker_path, "exec", "-u", "www-data", NC_CONTAINER, "php", "occ", *args])
    assert result.returncode == 0, (
        f"occ {' '.join(args)} failed with {result.returncode}: {result.stderr.strip()!r}"
    )
    return result.stdout


def enabled_apps(docker_path: str) -> set[str]:
    """The set of enabled app ids, read from ``occ app:list`` rather than from a guess."""
    payload = json.loads(occ(docker_path, "app:list", "--output=json"))
    listed = payload.get("enabled")
    assert isinstance(listed, dict), f"occ app:list answered no enabled section: {payload!r}"
    return set(listed)


def restart_nextcloud(docker_path: str) -> float:
    """Restart the Nextcloud and wait until ``occ status`` says ``installed: true`` again.

    This is the step that makes the whole file worth running (see the module docstring): the
    detection channels of all three families keep answering the old truth until the PHP process
    is new. The wait is a poll rather than a sleep, because a fixed sleep would either be too
    short on a slow machine or waste minutes on a fast one.
    """
    started = time.monotonic()
    result = _run([docker_path, "restart", NC_CONTAINER], timeout=BOOT_TIMEOUT)
    assert result.returncode == 0, f"docker restart {NC_CONTAINER} failed: {result.stderr!r}"

    last = ""
    while time.monotonic() - started < BOOT_TIMEOUT:
        argv = [docker_path, "exec", "-u", "www-data", NC_CONTAINER, "php", "occ"]
        probe = _run([*argv, "status", "--output=json"], timeout=120.0)
        last = (probe.stdout or probe.stderr).strip()
        if probe.returncode == 0:
            try:
                status = json.loads(last)
            except ValueError:
                status = {}
            if status.get("installed") is True and status.get("maintenance") is False:
                return time.monotonic() - started
        time.sleep(BOOT_POLL)
    raise AssertionError(f"{NC_CONTAINER} did not come back within {BOOT_TIMEOUT}s: {last!r}")


@contextmanager
def app_disabled(docker_path: str, app: str) -> Iterator[float]:
    """Switch one app off, restart, and put everything back afterwards, come what may.

    The restart on the way in is what makes the measurement real, and the restart on the way
    out is what makes the topology usable again for every other integration test. Both the
    disable and the enable are followed by ``capabilities.clear_cache()``, because a snapshot
    of this project lives 60 seconds and would otherwise answer the state from before the
    restart, which is exactly the false green this file exists to rule out.
    """
    occ(docker_path, "app:disable", app)
    try:
        elapsed = restart_nextcloud(docker_path)
        capabilities.clear_cache()
        yield elapsed
    finally:
        occ(docker_path, "app:enable", app)
        restart_nextcloud(docker_path)
        capabilities.clear_cache()


# --------------------------------------------------------------------------------------
# The identities the three tools run under
# --------------------------------------------------------------------------------------


def _appapi_clients(exapp_env: dict[str, str], user: str) -> NcClients:
    """The impersonating clients of one user, ``APP_SECRET`` as the only credential."""
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False, timeout=120.0),
        creds=Credentials(
            base_url=normalize_base_url(exapp_env["base_url"]),
            user=user,
            secret=exapp_env["app_secret"],
            mode=MODE_APPAPI,
            app_id=exapp_env["app_id"],
            app_version=exapp_env["app_version"],
            aa_version=exapp_env["aa_version"],
        ),
    )


@pytest.fixture
async def alice(exapp_env: dict[str, str]) -> AsyncIterator[NcClients]:
    clients = _appapi_clients(exapp_env, exapp_env["alice"])
    async with clients.client:
        capabilities.clear_cache()
        yield clients


async def _talk(clients: NcClients) -> dict[str, Any]:
    return await talk_tools.browse(clients, level="conversations", limit=10)


async def _tables(clients: NcClients) -> dict[str, Any]:
    return await tables_tools.browse(clients, level="tables")


async def _mail(clients: NcClients) -> dict[str, Any]:
    return await mail_tools.browse(clients, level="accounts")


async def _mail_full_text(clients: NcClients) -> dict[str, Any]:
    return await chatgpt.fetch(clients, ANY_MAIL_ID)


#: The entry points measured per family. Mail has two, because the full text path carries a
#: check of its own: a missing app has to be named there as well, and not only on the browse
#: path that happens to be the one a model reaches first.
ENTRY_POINTS: dict[str, dict[str, Callable[[NcClients], Awaitable[dict[str, Any]]]]] = {
    TALK: {"talk_browse": _talk},
    TABLES: {"tables_browse": _tables},
    MAIL: {"mail_browse": _mail, 'fetch("mail:1")': _mail_full_text},
}


# --------------------------------------------------------------------------------------
# The measurement, one disable/restore cycle per family and never more
# --------------------------------------------------------------------------------------


async def _lists_mail(clients: NcClients, path: str, what: str) -> bool:
    """Whether one of the two detection channels carries a ``mail`` entry right now."""
    response = await ocs.ocs_get(clients.client, clients.creds, path)
    payload = ocs.parse_ocs(response, what=what)
    entries = payload if isinstance(payload, list) else []
    return any(
        isinstance(entry, dict) and "mail" in (entry.get("app"), entry.get("id"))
        for entry in entries
    )


async def _measure(docker_path: str, clients: NcClients, app: str) -> dict[str, Any]:
    """One family, switched off with a restart in between, every entry point recorded.

    Nothing is asserted in here. What came back is written down, and the tests below read it,
    so a failing assertion never leaves an app switched off: the restore has already happened
    by the time any of them runs.
    """
    row: dict[str, Any] = {"app": app, "restarted": False, "answers": {}}
    with app_disabled(docker_path, app) as boot_seconds:
        row["restarted"] = True
        row["boot_seconds"] = round(boot_seconds, 1)
        for name, call in ENTRY_POINTS[app].items():
            try:
                answer = await call(clients)
            except ToolError as error:
                row["answers"][name] = {
                    "raised": True,
                    "type": type(error).__name__,
                    "message": error.message,
                    "hint": error.hint,
                }
            else:
                row["answers"][name] = {"raised": False, "answer": repr(answer)[:200]}
        if app == MAIL:
            row["navigation_lists_mail"] = await _lists_mail(
                clients, capabilities.NAVIGATION_PATH, "the app list of this account"
            )
            row["providers_list_mail"] = await _lists_mail(
                clients, "/search/providers", "the search providers"
            )
    return row


async def missing_row(docker_path: str, clients: NcClients, app: str) -> dict[str, Any]:
    """The recorded row of one family, measured at most once per session."""
    cached = _missing.get(app)
    if cached is not None:
        return cached
    row = await _measure(docker_path, clients, app)
    _missing[app] = row
    return row


def assert_named_and_nothing_else(row: dict[str, Any], entry_point: str) -> str:
    """The six assertions every missing row carries, in one place for all of them.

    The positive one is that the sentence is exactly the one the contract promises. The five
    negative ones are what the answer must not be: a leaked stack trace, the login page in two
    spellings, a redirect into it, and the 404 wording of the shared status mapping, which is
    precisely where a missing app would land without a check up front.
    """
    app = str(row["app"])
    answer = row["answers"][entry_point]
    assert answer["raised"], (
        f"{entry_point} answered instead of naming the missing app {app!r}: {answer['answer']}"
    )
    said = f"{answer['message']} {answer['hint']}"

    assert answer["message"] == EXPECTED_MESSAGE[app], (
        f"{entry_point} answered {answer['message']!r} instead of {EXPECTED_MESSAGE[app]!r}"
    )
    assert answer["hint"], f"{entry_point}: a refusal without a next step is a dead end"
    assert answer["hint"] != answer["message"], f"{entry_point}: the hint repeats the message"
    assert "Traceback" not in said, f"{entry_point} leaked a stack trace: {said!r}"
    assert not said.lstrip().startswith("<"), f"{entry_point} answered markup: {said!r}"
    assert "<html" not in said.casefold(), f"{entry_point} answered a page: {said!r}"
    assert "/login" not in said, f"{entry_point} pointed at a login page: {said!r}"
    assert FOUR_OH_FOUR_WORDING not in said, (
        f"{entry_point} fell into the 404 branch and tells the model to search inside an app "
        f"that is not installed: {said!r}"
    )
    assert row["restarted"], f"{entry_point} was measured without a restart and proves nothing"
    return said


# --------------------------------------------------------------------------------------
# Control: the three families answer while their app is there
# --------------------------------------------------------------------------------------


async def test_the_contract_sentences_are_the_ones_this_file_pins(docker: str) -> None:
    """Guard: the literals above are still what ``capabilities`` hands out.

    Without this, the three assertions on the exact sentence could quietly become assertions
    about a copy of the wording that nobody maintains, and the wording is what a user reads.
    """
    assert enabled_apps(docker) >= set(FAMILIES), (
        "this proof starts from an instance where all three apps are enabled"
    )
    for app in FAMILIES:
        error = capabilities.app_missing(app)
        assert error.message == EXPECTED_MESSAGE[app], (
            f"the contract sentence of {app!r} changed: {error.message!r}"
        )
        assert error.hint, f"the hint of {app!r} is empty"
        assert error.hint != error.message, f"the hint of {app!r} only repeats the message"


async def test_talk_answers_while_its_app_is_enabled(alice: NcClients) -> None:
    """Control one. Without it, the missing row of Talk would prove nothing at all."""
    answer = await _talk(alice)
    assert answer["level"] == "conversations"
    assert isinstance(answer["count"], int)
    _present[TALK] = f"talk_browse -> {answer['count']} conversations"


async def test_tables_answers_while_its_app_is_enabled(alice: NcClients) -> None:
    """Control two."""
    answer = await _tables(alice)
    assert answer["level"] == "tables"
    assert isinstance(answer["count"], int)
    _present[TABLES] = f"tables_browse -> {answer['count']} tables"


async def test_mail_answers_while_its_app_is_enabled(alice: NcClients) -> None:
    """Control three, and the control of the two detection channels at the same time.

    Both channels are read here as well as in the missing state, because the interesting
    failure is not "one of them is wrong" but "the two disagree": the navigation is the one
    the connector asks, and the search provider list is the second opinion that covers
    assumption A6 (an administrator can hide a navigation entry, and whether a hidden entry is
    absent from that answer is not measured).
    """
    answer = await _mail(alice)
    assert answer["level"] == "accounts"
    assert isinstance(answer["count"], int)

    navigation = await _lists_mail(
        alice, capabilities.NAVIGATION_PATH, "the app list of this account"
    )
    providers = await _lists_mail(alice, "/search/providers", "the search providers")
    assert navigation, "the navigation does not list mail while the app is enabled"
    assert providers, "the search providers do not list mail while the app is enabled"
    _present[MAIL] = (
        f"mail_browse -> {answer['count']} accounts, navigation={navigation}, providers={providers}"
    )


# --------------------------------------------------------------------------------------
# The missing state, after occ app:disable plus a restart
# --------------------------------------------------------------------------------------


async def test_talk_names_the_missing_app_after_a_restart(docker: str, alice: NcClients) -> None:
    """Row four: Talk, detected in the capabilities, gone after the restart."""
    row = await missing_row(docker, alice, TALK)
    assert_named_and_nothing_else(row, "talk_browse")


async def test_tables_names_the_missing_app_after_a_restart(docker: str, alice: NcClients) -> None:
    """Row five: Tables, detected by its explicit ``enabled`` flag, gone after the restart."""
    row = await missing_row(docker, alice, TABLES)
    assert_named_and_nothing_else(row, "tables_browse")


async def test_mail_browse_names_the_missing_app_after_a_restart(
    docker: str, alice: NcClients
) -> None:
    """Row six, first entry point: the navigation channel, gone after the restart."""
    row = await missing_row(docker, alice, MAIL)
    assert_named_and_nothing_else(row, "mail_browse")


async def test_the_mail_full_text_path_names_the_missing_app_too(
    docker: str, alice: NcClients
) -> None:
    """Row six, second entry point, and it is not a duplicate of the first.

    ``fetch`` reaches Mail through a different function with a check of its own, and without
    that check a Nextcloud without the Mail app would answer status 998 into the 404 branch of
    the shared status mapping. The id is one that needs to exist nowhere, because the check
    stands before the first request.
    """
    row = await missing_row(docker, alice, MAIL)
    assert_named_and_nothing_else(row, 'fetch("mail:1")')


async def test_both_detection_channels_lose_mail_when_the_app_is_gone(
    docker: str, alice: NcClients
) -> None:
    """The counter check of the detection way itself, on both channels at once.

    If these two ever disagree, the connector and its second opinion have drifted apart, and
    that is worth a red test even though only one of them is asked in production.
    """
    row = await missing_row(docker, alice, MAIL)
    assert row["navigation_lists_mail"] is False, (
        "the navigation still lists mail after the app was disabled and the instance restarted"
    )
    assert row["providers_list_mail"] is False, (
        "the search providers still list mail after the app was disabled and the instance "
        "restarted; the two detection channels disagree"
    )


# --------------------------------------------------------------------------------------
# The end state, measured rather than assumed
# --------------------------------------------------------------------------------------


async def test_the_instance_is_back_the_way_it_was(docker: str, alice: NcClients) -> None:
    """The cleanup guarantee, held by measurement: all three apps on, all three tools factual.

    This is the last test of the file on purpose. Everything else in the integration suite
    runs against the same throwaway topology, so a run of this file that ended with an app
    switched off would turn into a wrong statement about a different family somewhere else.
    """
    listed = enabled_apps(docker)
    missing = sorted(app for app in FAMILIES if app not in listed)
    assert not missing, f"occ app:list does not list {missing} as enabled after this run"

    capabilities.clear_cache()
    talk = await _talk(alice)
    tables = await _tables(alice)
    mail = await _mail(alice)
    assert talk["level"] == "conversations"
    assert tables["level"] == "tables"
    assert mail["level"] == "accounts"
    assert mail["count"] >= 1, (
        f"the mail account is gone after the disable/enable cycle: {mail!r}; "
        f"re-run {COMPOSE_HINT.split(' php ')[0]} and bash scripts/bootstrap_exapp.sh"
    )


async def test_the_six_rows_are_the_protocol_of_this_proof() -> None:
    """Print the rows and hold the negative conditions over all of them once more.

    The table is the deliverable of this plan: its values go into the summary verbatim, which
    is why they are printed rather than only asserted. Run the file with ``-s`` to see them.
    """
    if not _missing:
        pytest.skip("no family was measured in this run")

    print(f"\n{'family':<9} {'state':<9} {'restart':<8} {'entry point':<16} answer")
    for app in FAMILIES:
        present = _present.get(app, "not measured")
        print(f"{app:<9} {'present':<9} {'no':<8} {'-':<16} {present}")
        row = _missing.get(app)
        if row is None:
            continue
        for name, answer in row["answers"].items():
            said = answer["message"] if answer["raised"] else f"NO REFUSAL: {answer['answer']}"
            print(f"{app:<9} {'missing':<9} {'yes':<8} {name:<16} {said}")
        if app == MAIL:
            print(
                f"{app:<9} {'missing':<9} {'yes':<8} {'channels':<16} "
                f"navigation={row['navigation_lists_mail']} "
                f"providers={row['providers_list_mail']}"
            )
        print(f"{app:<9} {'missing':<9} {'yes':<8} {'boot':<16} {row['boot_seconds']}s")

    for app, row in _missing.items():
        for name in row["answers"]:
            assert_named_and_nothing_else(row, name)
        assert row["restarted"], f"{app} was measured without a restart"
