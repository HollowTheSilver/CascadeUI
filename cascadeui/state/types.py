
# // ========================================( Modules )======================================== // #


from typing import Dict, List, Any, Optional, Callable, Awaitable, TypeVar

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

# Type variable for generic functions
T = TypeVar('T')
