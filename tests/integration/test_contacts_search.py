"""Contacts against a real Nextcloud 34 with CardDAV (opt-in, read only).

This phase has no CardDAV write path, so these tests create nothing. They check the three
things a recorded fixture cannot answer: does the real address book path
(``addressbooks/users/<uid>/``) hold on a running instance, does the server really hand
out the two generated collections to every account, and does an account without an address
book of its own produce the error with the way out instead of an empty result.

The bootstrap prepares exactly that pair: ``occ dav:create-addressbook alice contacts``
runs for alice and never for bob.

Run it with::

    docker compose -f compose.test.yml up -d --wait
    bash scripts/bootstrap_test_nc.sh
    set -a && . ./.env.test && set +a && uv run pytest -m integration -q
"""

import os
import uuid

import httpx
import pytest

from mcp_connector.config import normalize_base_url
from mcp_connector.errors import ToolError
from mcp_connector.nextcloud import NcClients
from mcp_connector.nextcloud.clients import carddav
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.tools import contacts as contacts_tools

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def clients(live_env: dict[str, str | None]) -> NcClients:
    missing = [name for name, value in live_env.items() if not value]
    if missing:
        pytest.skip(f"no test Nextcloud configured (missing: {', '.join(sorted(missing))})")

    user = live_env["user"]
    assert user != "admin", "integration tests run as a normal user, never as admin"

    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False, timeout=30.0),
        creds=Credentials(
            base_url=normalize_base_url(str(live_env["base_url"])),
            user=str(user),
            secret=str(live_env["secret"]),
        ),
    )


@pytest.fixture
def clients_without_addressbook(live_env: dict[str, str | None]) -> NcClients:
    """bob: created by ``occ user:add``, so the bootstrap gives him no address book.

    He does own the two generated collections, because they appear as soon as an account
    authenticates once. They are not his address books, and the tool says so.
    """
    base_url = live_env["base_url"]
    user = os.environ.get("NC_MCP_TEST_USER2")
    secret = os.environ.get("NC_MCP_TEST_APP_PASSWORD2")
    if not base_url or not user or not secret:
        pytest.skip("no second test user configured (NC_MCP_TEST_USER2)")

    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False, timeout=30.0),
        creds=Credentials(
            base_url=normalize_base_url(str(base_url)),
            user=str(user),
            secret=str(secret),
        ),
    )


async def test_the_bootstrap_addressbook_is_discovered(clients: NcClients) -> None:
    """``occ dav:create-addressbook alice contacts`` uses the name directly as the URI."""
    books = await carddav.discover_addressbooks(clients.client, clients.creds)

    assert books, "the bootstrap creates an address book; pitfall 3 says login does not"
    assert "contacts" in {book.uri for book in books}


async def test_the_addressbook_url_carries_the_users_segment(clients: NcClients) -> None:
    """The real 404 proof: without ``users/`` the same request would not resolve."""
    url = carddav.addressbook_url(clients.creds, "contacts")
    assert "/remote.php/dav/addressbooks/users/" in url

    response = await clients.client.request(
        "PROPFIND",
        url,
        headers={"Depth": "0", "Content-Type": "application/xml"},
        content=carddav.build_discovery_body(),
        auth=httpx.BasicAuth(clients.creds.user, clients.creds.secret),
    )
    assert response.status_code == 207


async def test_the_generated_addressbooks_are_on_the_wire_but_not_in_the_result(
    clients: NcClients,
) -> None:
    """The filter has to earn its place: the server really does send these two."""
    response = await clients.client.request(
        "PROPFIND",
        carddav.addressbooks_home_url(clients.creds),
        headers={"Depth": "1", "Content-Type": "application/xml"},
        content=carddav.build_discovery_body(),
        auth=httpx.BasicAuth(clients.creds.user, clients.creds.secret),
    )
    assert response.status_code == 207
    assert "z-server-generated--system" in response.text
    assert "z-app-generated--contactsinteraction--recent" in response.text

    books = await carddav.discover_addressbooks(clients.client, clients.creds)
    assert not any(book.uri.startswith(carddav.GENERATED_PREFIXES) for book in books)


async def test_a_term_without_a_hit_returns_an_empty_list(clients: NcClients) -> None:
    """Zero hits are not an error, and the answer repeats the term that was searched."""
    term = f"kein-treffer-{uuid.uuid4().hex[:8]}"

    result = await contacts_tools.search(clients, term)

    assert result["contacts"] == []
    assert result["count"] == 0
    assert result["query"] == term
    assert "degraded" not in result


async def test_an_account_without_an_addressbook_gets_the_occ_hint(
    clients_without_addressbook: NcClients,
) -> None:
    """Needs a fresh instance, and that is a property of the case and not a defect.

    Read this before "fixing" a red run here. Where the Contacts app is enabled, Nextcloud
    creates the default ``contacts`` book for an account on one of its first CardDAV
    requests, and the search of this tool makes such a request. On a throwaway instance
    that is created and dropped per run, as the CI job does it, the refusal below is what
    happens and this case measures it. On a local instance that has been up for days the
    account has long owned that book, and then the search answers with an empty list
    instead: red here, green in CI, and the difference is the age of the instance.

    On 2026-09-17 this was misread as "the refusal is unreachable" and the case was
    rewritten to assert the empty list, against two local instances that had been running
    for days. CI, which builds its Nextcloud from scratch, refused that within the minute.
    The assertion below is the one that is right; if it goes red locally, recreate the
    instance rather than the case.
    """
    with pytest.raises(ToolError) as excinfo:
        await contacts_tools.search(clients_without_addressbook, "meier")

    assert "no address book" in excinfo.value.message.lower()
    assert "dav:create-addressbook" in excinfo.value.hint
