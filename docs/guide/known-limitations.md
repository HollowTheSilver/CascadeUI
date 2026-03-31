# Known Limitations

This page documents behaviors that are inherent to how CascadeUI and Discord
interact. These are not bugs — they are architectural constraints that would
require fundamentally different designs to remove. Understanding them helps you
build around them.

---

## V1 and V2 Views Cannot Push/Pop Between Each Other

**Affects:** Navigation between `StatefulView` (V1) and `StatefulLayoutView` (V2).

**What happens:** Calling `push()` or `pop()` between a V1 and V2 view raises
`TypeError`. For example, a V1 hub cannot push a V2 sub-view.

**Why:** Discord's `IS_COMPONENTS_V2` flag is a one-way switch per message. Once
a message is sent as V2 (LayoutView), it cannot revert to V1 (View + embeds), and
vice versa. Since push/pop reuse the same Discord message, mixing versions would
produce an invalid message state.

**Workaround:** Use `replace()` instead of `push()` for one-way transitions
between V1 and V2. `replace()` creates a new message, so the version flag starts
fresh. Note that `replace()` does not preserve navigation history (no back button).

---

## Ephemeral Messages Cannot Be Persistent

**Affects:** `PersistentView` and `PersistentLayoutView` sent as ephemeral
responses.

**What happens:** The view works during the current session but cannot be
re-attached after a bot restart.

**Why:** Ephemeral messages have no permanent message ID. Discord does not store
them server-side after the interaction token expires (~15 minutes). Without a
message ID, `setup_persistence()` cannot call `fetch_message()` to re-attach the
view.

**Workaround:** Send persistent views as regular (non-ephemeral) messages. If you
need a private persistent view, consider using a DM channel instead of ephemeral.

---

## Discord's 40-Component Limit per LayoutView

**Affects:** Complex V2 views with many interactive elements.

**What happens:** Discord rejects the message if the total component count exceeds
40 (Containers, TextDisplays, ActionRows, Buttons, Selects, Separators, etc. all
count toward this limit).

**Why:** This is a Discord API constraint, not a CascadeUI limitation. Each
component in a LayoutView's tree counts toward the 40-component budget.

**Workaround:** Use markdown-formatted `TextDisplay` components to aggregate
multiple items into a single component. For example, a list of 10 items as one
`TextDisplay` with line breaks costs 1 component instead of 10. Pagination
patterns (`PaginatedLayoutView`) and tab patterns (`TabLayoutView`) also help
distribute content across multiple states within the budget.

---

## Auto-Defer Timer and Manual Response

**Affects:** Views with `auto_defer = True` (the default) where callbacks
manually respond to the interaction.

**What happens:** If your callback takes longer than `auto_defer_delay` (default
2.5 seconds) to call `interaction.response`, the auto-defer timer fires first.
Your subsequent `interaction.response.send_message()` call will fail because the
response is already consumed.

**Why:** The auto-defer exists to prevent Discord's 3-second interaction timeout.
It checks `interaction.response.is_done()` before deferring, so it's safe if your
callback responds quickly. But if your callback does slow work *before* responding,
the timer wins the race.

**Workaround:** For callbacks that do slow work, defer explicitly at the start:

```python
async def my_slow_callback(self, interaction):
    await interaction.response.defer()  # Respond immediately
    result = await slow_operation()     # Then do the work
    await interaction.followup.send(f"Done: {result}")
```

This is standard discord.py practice and works naturally with CascadeUI's
auto-defer (which sees `is_done() == True` and skips).

---

## V2 Views Cannot Be Stripped From Messages

**Affects:** `StatefulLayoutView` and its subclasses on exit or timeout.

**What happens:** Calling `message.edit(view=None)` on a V2 message produces an
empty message (Discord error 50006). Unlike V1 views where stripping the view
leaves the embed intact, V2 views *are* the message content — removing the view
removes everything.

**Why:** In V1, the view (buttons) and content (embed) are separate. In V2, the
entire message is the `LayoutView`'s component tree. Sending `view=None` sends an
empty payload, which Discord rejects.

**Workaround:** CascadeUI handles this automatically. On `exit()` and `on_timeout()`,
V2 views call `_freeze_components()` to disable all interactive items, then edit
with the frozen view. The visual content is preserved but buttons/selects become
unclickable. If you need custom exit behavior, override `exit()` and call
`_freeze_components()` before editing.

---

## dispatch_scoped() Does Not Create Undo Snapshots

**Affects:** Views using both `dispatch_scoped()` and `enable_undo = True`.

**What happens:** Changes made via `dispatch_scoped()` cannot be undone with
`self.undo()`. The undo stack has no record of the change.

**Why:** `dispatch_scoped()` fires a `SCOPED_UPDATE` action, which is in the
`UndoMiddleware`'s skip list (`_SKIP_ACTIONS`). The middleware intentionally
ignores bookkeeping actions to avoid polluting the undo stack with framework
internals.

**Workaround:** Dispatch a custom action type instead:

```python
# Instead of:
self.dispatch_scoped({"theme": "dark"})

# Use a custom action:
self.dispatch("SETTINGS_UPDATED", {"theme": "dark"})
```

Register a custom reducer that writes to the same `_scoped` state path:

```python
@cascade_reducer("SETTINGS_UPDATED")
async def reduce_settings(action, state):
    scope_key = f"user:{action['payload']['user_id']}"
    scoped = state.setdefault("application", {}).setdefault("_scoped", {})
    scoped.setdefault(scope_key, {}).update(action["payload"]["data"])
    return state
```

The custom action will be tracked by `UndoMiddleware` and support undo/redo.

---

## Undo/Redo Does Not Sync Across Independent Views

**Affects:** Views with `enable_undo = True` that share scoped state with views
in a different session.

**What happens:** When View A performs an undo, View B (a different class sharing
the same scoped state key) does not receive a live UI update. The state *is*
restored correctly — if you refresh View B or navigate away and back, it shows
the correct values. But it won't update in real time.

**Why:** CascadeUI's subscriber system uses `state_selector()` to avoid
unnecessary UI updates. When undo restores a snapshot, the selector compares the
new state against what the subscriber last saw. If View B already observed those
values (because it was the one that originally made the change), the selector
reports "no change" and skips the notification. This is correct behavior for the
selector — it prevents redundant edits.

Normal dispatched actions (like `SETTINGS_UPDATED`) update the state
incrementally, so the selector always sees a delta. Undo restores entire
snapshots, which can produce states identical to what another view already
observed.

**Workaround:** This only occurs when two *different* view classes share the same
scoped state key *and* one of them has undo enabled. Within a single session
(e.g., the same settings menu pushed/popped through a nav stack), undo/redo
works correctly because the views share a session and the same subscriber context.

If you need cross-view undo reactivity, subscribe both views to `UNDO` and `REDO`
action types and implement a custom `update_from_state()` that always rebuilds,
bypassing the selector optimization:

```python
class MyView(StatefulLayoutView):
    subscribed_actions = {"MY_ACTION", "UNDO", "REDO"}

    def state_selector(self, state):
        # Return None to always trigger update_from_state on UNDO/REDO
        return None

    async def update_from_state(self, state):
        self._build_ui()
        if self.message:
            await self.message.edit(view=self)
```

!!! note
    Returning `None` from `state_selector()` disables the change-detection
    optimization for that view. It will rebuild on *every* subscribed action,
    not just when state actually changes. Use this sparingly.
