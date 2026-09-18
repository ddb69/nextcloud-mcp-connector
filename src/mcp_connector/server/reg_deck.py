"""Registration of the deck tools. The logic lives in :mod:`mcp_connector.tools.deck`.

``level`` is a ``Literal`` and therefore an enum in the input schema, not a free string:
the model sees the three valid values instead of guessing "card" or "lists" and paying a
round trip for the correction (D-06, D-14).

All Deck tools are listed unconditionally, even on an instance without the Deck app. A
credential dependent ``tools/list`` is not cacheable, breaks the token budget gate and
surprises clients that persist tool lists; the honest answer to a missing app is the
sentence the tool returns (SRV-04).
"""

import json
from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from pydantic import Field

from .. import deps
from ..errors import ToolError
from ..tools import deck as deck_tools
from . import CREATE_ONLY, DESTRUCTIVE, READ_ONLY, compact, graceful, mcp


@mcp.tool(annotations=READ_ONLY, structured_output=False)
@graceful
async def deck_browse(
    level: Annotated[
        Literal["boards", "stacks", "cards"],
        Field(description="What to list; stacks and cards need a board_id"),
    ] = "boards",
    board_id: Annotated[str, Field(description="Board id from level=boards, e.g. 2")] = "",
    stack_id: Annotated[str, Field(description="Optional: only cards of this stack")] = "",
    limit: Annotated[
        int, Field(ge=1, le=deck_tools.MAX_LIMIT, description="Maximum number of entries")
    ] = deck_tools.DEFAULT_LIMIT,
    ctx: Context | None = None,
) -> str:
    """List Deck boards, the stacks of a board, or its cards."""
    clients = deps.resolve_clients(ctx)
    return compact(
        await deck_tools.browse(
            clients,
            level=level,
            board_id=board_id or None,
            stack_id=stack_id or None,
            limit=limit,
        )
    )


@mcp.tool(annotations=CREATE_ONLY, structured_output=False)
@graceful
async def deck_create_card(
    board_id: Annotated[str, Field(description="Board id from deck_browse")],
    stack_id: Annotated[str, Field(description="Stack id from deck_browse level=stacks")],
    title: Annotated[str, Field(description="Card title, at most 255 characters")],
    description: Annotated[str, Field(description="Optional card text, Markdown")] = "",
    duedate: Annotated[
        str, Field(description="Optional due date, e.g. 2026-09-01T10:00:00+00:00")
    ] = "",
    ctx: Context | None = None,
) -> str:
    """Create a card in an existing stack; never changes or deletes an existing card."""
    clients = deps.resolve_clients(ctx)
    return compact(
        await deck_tools.create_card(
            clients,
            board_id=board_id,
            stack_id=stack_id,
            title=title,
            description=description or None,
            duedate=duedate or None,
        )
    )


@mcp.tool(annotations=DESTRUCTIVE, structured_output=False)
@graceful
async def deck_manage(
    action: Annotated[
        Literal[
            "update_card",
            "move_card",
            "create_stack",
            "create_label",
            "assign_label",
            "remove_label",
            "delete_card",
        ],
        Field(description="Management action; administrator opt-in required"),
    ],
    board_id: Annotated[str, Field(description="Board id from deck_browse")],
    values: Annotated[
        str,
        Field(
            description=(
                "JSON object: stack_id, card_id, title, description, duedate, "
                "target_stack_id, label_id, color or expected_title"
            )
        ),
    ] = "{}",
    ctx: Context | None = None,
) -> str:
    """Manage Deck cards, stacks and labels when explicitly enabled by an administrator."""
    try:
        supplied = json.loads(values)
    except (TypeError, json.JSONDecodeError):
        raise ToolError(
            message="Deck management values are not a JSON object.",
            hint='Use an object such as {"stack_id":"11","card_id":"42"}.',
        ) from None
    if not isinstance(supplied, dict):
        raise ToolError(
            message="Deck management values are not a JSON object.",
            hint='Use an object such as {"title":"Planned"}.',
        )
    return compact(
        await deck_tools.manage(
            deps.resolve_clients(ctx),
            action,
            board_id,
            str(supplied.get("stack_id") or ""),
            str(supplied.get("card_id") or ""),
            title=str(supplied.get("title") or ""),
            description=supplied.get("description"),
            duedate=supplied.get("duedate"),
            target_stack_id=str(supplied.get("target_stack_id") or ""),
            label_id=str(supplied.get("label_id") or ""),
            color=str(supplied.get("color") or ""),
            expected_title=str(supplied.get("expected_title") or ""),
        )
    )
