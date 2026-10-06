# // ========================================( Modules )======================================== // #


from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

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

# Selector: extracts the slice a subscriber watches; the store compares old and new
# to decide whether to notify. It reads the ``state`` argument, never the live
# store (``StateStore.get_scoped_from``), so it is correct whatever state it is
# evaluated against.
SelectorFn = Callable[[StateData], Any]

# Hook: async callable receiving (action, state) -> None.
# Hooks are read-only observers. They fire after reducers, inline in
# dispatch; ordering against the background cross-view subscriber tasks
# is not guaranteed either way.
HookFn = Callable[[Action, StateData], Awaitable[None]]

# Type variable for generic functions
T = TypeVar("T")
