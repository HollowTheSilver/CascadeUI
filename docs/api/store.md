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

Enables a persistence backend (e.g., `FileStorageBackend`). Typically called by `setup_persistence()`.

#### `restore_state()`

Loads state from the persistence backend.

#### `add_middleware(middleware_fn)`

Adds a middleware function to the dispatch pipeline.

#### `remove_middleware(middleware_fn)`

Removes a middleware function.

### Properties

- `action_history` (list): Recent dispatched actions
- `persistence_enabled` (bool): Whether a persistence backend is active

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

## `ActionCreators`

Static helper methods for building action payloads. Used internally; you can also call `store.dispatch()` or `view.dispatch()` directly with a type and payload dict.
