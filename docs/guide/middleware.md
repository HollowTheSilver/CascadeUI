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

`result` is the state after the reducer ran. What a middleware returns goes
back up the chain and is not stored, so returning a different mapping changes
nothing; to change the state, dispatch an action a reducer handles. A
middleware that raises after `next_fn` returns leaves the change in place:
subscribers, hooks, and persistence still see it, and then `dispatch()` raises.
An action that changes nothing, such as a broadcast only hooks handle, is
announced the same way. A dispatch cancelled after the reducer ran still saves
the change and tells subscribers, but hooks that had not started when the
cancel landed do not run. Persistence saves what a reducer commits, so a
change made by assigning `store.state` directly is not saved.

Middleware executes in registration order. Each middleware wraps the next one, forming a chain:

```
dispatch -> middleware_1 -> middleware_2 -> reducer
```

### Awaiting Before `next_fn`

A middleware can await before passing the action on, to write an audit row or
check a rate limit. Other dispatches can run and commit while it waits, so when
it passes on the `state` it was given, the next middleware and the reducer
receive the store's current state instead, and a change another dispatch made in
the meantime is kept. Each call to `next_fn` applies the action to the state
current at that call, so calling it twice applies the action twice.

A middleware that passes a new mapping (`{**state, ...}`) has that mapping
reduced as it is, so a change another dispatch commits while a middleware after
it awaits is not in it. Install a middleware that builds a mapping after any
middleware that awaits.

### Short-Circuiting

Return early without calling `next_fn` to block an action:

```python
async def block_spam(action, state, next_fn):
    if action["type"] == "SPAM_ACTION":
        return state  # Action never reaches the reducer
    return await next_fn(action, state)
```

A blocked action changes nothing and is not announced: no subscriber is told
and no `store.on()` hook runs. It still appears in `store.history`.

## Adding Middleware

The canonical install path is `setup_middleware`, which routes every middleware through a uniform install + `async initialize(store)` pipeline. Call it once from the bot author's `setup_hook`:

```python
from discord.ext import commands

from cascadeui import (
    LoggingMiddleware,
    PersistenceMiddleware,
    UndoMiddleware,
    setup_middleware,
)
from cascadeui.persistence import SQLiteBackend

class MyBot(commands.Bot):
    async def setup_hook(self):
        await setup_middleware(
            LoggingMiddleware(),
            PersistenceMiddleware(backend=SQLiteBackend("data.db"), bot=self),
            UndoMiddleware(),
        )
```

`setup_middleware` gates duplicate installs via `store.has_middleware(type(mw))`, then awaits each middleware's `initialize(store)` method when one is defined. Middlewares that need async startup (backend init, migrations, blocking rehydrate) own that work themselves.

!!! warning "Cogs do not install middleware"
    Middleware install belongs to the bot author, not a cog. A cog that called `setup_middleware(...)` inside its own `setup(bot)` would silently mutate the bot's store without the author's consent. Declare the dependency in the cog's docstring instead and let the bot's `setup_hook` satisfy it.

## Built-in Middleware

### Logging Middleware

Logs every dispatched action at INFO level:

```python
from cascadeui import LoggingMiddleware

await setup_middleware(LoggingMiddleware())
```

The `level` (default `INFO`) is the action stream's *emission* level, not a threshold: pass `level="DEBUG"` to keep routine action traffic out of INFO logs so it surfaces only when DEBUG is enabled. `setup_logging(actions=True)` (the default) auto-installs `LoggingMiddleware` at `INFO` for callers who already call `setup_logging` during startup; pass `setup_logging(actions="DEBUG")` to install it at a lower level instead, so a separate install step is not required.

### Persistence

Fans writes across two namespaces (registry, application) with independent debounce windows per namespace. Flushes immediately on the registry lifecycle actions (`PERSISTENT_VIEW_REGISTERED`, `PERSISTENT_VIEW_UNREGISTERED`), which reach a namespace configured with a zero debounce, and routes each action to the namespaces it touches via identity-diff. Scoped state rides under the application namespace; a scoped slot persists when its slot name is opted in (either via `persistent_slots = ("scoped",)` on the view class or via `SlotPolicy(persistent=True)` in `ApplicationPersistence.slots`).

Construct `PersistenceMiddleware` directly with the backends and bot reference it needs; `setup_middleware` installs it and its `initialize(store)` method runs the full pipeline (manager build, backend init, migrations, blocking rehydrate, message-cleanup listener, reattach). See [Persistence](persistence.md) for the full setup flow.

The middleware uses a state identity check to skip actions that don't mutate state, so dispatch-only or pure-bookkeeping actions never trigger a write. Only state-mutating actions on opted-in slots reach disk. Slots default to in-memory, and the scan looks only at the opted-in slot names, never the whole `application` tree. A slot is written when what it stores changes. A `@cascade_reducer` hands every slot back as a copy, so after such an action each opted-in slot is serialized and compared with what was last loaded or saved, and one that matches is not written again. A slot opts in via the `persistent_slots` class attribute on a `_StatefulMixin` subclass, `access_slot(..., persistent=True)` from a reducer, or `SlotPolicy(persistent=True)` at setup time.

### Undo Middleware

Captures state snapshots for views with `enable_undo = True`. Install once during setup:

```python
from cascadeui import UndoMiddleware, setup_middleware

await setup_middleware(UndoMiddleware())
```

`UndoMiddleware()` takes no arguments. Its `initialize(store)` method binds the middleware to the store during the install pass.

The middleware automatically:

- Checks if the dispatching view has `enable_undo = True`
- Records, as the reducer commits the action, a per-slot diff of the `application` keys it changed, plus the session's `shared_data` as it was, so steps follow the order the store committed them even when a middleware around it awaits or dispatches after `next_fn`
- Pushes the diff onto the view's undo stack. Only what the action touched is snapshotted: whole slots, and within a dict slot only the keys it changed, at any depth. Every user's scoped data shares the `scoped` slot, so one user's undo restores that user's keys and leaves every other user's alone. A write to the same key by another view is still overwritten
- Skips internal lifecycle actions (view creation, navigation, etc.)
- Respects batching (one snapshot per batch, not per action, placed where the batch's first change was made)

See [State Management -- Undo/Redo](state.md#undoredo) for the view-side API.

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

### Analytics

```python
async def analytics(action, state, next_fn):
    result = await next_fn(action, state)
    if action["type"] == "PURCHASE_COMPLETED":
        await send_to_analytics(action["payload"])
    return result
```

## Middleware Order

Middleware runs in the order passed to `setup_middleware`, which matters when middleware depends on each other:

```python
from cascadeui.persistence import SQLiteBackend

# Good: logging sees every action (including those blocked by rate limiting).
await setup_middleware(
    LoggingMiddleware(),
    UndoMiddleware(),
    PersistenceMiddleware(backend=SQLiteBackend("data.db"), bot=self),
)
```

Function-style middleware (plain `async def my_middleware(action, state, next_fn)` callables) bypasses the class-based install path. A class-based middleware is the right choice whenever the behavior is more than a one-off filter: the class gives `setup_middleware` a handle for duplicate-install detection, and an optional `initialize(store)` method lets the middleware own its async startup.
