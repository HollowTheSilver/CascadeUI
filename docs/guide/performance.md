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

The split between `reducer_ms` and `middleware_ms` exists because
slow middleware used to inflate `reducer_ms` and misdirect attention.
If `total_ms` is high, start with the largest phase:

- **`reducer_ms` dominates** -- the reducer itself is slow. Check for
  deepcopy usage (library reducers shallow-spread, but user reducers
  registered via `@cascade_reducer` still deepcopy state on entry by
  design; heavy computation should live in a selector or a computed
  value, not the reducer).
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
  reactive backoff arms and the edit is swallowed; on any other HTTP
  error the edit falls through to the channel path so a transient
  interaction-endpoint failure never loses the refresh. On a stall
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

The selector's return value is compared with `==`. Anything hashable
or comparable works:

- A scalar (`int`, `str`, `bool`, `None`)
- A tuple of scalars
- A `frozenset` of scalars

Mutable collections compare by value in Python (`dict == dict` does
element comparison), so returning a dict works, but returning a dict
snapshot on every dispatch costs the comparison work on every call.
Prefer a tuple of the specific keys the view cares about:

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

## See Also

- [DevTools](devtools.md) for the Inspector's other tabs.
- [State Management](state.md) for batching, computed values, and
  scoped state.
- [`cascadeui/devtools.py`](https://github.com/HollowTheSilver/CascadeUI/blob/main/cascadeui/devtools.py)
  for the Performance tab implementation.
