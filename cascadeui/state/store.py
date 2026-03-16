
# // ========================================( Modules )======================================== // #


import copy
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional, List, Set, Tuple, Union

# Import logging at module level
from ..utils.logging import AsyncLogger
from ..utils.tasks import get_task_manager
from ..utils.errors import with_error_boundary, safe_execute

from .types import Action, StateData, ReducerFn, SubscriberFn, MiddlewareFn, SelectorFn

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

        # Callbacks for state changes: {id: (callback, action_filter, selector)}
        self.subscribers: Dict[str, Tuple[SubscriberFn, Optional[Set[str]], Optional[SelectorFn]]] = {}

        # Memoized selector results for change detection
        self._last_selected: Dict[str, Any] = {}
        self._SENTINEL = object()  # Marker for "no previous value"

        # Core reducers
        self._core_reducers: Dict[str, ReducerFn] = {}

        # Custom reducers
        self._custom_reducers: Dict[str, ReducerFn] = {}

        # Combined reducers
        self.reducers: Dict[str, ReducerFn] = {}

        # Action history for debugging/time travel
        self.history: List[Action] = []
        self.history_limit = 100

        # Middleware pipeline (executed in order before reducers)
        self._middleware: List[MiddlewareFn] = []

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
            reduce_component_interaction,
            reduce_modal_submitted,
            reduce_persistent_view_registered,
            reduce_persistent_view_unregistered,
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
            "MODAL_SUBMITTED": reduce_modal_submitted,
            "PERSISTENT_VIEW_REGISTERED": reduce_persistent_view_registered,
            "PERSISTENT_VIEW_UNREGISTERED": reduce_persistent_view_unregistered,
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

    def add_middleware(self, middleware: MiddlewareFn) -> None:
        """Add middleware to the dispatch pipeline.

        Middleware runs in order between action creation and the reducer.
        Each middleware receives (action, state, next_fn) and must call
        next_fn(action, state) to continue the chain, or return state
        directly to short-circuit.
        """
        self._middleware.append(middleware)

    def remove_middleware(self, middleware: MiddlewareFn) -> None:
        """Remove a middleware from the pipeline."""
        if middleware in self._middleware:
            self._middleware.remove(middleware)

    async def _run_middleware_chain(self, action: Action, reducer_fn) -> StateData:
        """Build and execute the middleware chain ending at the reducer."""
        async def run_reducer(act, state):
            if reducer_fn:
                try:
                    new_state = await reducer_fn(act, state)
                    self.state = new_state
                    logger.debug(f"State updated by reducer for {act['type']}")
                except Exception as e:
                    logger.error(f"Error in reducer for {act['type']}: {e}", exc_info=True)
            else:
                logger.warning(f"No reducer found for action type {act['type']}")
            return self.state

        # Build the chain from inside out: last middleware wraps the reducer,
        # second-to-last wraps that, etc. Default args capture loop variables.
        chain = run_reducer
        for mw in reversed(self._middleware):
            def wrap(middleware=mw, next_fn=chain):
                async def step(act, state):
                    return await middleware(act, state, next_fn)
                return step
            chain = wrap()

        return await chain(action, self.state)

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

        # Run through middleware chain (ends at reducer)
        await self._run_middleware_chain(action, reducer)

        # Always notify subscribers regardless of reducer
        logger.debug(f"Notifying subscribers about {action_type}")
        await self._notify_subscribers(action)

        # Handle persistence (if no persistence middleware is installed)
        if self.persistence_enabled and self.persistence_backend:
            self.task_manager.create_task("state_store", self._persist_state())

        return self.state

    async def _notify_subscribers(self, action: Action) -> None:
        """Notify all subscribers about a state change."""
        tasks = []

        logger.debug(f"Notifying {len(self.subscribers)} subscribers about action: {action['type']}")

        for subscriber_id, (callback, action_filter, selector) in list(self.subscribers.items()):
            # Skip if the subscriber has a filter and this action isn't in it
            if action_filter is not None and action["type"] not in action_filter:
                continue

            # Skip if the subscriber has a selector and the selected value hasn't changed
            if selector is not None:
                try:
                    new_value = selector(self.state)
                except Exception:
                    new_value = self._SENTINEL  # On error, always notify
                old_value = self._last_selected.get(subscriber_id, self._SENTINEL)
                if new_value is not self._SENTINEL and old_value is not self._SENTINEL and new_value == old_value:
                    logger.debug(f"Skipping subscriber {subscriber_id}: selector unchanged")
                    continue
                self._last_selected[subscriber_id] = new_value

            # Skip notifying the source to avoid loops ONLY if explicitly configured
            if action.get("source") == subscriber_id and action.get("skip_self_notify", False):
                continue

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

    def subscribe(self, subscriber_id: str, callback: SubscriberFn,
                  action_filter: Optional[set] = None,
                  selector: Optional[SelectorFn] = None) -> None:
        """Register to receive state updates.

        Args:
            subscriber_id: Unique ID for this subscriber.
            callback: Async callable receiving (state, action).
            action_filter: Optional set of action types to listen for.
                           If None, the subscriber receives all actions.
            selector: Optional function that extracts a slice of state.
                      When set, the subscriber is only notified when the
                      selected value changes between dispatches.
        """
        self.subscribers[subscriber_id] = (callback, action_filter, selector)

    def unsubscribe(self, subscriber_id: str) -> None:
        """Stop receiving state updates."""
        if subscriber_id in self.subscribers:
            del self.subscribers[subscriber_id]
        self._last_selected.pop(subscriber_id, None)

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

