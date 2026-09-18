"""Deck tools: one browse tool with a level, and one create-only write (D-06, TOOL-04).

**One tool, three levels.** ``deck_browse(level=...)`` walks boards, stacks and cards.
Three separate tools would cost three slots in every client that limits them and three
schemas in every ``tools/list``, for navigation the model can express in one enum value.
The answer envelope is the same on every level (``level``, ``count``, ``results``), so the
model learns one shape instead of three.

**No N+1, by construction.** ``GET /boards/{id}/stacks`` already contains the cards, so
``level="cards"`` is exactly one HTTP request no matter how many stacks a board has. Asking
each stack for its cards separately would re-fetch a payload Deck already sent, and a unit
test counts the requests so it stays that way.

**Two things are explained before they can fail.** A missing Deck app stops both tools at
the capabilities check, before the first Deck request (SRV-04). And a user whose Nextcloud
forbids board creation is checked against the board's own permissions instead of being
walked into a 403: ``canCreateBoards`` governs new boards, not cards on an existing board,
so the honest question is whether *this* board grants an edit permission (threat T-01-63).

Deliberately absent: update, delete, board or stack creation. The client below has no code
for any of it, which is what makes the create-only annotation of ``deck_create_card``
honest rather than a promise (threat T-01-62).
"""

from typing import Any

from .. import config, ids
from ..errors import ToolError
from ..nextcloud import NcClients, capabilities
from ..nextcloud.clients import deck as deck_client

APP = "deck"

#: The three navigation levels of ``deck_browse``, in the order a model walks them.
LEVELS = ("boards", "stacks", "cards")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_LEVEL_HINT = f"Use one of: {', '.join(LEVELS)}."
_BOARD_HINT = "Call deck_browse with level=boards first; it lists the board ids."


