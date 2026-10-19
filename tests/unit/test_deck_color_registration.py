"""The public JSON wrapper must distinguish omitted color from explicitly clearing it."""

from unittest.mock import AsyncMock

import pytest

from mcp_connector.server import reg_deck


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("extra", "expected"), [("", None), (', "color": ""', ""), (', "color": "ffffff"', "ffffff")]
)
async def test_color_is_forwarded_without_erasing_omission(
    monkeypatch: pytest.MonkeyPatch, extra: str, expected: str | None
) -> None:
    clients = object()
    monkeypatch.setattr(reg_deck.deps, "resolve_clients", lambda ctx: clients)
    manage = AsyncMock(return_value={"id": 101})
    monkeypatch.setattr(reg_deck.deck_tools, "manage", manage)
    await reg_deck.deck_manage(
        action="update_card",
        board_id="2",
        values='{"stack_id": "11", "card_id": "101"' + extra + "}",
    )
    assert manage.await_args.kwargs["color"] == expected
