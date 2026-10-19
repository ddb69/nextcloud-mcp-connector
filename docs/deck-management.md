# Deck management

All writes below require the existing administrator opt-in `NC_MCP_DECK_MANAGE`.
`delete_card` and `delete_stack` additionally require `NC_MCP_DECK_DELETE`.
Nextcloud still checks the connected user's permissions. No board sharing or access rules change.

Use `deck_browse` to obtain actual board, stack and card IDs. The examples use placeholder IDs.
The `values` argument is a JSON-encoded object.

| Action | Example values | Behavior |
|---|---|---|
| `update_card` | `{"stack_id":"11","card_id":"101","color":"#2d73be"}` | Sets the card color. Six hex digits, optional `#`. |
| `update_card` | `{"stack_id":"11","card_id":"101","color":""}` | Removes the color. Omit `color` to preserve it. |
| `reorder_card` | `{"stack_id":"11","card_id":"101","order":0}` | Moves a card to the top of its existing stack. |
| `move_card` | `{"stack_id":"11","card_id":"101","target_stack_id":"12","order":2}` | Moves to another stack at the requested position. Without `order`, retains append behavior. |
| `create_stack` | `{"title":"Review","order":3}` | Creates a stack at the given position. |
| `update_stack` | `{"stack_id":"11","title":"Review","order":3}` | Renames/reorders a stack; omitted title/order remain unchanged. |
| `delete_stack` | `{"stack_id":"11","expected_title":"Empty"}` | Soft-deletes only an empty stack; refuses active or cards put aside and mismatched titles. |
| `assign_label` | `{"stack_id":"11","card_id":"101","label_id":"7"}` | Assigns an existing label, avoiding duplicate assignment. |
| `remove_label` | `{"stack_id":"11","card_id":"101","label_id":"7"}` | Removes an existing label; an already absent label is unchanged. |
| `set_card_colors` | `{"cards":[{"stack_id":"11","card_id":"101","color":"ffffff"}]}` | Applies 1-25 color-only updates in the specified board. |

Positions are nonnegative integers; zero is the top position. Deck handles the reorder operation.
Card updates preserve the existing stack, date, position, labels, assignees and other fields unless
that action explicitly changes them. Stack deletion checks both active cards and cards put aside;
it does not offer a cascade-delete option.

## Batches and verification

`set_card_colors` validates every entry before it starts writing and rejects duplicate card IDs.
Each entry must contain exactly `stack_id`, `card_id` and `color`. Errors from Nextcloud during
execution are reported per card, together with `updated` and `failed` counts. Completed entries
are not rolled back; retry only failed entries. A transport-level interruption can still interrupt
the batch, so read the cards again before retrying an interrupted call.

`deck_browse(level="cards", board_id="2")` now returns color, position and whether a card is put aside as well.
An additional browse flag selects the cards put aside (including per-stack counts). Deleted cards
remain excluded. This supports end-of-run verification without changing cards or using the browser.

Publish/redeploy the connector and refresh its tool schema in the MCP client before using new
actions. Adding a color to the JSON argument of an older deployed connector does not set it.
