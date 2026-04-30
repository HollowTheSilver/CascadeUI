# State Management

CascadeUI uses a Redux-inspired unidirectional data flow. All state lives in a
single store, updated through dispatched actions and immutable reducers. See
[Core Concepts -- Data Flow](concepts.md#data-flow) for the architecture
diagram, and [Core Concepts -- State Topology](concepts.md#state-topology) for
the full state dict structure.

---

## The State Store

The `StateStore` is a singleton:

```python
from cascadeui import get_store

store = get_store()  # Always returns the same instance
```

---

## Actions and Dispatch

Actions are plain dicts with `type`, `payload`, `source`, and `timestamp`.
Dispatch them through the store or a view:

```python
# From the store
await store.dispatch("MY_ACTION", {"key": "value"})

# From a view (adds self.id as source)
await self.dispatch("MY_ACTION", {"key": "value"})
```

!!! note "Subscriber failures don't propagate"
    `dispatch()` wraps subscriber callbacks in a safe handler that catches and
    logs exceptions. Post-dispatch logic runs regardless:
    ```python
    await self.dispatch("GAME_FINISHED", payload)
    await self._cleanup_attached_children()  # Always runs
    ```

### Built-in Actions

CascadeUI dispatches these automatically. For payload shapes, reducer behavior,
and middleware interaction, see the
[Built-in Actions API reference](../api/actions.md).

| Action | When |
|--------|------|
| `VIEW_CREATED` | View registered with the store |
| `VIEW_UPDATED` | View state modified |
| `VIEW_DESTROYED` | View cleaned up (exit or timeout) |
| `SESSION_CREATED` | New user session begins |
| `SESSION_UPDATED` | Session data updated via `update_session()` |
| `NAVIGATION_PUSH` | View pushed onto nav stack |
| `NAVIGATION_POP` | View popped from nav stack |
| `NAVIGATION_REPLACE` | View replaced (one-way) |
| `SCOPED_UPDATE` | Scoped state updated |
| `COMPONENT_INTERACTION` | Button or select clicked |
| `MODAL_SUBMITTED` | Modal form submitted |
| `PERSISTENT_VIEW_REGISTERED` | PersistentView sent and tracked |
| `PERSISTENT_VIEW_UNREGISTERED` | PersistentView removed |
| `UNDO` / `REDO` | Undo/redo performed |
| `BATCH_COMPLETE` | Batch of actions finishes |

---

## Custom Reducers

Most state-management needs are covered by [Application Slots](#application-slots)
and [Scoped State](#scoped-state) below -- the convenience layer that
`dispatch_scoped` writes through. Reach for `@cascade_reducer` when the
state shape genuinely outgrows the slot model: cross-view aggregations,
complex transitions, derived data that depends on multiple slots, or
custom action types that need their own dispatch grammar.

Reducers transform state in response to actions. `@cascade_reducer` handles
deep-copying automatically -- mutate and return directly:

```python
from cascadeui import cascade_reducer

@cascade_reducer("SCORE_UPDATED")
async def score_reducer(action, state):
    scores = state.setdefault("application", {}).setdefault("scores", {})
    scores[action["payload"]["user_id"]] = action["payload"]["score"]
    return state
```

!!! tip "No `copy.deepcopy` needed"
    `@cascade_reducer` passes a deep copy. Mutate directly and return.

!!! warning "Don't hold a reference to the snapshot across `await`"
    The dict passed to a reducer is a fresh deep copy that lives only for
    that one call. Capturing the snapshot in a closure or a class
    attribute and then reading it later returns a stale value -- the
    store has already moved on to the next snapshot. Read the live state
    via `store.state` (or a view's `state_store.state`) instead, and
    re-read after every `await` boundary if values may have changed in
    flight.

    ```python
    @cascade_reducer("ITEM_ADDED")
    async def add_item(action, state):
        items = state.setdefault("application", {}).setdefault("items", [])
        items.append(action["payload"])
        return state  # OK -- the store keeps this and discards the input snapshot

    # Outside reducers, never do this:
    snapshot = store.state                 # stale immediately after any dispatch
    await some_other_dispatch()
    snapshot["application"]["items"]       # reads the OLD copy, not current
    ```

---

## Subscribers

Subscribe to state changes with optional filtering:

```python
# All actions
store.subscribe("my-listener", my_callback)

# Specific action types only
store.subscribe("my-listener", my_callback,
    action_filter={"SCORE_UPDATED", "GAME_ENDED"})
```

### Selectors

Subscribe to a specific state slice -- the callback only fires when the
selected value changes:

```python
store.subscribe("score-watcher", on_scores_changed,
    selector=lambda state: state.get("scores", {}))
```

Views use selectors via `state_selector()`:

```python
class ScoreView(StatefulLayoutView):
    subscribed_actions = {"SCORE_UPDATED"}

    def state_selector(self, state):
        return state.get("scores", {}).get(self.persistence_key)
```

Views unsubscribe automatically on exit or timeout.

!!! warning "Read the `state` argument, not `self.state`"
    Every selector receives the candidate next state as its `state` argument.
    Reading `self.state` (or `self.state_store.state`) instead returns the
    *current* store state, which during a dispatch is whatever existed
    *before* the pending action was applied. That breaks subscription
    change-detection silently -- the selector returns the same value on
    every call, and the view's `on_state_changed` never fires.

    **Correct:** `return state.get("scores", {})`
    **Bug:** `return self.state.get("scores", {})`

    The same rule applies to `@computed` selector functions and to
    `StateStore.get_scoped_from(state, ...)` calls made from reducers.
    `get_scoped_from` is the staticmethod specifically designed to read
    from the deep-copied state handed to a reducer, without reaching
    back through `self.state_store.get_scoped(...)`.

### Concurrent Updates

When multiple dispatches target the same subscriber simultaneously (e.g. two
players clicking buttons at the same time), CascadeUI coalesces the
notifications automatically. The first notification runs `on_state_changed`
normally; any notifications that arrive while it is running set a pending flag
and return immediately. After the first completes, it re-runs once with the
latest store state to capture all pending changes.

This prevents concurrent `build_ui()` and `message.edit()` calls from racing
on the same view. Single-user views are unaffected - the lock is never
contended when only one dispatch runs at a time.

CascadeUI also preserves interaction routing during rebuilds. When `build_ui()`
calls `clear_items()`, old components stay routable in discord.py's internal
dispatch table until `message.edit()` completes the re-registration. Pending
interactions from other users are handled normally instead of being discarded.

---

## Application Slots

An **application slot** is a named bucket under `state["application"][name]`.
Every feature that wants a place in the state tree -- a game's fleet
positions, a dashboard's visit counts, a wizard's in-flight step -- gets one
slot and owns the shape inside it. Slot ownership is claimed by the first
write and inherited by everyone who reaches for the same name afterward.

Three helpers cover the read/write split so selectors never accidentally
mutate authoritative state and reducers never hand-roll the walk:

| Helper | Purpose | Where it's safe |
|--------|---------|------------------|
| `access_slot(state, name, key=None, *, default_factory=None, persistent=False)` | Write/init. Auto-vivifies `state["application"][name][key]` and returns the stored value. | Reducers (deep-copied state), `seed_initial_state` hook. |
| `read_slot(state, name, *path, default=None)` | Variadic pure read. Walks arbitrary depth via `dict.get` chains, never mutates. | `state_selector` methods, `@computed` selectors, anywhere holding live store state by reference. |
| `slot_property(name, slot=..., key=..., default=...)` | Descriptor for the canonical three-level shape (`application[slot][keyed][field]`) read at attribute-access time. | Class body of any view; reads `self.state_store.state`. |

```python
from cascadeui import (
    access_slot, read_slot, slot_property, cascade_reducer, StatefulLayoutView,
)


@cascade_reducer("SHIP_MOVED")
def reduce_ship_moved(action, state):
    # access_slot walks state["application"]["battleship"][user_id],
    # seeding {} on first access. Safe to mutate -- state is deep-copied.
    bs = access_slot(state, "battleship", action["payload"]["user_id"])
    bs["ships"] = action["payload"]["ships"]
    return state


class FleetView(StatefulLayoutView):
    state_scope = "user"

    # Canonical three-level read: application["battleship"][user_id]["ships"]
    ships = slot_property(
        "ships", slot="battleship", key=lambda self: self.user_id, default=[]
    )

    def state_selector(self, state):
        # read_slot is variadic -- pass any depth of path after the slot name.
        return read_slot(state, "battleship", self.user_id, "ships", default=[])
```

Slots are in-memory by default. Passing `persistent=True` to `access_slot`
(or declaring `persistent_slots = ("battleship",)` on the view class)
registers the slot for write-through persistence -- every subsequent write
to that name is fanned out to disk by `PersistenceMiddleware`. See the
[persistence guide](persistence.md) for the full opt-in model.

For paths deeper than three levels, the `slot_property` descriptor caps
out; declare a plain `@property` and call `read_slot` inside it:

```python
class StatsView(StatefulLayoutView):
    @property
    def combat_wins(self):
        return read_slot(
            self.state_store.state,
            "stats", self.guild_id, self.user_id, "combat", "wins",
            default=0,
        )
```

---

## Scoped State

Isolate state per user, guild, or globally. Scoped data is stored under
`state["application"]["scoped"]`, which means it shares the application
namespace's persistence plumbing -- opt a scoped slot in to disk with
`persistent_slots = ("scoped",)`.

### Setup

Set `state_scope` on the view class:

```python
class SettingsView(StatefulLayoutView):
    state_scope = "user"  # Each user gets independent state
```

| `state_scope` | Key | Use case |
|-------|-----|----------|
| `"user"` | User ID | Per-user preferences |
| `"guild"` | Guild ID | Per-server configuration |
| `"user_guild"` | User + Guild ID | Per-user-per-server isolation |
| `"global"` | (none) | Global shared namespace |
| `None` *(default)* | N/A | No scoping -- `dispatch_scoped` unavailable |

!!! note "`state_scope` vs `instance_scope`"
    Both accept the same string values but govern different subsystems.
    `state_scope` = where data is stored. `instance_scope` = how instances are
    counted. See [Views -- Instance Management](views.md#instance-management).

### Reading

```python
# Generic property (returns the view's own scope slice)
my_data = self.scoped_state

# Named accessors for hub views reading multiple scopes
user_prefs = self.user_scoped_state()
guild_config = self.guild_scoped_state()
per_server = self.user_guild_scoped_state()
global_settings = self.global_scoped_state()

# Override identifiers to read other users' data
other_user = self.user_scoped_state(user_id=other_id)
```

### Writing

```python
await self.dispatch_scoped({"clicks": 5, "name": "Alice"})
```

Scoped state merges with existing data.

!!! warning "Scoped data is in-memory by default"
    Scoped state is a Redux data-organization pattern, not a persistence
    default. Writes land in `state["application"]["scoped"]` (or the named
    bucket declared on `scoped_slot`) and stay in-memory unless the slot is
    opted in. Two ways to persist:

    - Class-level: `persistent_slots = ("scoped",)` on the view class, or
      `persistent_slots = ("my_slot",)` paired with `scoped_slot = "my_slot"`.
    - Setup-level: `SlotPolicy(persistent=True)` (plus optional
      `ttl_days=N`) in the `application` config passed to
      `PersistenceMiddleware`.

### Named scoped slots

Pass a `scoped_slot` class attribute to route a view's scoped reads and
writes into a named subsystem bucket instead of the default `"scoped"`
catch-all:

```python
class BattleshipView(StatefulLayoutView):
    state_scope = "user_guild"
    scoped_slot = "battleship_stats"
    persistent_slots = ("battleship_stats",)
```

Writes dispatched via `self.dispatch_scoped(...)` now land under
`state["application"]["battleship_stats"][<scope_key>]`. Multiple subsystems
co-exist cleanly under `application` without collision.

### Scoped family helpers

`StateStore` exposes a small family for working with scoped data
consistently from views, reducers, and selectors:

| Helper | When to use |
|--------|-------------|
| `self.scoped_state` | Property -- the current view's own scope slice. |
| `self.user_scoped_state(user_id=None)` | Read the `"user"` scope slice; defaults to this view's `user_id`. |
| `self.guild_scoped_state(guild_id=None)` | Read the `"guild"` scope slice; defaults to this view's `guild_id`. |
| `self.user_guild_scoped_state(user_id=None, guild_id=None)` | Read the `"user_guild"` composite scope slice. |
| `self.global_scoped_state()` | Read the `"global"` scope slice. |
| `self.dispatch_scoped(data)` | Write-through from a view -- merges into the scope slot. |
| `self.dispatch_scoped_as(scope, data, **ids)` | Write-through from a view with an explicit scope + identifiers. |
| `store.get_scoped(scope, **ids)` | Read from a live store (subscribers, devtools). |
| `StateStore.get_scoped_from(state, scope, **ids)` | Staticmethod -- read from the `state` arg handed to a reducer or `@computed` selector. |
| `store.iter_scoped(scope, slot_name="scoped")` | Iterate every scope bucket under a slot. |
| `StateStore.merge_scoped(state, scope, data, *, slot_name="scoped", subkey=None, **ids)` | Reducer-side writer -- mutates the deep-copied state in place. |

### Cross-View Reactivity

`dispatch_scoped()` fires `SCOPED_UPDATE`, which other views don't subscribe
to by default. For live cross-view updates, dispatch a named action with a
custom reducer:

```python
# This does NOT notify other views:
await self.dispatch_scoped({"theme": "dark"})

# This notifies all subscribers of "SETTINGS_UPDATED":
await self.dispatch("SETTINGS_UPDATED", {
    "scope_key": f"user:{self.user_id}",
    "changes": {"theme": "dark"},
})
```

| Method | Other views react? | Creates undo snapshots? |
|--------|-------------------|------------------------|
| `dispatch_scoped()` | No | Yes |
| `dispatch("NAMED_ACTION")` | Yes | Yes |

!!! note "`dispatch_scoped` and undo"
    `dispatch_scoped()` creates undo snapshots for views with
    `enable_undo = True` -- scoped data lives under `state["application"]`,
    which the undo middleware snapshots. The limitation is cross-view
    reactivity: `SCOPED_UPDATE` is not in any view's default
    `subscribed_actions`, so other views don't rebuild automatically.
    Named actions close that gap.

---

## Session Data

Session data is metadata shared across all views in a push/pop navigation
chain. Views that share a `session_id` (all pushed/popped views inherit the
parent's session) share the same `data` dict. Unlike scoped state, session data
is ephemeral - it lives for the duration of the session and is not persisted
across restarts.

!!! note "Independent invocations do not share a session by default"
    Every view invocation gets its own `session_id` because the auto-derived
    identity includes a per-instance UUID suffix. Two users opening the same
    view class -- or the same user opening it twice -- produce distinct
    sessions with independent `shared_data`. Push/pop chains still stay on
    one session because `_navigate_to` forwards `session_id` explicitly.
    Views that want repeat-open continuity (undo history surviving
    close-and-reopen, for instance) set `session_continuity: ClassVar[bool] =
    True` on the class; see [Five Pillars -- Session continuity](five-pillars.md#session-continuity)
    for the full contract.

### Reading

```python
config = self.shared_data  # dict (empty if no data set)
lang = self.shared_data.get("lang", "en")
```

### Writing

```python
await self.update_session(lang="fr", difficulty="hard")
```

`update_session` shallow-merges into the existing session data. Dispatches
`SESSION_UPDATED`, so views subscribing to that action are notified. Session
data changes are included in undo snapshots when `enable_undo = True`.

### When to Use Session Data

Session data fills the gap between scoped state and instance attributes:

| Storage | Scope | Persists? | Shared across views? |
|---------|-------|-----------|---------------------|
| Instance attributes | Single view | No | No |
| **Session data** | **Push/pop chain** | **No** | **Yes** |
| Scoped state | User/guild/global | Yes | Yes |

Typical use cases:

- **Wizard configuration** -- a multi-step wizard stores the chosen template or
  mode in session data so every step can read it without passing kwargs
- **Game settings** -- difficulty, turn timer, or house rules shared across
  all views in a game session
- **Navigation breadcrumbs** -- tracking which category was last visited in a
  settings hub, so the back button returns to the right place

### Why Session Data Is Not Persisted

Session data is tied to session bookkeeping, and sessions are inherently
transient: a `session_id` identifies a *live* navigation window, not a durable
container. When the last view in a session exits, the session record (and its
`shared_data`) is removed by the reducer. Across a bot restart, no view is
attached to any session, so every session is effectively orphaned. Persisting
`shared_data` would write entries keyed to `session_id` values that no future
view will ever reattach to -- the data would live on as tombstones.

If you want cross-view data that **does** survive restarts, reach for scoped
state instead. Scoped slots are keyed by `user_id`, `guild_id`, or explicit
identifiers -- all stable across restarts -- and opt into persistence via
`persistent_slots = ("scoped",)`. Two panels that need to coordinate across
restarts subscribe to the same scoped slot; a long wizard stores its
in-flight step in a scoped slot keyed to the user. The session layer keeps
its single responsibility (grouping live views) and the persistence layer
owns durable data with stable identity.

| Need | Where it lives |
|------|----------------|
| In-flight wizard step that survives restart | `access_slot(state, "wizard_state", user_id, persistent=True)` |
| Cross-panel settings shared per-guild | `dispatch_scoped(scope="guild", ...)` with `persistent_slots = ("scoped",)` |
| Temporary config passed between wizard steps in one sitting | `shared_data` via `update_session()` |
| Navigation breadcrumb for the current session | `shared_data` via `update_session()` |

Persistent views reattach with fresh `session_id` values by design -- a
restart is the end of liveness, and the new panel is a new session that
happens to use the same Discord message. Any data the panel cares about
lives in the scoped or application namespace, not in session bookkeeping.

### Cross-View Reactivity

Other views can subscribe to `SESSION_UPDATED` to react to session data
changes:

```python
class SubPageView(StatefulLayoutView):
    subscribed_actions = {"SESSION_UPDATED"}

    def state_selector(self, state):
        session = state.get("sessions", {}).get(self.session_id, {})
        return session.get("shared_data", {}).get("difficulty")

    def build_ui(self):
        difficulty = self.shared_data.get("difficulty", "normal")
        # Rebuild UI based on the shared session config
```

---

## Action Batching

Dispatch multiple actions atomically. Subscribers fire once after all actions
complete:

```python
async with self.batch() as b:
    await b.dispatch("VOTE_CAST", {"user_id": user_id, "delta": 1})
    await b.dispatch("VOTE_LOG", {"entry": "User voted +1"})
# Single notification cycle fires here
```

Inside the block, each dispatch runs through middleware and the reducer
immediately -- state is current at every line, so later dispatches can read
what earlier ones wrote. Only subscriber notifications are deferred until the
block exits, where one synthetic `BATCH_COMPLETE` action fires the fan-out.

### When to batch

Reach for `batch()` when a single user interaction triggers multiple state
changes that belong together logically. Each dispatch outside a batch fires
the full subscriber list, which means every connected view calls
`on_state_changed()` and queues a `message.edit()`. Six dispatches means
six rebuilds and six edits; one `batch()` collapses that to one.

Typical scenarios:

- **Reset-all / apply-all**: a "reset to defaults" button that writes six
  settings at once. See
  [`examples/v2_settings.py`](https://github.com/HollowTheSilver/CascadeUI/blob/main/examples/v2_settings.py)
  for the idiom.
- **Multi-step transitions**: a game "start round" action that clears the
  previous turn, rolls a new seed, and resets per-player state.
- **Wizard commit**: a final "submit" step that dispatches one action per
  collected field, then a terminal `WIZARD_COMPLETED`.

`batch()` does not help with a single slow dispatch. If one reducer or one
subscriber is the bottleneck, `batch()` changes nothing -- use the
[Performance](performance.md) tab to identify the phase, then tighten the
selector or cache with `@computed`.

### Transitivity and helpers

Every helper that eventually calls `store.dispatch()` participates in the
outer batch: `update_session()`, `dispatch_scoped()`, and the internal
`_register_state()` used by `send()`. Nested `batch()` blocks absorb into
the outermost batch, so composing batched operations is safe:

```python
async with self.batch():
    await self.dispatch("ROUND_STARTED")
    await self.update_session(seed=new_seed)  # joins the batch
    async with self.batch():                  # absorbs, no inner fan-out
        for pid in player_ids:
            await self.dispatch_scoped({"last_move": None})
# One BATCH_COMPLETE fires here
```

### Exception handling

If the `async with` block raises, every action queued during the batch is
dropped from the outgoing notification -- subscribers never see the partial
sequence. Reducers have already run (state is mutated in place as each
dispatch returns), so `batch()` is not a transaction; it is a notification
gate. Catch and inspect state explicitly if you need rollback semantics.

### The library already batches its own pipelines

`send()`, `push()` / `pop()` / `replace()`, and cascade cleanup on attached
children already wrap their internal dispatches in a single batch. Callers
don't need to wrap `await view.send()` in a batch themselves -- the
navigation and lifecycle sequences produce one notification cycle each
regardless.

!!! info "Batch and undo"
    With `UndoMiddleware` active, all actions in a batch produce a single
    undo entry. Reverting "reset all settings" restores every slice in one
    `UNDO`.

---

## Event Hooks

React to state lifecycle events without subscribing:

```python
async def on_interaction(action, state):
    print(f"Component {action['payload'].get('component_id')} clicked")

store.on("component_interaction", on_interaction)
store.off("component_interaction", on_interaction)
```

Hook names map to action types: `view_created` → `VIEW_CREATED`,
`component_interaction` → `COMPONENT_INTERACTION`, etc.

!!! note "Hooks vs subscribers vs middleware"
    - **Middleware** -- before the reducer, can modify/block actions
    - **Subscribers** -- after the reducer, filtered by action type + selector
    - **Hooks** -- after subscribers, read-only (logging, analytics)

---

## Computed Values

Register memoized derived state that any view can read without recalculating.
The `@computed` decorator combines a **selector** (picks a state slice to watch)
with a **compute function** (transforms that slice). The result is cached and
only recomputed when the selector output changes.

```python
from cascadeui import computed, get_store

@computed(selector=lambda s: s.get("application", {}).get("votes", {}))
def vote_totals(votes):
    return {lang: len(voters) for lang, voters in votes.items()}

# Any view reads the same cached result
store = get_store()
totals = store.computed["vote_totals"]
```

### How It Works

1. On access (`store.computed["name"]`), the selector runs against current state
2. If the selector output matches the last-seen value, the cached result returns
3. If the selector output changed, the compute function runs and the result is cached

Recomputation is lazy -- it only happens when a view reads the value, not on
every dispatch.

### Computed vs `state_selector()`

| | `@computed` | `state_selector()` |
|--|-------------|---------------------|
| **Scope** | Global, shared across all views | Per-view instance |
| **Purpose** | Derive and cache a transformed value | Detect whether a view's relevant state changed |
| **Access** | `store.computed["name"]` | Automatic (drives `on_state_changed`) |
| **Caching** | One cache per registered name | One cache per subscriber |

Use `@computed` when multiple views need the same derived value (totals,
rankings, aggregates). Use `state_selector()` when a single view needs to
filter which state changes trigger its rebuild.

### Invalidation

Force recomputation on next access:

```python
store.computed["vote_totals"].invalidate()
```

This is rarely needed -- the selector-based check handles most cases
automatically.

### Full Example

See [`v2_computed.py`](https://github.com/HollowTheSilver/CascadeUI/blob/main/examples/v2_computed.py)
for a complete poll example with computed totals and leader detection.

---

## Undo/Redo

Enable undo/redo per view:

```python
from cascadeui import UndoMiddleware, get_store

store = get_store()
store.add_middleware(UndoMiddleware(store))

class EditableView(StatefulLayoutView):
    enable_undo = True
    undo_limit = 20     # Max snapshots (default)

    async def undo_action(self, interaction):
        await self.undo()

    async def redo_action(self, interaction):
        await self.redo()
```

Snapshots capture both `state["application"]` and the session's `data` dict.
This means `dispatch_scoped()` changes, `update_session()` changes, and custom
reducer changes all round-trip through undo/redo. Internal lifecycle actions
are excluded from undo tracking. New actions after an undo clear the redo
stack (standard semantics).

---

## Action History

The store keeps a history of dispatched actions for debugging:

```python
history = store.action_history  # List of recent actions
```

The [DevTools Inspector](devtools.md) visualizes this history on the History
tab.
