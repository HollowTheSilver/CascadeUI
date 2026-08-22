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
counts toward the budget, counted recursively. discord.py raises from
`add_item()` the moment the tree would cross the cap, so the error arrives
while `build_ui()` is still composing rather than from Discord at send time.
CascadeUI re-raises it with the running count and a pointer to
`control_buttons(compact=True)`.

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

Route an explicit acknowledgement through `self._safe_defer(interaction)`
rather than a bare `interaction.response.defer()`. The auto-defer timer is
armed outside the interaction lock, so it can take the slot between your
`is_done()` check and your call. Under `serialize_interactions`, a click that
waits on the lock longer than `auto_defer_delay` is acked by the timer, and the
callback's own `defer()` then raises `InteractionResponded`, abandoning the
rest of the callback with no rebuild and no refresh. The library's `is_done()`
checks keep its own backstop from acking twice; they cannot protect a call made
from your callback.

---

## discord.py Constraints

These are limitations in discord.py's implementation, not the Discord API
itself. Future discord.py releases may resolve them.

### DynamicPersistentButton Timer Cannot Cover `from_custom_id`

`DynamicPersistentButton` arms its auto-defer timer inside `callback()`, but
discord.py runs `from_custom_id` (which reconstructs the button from the matched
`custom_id`) and `interaction_check` *before* `callback()`. A subclass whose
`from_custom_id` override does slow I/O (a database lookup to restore state, for
example) runs that work on the 3-second interaction clock with no ack backstop,
because `DynamicItem` exposes no per-item dispatch hook the way `Modal` does.
Keep `from_custom_id` overrides cheap and do slow work inside `on_click`, where
the timer covers it.

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
window, so there is no need to close and reopen from a parent panel.

