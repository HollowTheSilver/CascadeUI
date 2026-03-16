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

Reducers transform state in response to actions. They receive the action and current state, and must return a **new** state dict (never mutate the original):

```python
from cascadeui import cascade_reducer
import copy

@cascade_reducer("SCORE_UPDATED")
async def score_reducer(action, state):
    new_state = copy.deepcopy(state)
    new_state.setdefault("scores", {})
    user_id = action["payload"]["user_id"]
    new_state["scores"][user_id] = action["payload"]["score"]
    return new_state
```

!!! warning "Always use `copy.deepcopy`"
    Shallow copies can lead to shared references between old and new state, causing subtle bugs. `copy.deepcopy(state)` is the safe default.

### Built-in Actions

CascadeUI dispatches these actions automatically:

| Action | When |
|--------|------|
| `VIEW_CREATED` | A StatefulView is registered with the store |
| `VIEW_UPDATED` | A view's state is modified |
| `VIEW_DESTROYED` | A view is cleaned up (exit or timeout) |
| `SESSION_CREATED` | A new user session begins |
| `SESSION_UPDATED` | A session is modified |
| `NAVIGATION` | A view transition occurs |
| `COMPONENT_INTERACTION` | Any StatefulButton or StatefulSelect is clicked |
| `MODAL_SUBMITTED` | A modal form is submitted |
| `PERSISTENT_VIEW_REGISTERED` | A PersistentView is sent and tracked |
| `PERSISTENT_VIEW_UNREGISTERED` | A PersistentView is removed |

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

## Action History

The store keeps a history of dispatched actions for debugging:

```python
history = store.action_history  # List of recent actions
```

Use the [DevTools](devtools.md) inspector to browse this interactively.
