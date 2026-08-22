# Performance

CascadeUI's dispatch pipeline is designed so the hot path does as little
work as the feature set allows. When a bot starts feeling sluggish,
three things matter: what the profiler says, which layer is slow, and
which user-level knob tightens that layer.

This guide covers the profiler (what to measure), the dispatch
breakdown (what each number means), and the main optimization knob
every view author has access to -- the state selector.

---

## The Performance Tab

`/cascadeui inspect` opens the `InspectorView`. One of its tabs is
**Performance**. Profiling is off by default -- enabling it costs a
handful of `time.perf_counter()` calls per dispatch, which is
negligible against any Discord round-trip but non-zero, so recording
is opt-in.

**Enable recording.** Click **Enable** on the Performance tab, then
interact with the views you want to profile in another channel. The
inspector self-filters its own view and session from the samples, so
clicking around inside the inspector does not pollute the data.

**Return and click Refresh.** The tab displays three cards once
samples are present:

| Card | What it shows |
|------|---------------|
| Dispatch Timings | Per-phase wall time for every action dispatched |
| Subscriber Timings | Per-subscriber callback wall time, ranked by p95 |
| Refresh Timings | Per-view `refresh()` wall time, grouped by view class |

The Dispatch card answers "which phase is slow?" The Subscriber card
answers "which subscriber is slow?" The Refresh card answers "which
view class is slow to render?"

---

## Reading the Dispatch Breakdown

Each dispatch records five timing fields:

| Field | What it measures |
|-------|------------------|
| `reducer_ms` | The reducer function alone, from entry to return |
| `middleware_ms` | Everything in the dispatch chain that is not the reducer (logging, persistence, undo snapshots) |
| `notify_ms` | Subscriber fan-out -- inline wall time for the acting view's callback plus the scheduling overhead of background tasks for every other subscriber |
| `hooks_ms` | Registered `on()` hooks fired after notify |
| `total_ms` | Sum of all phases, end-to-end |

The split between `reducer_ms` and `middleware_ms` exists so that
slow middleware cannot inflate `reducer_ms` and misdirect attention.
If `total_ms` is high, start with the largest phase:

- **`reducer_ms` dominates** -- the reducer itself is slow. Check for
  deepcopy usage: library reducers shallow-spread, but a custom reducer
  registered via `@cascade_reducer` still deepcopies state on entry by
  design ("mutate freely" is the documented contract, and the copy is
  what makes that safe). It is the largest fixed cost on a custom-reducer
  dispatch, and it scales with how much state the store holds, not with
  how much the reducer writes. Heavy computation should live in a
  selector or a computed value, not the reducer.
- **`middleware_ms` dominates** -- something in the chain is slow.
  Common culprits: a logging middleware writing synchronously, a
  persistence middleware serializing large state on every action
  (check that `PersistenceMiddleware` is installed via
  `setup_middleware(...)`), a custom middleware awaiting network calls.
- **`notify_ms` dominates** -- the acting view's own `on_state_changed`
  is slow, or the store has many subscribers and the scheduling loop
  itself is heavy. Move to the Subscriber card for the per-subscriber
  breakdown.
- **`hooks_ms` dominates** -- a registered `store.on()` hook is slow.
  Hooks are awaited inline after `notify_ms` completes, so their wall
  time lands on the interaction's critical path. Hooks whose body has
  no ordering relationship with the dispatch can run out-of-band by
  wrapping the work in `asyncio.create_task(...)` and returning
  immediately.

---

## Reading the Subscriber Breakdown

The Subscriber card ranks subscriber callbacks by p95 descending,
showing the top 10. Each line:

```
ViewClass:scope_key  n=34  p95=48.22ms  max=156.03ms
```

- `n` is the number of samples (one per dispatch that reached this
  subscriber).
- `p95` is the 95th percentile wall time.
- `max` is the slowest single sample.

**A high p95 with low median is the most interesting signal.** It
means the subscriber is fast most of the time but occasionally hits a
slow path -- typically a `message.edit()` round-trip, a database
write, or a cold-cache computation. That's the exact profile where
selector-based skipping pays off: if the subscriber does not need to
react to this dispatch, the ideal outcome is to skip it entirely
rather than run the slow path.

