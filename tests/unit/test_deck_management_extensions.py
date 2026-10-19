"""Operational Deck actions preserve card state and refuse ambiguous destructive writes."""

import json

import httpx
import pytest
import respx

from mcp_connector.errors import ToolError
from mcp_connector.nextcloud import NcClients, capabilities
from mcp_connector.nextcloud.clients import deck
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.tools import deck as tools

BASE = "http://nc.test"
API = BASE + "/index.php/apps/deck/api/v1.0"
APP = BASE + "/index.php/apps/deck"
STACKS = API + "/boards/2/stacks"
CARD = STACKS + "/11/cards/101"


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> NcClients:
    capabilities.clear_cache()
    monkeypatch.setenv("NC_MCP_DECK_MANAGE", "on")
    monkeypatch.setenv("NC_MCP_DECK_DELETE", "on")
    return NcClients(httpx.AsyncClient(), Credentials(BASE, "alice", "test-password"))


def allow_deck(mock: respx.MockRouter) -> None:
    mock.get(BASE + "/ocs/v2.php/cloud/capabilities").respond(
        200,
        json={
            "ocs": {
                "meta": {"status": "ok", "statuscode": 200},
                "data": {"capabilities": {"deck": {"version": "1.18.3", "canCreateBoards": True}}},
            }
        },
    )


@pytest.mark.anyio
async def test_reorder_card_keeps_stack_and_other_card_fields(clients: NcClients) -> None:
    current = {
        "id": 101,
        "stackId": 11,
        "title": "Keep",
        "order": 8,
        "color": "FFFFFF",
        "duedate": "2027-01-01T17:00:00+00:00",
        "labels": [{"id": 7}],
    }
    with respx.mock as mock:
        allow_deck(mock)
        mock.get(CARD).respond(200, json=current)
        route = mock.put(APP + "/cards/101/reorder").respond(200, json=current)
        await tools.manage(clients, "reorder_card", "2", "11", "101", order=0)
    body = json.loads(route.calls[0].request.content)
    assert body == {**current, "order": 0}


@pytest.mark.anyio
@pytest.mark.parametrize("order", [-1, True, "1", 1.5])
async def test_invalid_order_never_writes(clients: NcClients, order: object) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ToolError):
            await deck.move_card(
                clients.client, clients.creds, 2, 11, 101, target_stack_id=11, order=order
            )
        assert not mock.calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("title", "order", "expected"),
    [("New", None, {"title": "New", "order": 4}), (None, 0, {"title": "Keep", "order": 0})],
)
async def test_stack_update_preserves_unspecified_field(
    clients: NcClients, title: str | None, order: int | None, expected: dict
) -> None:
    with respx.mock as mock:
        mock.get(STACKS).respond(200, json=[{"id": 11, "title": "Keep", "order": 4, "cards": []}])
        route = mock.put(STACKS + "/11").respond(200, json=expected)
        await deck.update_stack(clients.client, clients.creds, 2, 11, title=title, order=order)
    assert json.loads(route.calls[0].request.content) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("include_cards", [True, False])
