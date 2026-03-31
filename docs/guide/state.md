# State Management

CascadeUI uses a Redux-inspired unidirectional data flow. All state lives in a single store, updated through dispatched actions and immutable reducers.

## The State Store

The `StateStore` is a singleton that holds all application state:

```python
from cascadeui import get_store

store = get_store()  # Always returns the same instance
```

### Dispatching Actions

Actions are plain dicts with a `type` and `payload`. Dispatch them through the store or a view:

```python
# From the store directly
await store.dispatch("MY_ACTION", {"key": "value"})

# From a view (adds the view's ID as the action source)
await self.dispatch("MY_ACTION", {"key": "value"})
```

### Reducers

Reducers transform state in response to actions. They receive the action and a deep copy of the current state, and return the modified state:

```python
from cascadeui import cascade_reducer

@cascade_reducer("SCORE_UPDATED")
async def score_reducer(action, state):
    # @cascade_reducer passes a deep copy — mutate and return directly
    state.setdefault("scores", {})
    user_id = action["payload"]["user_id"]
    state["scores"][user_id] = action["payload"]["score"]
    return state
```

!!! tip "No `copy.deepcopy` needed"
    The `@cascade_reducer` decorator automatically passes a deep copy of state to your function. Mutate it directly and return it — no `import copy` required. If you call `store.dispatch()` with a raw reducer function (without the decorator), you are responsible for copying state yourself.

### Built-in Actions

CascadeUI dispatches these actions automatically:

| Action | When |
|--------|------|
| `VIEW_CREATED` | A StatefulView is registered with the store |
| `VIEW_UPDATED` | A view's state is modified |
| `VIEW_DESTROYED` | A view is cleaned up (exit or timeout) |
| `SESSION_CREATED` | A new user session begins |
| `SESSION_UPDATED` | A session is modified |
| `NAVIGATION_REPLACE` | A view is replaced (one-way transition) |
| `NAVIGATION_PUSH` | A view is pushed onto the navigation stack |
| `NAVIGATION_POP` | A view is popped from the navigation stack |
| `SCOPED_UPDATE` | Per-user or per-guild scoped state is updated |
| `COMPONENT_INTERACTION` | Any StatefulButton or StatefulSelect is clicked |
| `MODAL_SUBMITTED` | A modal form is submitted |
| `PERSISTENT_VIEW_REGISTERED` | A PersistentView is sent and tracked |
| `PERSISTENT_VIEW_UNREGISTERED` | A PersistentView is removed |
| `UNDO` | An undo operation is performed |
| `REDO` | A redo operation is performed |
| `BATCH_COMPLETE` | A batch of actions finishes (contains all batched actions) |

## Subscribers

Subscribe to state changes with optional filtering:

```python
# Subscribe to all actions
store.subscribe("my-listener", my_callback)

# Subscribe to specific action types only
store.subscribe("my-listener", my_callback,
    action_filter={"SCORE_UPDATED", "GAME_ENDED"})
```

### Selectors

Selectors let you subscribe to a specific slice of state. The callback only fires when the selected value actually changes:

```python
# Only notified when the score dict changes, not on every action
store.subscribe("score-watcher", on_scores_changed,
    selector=lambda state: state.get("scores", {}))
```

Views can use selectors too by overriding `state_selector()`:

```python
class ScoreView(StatefulView):
    subscribed_actions = {"SCORE_UPDATED"}

    def state_selector(self, state):
        # Only re-render when MY score changes
        return state.get("scores", {}).get(self.state_key)

    async def update_from_state(self, state):
        score = self.state_selector(state)
        if self.message and score is not None:
            await self.message.edit(embed=discord.Embed(title=f"Score: {score}"))
```

### Unsubscribing

```python
store.unsubscribe("my-listener")
```

Views unsubscribe automatically on exit or timeout.

## Action Batching

Dispatch multiple actions atomically. Subscribers and persistence fire once after all actions complete, not once per action:

```python
# From a view
async with self.batch() as b:
    await b.dispatch("VOTE_CAST", {"user_id": user_id, "delta": 1})
    await b.dispatch("VOTE_LOG", {"entry": "User voted +1"})
# Single notification cycle fires here

# From the store directly
async with store.batch() as b:
    await b.dispatch("FORM_UPDATED", payload1)
    await b.dispatch("NAVIGATION_REPLACE", payload2)
```

Inside a batch, each dispatch still runs through middleware and reducers immediately (state flows sequentially). Only subscriber notifications are deferred until the batch exits.

!!! info "Batch and undo"
    When `UndoMiddleware` is active, all actions in a batch produce a single undo entry. Undoing will revert everything the batch did.

## Event Hooks

React to state lifecycle events without modifying reducers or subscribing:

```python
store = get_store()

async def on_interaction(action, state):
    component = action["payload"].get("component_id", "?")
    print(f"Component {component} was clicked")

store.on("component_interaction", on_interaction)
store.off("component_interaction", on_interaction)  # Unregister
```

Hook names map to action types: `view_created` maps to `VIEW_CREATED`, `component_interaction` maps to `COMPONENT_INTERACTION`, etc. You can also pass the raw action type string directly.

!!! note "Hooks vs. subscribers vs. middleware"
    - **Middleware** runs *before* the reducer, can modify or block actions, and returns state.
    - **Subscribers** run *after* the reducer, receive state, and are filtered by action type and selector.
    - **Hooks** run *after* subscribers, receive the final state, and are read-only. Use hooks for logging, analytics, or side effects that don't need to modify state.

## Computed State

Derived values that cache automatically and only recompute when their input changes:

```python
from cascadeui import computed, get_store

@computed(selector=lambda s: s.get("application", {}).get("votes", {}))
def total_votes(votes):
    return sum(votes.values())

# Access anywhere
store = get_store()
total = store.computed["total_votes"]  # Cached until the votes dict changes
```

The `selector` picks which slice of state to watch. On access, if the selector's output hasn't changed since the last computation, the cached result is returned. No timer, no event: it's lazy.

```python
# Check if a computed value is registered
if "total_votes" in store.computed:
    total = store.computed["total_votes"]

# Force recomputation on next access
store._computed["total_votes"].invalidate()
```

## Action History

The store keeps a history of dispatched actions for debugging:

```python
history = store.action_history  # List of recent actions
```

Use the [DevTools](devtools.md) inspector to browse this interactively.