**A high median is a different story.** The subscriber is doing real
work on every notification. Optimizing means reducing the work itself
-- caching, memoizing, splitting into finer state slices.

---

## How the Dispatch Pipeline Handles Notifications

CascadeUI's subscriber fan-out is a hybrid. The *acting* view (the
subscriber whose id matches `action["source"]`) is awaited inline
inside `dispatch()`. Every other subscriber is scheduled as a
background task under the shared `"state_store_notify"` owner and
runs concurrently.

The split has three consequences:

- **The acting view's refresh lands flush with the button re-enable.**
  A component callback dispatches, the store runs the reducer, the
  acting subscriber's `on_state_changed` is awaited inline, and
  `dispatch()` returns. The interaction's own ack cycle carries the
  refresh, so the visual transition is synchronous with the click.
- **Cross-view subscribers cannot stall the acting dispatch.** A
  secondary view sitting behind a 429 backoff or a slow selector
  runs on the background task queue. The acting click keeps moving.
- **Batched dispatches inherit the same contract.** `push()`, `pop()`,
  and `send()` wrap their dispatches in `store.batch(source_id=...)`,
  which threads the acting view's id onto the batch's single
  `BATCH_COMPLETE` notification. The view the user is navigating *to*
  rides the ack cycle; background subscribers fan out as usual.
