# Known Limitations

This page documents platform constraints, upstream library constraints, and
CascadeUI-specific behaviors that cannot be changed without fundamentally
different designs. Entries are grouped by source.

---

## Discord Component Limits

**Affects:** Views with many interactive elements or complex layouts.

**V1 (`StatefulView`):** Maximum 5 ActionRows, with up to 5 buttons per row
(25 interactive components total). Select menus consume an entire row, so a view
with 2 selects has only 3 rows left for buttons. discord.py enforces this before
the message is sent.

**V2 (`StatefulLayoutView`):** Maximum 40 total components in the tree.
Containers, TextDisplays, ActionRows, Buttons, Selects, Separators - everything
counts toward the budget. Discord rejects the message if the count exceeds 40.

**Why:** Both limits are Discord API constraints, not CascadeUI limitations.

**Workarounds:**

- **V1:** Use `PaginatedView` or `TabView` to distribute controls across
  multiple pages/tabs within the 5-row budget.
- **V2:** Use markdown-formatted `TextDisplay` components to aggregate multiple
  items into a single component. A list of 10 items as one `TextDisplay` with
  line breaks costs 1 component instead of 10. `PaginatedLayoutView` and
  `TabLayoutView` also help distribute content across states within the budget.

---

## CascadeUI Constraints

These are side effects of how CascadeUI interacts with the Discord platform.
The library handles most of them automatically.

### V1 and V2 Views Cannot Push/Pop Between Each Other

`push()` or `pop()` between a V1 and V2 view raises `TypeError`. Discord's
`IS_COMPONENTS_V2` flag is a one-way switch per message - once set, the message
cannot revert to V1. Since push/pop reuse the same message, mixing versions
would produce an invalid state. Use `replace()` for one-way transitions between
V1 and V2 (creates a new message, no back button).

### V2 Views Cannot Be Stripped From Messages

Calling `message.edit(view=None)` on a V2 message produces an empty message
(error 50006) because V2 views *are* the message content. CascadeUI handles
this automatically: `exit()` and `on_timeout()` call `_freeze_components()` to
disable all interactive items, preserving the visual content while making
buttons and selects unclickable. Override `exit()` and call
`_freeze_components()` for custom exit behavior.

### Auto-Defer and the Response Slot

CascadeUI automatically acknowledges interactions after every callback, so most
callbacks need no `defer()` call at all. A timed safety net also defers
proactively if a callback runs longer than `auto_defer_delay` (default 2.5s).

The library provides helpers that handle the response slot transparently:

- **`self.respond()`** -- sends a message, falling back to
  `interaction.followup.send()` if auto-defer already consumed the slot
- **`self.open_modal()`** -- opens a modal, sending an ephemeral fallback
  if the slot is consumed (modals cannot follow a defer)

For the rare case where you need to claim the slot explicitly (e.g. a slow
callback that must send followups), respond before the timer:

```python
async def my_slow_callback(self, interaction):
    await self.respond(interaction, "Working...", ephemeral=True)
    result = await slow_operation()
    await interaction.followup.send(f"Done: {result}", ephemeral=True)
```

All auto-defer mechanisms check `is_done()` before firing, so explicit defers
and manual responses are safe to combine.

---

## discord.py Constraints

These are limitations in discord.py's implementation, not the Discord API
itself. Future discord.py releases may resolve them.

### V2 Modal Input Components Not Supported in Modals

Opening a modal containing `Checkbox`, `CheckboxGroup`, or `RadioGroup`
produces a `400 Bad Request` (error 50035). Discord's modal endpoint does
accept these component types with proper wrapping, but discord.py's `Modal`
serialization sends them as bare top-level components without the required
ActionRow wrapping and does not set the `components_v2` flag. Place structured
choices as inline components on the view itself (using `StatefulSelect` or
`toggle_section()`) and reserve modals for `TextInput` fields only. The
`v2_wizard.py` example demonstrates this hybrid pattern on step 4.

### Ephemeral Messages Cannot Be Fetched

Ephemeral messages have no permanent message ID accessible to the bot. This
means `PersistentView` and `PersistentLayoutView` cannot be sent as ephemeral
responses - the view works during the current session but cannot be re-attached
after a bot restart because the reattach pipeline has no message ID to call
`fetch_message()` with. Send persistent views as regular messages, or use a DM
channel for private persistent views.

---

## Ephemeral Editability Expires After 15 Minutes

**Affects:** Long-lived ephemeral views (live dashboards, private game panels)
sent via `view.send(ephemeral=True)`.

**What happens:** Discord's interaction token expires exactly 15 minutes after
it is created. Once expired, the bot can no longer edit or delete the original
ephemeral message via the webhook that produced it. Without mitigation, the
view's live updates simply stop after the wall.

**Why:** This is a Discord platform constraint. The webhook token attached to
the original interaction is scoped to a 15-minute lifetime and cannot be
extended.

