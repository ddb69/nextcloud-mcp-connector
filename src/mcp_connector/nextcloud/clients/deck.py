"""Deck REST v1.0 client: boards, stacks, cards and opt-in management paths.

The API lives at ``/index.php/apps/deck/api/v1.0`` and it is the strictest of the JSON
APIs this project speaks. Two headers are mandatory on **every** request, a plain GET
included: ``OCS-APIRequest: true`` and ``Content-Type: application/json`` (D-18). Without
the first one Nextcloud answers a browser login page with status 200, which is why the
headers are one constant here instead of an argument anyone could forget.

Deck does **not** answer in an OCS envelope. A failure is the bare object
``{"status": 403, "message": "Permission denied"}``, so every response goes through
:func:`ocs.parse_app_json`; letting an ``ocs.meta`` parser loose on that produces a
``KeyError`` where an actionable sentence belongs (pitfall 9).

Version 1.0 and not 1.1 on purpose. 1.1 exists since Deck 1.3.0 and only adds attachment
types, which this project does not touch, so 1.0 is the wider compatibility for the same
functionality.

Three local guards keep doomed requests from ever leaving, because a refused request is
cheaper than a 400 and its message is better:

* ids that go into the URL path must be digits (threat T-01-63)
* a title is trimmed, must not be empty and must not exceed 255 characters (threat T-01-67)
* a due date must be an ISO-8601 timestamp

Write paths are exposed only by the administratively gated Deck management tool. Card and
empty-stack deletion use explicit request verbs and require separate deletion opt-in and
exact-title confirmation in the tool. No board-sharing or permission writes are implemented.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from ...errors import ToolError
from ..credentials import Credentials
from . import ocs

#: The API generation this client speaks. 1.0 instead of 1.1 on purpose: 1.1 only adds
#: attachment types we do not use, and 1.0 is available on more instances.
SUPPORTED_API_VERSION = "1.0"

#: Base path of the Deck REST API. ``index.php`` is not optional on every instance.
DECK_API_PREFIX = f"/index.php/apps/deck/api/v{SUPPORTED_API_VERSION}"

#: Base path of Deck\'s app routes used by its own web client.
DECK_APP_PREFIX = "/index.php/apps/deck"

#: Web route of a single card (``deck.page.redirectToCard``), used for the ``url`` field.
DECK_WEB_PREFIX = "/index.php/apps/deck/card"

ARCHIVED_STACKS_SUFFIX = "/archived"

#: Deck rejects a longer title with a 400. The limit is checked before the request.
MAX_TITLE_LENGTH = 255

#: Where a new card lands: high enough to be appended at the end of the stack.
DEFAULT_CARD_ORDER = 999

#: The only card type Deck currently knows.
CARD_TYPE = "plain"

#: The two mandatory headers of D-18, plus the ``Accept`` that keeps a proxy from
#: negotiating HTML. Copied per request, never mutated in place.
DECK_HEADERS: Mapping[str, str] = {
    "OCS-APIRequest": "true",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

_DATE_HINT = "Use an ISO-8601 timestamp with an offset, for example 2026-09-01T10:00:00+00:00."


def api_url(creds: Credentials, path: str = "") -> str:
    """Build a Deck API URL; ``path`` is empty or starts with a slash."""
    if path and not path.startswith("/"):
        raise ValueError(f"a Deck path must start with a slash (got {path!r})")
    return f"{creds.base_url}{DECK_API_PREFIX}{path}"


def app_url(creds: Credentials, path: str = "") -> str:
    """Build a Deck app URL; ``path`` is empty or starts with a slash."""
    if path and not path.startswith("/"):
        raise ValueError(f"a Deck app path must start with a slash (got {path!r})")
    return f"{creds.base_url}{DECK_APP_PREFIX}{path}"


def web_url(creds: Credentials, card_id: str | int) -> str:
    """The link a human can open. Always built from the configured base URL."""
    return f"{creds.base_url}{DECK_WEB_PREFIX}/{card_id}"


async def get_boards(
    client: httpx.AsyncClient, creds: Credentials, *, details: bool = False
) -> list[dict[str, Any]]:
    """List the boards the user may see, optionally with labels, stacks and users."""
    response = await client.get(
        api_url(creds, "/boards"),
        params={"details": "true"} if details else None,
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_list(ocs.parse_app_json(response, what="the deck boards"), what="boards")


async def get_board(
    client: httpx.AsyncClient, creds: Credentials, board_id: str | int
) -> dict[str, Any]:
    """Read one board including its labels and access list."""
    board = _path_id(board_id, "board id")
    response = await client.get(
        api_url(creds, f"/boards/{board}"),
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what=f"the board {board}"), what="a board")


async def get_stacks(
    client: httpx.AsyncClient, creds: Credentials, board_id: str | int, *, archived: bool = False
) -> list[dict[str, Any]]:
    """List the stacks of a board; the answer already contains their cards.

    This is the single request behind ``deck_browse(level="cards")``. Asking every stack
    for its cards separately would be an N+1 over a payload Deck already sent.
    """
    board = _path_id(board_id, "board id")
    response = await client.get(
        api_url(creds, f"/boards/{board}/stacks" + (ARCHIVED_STACKS_SUFFIX if archived else "")),
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    payload = ocs.parse_app_json(response, what=f"the stacks of board {board}")
    return _as_list(payload, what="stacks")


async def get_card(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    card_id: str | int,
) -> dict[str, Any]:
    """Read one card by its canonical board, stack and card triple."""
    board = _path_id(board_id, "board id")
    stack = _path_id(stack_id, "stack id")
    card = _path_id(card_id, "card id")
    response = await client.get(
        api_url(creds, f"/boards/{board}/stacks/{stack}/cards/{card}"),
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what=f"the card {card}"), what="a card")


async def create_card(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    *,
    title: str,
    description: str | None = None,
    duedate: str | None = None,
    order: int = DEFAULT_CARD_ORDER,
) -> dict[str, Any]:
    """Create one card in an existing stack and return the object Deck stored."""
    board = _path_id(board_id, "board id")
    stack = _path_id(stack_id, "stack id")
    body: dict[str, Any] = {
        "title": check_title(title),
        "type": CARD_TYPE,
        "order": order,
    }
    if description:
        body["description"] = description
    if duedate:
        body["duedate"] = check_duedate(duedate)

    response = await client.post(
        api_url(creds, f"/boards/{board}/stacks/{stack}/cards"),
        json=body,
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what="the new card"), what="a card")


async def update_card(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    card_id: str | int,
    *,
    title: str | None = None,
    description: str | None = None,
    duedate: str | None = None,
    color: str | None = None,
) -> dict[str, Any]:
    """Update selected card fields while preserving the complete Deck update contract."""
    normalized_color = check_card_color(color) if color is not None else None
    board, stack, card = _card_path(board_id, stack_id, card_id)
    current = await get_card(client, creds, board, stack, card)
    body = {
        **current,
        "id": int(card),
        "stackId": int(stack),
        "title": check_title(title if title is not None else str(current.get("title") or "")),
        "type": current.get("type") or CARD_TYPE,
        "description": description if description is not None else current.get("description") or "",
        "order": current.get("order", DEFAULT_CARD_ORDER),
        "archived": bool(current.get("archived", False)),
        "deletedAt": current.get("deletedAt", 0),
        "duedate": check_duedate(duedate)
        if duedate
        else (None if duedate == "" else current.get("duedate")),
    }
    if color is not None:
        body["color"] = normalized_color
    response = await client.put(
        app_url(creds, f"/cards/{card}"),
        json=body,
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what=f"the updated card {card}"), what="a card")


async def move_card(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    card_id: str | int,
    *,
    target_stack_id: str | int,
    order: int = DEFAULT_CARD_ORDER,
) -> dict[str, Any]:
    """Move a card through the reorder endpoint used by Deck's web client."""
    board, stack, card = _card_path(board_id, stack_id, card_id)
    target = _path_id(target_stack_id, "target stack id")
    check_order(order)

    current = await get_card(client, creds, board, stack, card)
    body = {
        **current,
        "stackId": int(target),
        "order": order,
    }

    response = await client.put(
        app_url(creds, f"/cards/{card}/reorder"),
        json=body,
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    ocs.parse_app_json(response, what=f"the moved card {card}")

    return await get_card(client, creds, board, target, card)


def check_card_color(color: str) -> str | None:
    if not isinstance(color, str):
        raise ToolError(
            message="Card color must be a string.",
            hint="Use six hex digits or an empty string to clear the color.",
        )
    normalized = color.strip().removeprefix("#")
    if color.strip() and (
        len(normalized) != 6 or any(char not in "0123456789abcdefABCDEF" for char in normalized)
    ):
        raise ToolError(
            message=f"{color!r} is not a six-digit hex color.",
            hint="Use six hex digits or an empty string to clear the color.",
        )
    return normalized.upper() or None


def check_order(order: int) -> int:
    """Deck positions are nonnegative integers; bool is not a position."""
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise ToolError(
            message="order must be a nonnegative integer.", hint="Use 0 for the top position."
        )
    return order


async def update_stack(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    *,
    title: str | None = None,
    order: int | None = None,
) -> dict[str, Any]:
    board, stack = _path_id(board_id, "board id"), _path_id(stack_id, "stack id")
    if order is not None:
        check_order(order)
    stacks = await get_stacks(client, creds, board)
    current = next((item for item in stacks if str(item.get("id")) == stack), None)
    if current is None:
        raise ToolError(
            message=f"Board {board} has no stack {stack}.", hint="Read the board's stacks first."
        )
    response = await client.put(
        api_url(creds, f"/boards/{board}/stacks/{stack}"),
        json={
            "title": check_title(title if title is not None else str(current.get("title") or "")),
            "order": order if order is not None else current.get("order", DEFAULT_CARD_ORDER),
        },
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what=f"the updated stack {stack}"), what="a stack")


