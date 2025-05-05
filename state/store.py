# // ========================================( Modules )======================================== // #


import asyncio
import copy
from datetime import datetime
from typing import Dict, Any, Optional, List, Union

# Import logging at module level
from ..utils.logging import AsyncLogger
from ..utils.tasks import get_task_manager
from ..utils.errors import with_error_boundary, safe_execute

from .types import Action, StateData, ReducerFn, SubscriberFn

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")


# // ========================================( Class )======================================== // #


class StateStore:
    """Central state manager for the UI framework."""

    _instance = None  # Singleton instance

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(StateStore, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # Core state data
        self.state: StateData = {
            "sessions": {},  # User sessions
            "views": {},  # Active views
            "components": {},  # Component states
            "application": {},  # Application-specific data
        }

        # Callbacks for state changes
        self.subscribers: Dict[str, SubscriberFn] = {}

        # Core reducers
        self._core_reducers: Dict[str, ReducerFn] = {}

        # Custom reducers
        self._custom_reducers: Dict[str, ReducerFn] = {}

        # Combined reducers
        self.reducers: Dict[str, ReducerFn] = {}

        # Action history for debugging/time travel
        self.history: List[Action] = []
        self.history_limit = 100

        # Persistence settings
        self.persistence_enabled = False
        self.persistence_backend = None

        # Task management
        self.task_manager = get_task_manager()

        self._initialized = True
        logger.debug("StateStore initialized")

    def _load_core_reducers(self):
        """Load the built-in reducers only when needed."""
        if self._core_reducers:
            return

        # Import here to avoid circular imports
        from .reducers import (
            reduce_view_created,
            reduce_view_updated,
            reduce_view_destroyed,
            reduce_session_created,
            reduce_session_updated,
            reduce_navigation,
            reduce_component_interaction
        )

        # Register core reducers
        self._core_reducers = {
            "VIEW_CREATED": reduce_view_created,
            "VIEW_UPDATED": reduce_view_updated,
            "VIEW_DESTROYED": reduce_view_destroyed,
            "SESSION_CREATED": reduce_session_created,
            "SESSION_UPDATED": reduce_session_updated,
            "NAVIGATION": reduce_navigation,
            "COMPONENT_INTERACTION": reduce_component_interaction,
        }

        # Update combined reducers
        self.reducers = {**self._core_reducers, **self._custom_reducers}
        logger.debug("Core reducers loaded")

    def register_reducer(self, action_type: str, reducer: ReducerFn) -> None:
        """Register a custom reducer for a specific action type."""
        self._custom_reducers[action_type] = reducer
        self.reducers[action_type] = reducer

    def unregister_reducer(self, action_type: str) -> None:
        """Remove a custom reducer."""
        if action_type in self._custom_reducers:
            del self._custom_reducers[action_type]
            # Rebuild combined reducers
            self.reducers = {**self._core_reducers, **self._custom_reducers}

    @with_error_boundary("dispatch")
    async def dispatch(self, action_type: str, payload: Any = None,
                       source_id: Optional[str] = None) -> StateData:
        """
        Process an action by updating state and notifying subscribers.
        """
        # Create the action object
        action = {
            "type": action_type,
            "payload": payload or {},
            "source": source_id,
            "timestamp": datetime.now().isoformat()
        }

        logger.debug(f"Dispatching action {action_type} from source {source_id}")

        # Add to history for debugging
        self.history.append(action)
        if len(self.history) > self.history_limit:
            self.history.pop(0)

        # Make sure reducers are loaded
        self._load_core_reducers()

        # Find the appropriate reducer
        reducer = self.reducers.get(action_type)

        if reducer:
            logger.debug(f"Found reducer for action {action_type}")
            # Apply the reducer to transform state
            try:
                new_state = await reducer(action, self.state)
                self.state = new_state
                logger.debug(f"State updated by reducer for {action_type}")
            except Exception as e:
                logger.error(f"Error in reducer for {action_type}: {e}", exc_info=True)
        else:
            logger.warning(f"No reducer found for action type {action_type}")

        # Always notify subscribers regardless of reducer
        logger.debug(f"Notifying subscribers about {action_type}")
        await self._notify_subscribers(action)

        # Handle persistence
        if self.persistence_enabled and self.persistence_backend:
            self.task_manager.create_task("state_store", self._persist_state())

        return self.state

    async def _notify_subscribers(self, action: Action) -> None:
        """Notify all subscribers about a state change."""
        tasks = []

        logger.debug(f"Notifying {len(self.subscribers)} subscribers about action: {action['type']}")

        for subscriber_id, callback in list(self.subscribers.items()):
            # Skip notifying the source to avoid loops ONLY if explicitly configured
            # Allow self-notifications by default
            should_notify = True
            if action.get("source") == subscriber_id and action.get("skip_self_notify", False):
                should_notify = False

            if should_notify:
                logger.debug(f"Notifying subscriber {subscriber_id} about action {action['type']}")
                task = self.task_manager.create_task(
                    "state_store_notify",
                    self._safe_notify(subscriber_id, callback, action)
                )
                tasks.append(task)

        if tasks:
            # Wait for all notifications to complete
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.ALL_COMPLETED
            )

            # Check for errors
            for task in done:
                if task.exception():
                    logger.error(f"Error in subscriber notification: {task.exception()}")

    async def _safe_notify(self, subscriber_id: str, callback: SubscriberFn, action: Action) -> None:
        """Safely call a subscriber callback."""
        try:
            logger.debug(f"Executing notification callback for subscriber {subscriber_id}")
            await callback(self.state, action)
        except Exception as e:
            logger.error(f"Error notifying subscriber {subscriber_id}: {e}", exc_info=True)

            # Don't propagate the exception to avoid breaking notification chain
            # but log detailed error information for debugging
            import traceback
            logger.debug(f"Detailed error for {subscriber_id}:\n{traceback.format_exc()}")

    def subscribe(self, subscriber_id: str, callback: SubscriberFn) -> None:
        """Register to receive state updates."""
        self.subscribers[subscriber_id] = callback

    def unsubscribe(self, subscriber_id: str) -> None:
        """Stop receiving state updates."""
        if subscriber_id in self.subscribers:
            del self.subscribers[subscriber_id]

    async def _persist_state(self) -> None:
        """Persist the current state if a backend is configured."""
        if not self.persistence_enabled or not self.persistence_backend:
            return

        # Use safe_execute to prevent crashes
        await safe_execute(
            self.persistence_backend.save_state(self.state),
            fallback=None,
            log_error=True
        )

    async def restore_state(self) -> None:
        """Restore state from persistence backend."""
        if not self.persistence_enabled or not self.persistence_backend:
            return

        # Use safe_execute to prevent crashes
        restored_state = await safe_execute(
            self.persistence_backend.load_state(),
            fallback=None,
            log_error=True
        )

        if restored_state:
            self.state = restored_state
            logger.info("State restored from persistence backend")

    def enable_persistence(self, backend) -> None:
        """Enable state persistence with the specified backend."""
        self.persistence_enabled = True
        self.persistence_backend = backend
        logger.info(f"State persistence enabled with backend: {backend.__class__.__name__}")


# Singleton instance
_store_instance = None


def get_store() -> StateStore:
    """Get the global state store instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = StateStore()
    return _store_instance
