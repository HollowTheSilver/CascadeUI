# API: State Store

## `get_store()`

Returns the singleton `StateStore` instance.

```python
from cascadeui import get_store
store = get_store()
```

---

## `StateStore`

### Methods

#### `dispatch(action_type, payload=None, source=None)`

Dispatches an action through the middleware pipeline and into the matching reducer.

- `action_type` (str): The action type (e.g., `"COUNTER_UPDATED"`)
- `payload` (dict, optional): Data for the action
- `source` (str, optional): ID of the dispatching view

Subscriber failures are caught and logged internally -- `dispatch()` does not raise from subscriber errors.

#### `subscribe(subscriber_id, callback, action_filter=None, selector=None)`

Registers a subscriber for state change notifications. Views auto-subscribe during `__init__` and tear down through their own lifecycle hooks; direct calls from user code are rare.

- `subscriber_id` (str): Unique ID for the subscriber
- `callback` (callable): Async function called on matching state changes
- `action_filter` (set[str], optional): Only notify for these action types
- `selector` (callable, optional): Function `(state) -> value` that extracts a slice. Subscriber is only notified when the selected value changes.

#### `state`

Public attribute holding the current state dict. Read-only by convention; mutate state through `dispatch()`, not by assignment.

> **Persistence**
> The store has no direct persistence methods. Wire persistence through [`PersistenceMiddleware`](persistence.md), installed via [`setup_middleware`](#setup_middleware). The middleware's `initialize(store)` pass stashes a `PersistenceManager` on `store.persistence_manager` for runtime access (rehydrate is blocking; writes are debounced).

#### `has_middleware(middleware_cls) -> bool`

Returns `True` when an instance of the given middleware class is installed. Used by `setup_middleware` to gate duplicate installs; available to callers that need to branch on middleware presence.

```python
from cascadeui.state.middleware import UndoMiddleware

if store.has_middleware(UndoMiddleware):
    # undo/redo buttons will work
    ...
```

> **Install path**
> `store._add_middleware` and `store._remove_middleware` are internal. User code installs middleware through [`setup_middleware`](#setup_middleware), which gates duplicates via `has_middleware` and awaits each middleware's `initialize(store)` method in order.

#### `batch()`

Returns an async context manager that coalesces every dispatch inside the
block into a single subscriber notification at exit. Reducers run
immediately -- state is live throughout the block so later dispatches can
read earlier writes. At exit, one synthetic `BATCH_COMPLETE` action is
dispatched carrying the full action list.

```python
async with store.batch():
    await store.dispatch("ACTION_A", payload_a)
    await store.dispatch("ACTION_B", payload_b)
```

**Transitivity.** Any helper routing through `store.dispatch()`
(`update_session`, `dispatch_scoped`, view-level `dispatch`, and the
internal `_register_state`) participates in the active batch. Nested
`batch()` blocks absorb into the outermost batch; no intermediate
`BATCH_COMPLETE` is emitted.

**Exception semantics.** If the block raises, queued actions for this
batch are discarded before the exception propagates -- subscribers do not
see the partial sequence. Reducers have already executed, so state
reflects completed dispatches up to the raise point.

**Profiling.** Per-dispatch profiling samples are suppressed inside a
batch (individual `notify_ms` would be zero); the `BATCH_COMPLETE`
notification fires one sample for the whole batch.

See the [State Management guide](../guide/state.md#action-batching) for
typical scenarios.

#### `on(event_name, callback)`

Registers an event hook. `event_name` is a snake_case name (e.g., `"view_created"`) that maps to the action type `VIEW_CREATED`.

#### `off(event_name, callback)`

Removes an event hook.

#### `get_scoped(scope, *, user_id=None, guild_id=None)`

Returns scoped state for the given scope type and ID. Reads from the live `self.state`.

#### `get_scoped_from(state, scope, **identifiers)` (staticmethod)

Reads a scoped slice from an explicit `state` dict rather than the live store. Intended for `@computed` selectors (which receive `state` as input) and custom reducers (which mutate deep-copied state). Using `store.get_scoped()` inside a reducer would bypass the deep-copied state — `get_scoped_from(state, ...)` keeps the read aligned with what the reducer is mutating.

#### `iter_scoped(scope, slot_name="scoped")`

Iterates over every entry in the named scoped slot, yielding `(identifier_key, data)` pairs. Used by hub views that aggregate across many users or guilds (leaderboards, dashboards) without reaching into `state["application"]["scoped"]` directly.

#### `set_scoped(scope, data, *, user_id=None, guild_id=None)`

Sets scoped state for the given scope type and ID.

#### `get_active_views() -> Mapping[str, Any]`

Returns a read-only `MappingProxyType` over the internal active-view
registry (`view_id -> view instance`). The returned mapping is **live,
not a snapshot** -- subsequent `register_view` / `unregister_view` calls
on the store show through -- but mutation raises `TypeError`, so the
privacy boundary stays intact.

```python
from cascadeui import get_store

store = get_store()
for view_id, view in store.get_active_views().items():
    print(f"{view_id}: {type(view).__name__}")
```

Used by `DevToolsCog` to avoid reaching into `store._active_views`
directly. User code that needs to iterate or count live views can
consume the same accessor.

#### `merge_scoped(state, scope, data, *, slot_name="scoped", subkey=None, **identifiers)`

Reducer-side writer that merges `data` into the scope bucket and returns `state`. Completes the scoped family alongside `get_scoped_from` and `iter_scoped`. Used inside custom reducers to decode the canonical `{"scope", "identifiers", "data"}` payload emitted by `view.dispatch_scoped_as(...)`.

> **Reducer, computed, view-registry, and participant plumbing are internal**
> `_register_reducer`, `_register_computed`, `_register_view`/`_unregister_view`/`_get_active_views`, and `_register_participant`/`_unregister_participant` are single-underscore internals. User code reaches the same behavior through public entry points: `@cascade_reducer` and `@computed` decorators for registration, and `StatefulView.send()` / `exit()` / `register_participant()` for view lifecycle.

### Properties

- `state` (dict): The current state tree
- `action_history` (list): Recent dispatched actions
- `computed` (dict-like): Access computed values by name (e.g., `store.computed["total_votes"]`)

---

## `@cascade_reducer(action_type)`

Decorator that registers a reducer function for a custom action type.

```python
@cascade_reducer("MY_ACTION")
async def my_reducer(action, state):
    # State is already deep-copied by the decorator -- mutate directly
    state["my_key"] = action["payload"]["value"]
    return state
```

---

## `@computed(selector)`

Decorator that registers a memoized derived value on the global store. The
decorated function's `__name__` becomes the key in `store.computed`.

- `selector` (callable): `(state) -> value` that picks the input slice
- The decorated function receives the selector's output and returns the derived value
- Result is cached until the selector output changes

```python
@computed(selector=lambda s: s.get("application", {}).get("votes", {}))
def vote_totals(votes):
    return {lang: len(voters) for lang, voters in votes.items()}

# Access from any view:
totals = store.computed["vote_totals"]
```

## `ComputedValue`

The object created by `@computed`. Rarely used directly.

### Methods

#### `get(state) -> Any`

Returns the cached value, recomputing only if the selector output changed since
the last call.

#### `invalidate()`

Forces recomputation on the next `get()` call, regardless of whether the
selector output changed.

---

## `setup_middleware(*middlewares, store=None)` {#setup_middleware}

Top-level async helper that installs middleware into the store's dispatch chain. Each middleware is installed once (guarded by `store.has_middleware(type(mw))`), then its `async initialize(store)` method is awaited if one is defined.

```python
from cascadeui import setup_middleware
from cascadeui.persistence import SQLiteBackend
from cascadeui.state.middleware import (
    LoggingMiddleware,
    PersistenceMiddleware,
    UndoMiddleware,
)

class MyBot(commands.Bot):
    async def setup_hook(self):
        await setup_middleware(
            LoggingMiddleware(),
            PersistenceMiddleware(backend=SQLiteBackend("data.db"), bot=self),
            UndoMiddleware(),
        )
```

**Parameters**

- `*middlewares` -- middleware instances in the order they should appear in the dispatch chain.
- `store` -- optional explicit store. Defaults to the global singleton from `get_store()`.

**Idempotency.** `initialize` is always awaited, even when the middleware is already installed. Middlewares contract their `initialize` methods as idempotent -- subsequent calls return immediately -- so the always-await policy is safe.

---

## `ActionCreators`

Static helper methods for building action payloads. Used internally; you can also call `store.dispatch()` or `view.dispatch()` directly with a type and payload dict.

For the full list of built-in action types, payload shapes, and reducer behavior, see [Built-in Actions](actions.md).

---

## Type Aliases

### `StateData`

`Dict[str, Any]` -- the canonical state-dict shape passed to reducers, selectors, and middleware. Exported from the package root for type hints on custom reducers and `@computed` selectors.

```python
from cascadeui import StateData, cascade_reducer

@cascade_reducer("MY_ACTION")
async def my_reducer(action: dict, state: StateData) -> StateData:
    state.setdefault("application", {})["counter"] = action["payload"]
    return state
```

Additional type aliases (`Action`, `ReducerFn`, `SubscriberFn`, `MiddlewareFn`, `SelectorFn`, `HookFn`) live in `cascadeui/state/types.py` and can be imported directly from there when needed.
