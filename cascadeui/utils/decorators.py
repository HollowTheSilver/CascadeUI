# // ========================================( Modules )======================================== // #


import asyncio
import copy
from functools import wraps
from typing import Any, Callable, Dict

# Import logger
from ..utils.logging import AsyncLogger

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")


# // ========================================( Functions )======================================== // #


def cascade_reducer(action_type: str):
    """Decorator to register a reducer function with the state store.

    The decorated function receives ``(action, state)`` where ``state`` is
    already a deep copy -- mutate it freely and return it.  There is no
    need to ``import copy`` or call ``copy.deepcopy`` yourself.
    """

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(action: Dict[str, Any], state: Dict[str, Any]):
            return await func(action, copy.deepcopy(state))

        # Import lazily to avoid circular imports
        from ..state.singleton import get_store

        get_store().register_reducer(action_type, wrapper)
        logger.debug(f"Registered reducer for action type: {action_type}")

        return wrapper

    return decorator


def cascade_component(component_id: str = None):
    """Decorator to register a component callback."""

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(self, interaction):
            # Get component ID
            nonlocal component_id
            actual_id = component_id or func.__name__

            # Dispatch interaction action
            await self.dispatch(
                "COMPONENT_INTERACTION",
                {
                    "component_id": actual_id,
                    "view_id": self.id,
                    "user_id": interaction.user.id,
                    "handler": func.__name__,
                },
            )

            # Call original function
            return await func(self, interaction)

        return wrapper

    return decorator