async def delete_empty_stack(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    *,
    expected_title: str,
) -> dict[str, Any]:
    """Soft-delete only a confirmed empty stack, counting archived cards as well."""
    board, stack = _path_id(board_id, "board id"), _path_id(stack_id, "stack id")
    for archived in (False, True):
        stacks = await get_stacks(client, creds, board, archived=archived)
        current = next((item for item in stacks if str(item.get("id")) == stack), None)
        if current is None:
            raise ToolError(
                message=f"Board {board} has no stack {stack}.",
                hint="Read the board's stacks first.",
            )
        if not expected_title or current.get("title") != expected_title:
            raise ToolError(
                message="Stack deletion title does not match.",
                hint="Set expected_title to the exact current stack title.",
            )
        if not isinstance(current.get("cards"), list) or current["cards"]:
            raise ToolError(
                message="Only an empty stack can be deleted, including archived cards.",
                hint="Move its cards first; no cards are deleted by this action.",
            )
    response = await client.request(
        "DELETE",
        api_url(creds, f"/boards/{board}/stacks/{stack}"),
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what=f"the deleted stack {stack}"), what="a stack")


async def set_card_archived(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    card_id: str | int,
    *,
    archived: bool,
) -> dict[str, Any]:
    """Resolve through stack listings, which also work for archived cards."""
    board, stack, card = _card_path(board_id, stack_id, card_id)
    for source_archived in (not archived, archived):
        stacks = await get_stacks(client, creds, board, archived=source_archived)
        current_stack = next((item for item in stacks if str(item.get("id")) == stack), {})
        current = next(
            (
                item
                for item in current_stack.get("cards", [])
                if str(item.get("id")) == card and not item.get("deletedAt")
            ),
            None,
        )
        if current is None:
            continue
        if source_archived == archived:
            return current
        operation = "archive" if archived else "unarchive"
        response = await client.put(
            api_url(creds, f"/boards/{board}/stacks/{stack}/cards/{card}/{operation}"),
            json={},
            headers=dict(DECK_HEADERS),
            auth=creds.auth(),
        )
        return _as_dict(
            ocs.parse_app_json(response, what=f"the {operation}d card {card}"), what="a card"
        )
    raise ToolError(
        message=f"Card {card} was not found in board {board}, stack {stack}.",
        hint="Read active or archived cards to obtain its current location.",
    )


