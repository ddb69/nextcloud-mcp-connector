"""The OAuth connection proven over the whole ExApp chain (AUTH-02, AUTH-03, SC 4, SC 5).

``tests/unit/test_oauth_abuse.py`` asks these questions in process, against an application
assembled the way ``entry_exapp`` assembles it. This file asks them again where nothing can
be arranged: the token is issued by the running container, it travels to HaRP inside a real
``Authorization`` header, HaRP resolves what it can and hands the request on, and the answer
is what Nextcloud's own permission check allows::

    MCP client  ->  reverse proxy  ->  HaRP  ->  ExApp container  ->  Nextcloud ACLs

The four questions, in the order in which a failure of one makes the next meaningless:

1. A token that came out of the flow serves a tool call over the whole chain.
2. The same token still serves one after the container was restarted. This is the proof
   that the store is on the volume and that the data key survives with it: the key lives in
   Nextcloud's ExApp configuration, and a restart that could not read it back would answer
   every request with a 500 (plan 03-02, pitfall 12).
3. Two accounts stay two accounts: bob's token reaches nothing of alice's, in both
   directions, and alice finds her own content in the same run.
4. A revocation is a 401 with the same pointer an anonymous request gets, and a whole new
   connection can be built afterwards.

Plus the one measurement that only exists at a running instance: a series of bearers this
server never issued ends in 429 with ``Retry-After`` on the token endpoint, and the working
connection is untouched by it, because the throttle never sits on the MCP route (D-37).

The app id ``mcp_connector`` is frozen (docs/app-id-freeze.md), so the route is a literal
here rather than an interpolation. The whole client half of the flow, including the two
steps a browser normally performs, lives in ``scripts/oauth_flow_check.py`` and is imported
from there: one implementation, used by the repeatable run and by these checks.

Run it against the running HaRP topology::

    export HP_SHARED_KEY="$(openssl rand -hex 32)"
    docker compose -p nc-mcp-exapp -f compose.exapp.yml up -d --wait
    bash scripts/bootstrap_exapp.sh
    set -a && . ./.env.exapp && set +a
    uv run --no-sync pytest tests/integration/test_oauth_flow_exapp.py -m integration -q

Without that topology the default suite stays green: ``addopts`` deselects the integration
marker and the fixture below skips with the missing variable named.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from topology import EXAPP_CONTAINER

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

#: The app id is frozen (docs/app-id-freeze.md), so the HaRP route is a constant here.
EXAPP_MCP_PATH = "/exapps/mcp_connector/mcp"

#: How long a restarted container may take to answer again before the check gives up.
RESTART_TIMEOUT = 60.0

#: A confidential line that only alice's file carries, kept ASCII so a leak assertion is
#: exact (the choice ``test_permission_fidelity_exapp.py`` makes for its negative case).
SECRET_LINE = "Vertrauliche Notiz von alice, Strassenbudget."

#: Well past the ten refusals per window of ``oauth/throttle.py``.
FLOOD = 12


def _load_flow_check() -> ModuleType:
    """The client half of the flow, loaded from ``scripts/`` where it belongs.

    It lives there and not in ``src/`` for one reason that matters: it drives a Nextcloud
    sign in with a user's account password, and no module of the product may ever contain
    that (T-03-74). Loading it by path keeps ``scripts/`` off ``sys.path`` and makes the
    dependency of this file visible in one place.
    """
    path = Path(__file__).resolve().parents[2] / "scripts" / "oauth_flow_check.py"
    spec = importlib.util.spec_from_file_location("oauth_flow_check", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{path} could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


flow_check = _load_flow_check()


@pytest.fixture
def flow_env() -> dict[str, str]:
    """The values ``scripts/bootstrap_exapp.sh`` writes into ``.env.exapp`` for this run.

    Two of them are account passwords, which no other test of this repository needs: the
    sign in of the Login Flow v2 is a browser step, and this suite performs it over plain
    HTTP against the throwaway instance. When one value is missing the check skips with the
    variable named, the shape the rest of the integration suite uses.
    """
    required = {
        "nc": "NC_MCP_URL",
        "base": "NC_MCP_EXAPP_BASE",
        "alice": "NC_MCP_TEST_USER",
        "alice_password": "NC_MCP_TEST_PASSWORD",
        "alice_app_password": "NC_MCP_TEST_APP_PASSWORD",
        "bob": "NC_MCP_TEST_USER2",
        "bob_password": "NC_MCP_TEST_PASSWORD2",
    }
    values = {key: (os.environ.get(name) or "").strip() for key, name in required.items()}
    missing = sorted(required[key] for key, value in values.items() if not value)
    if missing:
        pytest.skip(f"no ExApp topology configured (missing: {', '.join(missing)})")
    assert values["alice"] != values["bob"], "the negative proof needs two accounts"
    assert values["base"].endswith("/exapps/mcp_connector"), (
        f"the app id is frozen as mcp_connector but the base is {values['base']!r}"
    )
    return values


@pytest.fixture
def alice_connection(flow_env: dict[str, str]) -> Any:
    """One finished OAuth connection of alice, built the way a connector builds one."""
    return flow_check.connect(
        flow_env["base"], flow_env["nc"], flow_env["alice"], flow_env["alice_password"]
    )


def payload(result: Any) -> dict[str, Any]:
    """The compact JSON a tool answers with (``structured_output=False``)."""
    assert not result.is_error, f"the tool call ended in an error: {texts(result)!r}"
    assert result.content, f"the tool answered without content: {result!r}"
    decoded = json.loads(texts(result)[0])
    assert isinstance(decoded, dict), f"the tool did not answer with an object: {decoded!r}"
    return decoded


def texts(result: Any) -> list[str]:
    return [c.text for c in result.content if getattr(c, "text", None) is not None]


#: The first message of an MCP session. It is the one request that needs no session id, so
#: it is what these raw checks send: a ``tools/list`` without a session is answered with 400
#: by the transport, which would hide the difference between an accepted and a refused
#: bearer behind a protocol error.
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "oauth-flow-exapp", "version": "1.0"},
    },
}


def anonymous_challenge(base: str) -> str:
    """The ``WWW-Authenticate`` value of a request with no token at all."""
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{base}/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json=INITIALIZE,
        )
    assert response.status_code == 401
    return response.headers.get("www-authenticate", "")


def bearer_response(base: str, token: str) -> httpx.Response:
    """One raw MCP request with one bearer, for the checks that read a status and a header."""
    with httpx.Client(timeout=30.0) as client:
        return client.post(
            f"{base}/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
            json=INITIALIZE,
        )


def flood_token_endpoint(base: str) -> httpx.Response | None:
    """Refused token requests until one is answered with 429, or ``None`` if none is.

    A plain synchronous helper on purpose: it is a series of blocking requests, and calling
    them from inside an async check would be exactly the blocking call the async lint of
    this project refuses.
    """
    with httpx.Client(timeout=30.0) as client:
        for _ in range(FLOOD):
            response = client.post(
                f"{base}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": uuid.uuid4().hex,
                    "client_id": "no-such-client",
                },
            )
            if response.status_code == 429:
                return response
    return None


def restart_exapp() -> None:
    """Restart the container the deploy daemon started, and wait until it answers again."""
    finished = subprocess.run(  # noqa: S603 - a fixed argument list, no shell, no user input
        ["docker", "restart", EXAPP_CONTAINER],  # noqa: S607 - docker is on PATH by contract
        capture_output=True,
        text=True,
        check=False,
    )
    assert finished.returncode == 0, f"the container could not be restarted: {finished.stderr}"


def wait_until_answering(base: str) -> float:
    """Block until the app answers its own 401 again, and report how long that took."""
    started = time.monotonic()
    while time.monotonic() - started < RESTART_TIMEOUT:
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{base}/.well-known/oauth-protected-resource/mcp")
            if response.status_code == 200:
                return time.monotonic() - started
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    raise AssertionError(f"the app did not answer again within {RESTART_TIMEOUT} seconds")


async def test_a_token_from_the_flow_serves_a_tool_call_over_the_whole_chain(
    flow_env: dict[str, str], alice_connection: Any
) -> None:
    """Point 7 of the measurement list: the token this deployment issued actually works.

    The bearer is checked by ``oauth/verifier.py`` inside the container, the user id comes
    out of the stored authorization and the Nextcloud call runs as that user. Nothing in
    this path is arranged by the test: HaRP cannot resolve an OAuth bearer to a Nextcloud
    account and does not have to, which is exactly the design of plan 03-01.
    """
    names = await flow_check.list_tools(flow_env["base"], alice_connection.access_token)
    assert "files_upload" in names, f"the chain served a short tool list: {sorted(names)}"
    assert "unified_search" in names
    assert len(names) >= 14

    marker = f"oauthchain{uuid.uuid4().hex[:10]}"
    written = payload(
        await flow_check.tool_call(
            flow_env["base"],
            alice_connection.access_token,
            "files_upload",
            {"path": f"/{marker}.md", "content": f"# {marker}\n{SECRET_LINE}\n"},
        )
    )
    assert written.get("path") == f"/{marker}.md", f"the upload did not land: {written!r}"


async def test_the_same_token_still_works_after_the_container_restarted(
    flow_env: dict[str, str], alice_connection: Any
) -> None:
    """Point 8: a restart is not a disconnection (pitfall 12, T-03-15).

    Three things have to survive for this to pass, and none of them is in the process: the
    store file on the persistent volume, the data key in Nextcloud's ExApp configuration,
    and the ability to read that key back. The last one is what makes this check the live
    proof of the configuration read of ``oauth/crypto.py``: the process cache of the
    verifier is empty after a restart, so the very first request has to go to the store,
    which has to be decrypted with a key that was fetched from Nextcloud again.
    """
    before = await flow_check.list_tools(flow_env["base"], alice_connection.access_token)

    restart_exapp()
    wait_until_answering(flow_env["base"])

    after = await flow_check.list_tools(flow_env["base"], alice_connection.access_token)
    assert after == before, "the tool surface changed across the restart"

    marker = f"afterrestart{uuid.uuid4().hex[:8]}"
    written = payload(
        await flow_check.tool_call(
            flow_env["base"],
            alice_connection.access_token,
            "files_upload",
            {"path": f"/{marker}.md", "content": "written after a container restart\n"},
        )
    )
    assert written.get("path") == f"/{marker}.md"


async def test_two_tokens_stay_two_accounts_over_the_whole_chain(
    flow_env: dict[str, str], alice_connection: Any
) -> None:
    """Point 9: the permission promise, this time with OAuth tokens instead of app passwords.

    ``test_permission_fidelity_exapp.py`` proves the same boundary for the credential HaRP
    resolves. Here the identity comes from a token this server issued and stored, which is
    a different path through the same container, so the leak test has to be repeated for
    it. The positive control is in the same run: an empty answer for bob only means
    something if alice finds her own file at the same moment.
    """
    bob = flow_check.connect(
        flow_env["base"], flow_env["nc"], flow_env["bob"], flow_env["bob_password"]
    )
    assert bob.access_token != alice_connection.access_token

    marker = f"nurfueralice{uuid.uuid4().hex[:10]}"
    await flow_check.tool_call(
        flow_env["base"],
        alice_connection.access_token,
        "files_upload",
        {"path": f"/{marker}.md", "content": f"# {marker}\n{SECRET_LINE}\n"},
    )

    mine = payload(
        await flow_check.tool_call(
            flow_env["base"], alice_connection.access_token, "files_search", {"query": marker}
        )
    )
    assert f"/{marker}.md" in {item["path"] for item in mine["items"]}, (
        f"alice cannot find her own file with her own token: {mine!r}"
    )

    theirs = payload(
        await flow_check.tool_call(
            flow_env["base"], bob.access_token, "files_search", {"query": marker}
        )
    )
    assert theirs["items"] == [], f"bob's token reached alice's file: {theirs!r}"
    assert theirs["count"] == 0

    wide = payload(
        await flow_check.tool_call(
            flow_env["base"], bob.access_token, "unified_search", {"query": marker}
        )
    )
    assert wide["results"] == [], f"a provider leaked alice's file to bob's token: {wide!r}"

    direct = await flow_check.tool_call(
        flow_env["base"], bob.access_token, "files_read", {"path": f"/{marker}.md"}
    )
    assert direct.is_error, f"bob read alice's file with his token: {texts(direct)!r}"
    assert SECRET_LINE not in " ".join(texts(direct))

    flow_check.revoke(bob)


async def test_a_revocation_is_a_401_with_the_same_pointer_and_a_reconnection_works(
    flow_env: dict[str, str], alice_connection: Any
) -> None:
    """Point 10: "revoked" means revoked before the next request, and it is not the end.

    The pointer matters as much as the status. A client that gets a 401 without
    ``resource_metadata`` cannot find the authorization server again and reports "could not
    connect" instead of walking the flow a second time (03-RESEARCH.md, pitfall 1). So the
    header of a revoked token has to be the header of a request with no token at all.
    """
    assert bearer_response(flow_env["base"], alice_connection.access_token).status_code == 200

    assert flow_check.revoke(alice_connection) == 200

    refused = bearer_response(flow_env["base"], alice_connection.access_token)
    assert refused.status_code == 401
    assert refused.headers.get("www-authenticate") == anonymous_challenge(flow_env["base"])
    assert refused.headers.get("cache-control") == "no-store"

    again = flow_check.connect(
        flow_env["base"], flow_env["nc"], flow_env["alice"], flow_env["alice_password"]
    )
    assert again.access_token != alice_connection.access_token
    names = await flow_check.list_tools(flow_env["base"], again.access_token)
    assert "files_upload" in names
    flow_check.revoke(again)


async def test_a_flood_of_unknown_bearers_throttles_the_token_endpoint_and_nothing_else(
    flow_env: dict[str, str], alice_connection: Any
) -> None:
    """The throttle of D-37, at the instance, plus the boundary that makes it usable.

    Two statements, and the second one is the reason the first is safe. A series of refused
    token requests ends in 429 with ``Retry-After``, and that window applies to the token
    endpoint of this source. It does not apply to the MCP route: a working connection keeps
    working through the whole flood, because rate limiting the actual work of this server
    would be our own denial of service.

    The literal recovery after the window is not asserted here: the window is five minutes
    and a suite that sleeps that long is a suite nobody runs. It is measured once by hand
    and recorded in ``docs/oauth-setup.md``; what is asserted here is the number the header
    promises and the connection that is unaffected by it.
    """
    working = await flow_check.list_tools(flow_env["base"], alice_connection.access_token)

    throttled = flood_token_endpoint(flow_env["base"])

    assert throttled is not None, f"{FLOOD} refused token requests did not reach the throttle"
    assert throttled.headers.get("retry-after"), "the 429 promises no waiting time"
    assert int(throttled.headers["retry-after"]) > 0
    assert throttled.headers.get("cache-control") == "no-store"

    still_working = await flow_check.list_tools(flow_env["base"], alice_connection.access_token)
    assert still_working == working, "the throttle of the token endpoint reached the MCP route"

    # The window lives in the process, and the next check must not inherit it.
    restart_exapp()
    wait_until_answering(flow_env["base"])


def refuse_decisions(base: str, count: int) -> list[int]:
    """Post ``count`` consent decisions with no Nextcloud account, and collect the statuses.

    The body is deliberately junk: the flow does not exist, so the refusal happens for the
    reason this check is about (no account behind the request) and no state is touched.
    """
    with httpx.Client(timeout=30.0) as client:
        return [
            client.post(
                f"{base}/authorize/decide",
                data={"flow": uuid.uuid4().hex, "confirm": uuid.uuid4().hex, "decision": "approve"},
            ).status_code
            for _ in range(count)
        ]


def still_reachable(base: str) -> tuple[int, int]:
    """The status of one discovery document and of one anonymous MCP request, in that order.

    Sync like every other request helper of this file, because a blocking call belongs
    outside the async body of a check (the async lint of this project).
    """
    with httpx.Client(timeout=30.0) as client:
        document = client.get(f"{base}/.well-known/oauth-authorization-server")
        challenge = client.post(
            f"{base}/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json=INITIALIZE,
        )
    return document.status_code, challenge.status_code


async def test_refused_decisions_do_not_take_the_whole_app_off_the_network(
    flow_env: dict[str, str],
) -> None:
    """The regression guard of the access level of ``/authorize/decide``.

    While that route was declared ``USER``, HaRP refused an anonymous decision itself with
    403 *and* counted it in a blacklist of its own. Ten of those from one address inside
    ``HP_BLACKLIST_WINDOW`` (300 seconds by default) answered that address with 502 on every
    route of this app for the rest of the window: the discovery documents, ``/mcp``, all of
    it. Refusals are the normal traffic of this route, not an anomaly, so that was a remote
    off switch anybody could pull, and two runs of this very file pulled it by accident.

    The route is PUBLIC now and the refusal is the app's own, which is why this check asks
    for more refusals than HaRP's threshold and then asks whether the app is still there.
    The refusals themselves are allowed to end in this app's own 429: that throttle sits on
    one path class, answers with ``Retry-After`` and is the designed bound. What may not
    happen is a 502, and what may not happen is a discovery document that stops answering.
    """
    base = flow_env["base"]
    statuses = refuse_decisions(base, FLOOD)

    assert 502 not in statuses, "a refused decision reached HaRP's blacklist and banned us"
    assert set(statuses) <= {400, 429}, f"unexpected answers to a refused decision: {statuses}"
    assert statuses[0] == 400, "the first refusal is the app's own page, not a proxy answer"

    document, challenge = still_reachable(base)

    assert document == 200, "the discovery document stopped answering this source"
    assert challenge == 401, "the MCP route stopped answering this source"

    # The refusals above live in this app's own window, and the next check must not inherit
    # them: it walks the same path class to build a connection.
    restart_exapp()
    wait_until_answering(base)