**What the replacement is rebuilt from.** The reopen reconstructs the view
from the kwargs its constructor was given, plus whatever
[`get_nav_state()`](../api/views.md#get_nav_state-override) returns. An attribute
assigned after `__init__` is not carried unless that hook names it, and the
loss is invisible until a reopen actually fires. A view holding state its
constructor did not receive (a step index, a confirmed flag, a fetched row)
overrides `get_nav_state()` / `restore_nav_state()` to carry it.

The arming deadline is measured from the original `send(ephemeral=True)`,
because the 15-minute window belongs to the original interaction. Views
reached by `push()` or `pop()` arm against that same deadline under their own
policy: an explicit `auto_refresh_ephemeral = False` keeps the handoff off, an
explicit `True` engages it even when the original send declined, and the
default `None` inherits the effective policy of the view navigated from --
the nearest explicit setting on the chain, else the original send's
derivation.

See `auto_refresh_ephemeral` in
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
correctly; Discord's client may nonetheless briefly show *"This
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
- `ack_first = True` -- acknowledges before the access checks and
  the callback run, so a queued click is acked no matter how long it
  waited on the lock. The cost is the acting-view fast path: every
  refresh takes two calls instead of one, and `open_modal()` falls
  back to an ephemeral message because the slot is already consumed
  by the time the callback opens it.

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
- `ack_first = True` -- the view-wide form of the same trade. Every
  callback acks before it runs, so no click can stall past the
  deadline, and in exchange the view gives up the one-call fast path
  permanently rather than losing it on the occasional slow edit.
  Worth it on views whose callbacks routinely risk the ack budget;
  on everything else it pays two calls per refresh to avoid a rare
  one-frame delay. Callbacks on such a view cannot open modals
  either, since `open_modal()` needs the un-acked slot.

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

**Distinct from a hung connection, and from a dropped one.** Everything
above concerns slow *responses* -- Discord eventually replies. A
connection that opens but never responds (a TCP-level hang) is a
different failure, and
discord.py issues edits with no total HTTP timeout. `edit_timeout`
(default `60.0` seconds) bounds every refresh, navigation, and
teardown edit so a hung socket is cancelled and the view recovers on
the next interaction rather than pinning indefinitely. It does not
change the fast path, which keeps its own sub-`auto_defer_delay`
bound. Set `edit_timeout = None` to restore unbounded awaits, or
raise it (e.g. `120.0`) for views that routinely upload large
attachments.

A request that never reached Discord at all is a third case and is
classified separately, even when it surfaces as a timeout: aiohttp's
connect and socket timeouts are `asyncio.TimeoutError` *and*
`aiohttp.ClientError`, and the library reads them as transport rather
than as a stalled edit. Those set `refresh_degraded`; a genuine
`edit_timeout` cancellation does not. See
[Transport Failures Degrade Quietly](#transport-failures-degrade-quietly).

**On observing this at scale.** Run `/cascadeui perf` against real
user load. If `notify_ms` p95 routinely exceeds
`(auto_defer_delay - 1.0) * 1000` ms, the fast path is being
cancelled often enough to be visible -- tune toward whatever
threshold the data implies.

---

## Transport Failures Degrade Quietly

A request can fail before Discord ever sees it: a reset connection, a
dropped keep-alive, a DNS blip. These raise from aiohttp rather than
discord.py, so they carry no HTTP status and are not `HTTPException`.
CascadeUI treats them as a third sibling alongside `HTTPException` and
`RateLimited`, and the response is always the same shape: **log a
warning naming the cause, and carry on**. The alternative is raising
out of a component callback, where the exception reaches `on_error`
and renders a failure card over content that is perfectly fine.

What that means per surface:

| Surface | On a transport failure |
|---|---|
| `refresh()` | The edit is dropped and the render baseline is cleared, so the next state change re-ships the tree unconditionally. |
| `send()`'s post-send message re-fetch | The message stays; `send()` returns it rather than reporting a live message as a failed send. |
| `push()` / `pop()` | The channel endpoint is tried before giving up. If that fails too, the navigation rolls back and the source view stays live and clickable. |
| `respond()` / `respond_safe()` | The reply is dropped. It is **not** retried on the followup path: whether the first send landed is unknowable from here, and a duplicate reply is worse than a missing transient notice. |
| `open_modal()` | Returns `False`. The fallback notice is skipped, since it would travel the same broken connection. |

**The cost is one missed update or one missed notice.** Nothing is
left half-applied: state changes are already committed before the edit
is attempted, so the next interaction renders from correct state.

**Paging patterns rewind their own cursor.** A page, wizard step, or
active tab moves before the repaint, so a dropped edit would otherwise
leave the reader where they were with the cursor already moved, and the
next press would skip past content they never saw. `PaginatedView`,
`PaginatedLayoutView`, `WizardView`, `WizardLayoutView`, `TabView`,
`TabLayoutView`, `PaginatedRegion`, and `Collapsible` all put the cursor
back when the edit never landed, so the recovery press moves one step.
The `on_*` hook is not re-fired: it reports the navigation the user asked
for, which did happen; only the render did not.

This covers cursor moves the reader drove. A data rebuild
(`refresh_data`, `refresh_pages`, `rebuild_pages`) that shrinks the page
count still clamps the cursor into the new range even if its edit is
dropped, because the page the reader was on no longer exists. The render
baseline is cleared either way, so the next refresh ships the corrected
view.

**A callback that changes the view before rendering owes the same
rollback.** This is the case to watch in your own code, because the
library cannot know which of your attributes should survive a render
that never happened. The sharp shape is a control armed on one press
and executed on the next: if the arming render is dropped, the button
on screen still looks unarmed, so the obvious response is to press it
again -- and that press executes, because the flag is already set. A
dropped packet turns a two-press confirmation into one, on exactly the
controls that ask for confirmation because they are destructive.

`restore_on_dropped_render()` puts the rollback next to the write:

```python
with self.restore_on_dropped_render("_confirming"):
    self._confirming = True
    self.build_ui()
    await self.refresh()
```

It snapshots each named attribute and rebinds it only when the render
inside the block was dropped, so the flag and the screen cannot disagree.
The snapshot holds the attribute's value, which makes flags and cursors
the fit and a collection edited in place the exception. Reading
`refresh_degraded` directly stays available for anything else.

**Watch for these in logs** rather than in exception handlers. Every
degradation above logs at `WARNING` through the `cascadeui` logger and
names the underlying error, so `setup_logging()` surfaces them without
any per-call-site handling. A burst of them means the host lost its
connection to Discord, not that anything in the view is wrong.

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
| Options per `CheckboxGroup` | 1-10 |
| Options per `RadioGroup` | 2-10 |
| Total components per V2 LayoutView | 40 |
| Components per ActionRow (V2) | 5 |
| `custom_id` length | 100 characters |
| Component `id` (V2) | 1 to 2147483647 |

### Text Limits

| Constraint | Limit |
|---|---|
| Button label | 80 characters |
| Button url (link buttons) | 512 characters |
| Select option label | 100 characters |
| Select placeholder | 150 characters |
| TextInput value | 4000 characters |
| TextDisplay content (V2) | 4000 characters |
| All display text in one message (V2) | 4000 characters |
| Thumbnail / MediaGalleryItem description (V2) | 1024 characters |
| Modal title | 45 characters |

The TextDisplay and whole-message rows are different limits that share a number. The
per-node one is pre-flighted and raises; the message total is not, because
Discord documents the cap while discord.py counts it without enforcing it,
and refusing outright would reject messages Discord accepts. A V2
view over the total logs a warning naming the measured figure at every seam
that ships a tree, and `LayoutView.content_length()` reports the running
total if you want to check a budget while composing. Compare it against
`MAX_MESSAGE_CHARACTERS`.

Many small nodes are the shape to watch for: each passes its own check,
and the total crosses anyway, with nothing about any single component
looking wrong.

Know what that counter reaches before trusting its silence: it sums
`TextDisplay` content and nothing else. Button labels, select placeholders,
and option labels are all display text it never sees, so on a control-heavy
screen the counter reports only a fraction of the text the message carries.

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
| Inputs per Modal | 5 |

### Message Limits

| Constraint | Limit |
|---|---|
| Message content | 2000 characters |
| Files per message | 10 |
