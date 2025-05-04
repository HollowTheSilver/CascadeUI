
# // ========================================( Modules )======================================== // #


import asyncio
import copy
from datetime import datetime
from typing import Dict, Any, Callable, Awaitable, Optional, List, Union

# Import logging at module level
from ..utils.logging import AsyncLogger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")

# Type definitions
StateData = Dict[str, Any]
Action = Dict[str, Any]
ReducerFn = Callable[[Action, StateData], Awaitable[StateData]]
SubscriberFn = Callable[[StateData, Action], Awaitable[None]]


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

    def _register_core_reducers(self):
        """Register the built-in reducers."""
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

    async def dispatch(self, action_type: str, payload: Any = None,
                       source_id: Optional[str] = None) -> StateData:
        """
        Process an action by updating state and notifying subscribers.

        Args:
            action_type: The type of action being dispatched
            payload: Data associated with the action
            source_id: ID of the component that dispatched the action

        Returns:
            The updated state
        """
        # Create the action object
        action = {
            "type": action_type,
            "payload": payload or {},
            "source": source_id,
            "timestamp": datetime.now().isoformat()
        }

        # Add to history for debugging
        self.history.append(action)
        if len(self.history) > self.history_limit:
            self.history.pop(0)

        # Make sure we have the core reducers
        if not self._core_reducers:
            self._register_core_reducers()

        # Find the appropriate reducer
        reducer = self.reducers.get(action_type)

        if reducer:
            # Apply the reducer to transform state
            new_state = await reducer(action, self.state)
            self.state = new_state

            # Notify subscribers
            await self._notify_subscribers(action)

            # Handle persistence if enabled
            if self.persistence_enabled and self.persistence_backend:
                if action_type in [
                    "VIEW_CREATED", "VIEW_UPDATED", "VIEW_DESTROYED",
                    "SESSION_CREATED", "SESSION_UPDATED", "NAVIGATION"
                ]:
                    asyncio.create_task(self._persist_state())

        return self.state

    async def _notify_subscribers(self, action: Action) -> None:
        """Notify all subscribers about a state change."""
        tasks = []

        for subscriber_id, callback in list(self.subscribers.items()):
            # Skip notifying the source to avoid loops
            if subscriber_id != action.get("source"):
                task = asyncio.create_task(self._safe_notify(subscriber_id, callback, action))
                tasks.append(task)

        if tasks:
            # Wait for all notifications to complete
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_notify(self, subscriber_id: str, callback: SubscriberFn,
                           action: Action) -> None:
        """Safely call a subscriber callback."""
        try:
            await callback(self.state, action)
        except Exception as e:
            print(f"Error notifying subscriber {subscriber_id}: {e}")

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

        try:
            await self.persistence_backend.save_state(self.state)
        except Exception as e:
            print(f"Error persisting state: {e}")

    async def restore_state(self) -> None:
        """Restore state from persistence backend."""
        if not self.persistence_enabled or not self.persistence_backend:
            return

        try:
            restored_state = await self.persistence_backend.load_state()
            if restored_state:
                self.state = restored_state
        except Exception as e:
            print(f"Error restoring state: {e}")

    def enable_persistence(self, backend) -> None:
        """Enable state persistence with the specified backend."""
        self.persistence_enabled = True
        self.persistence_backend = backend


# Singleton instance
_store_instance = None


def get_store() -> StateStore:
    """Get the global state store instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = StateStore()
    return _store_instance
