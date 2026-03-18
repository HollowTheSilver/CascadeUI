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

#### `subscribe(subscriber_id, callback, action_filter=None, selector=None)`

Registers a subscriber for state change notifications.

- `subscriber_id` (str): Unique ID for the subscriber
- `callback` (callable): Async function called on matching state changes
- `action_filter` (set[str], optional): Only notify for these action types
- `selector` (callable, optional): Function `(state) -> value` that extracts a slice. Subscriber is only notified when the selected value changes.

#### `unsubscribe(subscriber_id)`

Removes a subscriber.

#### `register_reducer(action_type, reducer_fn)`

Registers a reducer for an action type. Prefer using `@cascade_reducer` instead.

#### `get_state()`

Returns the current state dict.

#### `enable_persistence(backend)`

Enables a persistence backend (e.g., `SQLiteBackend`). Typically called by `setup_persistence()`.

#### `restore_state()`

Loads state from the persistence backend.

#### `add_middleware(middleware_fn)`

Adds a middleware function to the dispatch pipeline.

#### `remove_middleware(middleware_fn)`

Removes a middleware function.

#### `batch()`

Returns an async context manager for batching multiple dispatches into a single notification cycle.

```python
async with store.batch() as b:
    await b.dispatch("ACTION_A", payload_a)
    await b.dispatch("ACTION_B", payload_b)
```

#### `on(event_name, callback)`

Registers an event hook. `event_name` is a snake_case name (e.g., `"view_created"`) that maps to the action type `VIEW_CREATED`.

#### `off(event_name, callback)`

Removes an event hook.

#### `register_computed(name, computed_value)`

Registers a `ComputedValue` instance. Typically called by the `@computed` decorator.

#### `get_scoped(scope, *, user_id=None, guild_id=None)`

Returns scoped state for the given scope type and ID.

#### `set_scoped(scope, data, *, user_id=None, guild_id=None)`

Sets scoped state for the given scope type and ID.

#### `register_view(view)`

Registers a live view instance in the active view registry. Called internally by `StatefulView.send()` and `_navigate_to()`. Idempotent -- safe to call multiple times for the same view. Used by session limiting to track active instances.

#### `unregister_view(view_id)`

Removes a view from the active view registry. Idempotent. Called internally by `exit()`, `on_timeout()`, and `_navigate_to()`.

#### `get_active_views(view_type, scope_key)`

Returns a list of active view instances matching the given type name and scope key, ordered oldest-first.

- `view_type` (str): The view class name (e.g., `"SettingsView"`)
- `scope_key` (str): The scope key (e.g., `"user_guild:123:456"`)

**Returns:** `list` of view instances.

### Properties

- `state` (dict): The current state tree
- `action_history` (list): Recent dispatched actions
- `persistence_enabled` (bool): Whether a persistence backend is active
- `computed` (dict-like): Access computed values by name (e.g., `store.computed["total_votes"]`)

---

## `@cascade_reducer(action_type)`

Decorator that registers a reducer function for a custom action type.

```python
@cascade_reducer("MY_ACTION")
async def my_reducer(action, state):
    new_state = copy.deepcopy(state)
    # ... modify new_state ...
    return new_state
```

---

## `@computed(selector)`

Decorator that registers a computed/derived value on the global store.

```python
@computed(selector=lambda s: s.get("application", {}).get("votes", {}))
def total_votes(votes):
    return sum(votes.values())

# Access:
store.computed["total_votes"]
```

---

## `ActionCreators`

Static helper methods for building action payloads. Used internally; you can also call `store.dispatch()` or `view.dispatch()` directly with a type and payload dict.