async def test_empty_stack_deletion_reads_both_views_and_confirms_title(
    clients: NcClients, include_cards: bool
) -> None:
    empty = [{"id": 11, "title": "Empty", **({"cards": []} if include_cards else {})}]
    with respx.mock as mock:
        allow_deck(mock)
        mock.get(STACKS).respond(200, json=empty)
        mock.get(STACKS + "/archived").respond(200, json=empty)
        route = mock.delete(STACKS + "/11").respond(200, json=empty[0])
        result = await tools.manage(clients, "delete_stack", "2", "11", expected_title="Empty")
    assert result["deleted"]
    assert route.call_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("active", "archived", "title"),
    [([], [{"id": 5}], "Empty"), ([{"id": 4}], [], "Empty"), ([], [], "Wrong")],
)
async def test_stack_deletion_refuses_cards_or_wrong_confirmation(
    clients: NcClients, active: list, archived: list, title: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(STACKS).respond(200, json=[{"id": 11, "title": "Empty", "cards": active}])
        mock.get(STACKS + "/archived").respond(
            200, json=[{"id": 11, "title": "Empty", "cards": archived}]
        )
        route = mock.delete(STACKS + "/11").respond(200, json={})
        with pytest.raises(ToolError):
            await deck.delete_empty_stack(
                clients.client, clients.creds, 2, 11, expected_title=title
            )
        assert not route.called


@pytest.mark.anyio
async def test_stack_deletion_still_needs_the_delete_switch(
    clients: NcClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NC_MCP_DECK_DELETE")
    with respx.mock as mock:
        allow_deck(mock)
        with pytest.raises(ToolError, match="disabled"):
            await tools.manage(clients, "delete_stack", "2", "11", expected_title="Empty")


@pytest.mark.anyio
@pytest.mark.parametrize("archived", [False, True])
async def test_archive_transitions_resolve_membership_without_reading_archived_card_endpoint(
    clients: NcClients, archived: bool
) -> None:
    current = {"id": 101, "archived": not archived}
    operation = "archive" if archived else "unarchive"
    source = STACKS if archived else STACKS + "/archived"
    with respx.mock as mock:
        mock.get(source).respond(200, json=[{"id": 11, "cards": [current]}])
        route = mock.put(CARD + "/" + operation).respond(
            200, json={**current, "archived": archived}
        )
        result = await deck.set_card_archived(
            clients.client, clients.creds, 2, 11, 101, archived=archived
        )
    assert result["archived"] == archived
    assert route.call_count == 1


@pytest.mark.anyio
async def test_label_assignment_is_idempotent_and_removal_returns_fresh_labels(
    clients: NcClients,
) -> None:
    current = {"id": 101, "labels": [{"id": 7}]}
    with respx.mock as mock:
        mock.get(CARD).respond(200, json=current)
        result = await deck.set_card_label(
            clients.client, clients.creds, 2, 11, 101, label_id=7, assigned=True
        )
        assert result == current
    with respx.mock as mock:
        mock.get(CARD).mock(
            side_effect=[
                httpx.Response(200, json=current),
                httpx.Response(200, json={"id": 101, "labels": []}),
            ]
        )
        mock.put(CARD + "/removeLabel").respond(200, json=current)
        result = await deck.set_card_label(
            clients.client, clients.creds, 2, 11, 101, label_id=7, assigned=False
        )
        assert result["labels"] == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "cards",
    [
        [
            {"stack_id": 11, "card_id": 101, "color": "ffffff"},
            {"stack_id": 11, "card_id": 102, "color": "bad"},
        ],
        [{"stack_id": 11, "card_id": 101, "color": "ffffff"}] * 2,
        [],
    ],
)
async def test_color_batch_validates_every_entry_before_writing(
    clients: NcClients, cards: list
) -> None:
    with respx.mock as mock:
        allow_deck(mock)
        with pytest.raises(ToolError):
            await tools.manage(clients, "set_card_colors", "2", cards=cards)


@pytest.mark.anyio
async def test_color_batch_reports_partial_server_failure(clients: NcClients) -> None:
    with respx.mock as mock:
        allow_deck(mock)
        mock.get(CARD).respond(200, json={"id": 101, "title": "Keep", "stackId": 11})
        mock.put(APP + "/cards/101").respond(200, json={"id": 101, "color": "FFFFFF"})
        mock.get(STACKS + "/11/cards/102").respond(403, json={"message": "Permission denied"})
        result = await tools.manage(
            clients,
            "set_card_colors",
            "2",
            cards=[
                {"stack_id": 11, "card_id": 101, "color": "ffffff"},
                {"stack_id": 11, "card_id": 102, "color": "0082c9"},
            ],
        )
    assert result["updated"] == 1
    assert result["failed"] == 1