**Mitigation:** `auto_refresh_ephemeral` defaults to `None`, which derives the
behavior from `timeout` -- any ephemeral whose timeout exceeds the 15-minute
webhook window (or has no timeout at all) engages the refresh handoff
automatically; in-window ephemerals (`timeout <= 900`) decline it and expire
naturally. Shortly before the wall, CascadeUI replaces
the view's children with a single "Continue Session" button. When the user
clicks it, the click carries a brand new interaction token (independent of the
original), and CascadeUI spawns a fresh ephemeral with another full 15-minute
window. The handoff preserves all state -- no need to close and reopen
from a parent panel. See `auto_refresh_ephemeral` in
[`api/views.md`](../api/views.md) for the customization knobs
(`refresh_warning_seconds`, `refresh_button_label`, `refresh_button_emoji`,
`refresh_button_style`, and the `_build_refresh_button` hook). Set
`auto_refresh_ephemeral = False` to disable the handoff for short-lived
display ephemerals that should expire naturally.

The `v2_battleship.py` example uses this on the private fleet panel.

**Sub-limitation: stale ephemeral messages cannot be deleted after the token
expires.** When CascadeUI replaces an old ephemeral view (via
`instance_policy="replace"`, `auto_refresh_ephemeral` handoff, or any
`exit(delete_message=True)` call), it attempts to delete the old message.
Inside the original 15-minute window this succeeds cleanly. **Past the 15-minute
wall, the delete call fails at the Discord platform level** -- the webhook
token CascadeUI needs to delete the message is the exact thing that just
expired.

**What you see** when this happens: the stale ephemeral remains visible in
the user's DMs alongside the new one. Its buttons are non-functional (Discord
rejects clicks with *"This interaction failed"* because the click requires
the dead token). The library has already unsubscribed the stale view from
the store, unregistered it from `_active_views` and `_instance_index`, and
dispatched `VIEW_DESTROYED`, so it holds no bookkeeping resources and
cannot receive live updates.

**What CascadeUI guarantees** despite the visual ghost:

- Session registry consistency -- exactly one entry per live view, zero
  orphans, zero duplicates.
- No crashes -- failed delete calls are caught at `base.py` with a logged
  hint about token expiry; state updates that reach stale views fail
  silently via `refresh()`'s `NotFound` guard and the store's subscriber
  try/except wrapper.
- Correct parent/child accounting -- `_cleanup_attached_children` prunes finished
  entries on every pass, so long-lived parents that spawn many refreshed
  children (e.g. a game view across many rounds) do not accumulate stale
  references.
- The new/refreshed view is fully functional, correctly wired to the
  store, and receives all live updates.

**Why there is no workaround:** deleting an ephemeral is a
[documented Discord API](https://discord.com/developers/docs/interactions/receiving-and-responding#edit-followup-message)
operation that requires the webhook token from the original interaction.
When that token expires, Discord removes the bot's ability to touch the
message -- there is no alternate endpoint, no admin override, and no way
to "reclaim" the token. The refresh button pattern exists specifically
because it uses the *click's* new token instead of the *original send's*
dead one, which is the only way to sidestep the constraint. If the user
ignores the refresh button and re-opens the panel from the parent view
instead, they are trading one ghost panel per refresh cycle for the
convenience of not having to click the in-panel button.

**Impact on game/app state:** none. Only the visual presentation is
affected. Downstream logic that reads `_active_views`, session scope
keys, subscribers, or `_attached_children` sees consistent, correct data.

---

## Discord API Quick Reference

These are hard limits enforced by the Discord API. They apply to all bots
regardless of framework. CascadeUI does not add or remove any of these
constraints. discord.py raises errors for most of them before the request is
sent.

### Component Limits

| Constraint | Limit |
|---|---|
| ActionRows per V1 View | 5 |
| Buttons per ActionRow | 5 |
| Select menus per ActionRow | 1 (consumes the entire row) |
| Options per Select menu | 25 |
| Total components per V2 LayoutView | 40 |
| Components per ActionRow (V2) | 5 |
| `custom_id` length | 100 characters |

### Text Limits

| Constraint | Limit |
|---|---|
| Button label | 80 characters |
| Select option label | 100 characters |
| Select placeholder | 150 characters |
| TextInput value | 4000 characters |
| TextDisplay content (V2) | 4000 characters |
| Modal title | 45 characters |

### Embed Limits

| Constraint | Limit |
|---|---|
| Embeds per message | 10 |
| Embed title | 256 characters |
| Embed description | 4096 characters |
| Embed fields | 25 |
| Embed field name | 256 characters |
| Embed field value | 1024 characters |
| Embed footer text | 2048 characters |
| Embed author name | 256 characters |
| Total embed characters | 6000 |

### Interaction Constraints

| Constraint | Limit |
|---|---|
| Interaction response window | 3 seconds |
| Interaction token lifetime | 15 minutes |
| Ephemeral messages | Not editable/deletable after token expiry |
| Modals per interaction | 1 (must be the initial response) |
| TextInputs per Modal | 5 |

### Message Limits

| Constraint | Limit |
|---|---|
| Message content | 2000 characters |
| Files per message | 10 |
