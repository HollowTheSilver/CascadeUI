# Middleware

Middleware sits between `dispatch()` and the reducer, letting you intercept, transform, log, or block actions without modifying core logic.

## How Middleware Works

A middleware function receives three arguments:

- `action` - the action being dispatched
- `state` - the current state
- `next_fn` - call this to pass the action to the next middleware (or the reducer)

```python
async def my_middleware(action, state, next_fn):
    # Before the reducer runs
    print(f"Action: {action['type']}")

    # Pass to next middleware / reducer
    result = await next_fn(action, state)

    # After the reducer runs
    print(f"New state keys: {list(result.keys())}")

    return result
```

Middleware executes in registration order. Each middleware wraps the next one, forming a chain:

```
dispatch -> middleware_1 -> middleware_2 -> reducer
```

### Short-Circuiting

Return early without calling `next_fn` to block an action:

```python
async def block_spam(action, state, next_fn):
    if action["type"] == "SPAM_ACTION":
        return state  # Action never reaches the reducer
    return await next_fn(action, state)
```

## Adding and Removing Middleware

```python
from cascadeui import get_store

store = get_store()
store.add_middleware(my_middleware)
store.remove_middleware(my_middleware)
```

## Built-in Middleware

### Logging Middleware

Logs every dispatched action at INFO level:

```python
from cascadeui import logging_middleware

store.add_middleware(logging_middleware())
```

### Debounced Persistence

Batches disk writes to avoid writing on every single action. Flushes immediately on lifecycle actions (`VIEW_DESTROYED`) and exposes `flush_now()` for shutdown hooks:

```python
from cascadeui import DebouncedPersistence

persistence = DebouncedPersistence(store, interval=2.0)
store.add_middleware(persistence)

# In your shutdown handler:
await persistence.flush_now()
```

## Custom Middleware Examples

### Rate Limiting

```python
from datetime import datetime, timedelta

_last_dispatch = {}

async def rate_limit(action, state, next_fn):
    key = action.get("source")
    if key:
        now = datetime.now()
        last = _last_dispatch.get(key)
        if last and (now - last) < timedelta(seconds=1):
            return state  # Rate limited, skip
        _last_dispatch[key] = now
    return await next_fn(action, state)
```

### Action Validation

```python
async def validate_actions(action, state, next_fn):
    if action["type"] == "SCORE_UPDATED":
        score = action["payload"].get("score", 0)
        if score < 0:
            action["payload"]["score"] = 0  # Clamp to minimum
    return await next_fn(action, state)
```
