# // ========================================( Modules )======================================== // #


from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

# Basic type aliases for readability
ViewId = str
SessionId = str
ComponentId = str
UserId = Optional[int]
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
SelectorFn = Callable[[StateData], Any]

# Hook: async callable receiving (action, state) -> None.
# Hooks are read-only observers that fire after reducers and subscribers.
HookFn = Callable[[Action, StateData], Awaitable[None]]

# Type variable for generic functions
T = TypeVar("T")