async def create_stack(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    *,
    title: str,
    order: int = DEFAULT_CARD_ORDER,
) -> dict[str, Any]:
    board = _path_id(board_id, "board id")
    response = await client.post(
        api_url(creds, f"/boards/{board}/stacks"),
        json={"title": check_title(title), "order": order},
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what="the new stack"), what="a stack")


async def create_label(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    *,
    title: str,
    color: str,
) -> dict[str, Any]:
    board = _path_id(board_id, "board id")
    normalized = color.strip().lstrip("#")
    if len(normalized) != 6 or any(char not in "0123456789abcdefABCDEF" for char in normalized):
        raise ToolError(
            message=f"{color!r} is not a six-digit hex color.", hint="Use a color like 0082C9."
        )
    response = await client.post(
        api_url(creds, f"/boards/{board}/labels"),
        json={"title": check_title(title), "color": normalized.upper()},
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what="the new label"), what="a label")


async def set_card_label(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    card_id: str | int,
    *,
    label_id: str | int,
    assigned: bool,
) -> dict[str, Any]:
    board, stack, card = _card_path(board_id, stack_id, card_id)
    label = _path_id(label_id, "label id")
    operation = "assignLabel" if assigned else "removeLabel"
    current = await get_card(client, creds, board, stack, card)
    present = any(str(item.get("id")) == label for item in current.get("labels", []))
    if present == assigned:
        return current
    response = await client.put(
        api_url(creds, f"/boards/{board}/stacks/{stack}/cards/{card}/{operation}"),
        json={"labelId": int(label)},
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    ocs.parse_app_json(response, what=f"the relabelled card {card}")
    return await get_card(client, creds, board, stack, card)


async def delete_card(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    card_id: str | int,
) -> dict[str, Any]:
    board, stack, card = _card_path(board_id, stack_id, card_id)
    response = await client.request(
        "DELETE",
        api_url(creds, f"/boards/{board}/stacks/{stack}/cards/{card}"),
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what=f"the deleted card {card}"), what="a card")


