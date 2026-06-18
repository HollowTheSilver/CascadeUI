# // ========================================( Modules )======================================== // #


from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

# Public type aliases used across the state module.
ViewId = str
SessionId = str
ComponentId = str
UserId = Optional[int]
GuildId = Optional[int]
Timestamp = str

# Simple action type
Action = Dict[str, Any]

# Simple state type
StateData = Dict[str, Any]

# Callback types
ReducerFn = Callable[[Action, StateData], Awaitable[StateData]]
SubscriberFn = Callable[[StateData, Action], Awaitable[None]]

# Middleware: async callable receiving (action, state, next_fn) -> StateData.
# next_fn continues the chain or runs the reducer if last.
MiddlewareFn = Callable[[Action, StateData, Callable], Awaitable[StateData]]

# Selector: extracts a slice of state for change detection.
# Returns any value; the store compares old vs new to decide whether to notify.
#
# Selector purity contract: ``state_selector(self, state)`` (and any free
# ``SelectorFn``) must read from the ``state`` argument, never from the live
# store (e.g. ``self.scoped_state``, ``self.user_scoped_state()``, or
# ``self.state_store.state``). The dispatcher passes the post-reduce snapshot
# to the selector so the equality check sees the same slice the subscriber
# will see; reading the live store races the dispatch and may compare a half-
# applied state against the new snapshot. ``StateStore.get_scoped_from(state,
# scope, **ids)`` is the right read inside selectors.
SelectorFn = Callable[[StateData], Any]

# Hook: async callable receiving (action, state) -> None.
# Hooks are read-only observers that fire after reducers and subscribers.
HookFn = Callable[[Action, StateData], Awaitable[None]]

# Type variable for generic functions
T = TypeVar("T")
