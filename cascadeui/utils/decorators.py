# // ========================================( Modules )======================================== // #


import asyncio
import copy
from functools import wraps
from typing import Any, Callable, Dict

import logging

logger = logging.getLogger(__name__)


# // ========================================( Functions )======================================== // #


def cascade_reducer(action_type: str):
    """Decorator to register a reducer function with the state store.

    The decorated function receives ``(action, state)`` where ``state`` is
    already a deep copy -- mutate it freely and return it.  Do not cache
    ``state_store.state`` references across dispatches; the reducer's
    snapshot is per-call and outside refs grow stale.

    Raises ``ValueError`` at decoration time when ``action_type`` collides
    with a built-in action (VIEW_CREATED, NAVIGATION_PUSH, UNDO, etc.).
    Reach for middleware or a store hook instead -- shadowing the built-in
    reducer would silently break sessions, navigation, and undo bookkeeping.
    """
    from ..state.reducers import _BUILTIN_REDUCER_ACTIONS

    if action_type in _BUILTIN_REDUCER_ACTIONS:
        raise ValueError(
            f"Cannot register a custom reducer for built-in action {action_type!r}. "
            f"Built-in actions drive CascadeUI's session, navigation, and undo "
            f"machinery. Use middleware (install via setup_middleware(YourMiddleware())) "
            f"for cross-cutting observation, or store.on({action_type!r}, ...) for side-effects."
        )

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(action: Dict[str, Any], state: Dict[str, Any]):
            return await func(action, copy.deepcopy(state))

        # Import lazily to avoid circular imports
        from ..state.singleton import get_store

        get_store()._register_reducer(action_type, wrapper)
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
