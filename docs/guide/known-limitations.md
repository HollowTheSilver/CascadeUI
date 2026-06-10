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
- The new/refreshed view does not inherit the dead token -- it carries
  the click's fresh token, so subsequent edits and live updates land
  through a working webhook endpoint rather than the expired one.

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

## Burst-Click Toast Under `serialize_interactions = True`

CascadeUI serializes interactions per view by default
(`serialize_interactions = True`) so that rapid-fire clicks do not
race each other's `message.edit()` calls. The lock holds each click's
callback until the previous click finishes its rebuild and edit.
Combined with Discord's REST latency (hundreds of milliseconds per
call, varying with backend load, geography, and the bot's own
resource pressure), a fast clicker can saturate the queue: enough
clicks in a short window eventually push a queued click past the
auto-defer threshold. The exact threshold depends on per-click
latency, which is itself unstable at scale -- the library has not
been stress-tested with hundreds of concurrent users on a single
view. The auto-defer timer pre-acks the queued click, the
acting-view fast path is then disqualified, the refresh falls
through to the channel endpoint, and the work completes
correctly -- but Discord's client may briefly show *"This
interaction failed"* before the channel-endpoint edit lands.

**What actually happens:** the click DID succeed. State mutated, the
reducer ran, subscribers fired, the message edited. The toast is a
Discord UI artifact, not a library failure. The bot's logs show no
error.

**Mitigations** (per-view, all class-attribute overrides):

- `auto_defer_delay = 2.8` -- gives the queue a wider window before
  pre-acking. Stay under 3.0s; Discord's hard interaction timeout
  is the ceiling.
- `serialize_interactions = False` -- skips the lock entirely on
  views where parallel rebuilds are safe (read-only displays, views
  that mutate independent state slices). Race-prone views (game
  boards, shared lists) should keep the lock.

**Why no library-default fix:** dropping the lock reintroduces
concurrent rebuild races, where rapid clicks can produce visual
flicker and occasional state corruption rather than a brief toast.
Raising the default `auto_defer_delay` past 2.5s reduces the
defer-call headroom (currently 500ms before Discord's 3s wall);
under Discord-side latency spikes the defer call could itself land
past the wall, trading one toast cause for another. The per-view
knobs let bot authors tune their burst-prone views without weakening
the global default for views that have different timing
characteristics.

**On observing this at scale.** CascadeUI ships
`/cascadeui perf [on|off|status|clear]` (and the Inspector's
Performance tab) to collect per-dispatch timing samples without
patching the library. Bot authors running at higher concurrency than
the development test bench should turn profiling on for a session
and inspect `notify_ms` p95/max against their actual user load --
that data, not the development-bench timings cited above, is what
should drive any per-view tuning.

---

## Fast-Path Stall Under Discord Edit Latency

The acting-view fast path normally combines the message edit and
the interaction ack into one HTTP round trip in tens of milliseconds.
Under genuine Discord-side latency on the interaction-edit endpoint
(latency spike, ephemeral backend under load, geographic routing
pressure), the same call can take longer than
`auto_defer_delay - 1.0` seconds. When that happens, the `wait_for`
guard cancels the stalled edit and `refresh()` returns immediately.
The auto-defer timer then fires the standalone ack at
`auto_defer_delay` seconds with the full remaining budget, so the
click is acked normally and no *"interaction failed"* toast appears.

**The cost is one missed visible UI update for that click.** The
rebuilt component tree is NOT re-shipped through the channel
endpoint after the stall, because a second edit attempt on top of
the cancelled fast path would consume the timer's ack budget and
reintroduce the very toast the design exists to prevent. The next
state-change refresh ships the up-to-date tree, so users see the
cumulative effect of any clicks that landed during stalls.

In practice this matters only on the rare clicks where Discord
itself is slow. Views that mutate visible state on every click
(toggles, game boards, settings panels) rarely notice -- the next
click refreshes the tree.

**Mitigations** (per-view, all class-attribute overrides):

- `auto_defer_delay = 2.8` -- widens both the fast-path budget
  (1.8s) and the timer fire window. Same trade-off as the
  burst-click section above; stay under 3.0s.
- For callbacks where heavy work plus refresh routinely exceeds a
  second, follow the slow-callback pattern in
  [`concepts.md`](concepts.md#exception-callbacks-that-genuinely-take-more-than-two-seconds)
  (`await self._safe_defer(interaction)` at the top of the
  callback). The click acks immediately and the refresh routes
  through the channel endpoint deliberately.

**A residual case CascadeUI cannot eliminate.** The auto-defer
timer's own `defer()` call is itself a Discord HTTP request. Under
sustained Discord-side latency, the timer's ack call can also take
longer than expected. If both the fast-path edit AND the timer's
defer hit the same latency window, the cumulative cost can cross
the 3-second deadline and a toast appears. This applies to any
interaction, not just refreshing ones -- a select-menu callback
that does nothing more than store an instance attribute can still
hit the toast if Discord's defer endpoint is slow at that moment.
Geographic distance to Discord's POPs is the dominant variable;
bots running far from Discord's regions hit this more than ones
running near them. The framework cannot mitigate platform-wide
latency.

**Distinct from a hung connection.** Everything above concerns slow
*responses* -- Discord eventually replies. A connection that opens
but never responds (a TCP-level hang) is a different failure, and
discord.py issues edits with no total HTTP timeout. `edit_timeout`
(default `60.0` seconds) bounds every refresh, navigation, and
teardown edit so a hung socket is cancelled and the view recovers on
the next interaction rather than pinning indefinitely. It does not
change the fast path, which keeps its own sub-`auto_defer_delay`
bound. Set `edit_timeout = None` to restore unbounded awaits, or
raise it (e.g. `120.0`) for views that routinely upload large
attachments.

**On observing this at scale.** Run `/cascadeui perf` against real
user load. If `notify_ms` p95 routinely exceeds
`(auto_defer_delay - 1.0) * 1000` ms, the fast path is being
cancelled often enough to be visible -- tune toward whatever
threshold the data implies.

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