def _card_path(
    board_id: str | int, stack_id: str | int, card_id: str | int
) -> tuple[str, str, str]:
    return (
        _path_id(board_id, "board id"),
        _path_id(stack_id, "stack id"),
        _path_id(card_id, "card id"),
    )


def check_title(title: str) -> str:
    """Return the trimmed title, or refuse before a request Deck would answer with 400."""
    value = (title or "").strip()
    if not value:
        raise ToolError(
            message="A card needs a title.",
            hint="Give a short title, for example 'Übergabe vorbereiten'.",
        )
    if len(value) > MAX_TITLE_LENGTH:
        raise ToolError(
            message=(
                f"A Deck card title may hold {MAX_TITLE_LENGTH} characters, "
                f"this one has {len(value)}."
            ),
            hint="Shorten the title and put the rest into the description.",
        )
    return value


def check_duedate(duedate: str) -> str:
    """Return the ISO-8601 due date, or refuse before the request."""
    value = (duedate or "").strip()
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(
            message=f"{duedate!r} is not an ISO-8601 date.",
            hint=_DATE_HINT,
        ) from None
    return value


def _path_id(value: str | int, what: str) -> str:
    """Ids are numeric in Deck; anything else is a bug or an attempt (threat T-01-63)."""
    text = str(value).strip()
    if not text.isdigit():
        raise ToolError(
            message=f"{value!r} is not a numeric {what}.",
            hint="Use an id from deck_browse; Deck addresses boards, stacks and cards by number.",
        )
    return text


def _as_list(payload: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ToolError(
            message=f"Nextcloud answered with something that is not a list of {what}.",
            hint="Check that the Deck app is enabled and up to date on that instance.",
        )
    return [item for item in payload if isinstance(item, dict)]


def _as_dict(payload: Any, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ToolError(
            message=f"Nextcloud answered with something that is not {what}.",
            hint="Check that the Deck app is enabled and up to date on that instance.",
        )
    return payload