async def browse(
    clients: NcClients,
    level: str = "boards",
    board_id: str | None = None,
    stack_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Walk the user's Deck: boards, the stacks of a board, or its cards."""
    if level not in LEVELS:
        raise ToolError(message=f"{level!r} is not a Deck level.", hint=_LEVEL_HINT)
    capped = min(max(limit, 1), MAX_LIMIT)

    await capabilities.require_app(clients, APP)

    if level == "boards":
        return _envelope(level, await _boards(clients), capped)

    board = (board_id or "").strip()
    if not board:
        raise ToolError(
            message=f"level={level!r} needs a board_id.",
            hint=_BOARD_HINT,
        )

    stacks = await deck_client.get_stacks(clients.client, clients.creds, board)
    if level == "stacks":
        return _envelope(level, [_stack(stack) for stack in stacks], capped)
    return _envelope(level, _cards(clients, board, stacks, (stack_id or "").strip()), capped)


async def create_card(
    clients: NcClients,
    board_id: str,
    stack_id: str,
    title: str,
    description: str | None = None,
    duedate: str | None = None,
) -> dict[str, Any]:
    """Create one card in an existing stack and return its canonical long id."""
    caps = await capabilities.require_app(clients, APP)

    # Validated here and not only in the client: an unusable title or date must not cost
    # the permission round trip below either.
    wanted = deck_client.check_title(title)
    if duedate:
        duedate = deck_client.check_duedate(duedate)

    board = (board_id or "").strip()
    stack = (stack_id or "").strip()
    if not board or not stack:
        raise ToolError(
            message="A new card needs a board_id and a stack_id.",
            hint="Call deck_browse with level=stacks for the board to get both ids.",
        )

    if not caps.can_create_boards:
        await _require_write_permission(clients, board)

    card = await deck_client.create_card(
        clients.client,
        clients.creds,
        board,
        stack,
        title=wanted,
        description=description or None,
        duedate=duedate or None,
    )

    card_id = card.get("id")
    if card_id in (None, ""):
        raise ToolError(
            message="Nextcloud created the card but reported no id.",
            hint="Look for the card in the Deck app; it was probably created.",
        )

    result: dict[str, Any] = {
        "id": ids.encode_card(board, card.get("stackId") or stack, card_id),
        "title": str(card.get("title") or wanted),
        "url": deck_client.web_url(clients.creds, card_id),
    }
    if card.get("duedate"):
        result["duedate"] = card["duedate"]
    return result


MANAGE_ACTIONS = (
    "update_card",
    "move_card",
    "create_stack",
    "create_label",
    "assign_label",
    "remove_label",
    "delete_card",
)


async def manage(
    clients: NcClients,
    action: str,
    board_id: str,
    stack_id: str = "",
    card_id: str = "",
    *,
    title: str = "",
    description: str | None = None,
    duedate: str | None = None,
    target_stack_id: str = "",
    label_id: str = "",
    color: str = "",
    expected_title: str = "",
) -> dict[str, Any]:
    """Perform one explicitly enabled Deck management action."""
    if not config.deck_manage_enabled():
        raise ToolError(
            message="Deck management is disabled by the administrator.",
            hint=f"Enable {config.ENV_DECK_MANAGE} and restart the connector.",
        )
    if action not in MANAGE_ACTIONS:
        raise ToolError(
            message=f"{action!r} is not a Deck management action.",
            hint=f"Use one of: {', '.join(MANAGE_ACTIONS)}.",
        )
    await capabilities.require_app(clients, APP)
    if not board_id.strip():
        raise ToolError(message="Deck management needs a board_id.", hint=_BOARD_HINT)

    if action == "create_stack":
        return await deck_client.create_stack(clients.client, clients.creds, board_id, title=title)
    if action == "create_label":
        return await deck_client.create_label(
            clients.client, clients.creds, board_id, title=title, color=color
        )

    if not stack_id.strip() or not card_id.strip():
        raise ToolError(
            message=f"{action} needs stack_id and card_id.",
            hint="Use deck_browse with level=cards to get the canonical ids.",
        )
    if action == "update_card":
        return await deck_client.update_card(
            clients.client,
            clients.creds,
            board_id,
            stack_id,
            card_id,
            title=title or None,
            description=description,
            duedate=duedate,
        )
    if action == "move_card":
        return await deck_client.move_card(
            clients.client,
            clients.creds,
            board_id,
            stack_id,
            card_id,
            target_stack_id=target_stack_id,
        )
    if action in {"assign_label", "remove_label"}:
        return await deck_client.set_card_label(
            clients.client,
            clients.creds,
            board_id,
            stack_id,
            card_id,
            label_id=label_id,
            assigned=action == "assign_label",
        )

    if not config.deck_delete_enabled():
        raise ToolError(
            message="Deck deletion is disabled by the administrator.",
            hint=f"Enable {config.ENV_DECK_DELETE} and restart the connector.",
        )
    current = await deck_client.get_card(clients.client, clients.creds, board_id, stack_id, card_id)
    actual_title = str(current.get("title") or "")
    if not expected_title or expected_title != actual_title:
        raise ToolError(
            message=(
                f"Deletion confirmation does not match card {card_id}, whose current title "
                f"is {actual_title!r}."
            ),
            hint="Repeat the call with expected_title set to that exact current title.",
        )
    deleted = await deck_client.delete_card(
        clients.client, clients.creds, board_id, stack_id, card_id
    )
    return {
        "deleted": True,
        "id": ids.encode_card(board_id, stack_id, card_id),
        "title": actual_title,
        "server": deleted,
    }


async def _boards(clients: NcClients) -> list[dict[str, Any]]:
    """Board ids and titles, plus whether the user may write to them at all."""
    boards = await deck_client.get_boards(clients.client, clients.creds)
    return [
        {
            "id": board.get("id"),
            "title": str(board.get("title") or ""),
            "can_edit": _can_edit(board),
        }
        for board in boards
        if not board.get("archived") and not board.get("deletedAt")
    ]


def _stack(stack: dict[str, Any]) -> dict[str, Any]:
    """The navigation level: how many cards, not which ones."""
    cards = stack.get("cards")
    return {
        "id": stack.get("id"),
        "title": str(stack.get("title") or ""),
        "cards": len(cards) if isinstance(cards, list) else 0,
    }


def _cards(
    clients: NcClients,
    board: str,
    stacks: list[dict[str, Any]],
    stack_filter: str,
) -> list[dict[str, Any]]:
    """Flatten the cards Deck already sent, optionally narrowed to one stack."""
    if stack_filter:
        known = [str(stack.get("id")) for stack in stacks]
        if stack_filter not in known:
            raise ToolError(
                message=f"Board {board} has no stack {stack_filter}.",
                hint=f"Stacks of this board: {', '.join(known) or 'none'}.",
            )
        stacks = [stack for stack in stacks if str(stack.get("id")) == stack_filter]

    results: list[dict[str, Any]] = []
    for stack in stacks:
        cards = stack.get("cards")
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, dict) or card.get("deletedAt") or card.get("archived"):
                continue
            card_id = card.get("id")
            # A card without an id or without a stack cannot be addressed again later, and
            # a guessed id resolves to a different card, so it is skipped instead.
            owning_stack = card.get("stackId") or stack.get("id")
            if card_id in (None, "") or owning_stack in (None, ""):
                continue
            entry: dict[str, Any] = {
                "id": ids.encode_card(board, str(owning_stack), str(card_id)),
                "title": str(card.get("title") or ""),
                "stack": str(stack.get("title") or ""),
                "url": deck_client.web_url(clients.creds, card_id),
            }
            if card.get("duedate"):
                entry["duedate"] = card["duedate"]
            results.append(entry)
    return results


async def _require_write_permission(clients: NcClients, board: str) -> None:
    """Refuse a card the instance would refuse anyway, and say which board it was.

    ``canCreateBoards`` is false on instances that restrict board creation to a group.
    That says nothing about a board the user already has an edit permission on, so the
    board list decides, and only a board that really is read-only ends the call here.
    """
    boards = await deck_client.get_boards(clients.client, clients.creds)
    match = next((item for item in boards if str(item.get("id")) == board), None)
    if match is not None and _can_edit(match):
        return

    known = "This Nextcloud does not allow this account to create boards"
    if match is None:
        raise ToolError(
            message=f"Board {board} is not among the boards this account may use.",
            hint=f"{known}. Call deck_browse with level=boards to see the available boards.",
        )
    raise ToolError(
        message=f"No permission to add a card to board {board} ({match.get('title')}).",
        hint=(
            f"{known} and this board is read-only for it. Ask its owner in Nextcloud for "
            "an edit permission, or pick a board that deck_browse reports with can_edit."
        ),
    )


def _can_edit(board: dict[str, Any]) -> bool:
    permissions = board.get("permissions")
    permissions = permissions if isinstance(permissions, dict) else {}
    return bool(permissions.get("PERMISSION_EDIT"))


def _envelope(level: str, results: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """One answer shape for all three levels, truncation named instead of silent."""
    kept = results[:limit]
    answer: dict[str, Any] = {"level": level, "count": len(kept), "results": kept}
    if len(results) > len(kept):
        answer["truncated"] = True
    return answer