- **The acting view's refresh ships as one REST round-trip, not two.**
  When the handled interaction is a component click targeting this
  view's message and its response slot is still open, `refresh()`
  routes the edit through `interaction.response.edit_message()`, which
  combines the Discord ack packet with the edit payload in a single
  request. Disqualified cases (modal submits, cross-view dispatches,
  missing message, already-deferred responses) fall through to the
  channel `PATCH` endpoint with no behavior change. On a 429 the
  reactive backoff arms and the edit is re-queued to ship once the
  window clears; on any other HTTP error the edit falls through to the
  channel path so a transient interaction-endpoint failure never loses
  the refresh. On a stall
  past `auto_defer_delay - 1.0` seconds (default 1.5s), the
  `wait_for` guard cancels the in-flight edit and `refresh()` returns
  immediately rather than falling through -- a second edit on top of
  the cancelled fast path would consume the auto-defer timer's ack
  budget under genuine Discord latency. See
  [Fast-Path Stall Under Discord Edit Latency](known-limitations.md#fast-path-stall-under-discord-edit-latency)
  for the trade-off. **Pattern callbacks deliberately do NOT
  pre-defer** because a manual `defer()` consumes the response slot
  and forces the refresh onto the slower two-call channel path. The
  post-callback defer in `_scheduled_task` acks the interaction after
  the callback returns, keeping the fast path engaged for every
  rebuild+refresh click.

The ordering tradeoff: subscriber completion relative to hook
completion is no longer strict. Hooks are awaited inline after
`_notify_subscribers` returns, but background subscriber tasks keep
running after that return, so a hook and a cross-view subscriber
scheduled on the same dispatch can complete in either order. Code
that needs a strict sequence should use an explicit subscription
chain rather than the implicit subscriber-then-hook ordering.

Tests that assert on cross-view subscriber side effects -- a counter
incremented in a secondary view, a list written by a non-acting
subscriber -- need to drain the background tasks before the assertion
runs. The store exposes an internal flush helper the test suite uses
for this purpose; see the cross-view notification tests in
`tests/test_state_store.py` for the pattern. Production code has no
flush requirement: views subscribe once and never block on subscriber
completion mid-interaction.

### Fan-out cost at scale

Background subscriber notification is scheduled work, not free work --
each subscribed view costs roughly 0.25ms of CPU on the event loop per
dispatch it reacts to. A single dispatch is negligible at that rate.
The ceiling shows up when the *subscriber count* grows with the
deployment: a bot running hundreds of guilds with hundreds of
persistent, always-subscribed views turns one broadcast-style dispatch
(a global announcement, a scheduled sweep, anything that touches state
every subscriber selects on) into a proportionally larger block of
scheduling work on the same event loop that also has to ack every
other bot's interactions within Discord's 3-second window.

This is not a per-dispatch latency problem: the acting view's own
refresh is unaffected, since it is awaited inline and every other
subscriber runs in the background. It is an event-loop saturation
problem: past the point where scheduled subscriber work outpaces the
loop's spare capacity, the failure mode is bot-wide, not scoped to the
view that dispatched. Interactions unrelated to the dispatch start
missing the 3-second ack deadline because the loop is busy running
notification callbacks. `state_selector()` is the mitigation -- a
tight selector removes a subscriber from the fan-out entirely for
dispatches it does not care about, which is the only lever that
reduces subscriber *count* rather than per-subscriber cost. Watch the
Subscriber card's `n` column at deployment scale, not just `p95`: a
high `n` across many view classes is the signal that selectors are
under-applied, even when no single subscriber looks slow.

---

## Exporting a Profile

The Performance tab's **Export Report** button produces a complete
profiling snapshot as an ephemeral file attachment: a markdown summary
followed by a JSON appendix containing every recorded sample (no
truncation, no top-N filtering). The on-screen cards aggregate for
readability; the export is the raw record.

**Use it when:**

- Filing a performance bug report. Attach the exported file instead of
  screenshotting a truncated summary.
- Capturing a before/after comparison during selector or `@computed`
  work. Export, make the change, clear samples, replay the same
  interactions, export again, diff.
- Sharing a profile with a reviewer asynchronously. The markdown
  renders in any GitHub comment or gist; the JSON appendix makes the
  data machine-readable for custom analysis.

The inspector self-filters its own view and session from the export,
so the data reflects the application workload only.

---

## The Primary Knob: State Selectors

Every `StatefulView` and `StatefulLayoutView` inherits a
`state_selector()` method that returns `None` by default. When a
subclass overrides it, the store calls the selector on every dispatch
and compares the return value to the previous one. If the values are
equal, the subscriber is **not notified** -- the view's
`on_state_changed()` never runs, no `build_ui()` rebuild happens,
no `message.edit()` is queued.

This is the library's primary user-facing optimization knob.
Subscribers that react to every dispatch are the ones that show up at
the top of the Subscriber card.

### Minimal selector

```python
class CounterView(StatefulLayoutView):
    def state_selector(self, state):
        # Only re-render when this user's counter actually changes.
        return self.user_scoped_state().get("count")
```

Before the selector: every dispatch in the session (navigation, modal
submissions, unrelated component clicks) fires
`on_state_changed()` on `CounterView`. After the selector: only
dispatches that change `count` fire the update.

### Selector return value rules

The store skips the subscriber when the selector's return value is
unchanged. The comparison checks identity (`is`) first and falls back
to equality (`==`) only when identity fails. Anything hashable or
comparable works:

- A scalar (`int`, `str`, `bool`, `None`)
- A tuple of scalars
- A `frozenset` of scalars

The identity check is what makes a bare whole-bucket selector cheap:
the built-in reducers shallow-spread, so a slice the current dispatch
did not touch is the *same object* it was before, and `is` answers
"unchanged" without walking it. Dict equality has no such shortcut of
its own, so this is worth more than it looks: comparing a
50,000-key dict to *itself* still walks every entry.

Two things switch the shortcut off. A selector that builds a fresh
container on every call (`dict(...)`, a comprehension, a manual
snapshot) returns a new object whether or not the underlying data
changed, so every comparison falls through to `==`. And a dispatch
handled by a custom `@cascade_reducer` hands the reducer its own copy to
mutate, which rebuilds every dict and list in the state. A slice that is a scalar or a tuple of scalars comes
back as the same object and still matches by identity. Every
bucket-shaped slice is a new object and falls through to `==` for that
dispatch, whether or not the reducer touched it. Equality is still the
backstop and it short-circuits on the first difference, so this costs
the most when a large slice is *unchanged*, which is the case identity
would otherwise have answered for free.

Mutable collections compare by value in Python (`dict == dict` does
element comparison), so returning a dict works. Both dict and tuple
comparison stop at the first difference, so the cost that matters is
the *matching* case, which has to walk to the end either way. Prefer a
tuple of the specific keys the view cares about: it holds fewer
elements than the bucket it came from, and each comparison is a slot
read rather than a hash lookup.

```python
def state_selector(self, state):
    # Tuple of sorted items -- stable, hashable, fast to compare.
    settings = self.user_scoped_state().get("settings") or {}
    return tuple(sorted(settings.items()))
```

### Composite selectors

A view that cares about two independent slices returns a tuple of
both:

```python
def state_selector(self, state):
    user_s = self.user_scoped_state().get("settings")
    guild_s = self.user_guild_scoped_state().get("settings")
    return (
        tuple(sorted(user_s.items())) if user_s else None,
        tuple(sorted(guild_s.items())) if guild_s else None,
    )
```

The store re-renders when either slice changes.

### When a selector returns None

`None` is a normal return value, not a sentinel. The store remembers
the selector's last value and skips notification when the current
value compares equal, so returning `None` twice in a row *does* skip
the subscriber (`None == None` is `True`). The internal sentinel that
forces notification is a private `object()` instance used only for
"no previous value recorded yet" and for selector errors.

Prefer explicit empty values (`()`, `0`, `""`) over `None` when the
slice might legitimately be absent. It keeps the selector's intent
readable -- `()` says "no items," while `None` is ambiguous between
"no data yet" and "data exists but is empty."

### UNDO and REDO bypass the action filter, not the selector

`UNDO` and `REDO` skip the `action_filter` gate so cross-view
subscribers receive them even when their filter excludes those
action types. The selector comparison still runs afterward. A
selector that returns the same value before and after a revert
(because the slice it watches happened to land on the same data)
will correctly skip the subscriber -- the revert is not a re-render
signal on its own.

---

## Before/After Workflow

The Performance tab makes selector work falsifiable. The measurement
loop:

1. Open the inspector, enable recording, click the view you suspect
   is over-rendering.
2. Note the view's row in the Subscriber card. Record `n` and `p95`.
3. Add or tighten the view's `state_selector()`.
4. Click **Clear Samples**, repeat the same interactions.
5. Compare the new `n` and `p95` for the same subscriber.

A good selector reduces `n` dramatically (the subscriber is skipped
on most dispatches) and leaves `p95` roughly the same (when it does
run, the work is unchanged). If `n` drops but `p95` rises, the new
selector is missing a case the view actually needs.

---

## When Selectors Are Not Enough

Selectors remove notifications that don't need to run. They do not
help when the notification itself is the critical path -- for example,
a view that *must* re-render on every game-state change and the
re-render is inherently slow.

For that case, the library has two complementary tools:

- **`batch()` for multi-action sequences.** `async with store.batch()`
  coalesces every dispatch inside the block into a single
  `BATCH_COMPLETE` notification at exit. A "reset all" button that
  writes six settings fires six reducer passes and one subscriber
  fan-out, not six. The library already batches its own pipelines
  (`send()`, `push()`/`pop()`, attached-child cleanup), so user code
  only needs to wrap application-level multi-dispatch sequences. See
  [State Management -- Action Batching](state.md#action-batching)
  for the full idiom and transitivity rules.
- **`@computed` for expensive derived values.** If the view's
  `build_ui()` does heavy computation from state, cache the result
  via `@computed` so repeated dispatches against the same underlying
  data reuse the cached value. See
  [State Management -- Computed Values](state.md#computed-values) for
  the full API.

The ordering for optimization work:

1. Measure with the Performance tab.
2. Add or tighten selectors on the subscribers at the top of the
   list.
3. Wrap related dispatch sequences in `batch()`.
4. Cache expensive derived values with `@computed`.
5. Re-measure.

Each step produces a number the Performance tab can compare against.

---

## Timing and Concurrency Knobs

Selectors, `batch()`, and `@computed` reduce how much *work* a dispatch
does. A separate family of class attributes governs interaction
*timing* and edit *concurrency* -- the dials that matter when a view
does unavoidable async work per interaction (a database read, an
attachment upload, a heavy render). They live on every `StatefulView`
and `StatefulLayoutView`:

| Attribute | Default | What it governs | Raise or change it when |
|-----------|---------|-----------------|-------------------------|
| `auto_defer` | `True` | The ack safety net. A background timer acknowledges the interaction if the callback has not responded in time. | Keep it on. Turning it off removes the only thing standing between a slow callback and Discord's 3-second ack wall. |
| `auto_defer_delay` | `2.5` | How long the safety net waits before acking (seconds). | Rarely. Two edit budgets derive from it by `-1.0` (the acting-view fast path and the ack-coupled navigation edits), so lowering it to ack sooner also shrinks the in-place edit window. The default leaves headroom under the 3s wall. |
| `ack_first` | `False` | Acks before the access checks and the callback run, so the ack lands even if the callback then starves the event loop. | A callback does synchronous work heavy enough to delay the safety-net timer. It costs the one-call refresh fast path on every render, and `open_modal()` degrades to an ephemeral message because the slot is already spent. |
| `serialize_interactions` | `True` | Serializes callbacks behind a lock so rapid clicks cannot fire racing `message.edit()` calls. | Keep it on for views that edit one shared message. It serializes the edits, not the data loads. |
| `refresh_cooldown_ms` | `None` | A proactive throttle on **background** re-renders: state-driven edits inside the window coalesce into one deferred render, and a `reload()` in the window defers its `on_load()` fetch too, not just the edit. Edits answering a click on the view's own message are exempt. | A view re-renders rapidly on its own and you want fewer REST round-trips. Not a spam guard (see below). The reactive 429 backoff is always on regardless. |
| `edit_timeout` | `60.0` | The ceiling on every edit the library issues after the initial send. A stalled edit is cancelled at this bound. | Uploads or large payloads need longer than 60s per edit. Set `None` to await with no ceiling. |
| `timeout` | `180` (discord.py default) | The discord.py view timeout, in seconds. Values over 900s engage the ephemeral refresh handoff automatically. | Long-lived panels. A persistent view sets `timeout = None`. |

One more knob lives on the persistence layer rather than the view:
`PersistenceMiddleware(restore_concurrency=8)` bounds how many
persistent views reattach concurrently on startup. Raise it when a bot
restores many panels and the serial fetch cost dominates `setup_hook`.
The post-ready repaint that follows serializes panels sharing a channel
regardless of this value (message edits rate-bucket per channel), so
raising the bound speeds deployments whose panels are spread across
channels, not stacked in one.

`auto_defer` is the one to understand. The safety-net timer runs
independently of the serialization lock, so a callback that *awaits* for a
long time still gets acked at `auto_defer_delay` seconds: it produces a
slow response, not a failed one. The timer is an ordinary task on the
event loop, though, so a callback that *blocks* the loop (a long CPU-bound
render, a synchronous file or database call) starves the timer along with
everything else and the ack never lands. `ack_first = True` covers that
case by acking before the callback runs at all. Otherwise the 3-second
wall becomes a risk only when the safety net is weakened
(`auto_defer = False`, or `auto_defer_delay` raised toward 3s) *and* the
render is slow.

### Watching the safety net

The net reports the two outcomes worth acting on, so a slow surface is
visible before it starts failing:

```
INFO     Auto-defer backstop acked for FleetPanel after the handler used its
         full 2.50s budget: 2.61s since interaction creation by local clock
WARNING  Auto-defer ack missed the 3s deadline in FleetPanel: 3.42s since
         interaction creation by local clock (event-loop congestion, slow
         pre-callback work, or a rate-limit wait inside an earlier HTTP call)
```

The INFO line is the one to watch for. It means the handler spent its
entire budget without answering and the net caught it, so the click still
worked and nobody complained. A surface that logs it under load is the
same surface that logs the WARNING once the loop gets busier. The
WARNING means Discord already dropped the interaction and the user saw
"This interaction failed".

The window both lines measure opens before the access checks and before
any `serialize_interactions` lock wait, not just around your callback, so
the elapsed time covers queueing behind another click as well as the work
itself. Nothing is timed on the healthy path: a callback that answers in
time cancels the timer mid-sleep and no measurement is taken.

Neither line is a substitute for the profiler above. They price the ack
window; `enable_perf()` prices the dispatch and the render that follow.

### Pacing a Panel vs. Guarding a Button

Two throttles exist and they solve different problems. Reaching for the
wrong one is the common mistake, so pick by the question you are
answering:

| The question | The tool | Scope |
|--------------|----------|-------|
| "This panel re-renders itself too often." | `refresh_cooldown_ms` | The whole view. Paces re-renders the library starts. |
| "This one control is expensive and people mash it." | `with_cooldown` | One component, one clicker. |
| "Discord is telling the bot to slow down." | Nothing. It is automatic. | Always on, never configurable. |

`refresh_cooldown_ms` paces **background** work: a live scoreboard
re-rendering on every ingest, a panel that reloads on a timer. Edits made
in direct answer to a click are exempt: a user who pressed a button is
owed a response, and making them wait out a window some unrelated
background reload happened to arm is not pacing, it is a lag bug.

That exemption is also why it is the wrong spam guard. It is view-wide,
so it would punish every viewer of a shared panel for one person's
clicking. It no longer touches the clicks anyway:

```python
# Wrong: this paces the panel's own re-renders. It does nothing to the
# clicking, and it slows every other viewer down.
class BoardView(StatefulLayoutView):
    refresh_cooldown_ms = 2500

# Right: throttle the control, for the clicker, and leave the rest alone.
def build_ui(self):
    self.clear_items()
    refresh = StatefulButton(label="Refresh", callback=self._reload_data)
    with_cooldown(refresh, seconds=5, scope="user")
    self.add_item(ActionRow(refresh))
```

`with_cooldown` holds its deadlines on the owning view, so wrapping a
component your build method constructs fresh each render works: the
deadline outlives the component. Its default key is the `custom_id` you
set, or the wrapped callback's qualified name when you set none. Two
buttons calling two named handlers get two cooldowns for free. Buttons
built in a loop do not: their per-item callbacks all carry the factory's
qualified name, so they default to one shared deadline and throttle each
other. Give those a `custom_id=` or an explicit `key=`, the same fix that
separates buttons deliberately wired to one handler. Scope follows the
same four-value grammar as `state_scope` (`"user"`, `"guild"`,
`"user_guild"`, `"global"`).

Neither knob touches Discord's own rate limiting. That backoff is always
on, never configurable, and reads the delay Discord asks for.

---

## Keep HTTP Off the Render Path

`build_ui()` runs synchronously on the render path (the lead-up to
the interaction's response), and `on_load()`, though async, is awaited
before the first paint. Any HTTP a view issues in either still competes
with that interaction's own acknowledgement and adds directly to how
long the view takes to appear.

The sharp edge is a per-item fan-out. Resolving an avatar per row with
`bot.fetch_user(...)`, syncing a role per entry, or looking a record up
per cell reads as parallel work but is not: calls that share a Discord
rate-limit bucket (and `fetch_user` calls all share one -- the user id
is not a bucket-routing parameter) run **serially**, so `asyncio.gather`
over them does not overlap the round-trips. A leaderboard that fetches
twenty avatars on open pays twenty sequential round-trips before the
first paint.

Resolve it off the hot path instead:

- **Read from cache.** `bot.get_user(id)` is a synchronous cache lookup
  with no HTTP; pair it with a default fallback when the cache misses.
  The same applies to `guild.get_member`, `guild.get_role`, and the
  other `get_*` accessors.
- **Do bulk I/O once, in a preload.** If a view genuinely needs remote
  data, fetch it a single time in `on_load()` and render from the
  result, rather than issuing one request per rendered element.

The principle generalizes: a view should render from data it already
holds. When it must reach out, it reaches out once, not once per row.

CascadeUI flags this automatically. When `on_load()` overruns
`auto_defer_delay` (the interaction-timing budget, default 2.5s), the
library logs a warning naming the view and the elapsed time, once per view
class so a consistently slow preload surfaces without flooding the log. A
`ProfileView.on_load() took 4.2s` line names the exact view to move off the
hot path.

---

## See Also

- [DevTools](devtools.md) for the Inspector's other tabs.
- [State Management](state.md) for batching, computed values, and
  scoped state.
- [`cascadeui/devtools.py`](https://github.com/HollowTheSilver/CascadeUI/blob/main/cascadeui/devtools.py)
  for the Performance tab implementation.
