
# // ========================================( Modules )======================================== // #


from functools import wraps
from typing import Callable, Dict, Any
import asyncio

# Import logger
from ..utils.logging import AsyncLogger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")


# // ========================================( Functions )======================================== // #


def cascade_reducer(action_type: str):
    """Decorator to register a reducer function with the state store."""

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(action: Dict[str, Any], state: Dict[str, Any]):
            return await func(action, state)

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
            await self.dispatch("COMPONENT_INTERACTION", {
                "component_id": actual_id,
                "view_id": self.id,
                "user_id": interaction.user.id,
                "handler": func.__name__
            })

            # Call original function
            return await func(self, interaction)

        return wrapper

    return decorator


def cascade_persistent(file_path: str = None):
    """Decorator to make a view class persistent."""

    def decorator(cls):
        original_init = cls.__init__

        @wraps(cls.__init__)
        def new_init(self, *args, **kwargs):
            # Call original init
            original_init(self, *args, **kwargs)

            # Set up persistence
            from ..persistence.storage import FileStorageBackend
            storage = FileStorageBackend(file_path or f"{cls.__name__}_state.json")

            # Enable persistence in state store
            self.state_store.enable_persistence(storage)

            # Attempt to restore state
            asyncio.create_task(self.state_store.restore_state())

        cls.__init__ = new_init
        return cls

    return decorator
