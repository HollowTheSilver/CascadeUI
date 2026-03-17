
# // ========================================( Modules )======================================== // #


import copy
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional, List, Set, Tuple, Union

# Import logging at module level
from ..utils.logging import AsyncLogger
from ..utils.tasks import get_task_manager
from ..utils.errors import with_error_boundary, safe_execute

from .types import Action, StateData, ReducerFn, SubscriberFn, MiddlewareFn, SelectorFn, HookFn

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")


# // ========================================( Batch Context )======================================== // #


class BatchContext:
    """Async context manager for atomic multi-dispatch transactions.

    Dispatches within a batch run middleware + reducer immediately (state
    flows through sequentially), but subscriber notification and persistence
    are deferred until the batch exits. On exit, a single notification cycle
    fires with a synthetic BATCH_COMPLETE action.
    """

    def __init__(self, store: "StateStore"):
        self._store = store
        self._actions: List[Action] = []

    async def dispatch(self, action_type: str, payload: Any = None,
                       source_id: Optional[str] = None) -> StateData:
        """Dispatch an action within the batch (no subscriber notification)."""
        action = {
            "type": action_type,
            "payload": payload or {},
            "source": source_id,
            "timestamp": datetime.now().isoformat(),
        }

        logger.debug(f"Batch dispatch: {action_type} from source {source_id}")

        # Record in history
        self._store.history.append(action)
        if len(self._store.history) > self._store.history_limit:
            self._store.history.pop(0)

        # Ensure reducers are loaded
        self._store._load_core_reducers()

        # Run through middleware chain (ends at reducer)
        reducer = self._store.reducers.get(action_type)
        if reducer:
            logger.debug(f"Found reducer for batched action {action_type}")
        await self._store._run_middleware_chain(action, reducer)

        self._actions.append(action)
        return self._store.state

    async def __aenter__(self):
        self._store._batch_depth += 1
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._store._batch_depth -= 1

        if exc_type is not None:
            return False

        if not self._actions:
            return False

        # Fire a single notification with BATCH_COMPLETE
        batch_action = {
            "type": "BATCH_COMPLETE",
            "payload": {"actions": self._actions},
            "source": None,
            "timestamp": datetime.now().isoformat(),
        }

        logger.debug(f"Batch complete: {len(self._actions)} actions")
        await self._store._notify_subscribers(batch_action)

        # Fire hooks for each batched action, then for BATCH_COMPLETE
        for action in self._actions:
            await self._store._fire_hooks(action)
        await self._store._fire_hooks(batch_action)

        # Persist once at the end
        if self._store.persistence_enabled and self._store.persistence_backend:
            self._store.task_manager.create_task("state_store", self._store._persist_state())

        return False


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

        # Event hooks registry: {hook_name: [callbacks]}
        self._hooks: Dict[str, List[HookFn]] = {}

        # Computed values registry: {name: ComputedValue}
        self._computed: Dict[str, Any] = {}

        # Batch depth counter (supports nested/concurrent batches)
        self._batch_depth: int = 0

        # Views that have undo enabled: {view_id: undo_limit}
        # Populated by StatefulView.__init__ when enable_undo = True
        self._undo_enabled_views: Dict[str, int] = {}

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
            reduce_navigation_replace,
            reduce_component_interaction,
            reduce_modal_submitted,
            reduce_persistent_view_registered,
            reduce_persistent_view_unregistered,
            reduce_navigation_push,
            reduce_navigation_pop,
            reduce_scoped_update,
            reduce_undo,
            reduce_redo,
        )

        # Register core reducers
        self._core_reducers = {
            "VIEW_CREATED": reduce_view_created,
            "VIEW_UPDATED": reduce_view_updated,
            "VIEW_DESTROYED": reduce_view_destroyed,
            "SESSION_CREATED": reduce_session_created,
            "SESSION_UPDATED": reduce_session_updated,
            "NAVIGATION_REPLACE": reduce_navigation_replace,
            "COMPONENT_INTERACTION": reduce_component_interaction,
            "MODAL_SUBMITTED": reduce_modal_submitted,
            "PERSISTENT_VIEW_REGISTERED": reduce_persistent_view_registered,
            "PERSISTENT_VIEW_UNREGISTERED": reduce_persistent_view_unregistered,
            "NAVIGATION_PUSH": reduce_navigation_push,
            "NAVIGATION_POP": reduce_navigation_pop,
            "SCOPED_UPDATE": reduce_scoped_update,
            "UNDO": reduce_undo,
            "REDO": reduce_redo,
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

    @property
    def _batching(self) -> bool:
        """Whether any batch context is currently active."""
        return self._batch_depth > 0

    # // ========================================( Batching )======================================== // #

    def batch(self) -> BatchContext:
        """Start an atomic batch of dispatches.

        Usage:
            async with store.batch() as batch:
                await batch.dispatch("ACTION_A", payload1)
                await batch.dispatch("ACTION_B", payload2)
            # Single notification fires here with BATCH_COMPLETE
        """
        return BatchContext(self)

    # // ========================================( Hooks )======================================== // #

    # Mapping from friendly hook names to action types
    _HOOK_ACTION_MAP = {
        "view_created": "VIEW_CREATED",
        "view_updated": "VIEW_UPDATED",
        "view_destroyed": "VIEW_DESTROYED",
        "session_start": "SESSION_CREATED",
        "session_updated": "SESSION_UPDATED",
        "navigation_replace": "NAVIGATION_REPLACE",
        "navigation_push": "NAVIGATION_PUSH",
        "navigation_pop": "NAVIGATION_POP",
        "component_interaction": "COMPONENT_INTERACTION",
        "modal_submitted": "MODAL_SUBMITTED",
        "batch_complete": "BATCH_COMPLETE",
        "scoped_update": "SCOPED_UPDATE",
    }

    def on(self, hook_name: str, callback: HookFn) -> None:
        """Register a hook that fires after reducers and subscribers.

        Hook names map to action types (e.g. "view_created" -> VIEW_CREATED).
        You can also pass the raw action type directly.

        Args:
            hook_name: Friendly name (e.g. "view_created") or action type (e.g. "VIEW_CREATED").
            callback: Async function receiving (action, state) -> None.
        """
        # Resolve friendly name to action type
        action_type = self._HOOK_ACTION_MAP.get(hook_name, hook_name)
        if action_type not in self._hooks:
            self._hooks[action_type] = []
        self._hooks[action_type].append(callback)

    def off(self, hook_name: str, callback: HookFn) -> None:
        """Remove a previously registered hook."""
        action_type = self._HOOK_ACTION_MAP.get(hook_name, hook_name)
        if action_type in self._hooks:
            try:
                self._hooks[action_type].remove(callback)
            except ValueError:
                pass
            if not self._hooks[action_type]:
                del self._hooks[action_type]

    async def _fire_hooks(self, action: Action) -> None:
        """Fire all hooks registered for this action type."""
        hooks = self._hooks.get(action["type"], [])
        for hook in hooks:
            try:
                await hook(action, self.state)
            except Exception as e:
                logger.error(f"Error in hook for {action['type']}: {e}", exc_info=True)

    # // ========================================( Computed State )======================================== // #

    def register_computed(self, name: str, computed_value) -> None:
        """Register a computed value by name."""
        self._computed[name] = computed_value

    @property
    def computed(self) -> "_ComputedAccessor":
        """Access computed values by name: store.computed["total_votes"]."""
        return _ComputedAccessor(self)

    # // ========================================( State Scoping )======================================== // #

    def get_scoped(self, scope: str, **identifiers) -> Dict[str, Any]:
        """Get scoped state for a given scope and identifier.

        Args:
            scope: "user" or "guild"
            **identifiers: user_id=123 or guild_id=456
        """
        scope_key = self._build_scope_key(scope, **identifiers)
        scoped = self.state.get("application", {}).get("_scoped", {})
        return scoped.get(scope_key, {})

    def set_scoped(self, scope: str, data: Dict[str, Any], **identifiers) -> None:
        """Set scoped state directly (prefer dispatch for tracked changes)."""
        scope_key = self._build_scope_key(scope, **identifiers)
        if "application" not in self.state:
            self.state["application"] = {}
        if "_scoped" not in self.state["application"]:
            self.state["application"]["_scoped"] = {}
        self.state["application"]["_scoped"][scope_key] = data

    @staticmethod
    def _build_scope_key(scope: str, **identifiers) -> str:
        """Build a namespaced key like 'user:12345' or 'guild:67890'."""
        if scope == "user":
            uid = identifiers.get("user_id")
            if uid is None:
                raise ValueError("user_id is required for user scope")
            return f"user:{uid}"
        elif scope == "guild":
            gid = identifiers.get("guild_id")
            if gid is None:
                raise ValueError("guild_id is required for guild scope")
            return f"guild:{gid}"
        else:
            raise ValueError(f"Unknown scope: {scope}")

    # // ========================================( Dispatch )======================================== // #

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

        # Fire hooks after subscribers
        await self._fire_hooks(action)

        # Handle persistence (if no persistence middleware is installed)
        if self.persistence_enabled and self.persistence_backend:
            self.task_manager.create_task("state_store", self._persist_state())

        return self.state

    async def _notify_subscribers(self, action: Action) -> None:
        """Notify all subscribers about a state change."""
        tasks = []

        logger.debug(f"Notifying {len(self.subscribers)} subscribers about action: {action['type']}")

        for subscriber_id, (callback, action_filter, selector) in list(self.subscribers.items()):
            # For BATCH_COMPLETE, check subscriber's filter against any batched action
            if action["type"] == "BATCH_COMPLETE":
                if action_filter is not None:
                    batched_types = {a["type"] for a in action["payload"].get("actions", [])}
                    if not (action_filter & batched_types) and "BATCH_COMPLETE" not in action_filter:
                        continue
            else:
                # Normal action: skip if the subscriber has a filter and this action isn't in it
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

        try:
            result = await self.persistence_backend.save_state(self.state)
            if result:
                logger.debug("State persisted successfully")
            else:
                logger.warning("Persistence backend returned failure")
        except Exception as e:
            logger.error(f"Error persisting state: {e}", exc_info=True)

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


# // ========================================( Computed Accessor )======================================== // #


class _ComputedAccessor:
    """Dict-like accessor for computed values on the store."""

    def __init__(self, store: StateStore):
        self._store = store

    def __getitem__(self, name: str):
        if name not in self._store._computed:
            raise KeyError(f"No computed value registered with name '{name}'")
        return self._store._computed[name].get(self._store.state)

    def __contains__(self, name: str) -> bool:
        return name in self._store._computed
