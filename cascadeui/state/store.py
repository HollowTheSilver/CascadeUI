# // ========================================( Modules )======================================== // #


import asyncio
import contextvars
import copy
import logging
import time
from datetime import datetime
from types import MappingProxyType
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set, Tuple, Union

from ..utils.errors import with_error_boundary
from ..utils.tasks import get_task_manager
from .actions import ActionCreators
from .slots import access_slot, read_slot
from .types import Action, HookFn, MiddlewareFn, ReducerFn, SelectorFn, StateData, SubscriberFn

logger = logging.getLogger(__name__)


# Contextvar holding the live edit counter for the current dispatch (or batch).
# Subscriber tasks capture this at ``asyncio.create_task()`` time, so a slow
# subscriber that calls ``refresh()`` after dispatch returns still bumps the
# dispatch's own counter rather than whatever top-of-stack happens to be active.
# Stored as a single-element list ``[int]`` so the reference can be shared and
# mutated by the subscriber task even though the sample dict has already been
# appended to ``_perf_samples``. Finalized to an int in ``_flush_notifications``.
_CURRENT_EDIT_COUNTER: contextvars.ContextVar[Optional[List[int]]] = contextvars.ContextVar(
    "_CURRENT_EDIT_COUNTER", default=None
)


# Contextvar holding the live component interaction for the current dispatch.
# Set by ``StatefulComponent.create_stateful_callback`` around the callback +
# dispatch sequence. Read by ``_StatefulMixin.refresh()`` to piggyback the
# state-driven edit onto the interaction's own ack packet via
# ``interaction.response.edit_message(...)`` instead of a separate channel
# REST call -- saving one round-trip on the acting-view's visual refresh.
# Falls through to the channel endpoint for every condition that disqualifies
# the fast path (non-component interaction, response already acked, message
# mismatch, or any HTTPException other than 429). ``None`` default is always
# safe: dispatches outside a component callback (persistence rehydrate,
# programmatic dispatch from a hook) never see the fast path.
_CURRENT_INTERACTION: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "_CURRENT_INTERACTION", default=None
)


# // ========================================( Batch Context )======================================== // #


class BatchContext:
    """Async context manager for atomic multi-dispatch transactions.

    Any ``store.dispatch()`` call made while a batch is active runs its
    middleware and reducer inline (state flows sequentially) but defers
    subscriber notification, hooks, and persistence until the outermost
    batch exits. At that point a single synthetic ``BATCH_COMPLETE`` action
    fires one notification cycle with the full action list.

    Transitive dispatches collapse into the batch automatically -- helper
    methods like ``_register_state()``, ``update_session()``, and the
    view-level ``dispatch()`` all route through ``store.dispatch()`` and
    are batched without the caller threading a context.
    """

    def __init__(self, store: "StateStore", source_id: Optional[str] = None):
        self._store = store
        self._start_idx = 0
        # Snapshot of ``store.state`` at batch entry -- compared at outermost
        # exit to skip persistence when the batch produced no state change.
        self._snapshot_state: Optional[StateData] = None
        # Propagated into ``BATCH_COMPLETE["source"]`` so _notify_subscribers
        # can award the inline slot to the acting view even in batched regimes
        # (push/pop, _send_pipeline, _cleanup_attached_children). ``None``
        # keeps the pre-source-threading fan-out behavior. Exposed publicly
        # and mutable so callers whose acting view is created mid-batch
        # (e.g. ``_navigate_to``'s new_view) can rebind after construction:
        # ``async with store.batch() as batch: batch.source_id = new_view.id``
        self.source_id = source_id

    async def __aenter__(self):
        # Remember where this batch started so nested batches don't flush
        # the outer batch's queued actions on inner exit.
        self._start_idx = len(self._store._batched_actions)
        self._snapshot_state = self._store.state
        self._store._batch_depth += 1
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._store._batch_depth -= 1

        if exc_type is not None:
            # Drop any actions queued in this batch. Outer batches keep their
            # actions intact because slicing from ``_start_idx`` preserves them.
            del self._store._batched_actions[self._start_idx :]
            return False

        # Nested batches absorb into the outer batch. Only the outermost
        # exit fires BATCH_COMPLETE.
        if self._store._batch_depth > 0:
            return False

        actions = self._store._batched_actions
        self._store._batched_actions = []

        if not actions:
            return False

        # Undo middleware no-ops per-dispatch during batches so only one
        # snapshot captures the pre-batch state instead of N. Delegate
        # the commit-time push to the middleware so _SKIP_ACTIONS and the
        # snapshot shape stay owned in one place.
        undo_mw = self._store._undo_middleware
        if undo_mw is not None:
            undo_mw.finalize_batch(self._snapshot_state, actions)

        batch_action = {
            "type": "BATCH_COMPLETE",
            "payload": {"actions": actions},
            "source": self.source_id,
            "timestamp": datetime.now().isoformat(),
        }

        logger.debug(f"Batch complete: {len(actions)} actions")

        # BATCH_COMPLETE runs its own perf-sampling block because it bypasses
        # ``store.dispatch()``. Per-action samples are suppressed inside the
        # batch (notify_ms would be zero, hooks are amortized), so the batch
        # sample carries the whole fan-out cost under a single row.
        store = self._store
        if store._perf_enabled:
            edit_counter: List[int] = [0]
            store._perf_edit_stack.append(edit_counter)
            token = _CURRENT_EDIT_COUNTER.set(edit_counter)
            try:
                t0 = time.perf_counter()
                await store._notify_subscribers(batch_action)
                t1 = time.perf_counter()
                for action in actions:
                    await store._fire_hooks(action)
                await store._fire_hooks(batch_action)
                t2 = time.perf_counter()
            finally:
                _CURRENT_EDIT_COUNTER.reset(token)
                store._perf_edit_stack.pop()
            store._perf_samples.append(
                {
                    "action": "BATCH_COMPLETE",
                    "reducer_ms": 0.0,
                    "middleware_ms": 0.0,
                    "notify_ms": (t1 - t0) * 1000,
                    "hooks_ms": (t2 - t1) * 1000,
                    "total_ms": (t2 - t0) * 1000,
                    "subscribers": len(store.subscribers),
                    "edits": edit_counter,  # live ref, finalized in _flush_notifications
                    "timestamp": batch_action["timestamp"],
                    "batch_size": len(actions),
                }
            )
        else:
            await store._notify_subscribers(batch_action)
            for action in actions:
                await store._fire_hooks(action)
            await store._fire_hooks(batch_action)

        # Persistence for the batch is driven entirely by
        # PersistenceMiddleware (installed via setup_middleware). The store
        # no longer carries a fallback writer.
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

    @staticmethod
    def _build_initial_state() -> StateData:
        """Return the canonical top-level state shape used by ``__init__``.

        Extracted so every seam that rebuilds state from scratch (devtools
        ``reset``, test fixtures, future snapshot-restore paths) stays
        structurally aligned with ``__init__``. Adding a new top-level key
        means editing one place, not hunting every hardcoded literal.
        """
        return {
            "sessions": {},
            "views": {},
            "components": {},
            "application": {},
        }

    def __init__(self):
        if self._initialized:
            return

        # Core state data. Scoped slices live under application (at
        # state["application"]["scoped"]) so the opt-in persistence seam
        # at _route_application covers them uniformly -- declaring
        # persistent_slots = ("scoped",) on a view persists scoped data
        # through the same mechanism as any other application slot.
        self.state: StateData = self._build_initial_state()

        # Callbacks for state changes: {id: (callback, action_filter, selector)}
        self.subscribers: Dict[
            str, Tuple[SubscriberFn, Optional[Set[str]], Optional[SelectorFn]]
        ] = {}

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
        self._undo_middleware = None

        # Event hooks registry: {hook_name: [callbacks]}
        self._hooks: Dict[str, List[HookFn]] = {}

        # Computed values registry: {name: ComputedValue}
        # Seeded from the module-level @computed registry so decorators that
        # ran at import time survive a store reset (e.g. between tests).
        self._computed: Dict[str, Any] = {}
        from .computed import _COMPUTED_REGISTRY, ComputedValue

        for _name, (_selector, _fn) in _COMPUTED_REGISTRY.items():
            self._computed[_name] = ComputedValue(_name, _selector, _fn)

        # Batch depth counter. ``dispatch()`` checks this: when > 0, the
        # action is queued into ``_batched_actions`` and notification is
        # deferred until the outermost ``BatchContext`` exits. Nested
        # batches absorb into the outer batch.
        self._batch_depth: int = 0
        self._batched_actions: List[Action] = []

        # Views that have undo enabled: {view_id: undo_limit}
        # Populated by StatefulView.__init__ when enable_undo = True
        self._undo_enabled_views: Dict[str, int] = {}

        # Active view instance registry: view_id -> view instance
        self._active_views: Dict[str, Any] = {}

        # Instance index: (view_type, scope_key) -> [view_id, ...] oldest-first
        self._instance_index: Dict[tuple, list] = {}

        # Message deletion cleanup listener
        self._cleanup_listener_installed = False

        # Task management
        self.task_manager = get_task_manager()

        # Per-dispatch profiling (opt-in; disabled by default so there is
        # zero cost on the hot path when no one is looking). When enabled,
        # every ``dispatch()`` records a sample into ``_perf_samples``
        # with timing for the reducer + middleware chain, the subscriber
        # notification fan-out, and total wall time. See ``enable_perf``
        # / ``disable_perf`` / ``clear_perf``.
        import collections as _collections

        self._perf_enabled: bool = False
        self._perf_samples: _collections.deque = _collections.deque(maxlen=100)
        # Parallel deque for view-refresh timings. Populated by
        # ``_StatefulMixin.refresh`` when perf is enabled. Kept separate
        # from ``_perf_samples`` because the record shape is different
        # (per-view Discord edit, not per-dispatch).
        self._refresh_samples: _collections.deque = _collections.deque(maxlen=100)
        # Per-dispatch edit counter. Pushed at the start of a profiled
        # dispatch, incremented by ``refresh()`` when an actual Discord
        # edit fires (not when the render-hash short-circuit skips),
        # popped at the end to record the tally. A list-stack handles
        # nested dispatches (a subscriber's ``on_state_changed``
        # dispatching its own action) without double-counting.
        self._perf_edit_stack: list = []
        # Parallel stack for reducer-only timing. ``run_reducer`` writes
        # the top-of-stack slot when profiling is on; the dispatch site
        # reads it back and subtracts from the chain total to derive
        # ``middleware_ms``. Stack handles nested dispatches the same
        # way ``_perf_edit_stack`` does.
        self._perf_reducer_stack: list = []
        # Per-subscriber timing samples. Populated by ``_safe_notify``
        # when profiling is on. Larger maxlen than ``_perf_samples``
        # because a single dispatch can fan out to many subscribers,
        # and the ring buffer needs to hold a useful window of them.
        # Each sample: {subscriber_id, action, ms, timestamp}.
        self._notify_samples: _collections.deque = _collections.deque(maxlen=500)

        self._initialized = True
        logger.debug("StateStore initialized")

    def enable_perf(self) -> None:
        """Start recording per-dispatch timing samples.

        Samples accumulate in ``_perf_samples`` (capped at 100 most
        recent). Each sample is a dict with keys ``action``, ``total_ms``,
        ``reducer_ms``, ``middleware_ms``, ``notify_ms``, ``hooks_ms``,
        ``subscribers``, ``edits``, ``timestamp``.  Per-subscriber
        callback timings accumulate separately in ``_notify_samples``
        (capped at 500) -- one entry per subscriber per dispatch.
        Overhead while enabled is a handful of ``time.perf_counter()``
        calls per dispatch -- negligible relative to a REST round-trip
        but non-zero, so the default is off.
        """
        self._perf_enabled = True

    def disable_perf(self) -> None:
        """Stop recording perf samples. Existing samples are preserved."""
        self._perf_enabled = False

    def clear_perf(self) -> None:
        """Drop all recorded perf samples."""
        self._perf_samples.clear()
        self._refresh_samples.clear()
        self._perf_edit_stack.clear()
        self._perf_reducer_stack.clear()
        self._notify_samples.clear()

    def _load_core_reducers(self):
        """Load the built-in reducers only when needed."""
        if self._core_reducers:
            return

        # Import here to avoid circular imports
        from .reducers import (
            reduce_component_interaction,
            reduce_inspector_purged_stale,
            reduce_modal_submitted,
            reduce_navigation_pop,
            reduce_navigation_push,
            reduce_navigation_replace,
            reduce_persistent_view_registered,
            reduce_persistent_view_unregistered,
            reduce_redo,
            reduce_scoped_update,
            reduce_session_created,
            reduce_session_updated,
            reduce_undo,
            reduce_view_created,
            reduce_view_destroyed,
            reduce_view_updated,
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
            "INSPECTOR_PURGED_STALE": reduce_inspector_purged_stale,
        }

        # Update combined reducers
        self.reducers = {**self._core_reducers, **self._custom_reducers}
        logger.debug("Core reducers loaded")

    def _register_reducer(self, action_type: str, reducer: ReducerFn) -> None:
        """Register a custom reducer for a specific action type.

        Internal plumbing. The canonical user path is the
        :func:`~cascadeui.utils.decorators.cascade_reducer` decorator.
        """
        if action_type in self._custom_reducers:
            logger.warning(f"Overwriting existing reducer for action type: {action_type}")
        self._custom_reducers[action_type] = reducer
        self.reducers[action_type] = reducer

    def _unregister_reducer(self, action_type: str) -> None:
        """Remove a custom reducer. Internal plumbing."""
        if action_type in self._custom_reducers:
            del self._custom_reducers[action_type]
            # Rebuild combined reducers
            self.reducers = {**self._core_reducers, **self._custom_reducers}

    def _add_middleware(self, middleware: MiddlewareFn) -> None:
        """Add middleware to the dispatch pipeline.

        Internal plumbing. The canonical user path is
        :func:`~cascadeui.setup_middleware`, which handles install +
        async initialize in one step. Direct calls to ``_add_middleware``
        skip the initialize pass; use them only when the middleware has
        no async startup.

        Middleware runs in order between action creation and the reducer.
        Each middleware receives (action, state, next_fn) and must call
        next_fn(action, state) to continue the chain, or return state
        directly to short-circuit.
        """
        self._middleware.append(middleware)
        from .middleware.undo import UndoMiddleware

        if isinstance(middleware, UndoMiddleware):
            self._undo_middleware = middleware

    def _remove_middleware(self, middleware: MiddlewareFn) -> None:
        """Remove a middleware from the pipeline. Internal plumbing."""
        if middleware in self._middleware:
            self._middleware.remove(middleware)
            if middleware is self._undo_middleware:
                self._undo_middleware = None

    def has_middleware(self, middleware_cls: type) -> bool:
        """Return ``True`` if any installed middleware is an instance of ``middleware_cls``.

        Public accessor that replaces ad-hoc reads of the private
        ``_middleware`` list. Used by
        :func:`~cascadeui.setup_middleware` to gate duplicate installs
        and by user code checking middleware presence before conditional
        behavior.

        Subclasses of ``middleware_cls`` are matched as well, since
        ``isinstance`` is used internally.
        """
        return any(isinstance(m, middleware_cls) for m in self._middleware)

    async def _run_middleware_chain(self, action: Action, reducer_fn) -> StateData:
        """Build and execute the middleware chain ending at the reducer."""

        async def run_reducer(act, state):
            # When profiling is on, record the reducer-only wall time into the
            # top of ``_perf_reducer_stack`` so the dispatch site can subtract
            # it from the chain total to derive ``middleware_ms``. The stack
            # slot is pushed by ``dispatch()`` before the chain runs, so the
            # write target always exists when profiling is active.
            perf = self._perf_enabled and self._perf_reducer_stack
            if perf:
                r0 = time.perf_counter()
            if reducer_fn:
                try:
                    new_state = await reducer_fn(act, state)
                    self.state = new_state
                    logger.debug(f"State updated by reducer for {act['type']}")
                except Exception as e:
                    logger.error(f"Error in reducer for {act['type']}: {e}", exc_info=True)
            else:
                # Dispatch-only actions (no reducer) are a normal pattern for cross-view
                # broadcasts. Only warn when nothing subscribes -- that's the real "typo" case.
                action_type = act["type"]
                has_listener = any(
                    flt is None or action_type in flt for _, flt, _ in self.subscribers.values()
                )
                if has_listener:
                    logger.debug(f"No reducer for {action_type} (broadcast-only)")
                else:
                    logger.warning(f"No reducer found for action type {action_type}")
            if perf:
                self._perf_reducer_stack[-1] = (time.perf_counter() - r0) * 1000
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

    def batch(self, source_id: Optional[str] = None) -> BatchContext:
        """Start an atomic batch of dispatches.

        All ``store.dispatch()`` calls made while the batch is active queue
        into the batch, including transitive ones from view helpers like
        ``update_session()``, ``push()``, and ``_register_state()``. One
        ``BATCH_COMPLETE`` notification fires at the outermost exit.

        ``source_id`` identifies the acting view whose refresh should ride
        the interaction's own ack cycle. When supplied, ``BATCH_COMPLETE``
        carries it as ``action["source"]`` and ``_notify_subscribers``
        awaits the matching subscriber inline after the fan-out loop --
        so nav transitions and send-pipeline batches restore the same
        visual coherence single-dispatch paths already have.
        ``source_id=None`` keeps pure fire-and-forget for all subscribers.

        Usage:
            async with store.batch():
                await view.push(OtherView)           # transitively batched
                await view.update_session(x=1)       # transitively batched
                await store.dispatch("MY_ACTION")    # batched

        The returned context also exposes ``batch.dispatch(...)`` as a
        back-compat shim equivalent to ``store.dispatch(...)``.
        """
        return BatchContext(self, source_id=source_id)

    # // ========================================( Hooks )======================================== // #

    # Mapping from friendly hook names to action types.
    _HOOK_ACTION_MAP = {
        "view_created": "VIEW_CREATED",
        "view_updated": "VIEW_UPDATED",
        "view_destroyed": "VIEW_DESTROYED",
        "session_created": "SESSION_CREATED",
        "session_updated": "SESSION_UPDATED",
        "navigation_replace": "NAVIGATION_REPLACE",
        "navigation_push": "NAVIGATION_PUSH",
        "navigation_pop": "NAVIGATION_POP",
        "component_interaction": "COMPONENT_INTERACTION",
        "modal_submitted": "MODAL_SUBMITTED",
        "batch_complete": "BATCH_COMPLETE",
        "scoped_update": "SCOPED_UPDATE",
        "undo": "UNDO",
        "redo": "REDO",
        "persistent_view_registered": "PERSISTENT_VIEW_REGISTERED",
        "persistent_view_unregistered": "PERSISTENT_VIEW_UNREGISTERED",
        "inspector_purged_stale": "INSPECTOR_PURGED_STALE",
        "application_slots_pruned": "APPLICATION_SLOTS_PRUNED",
        "registry_pruned": "REGISTRY_PRUNED",
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

    def _register_computed(self, name: str, computed_value) -> None:
        """Register a computed value by name. Internal plumbing.

        Dual-writes to both this store's local ``_computed`` cache and
        the module-level ``_COMPUTED_REGISTRY`` recipe so imperative and
        decorator paths produce identical end state. Without the
        registry write, imperative registrations would not survive a
        store reset.

        The canonical user path is the
        :func:`~cascadeui.state.computed.computed` decorator.
        """
        from .computed import _COMPUTED_REGISTRY, ComputedValue

        self._computed[name] = computed_value
        if isinstance(computed_value, ComputedValue):
            _COMPUTED_REGISTRY[name] = (
                computed_value._selector,
                computed_value._compute_fn,
            )

    def _unregister_computed(self, name: str) -> None:
        """Unregister a computed value by name. Internal plumbing.

        Dual-clears both the local ``_computed`` cache and the global
        ``_COMPUTED_REGISTRY`` recipe so neither the current store nor
        any future fresh-init store will resurrect the computed. Silent
        no-op when ``name`` is not registered, matching
        ``dict.pop(name, None)`` semantics.
        """
        from .computed import _COMPUTED_REGISTRY

        self._computed.pop(name, None)
        _COMPUTED_REGISTRY.pop(name, None)

    @property
    def computed(self) -> "_ComputedAccessor":
        """Access computed values by name: store.computed["total_votes"]."""
        return _ComputedAccessor(self)

    # // ========================================( State Scoping )======================================== // #

    def get_scoped(self, scope: str, *, slot_name: str = "scoped", **identifiers) -> Dict[str, Any]:
        """Get scoped state for a given scope and identifier.

        Args:
            scope: "user", "guild", "user_guild", or "global".
            slot_name: Named bucket under ``state["application"]``. Defaults
                to the shared ``"scoped"`` bucket so generic callers keep
                working; views with a ``scoped_slot`` class attribute pass
                their own bucket name for subsystem isolation.
            **identifiers: user_id=123 or guild_id=456
        """
        return self.get_scoped_from(self.state, scope, slot_name=slot_name, **identifiers)

    @staticmethod
    def get_scoped_from(
        state: Dict[str, Any],
        scope: str,
        *,
        slot_name: str = "scoped",
        **identifiers,
    ) -> Dict[str, Any]:
        """Read a scoped slice from an explicit state dict.

        Parallel to ``get_scoped`` but takes ``state`` as an argument instead
        of reading ``self.state``. Intended for ``@computed`` selectors
        (which receive ``state`` as their input) and custom reducers (which
        mutate the deep-copied state they were passed).

        Args:
            state: The state dict to read from.
            scope: "user", "guild", "user_guild", or "global".
            slot_name: Named bucket under ``state["application"]``.
            **identifiers: user_id=..., guild_id=..., as appropriate for scope.
        """
        scope_key = StateStore._build_scope_key(scope, **identifiers)
        return read_slot(state, slot_name, scope_key, default={})

    @staticmethod
    def iter_scoped(
        state: Dict[str, Any],
        scope: str,
        *,
        slot_name: str = "scoped",
        **filter_ids,
    ) -> Iterator[Tuple[Dict[str, int], Any]]:
        """Iterate ``(identifiers_dict, value)`` pairs for a scoped slot.

        Yields every entry whose scope key matches ``scope`` and any
        identifiers supplied via ``filter_ids``. Unsupplied identifiers
        act as wildcards -- pass ``guild_id=`` only and the scan
        discovers every ``user_id`` in that guild. Keys that don't parse
        (wrong segment count, non-integer id) are silently skipped.

        Intended for leaderboards, bulk-scan reducers, and maintenance
        helpers that need to walk a scoped bucket without knowing every
        identifier up front. Use ``get_scoped`` / ``get_scoped_from``
        when the identifiers are known.

        Args:
            state: State dict to read (``store.state`` or a reducer snapshot).
            scope: One of ``"user"``, ``"guild"``, ``"user_guild"``, ``"global"``.
            slot_name: Named bucket under ``state["application"]``.
            **filter_ids: Identifiers to filter by (``user_id``, ``guild_id``).

        Yields:
            Pairs of ``(identifiers_dict, value)``. The identifiers dict
            contains every id associated with the scope
            (``{"user_id": int, "guild_id": int}`` for ``user_guild``).
        """
        bucket = read_slot(state, slot_name)
        filter_uid = filter_ids.get("user_id")
        filter_gid = filter_ids.get("guild_id")

        if scope == "user":
            prefix = "user:"
            for key, value in bucket.items():
                if not key.startswith(prefix):
                    continue
                parts = key.split(":")
                if len(parts) != 2:
                    continue
                try:
                    uid = int(parts[1])
                except ValueError:
                    continue
                if filter_uid is not None and uid != filter_uid:
                    continue
                yield ({"user_id": uid}, value)
            return

        if scope == "guild":
            prefix = "guild:"
            for key, value in bucket.items():
                if not key.startswith(prefix):
                    continue
                parts = key.split(":")
                if len(parts) != 2:
                    continue
                try:
                    gid = int(parts[1])
                except ValueError:
                    continue
                if filter_gid is not None and gid != filter_gid:
                    continue
                yield ({"guild_id": gid}, value)
            return

        if scope == "user_guild":
            prefix = "user_guild:"
            for key, value in bucket.items():
                if not key.startswith(prefix):
                    continue
                parts = key.split(":")
                if len(parts) != 3:
                    continue
                try:
                    uid = int(parts[1])
                    gid = int(parts[2])
                except ValueError:
                    continue
                if filter_uid is not None and uid != filter_uid:
                    continue
                if filter_gid is not None and gid != filter_gid:
                    continue
                yield ({"user_id": uid, "guild_id": gid}, value)
            return

        if scope == "global":
            value = bucket.get("global")
            if value is not None:
                yield ({}, value)
            return

        raise ValueError(f"Unknown scope: {scope!r}")

    def set_scoped(
        self,
        scope: str,
        data: Dict[str, Any],
        *,
        slot_name: str = "scoped",
        **identifiers,
    ) -> None:
        """Set scoped state directly (prefer dispatch for tracked changes)."""
        scope_key = self._build_scope_key(scope, **identifiers)
        bucket = access_slot(self.state, slot_name)
        bucket[scope_key] = data

    @staticmethod
    def merge_scoped(
        state: Dict[str, Any],
        scope: str,
        data: Dict[str, Any],
        *,
        slot_name: str = "scoped",
        subkey: Optional[str] = None,
        **identifiers,
    ) -> Dict[str, Any]:
        """Merge a data dict into a scoped bucket and return ``state``.

        Reducer-side writer paired with ``get_scoped_from`` / ``iter_scoped``.
        Decodes the canonical ``{"scope", "identifiers", "data"}`` payload shape
        emitted by ``view.dispatch_scoped_as(...)`` without forcing callers to
        reach ``_build_scope_key``. Falsy ``scope`` or missing-identifier cases
        return ``state`` untouched, matching how the built-in ``SCOPED_UPDATE``
        reducer degrades.

        Args:
            state: State dict to mutate (typically the deep-copied reducer state).
            scope: ``"user"``, ``"guild"``, ``"user_guild"``, or ``"global"``.
            data: Dict merged into the target via ``update()``.
            slot_name: Named bucket under ``state["application"]``.
            subkey: When provided, ``data`` is merged into ``slot[key][subkey]``
                (auto-vivified via ``setdefault``) instead of ``slot[key]`` itself.
                Use when multiple reducers write disjoint sections into one
                scope key (for example ``subkey="settings"`` vs ``subkey="stats"``).
            **identifiers: ``user_id=...``, ``guild_id=...`` for the scope.

        Returns:
            The same ``state`` dict, with the merge applied. Returning state
            from the reducer is idiomatic; mutation happens in place.
        """
        if not scope:
            return state
        try:
            scope_key = StateStore._build_scope_key(scope, **identifiers)
        except ValueError:
            return state
        target = access_slot(state, slot_name, scope_key)
        if subkey is not None:
            target = target.setdefault(subkey, {})
        target.update(data)
        return state

    @staticmethod
    def _build_scope_key(scope: str, **identifiers) -> str:
        """Build a namespaced key for a scoped state slice.

        Key formats:
            user        -> "user:{user_id}"
            guild       -> "guild:{guild_id}"
            user_guild  -> "user_guild:{user_id}:{guild_id}"
            global      -> "global"
        """
        if scope == "user":
            uid = identifiers.get("user_id")
            if uid is None:
                raise ValueError("user_id is required for 'user' scope")
            return f"user:{uid}"
        if scope == "guild":
            gid = identifiers.get("guild_id")
            if gid is None:
                raise ValueError("guild_id is required for 'guild' scope")
            return f"guild:{gid}"
        if scope == "user_guild":
            uid = identifiers.get("user_id")
            gid = identifiers.get("guild_id")
            if uid is None or gid is None:
                raise ValueError("user_id and guild_id are both required for 'user_guild' scope")
            return f"user_guild:{uid}:{gid}"
        if scope == "global":
            return "global"
        raise ValueError(f"Unknown scope: {scope!r}")

    # // ========================================( View Registry )======================================== // #

    def _add_to_instance_index(self, view_id: str, view_type: str, scope_key: str) -> None:
        """Add a view ID to the session index under the given type+scope key."""
        key = (view_type, scope_key)
        self._instance_index.setdefault(key, []).append(view_id)

    def _remove_from_instance_index(self, view_id: str, view_type: str, scope_key: str) -> None:
        """Remove a view ID from the session index for the given type+scope key."""
        key = (view_type, scope_key)
        ids = self._instance_index.get(key, [])
        if view_id in ids:
            ids.remove(view_id)
            if not ids:
                del self._instance_index[key]

    def _register_view(self, view) -> None:
        """Register a live view instance. Internal plumbing. Idempotent.

        Uses ``view._instance_root_class`` (if set) as the type key so that
        navigated sub-views are tracked under the root view's class name.
        Called from the view pipeline, never directly by user code.
        """
        already_registered = view.id in self._active_views
        self._active_views[view.id] = view
        scope_key = self._build_instance_scope_key(view)
        if scope_key is not None and not already_registered:
            view_type = (
                getattr(view, "_instance_root_class", None) or view.__class__._class_session_key()
            )
            self._add_to_instance_index(view.id, view_type, scope_key)

    def _unregister_view(self, view_id: str) -> None:
        """Remove a view from the registry. Internal plumbing. Idempotent.

        Cleans up both the owner's scope key and any participant scope keys.
        Called from view teardown paths, never directly by user code.
        """
        view = self._active_views.pop(view_id, None)
        if view is not None:
            view_type = (
                getattr(view, "_instance_root_class", None) or view.__class__._class_session_key()
            )

            # Remove owner scope key
            scope_key = self._build_instance_scope_key(view)
            if scope_key is not None:
                self._remove_from_instance_index(view_id, view_type, scope_key)

            # Remove participant scope keys (skip if same as owner's key)
            for pid in getattr(view, "_participants", set()):
                p_key = self._build_instance_scope_key(view, user_id=pid)
                if p_key is not None and p_key != scope_key:
                    self._remove_from_instance_index(view_id, view_type, p_key)

    async def _destroy_view(self, view_id: str, *, source_id: Optional[str] = None) -> bool:
        """Atomic view teardown: dispatch ``VIEW_DESTROYED``, then drop the active entry.

        A live view occupies two registries: ``state["views"]`` (the Redux
        source of truth, mutated only through the reducer) and ``_active_views``
        (the sync instance-limit and inspector index). This method tears them
        down in a fixed order. The async ``VIEW_DESTROYED`` dispatch removes the
        ``state["views"]`` entry first; ``_unregister_view`` clears the
        ``_active_views`` entry only once state confirms the view is gone.

        The ordering keeps the two registries consistent under failure. A
        raising middleware, a reducer error the dispatch chain logs and
        absorbs, or a cancellation mid-dispatch all leave both registries
        intact rather than producing the inspector-flagged divergence (a view
        present in ``state["views"]`` but absent from ``_active_views``).
        Over-retention is transient and self-heals on the next teardown or
        restart.

        Idempotent and safe under double-teardown. Returns ``True`` when the
        view was fully removed, ``False`` when the state removal did not land
        and the active-registry entry was retained.
        """
        try:
            await self.dispatch(
                "VIEW_DESTROYED", ActionCreators.view_destroyed(view_id), source_id=source_id
            )
        except Exception:
            # Log and fall through to the post-dispatch state check below: if
            # the reducer ran before the exception (state already clean), the
            # finally clears the active entry and the check returns True; if not,
            # the check returns False and retains both registries.
            logger.exception(
                f"VIEW_DESTROYED dispatch failed for {view_id}; "
                f"checking whether the state entry was removed."
            )
        finally:
            # Clear the active entry only once state confirms the removal. The
            # finally clause also covers a cancellation mid-dispatch: when the
            # reducer already removed the state entry before the await was
            # cancelled, the active entry still gets cleared, so a cancelled
            # teardown cannot leave the view stranded in _active_views.
            if view_id not in self.state.get("views", {}):
                self._unregister_view(view_id)
        if view_id not in self.state.get("views", {}):
            return True
        logger.warning(
            f"VIEW_DESTROYED did not remove {view_id} from state; "
            f"retaining active-registry entry to avoid a ghost."
        )
        return False

    def _get_active_views(self, view_type: str, scope_key: str) -> list:
        """Return active view instances for a type+scope, oldest-first. Internal plumbing."""
        key = (view_type, scope_key)
        ids = self._instance_index.get(key, [])
        return [self._active_views[vid] for vid in ids if vid in self._active_views]

    def get_active_views(self) -> Mapping[str, Any]:
        """Read-only view of the active view registry (view_id -> view instance).

        Callers outside the store (devtools, diagnostics, test harnesses)
        read through this accessor instead of reaching for ``_active_views``
        directly. The returned mapping reflects registrations live but
        rejects mutation -- all bookkeeping goes through ``register_view``
        and ``unregister_view``.
        """
        return MappingProxyType(self._active_views)

    def _register_participant(self, view, user_id: int) -> None:
        """Add a participant's scope key to the session index for a view.

        Skips registration if the participant's scope key is the same as the
        owner's (guild and global scopes don't include user_id, so participant
        keys would be duplicates).
        """
        scope_key = self._build_instance_scope_key(view, user_id=user_id)
        owner_key = self._build_instance_scope_key(view)
        if scope_key is not None and scope_key != owner_key:
            view_type = (
                getattr(view, "_instance_root_class", None) or view.__class__._class_session_key()
            )
            self._add_to_instance_index(view.id, view_type, scope_key)

    def _unregister_participant(self, view, user_id: int) -> None:
        """Remove a participant's scope key from the session index for a view."""
        scope_key = self._build_instance_scope_key(view, user_id=user_id)
        owner_key = self._build_instance_scope_key(view)
        if scope_key is not None and scope_key != owner_key:
            view_type = (
                getattr(view, "_instance_root_class", None) or view.__class__._class_session_key()
            )
            self._remove_from_instance_index(view.id, view_type, scope_key)

    @staticmethod
    def _build_instance_scope_key(view, user_id=None) -> Optional[str]:
        """Build the scope key from a view's instance_scope and identity fields.

        Args:
            view: The view to build the scope key for.
            user_id: Optional override for the view's user_id. Used to build
                scope keys for participants (non-owner users tracked in the
                session index). Only affects "user" and "user_guild" scopes.
        """
        scope = view.instance_scope
        uid = user_id if user_id is not None else view.user_id
        if scope == "user":
            return f"user:{uid}" if uid else None
        elif scope == "guild":
            return f"guild:{view.guild_id}" if view.guild_id else None
        elif scope == "user_guild":
            if uid and view.guild_id:
                return f"user_guild:{uid}:{view.guild_id}"
            return None
        elif scope == "global":
            return "global"
        return None

    # // ========================================( Message Cleanup )======================================== // #

    def _install_message_cleanup(self, bot) -> None:
        """Register gateway listeners that clean up views when their message is deleted.

        Idempotent -- safe to call multiple times. Called automatically from
        ``send()`` (on first successful send) and from
        :meth:`PersistenceMiddleware.initialize` when ``bot=`` is supplied.
        """
        if self._cleanup_listener_installed:
            return
        self._cleanup_listener_installed = True

        store = self

        @bot.listen("on_raw_message_delete")
        async def _cascadeui_message_cleanup(payload):
            for view in list(store._active_views.values()):
                if view._message and view._message.id == payload.message_id:
                    await view.on_message_delete()
                    break

        @bot.listen("on_raw_bulk_message_delete")
        async def _cascadeui_bulk_message_cleanup(payload):
            deleted_ids = set(payload.message_ids)
            for view in list(store._active_views.values()):
                if view._message and view._message.id in deleted_ids:
                    await view.on_message_delete()

        logger.debug("Message deletion cleanup listener installed")

    # // ========================================( Dispatch )======================================== // #

    @with_error_boundary("dispatch")
    async def dispatch(
        self, action_type: str, payload: Any = None, source_id: Optional[str] = None
    ) -> StateData:
        """
        Process an action by updating state and notifying subscribers.

        When called inside ``async with store.batch()``, the reducer runs
        inline but notification, hooks, and persistence defer to the outer
        batch exit. This is what makes ``batch()`` work transitively for
        view-level helpers that route through ``store.dispatch()``.
        """
        # Create the action object
        action = {
            "type": action_type,
            "payload": payload or {},
            "source": source_id,
            "timestamp": datetime.now().isoformat(),
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

        # Batched path: run reducer inline, queue the action, return early.
        # Notification, hooks, and persistence fire once at the outer
        # ``BatchContext`` exit. Individual profiling samples are suppressed
        # because notify_ms would be zero and hooks_ms is amortized across
        # the batch -- per-action timings are misleading in this mode.
        if self._batch_depth > 0:
            await self._run_middleware_chain(action, reducer)
            self._batched_actions.append(action)
            return self.state

        # Capture pre-dispatch state identity so the persistence gate at the
        # bottom can skip the write when the reducer returned ``state`` (no-op
        # payload, missing entity, etc.) or raised an exception that left
        # ``self.state`` unreassigned.
        prev_state = self.state

        # Opt-in profiling. The hot path is a single bool check when
        # disabled; no timestamps, no sample dict, no deque append.
        if self._perf_enabled:
            # Shared mutable counter: refresh() in any subscriber task (which
            # captured the contextvar at task creation time) increments this
            # list in place, and the sample dict stores the same reference so
            # late arrivals are still attributed to the right dispatch.
            edit_counter: List[int] = [0]
            self._perf_edit_stack.append(edit_counter)
            self._perf_reducer_stack.append(0.0)
            token = _CURRENT_EDIT_COUNTER.set(edit_counter)
            try:
                t0 = time.perf_counter()
                await self._run_middleware_chain(action, reducer)
                t1 = time.perf_counter()
                logger.debug(f"Notifying subscribers about {action_type}")
                await self._notify_subscribers(action)
                t2 = time.perf_counter()
                await self._fire_hooks(action)
                t3 = time.perf_counter()
            finally:
                _CURRENT_EDIT_COUNTER.reset(token)
                self._perf_edit_stack.pop()
            reducer_ms = self._perf_reducer_stack.pop()
            chain_ms = (t1 - t0) * 1000
            # Middleware time is everything in the chain that wasn't the
            # reducer itself. Clamp to 0 to guard against clock drift on
            # trivial no-op reducers where the subtraction could go slightly
            # negative.
            middleware_ms = max(0.0, chain_ms - reducer_ms)
            self._perf_samples.append(
                {
                    "action": action_type,
                    "reducer_ms": reducer_ms,
                    "middleware_ms": middleware_ms,
                    "notify_ms": (t2 - t1) * 1000,
                    "hooks_ms": (t3 - t2) * 1000,
                    "total_ms": (t3 - t0) * 1000,
                    "subscribers": len(self.subscribers),
                    # Live reference -- a list that late subscriber refreshes may
                    # still mutate. ``_flush_notifications()`` finalizes this to an
                    # int once in-flight tasks drain.
                    "edits": edit_counter,
                    "timestamp": action["timestamp"],
                }
            )
        else:
            await self._run_middleware_chain(action, reducer)
            logger.debug(f"Notifying subscribers about {action_type}")
            await self._notify_subscribers(action)
            await self._fire_hooks(action)

        # Persistence is driven by PersistenceMiddleware (installed via
        # setup_middleware). The store has no fallback writer.
        return self.state

    async def _notify_subscribers(self, action: Action) -> None:
        """Notify all subscribers about a state change.

        The subscriber matching ``action["source"]`` (the acting view, set by
        ``_StatefulMixin.dispatch`` via ``source_id=self.id``) is awaited
        inline after the fan-out loop, so its ``message.edit()`` lands flush
        with the interaction's own ack cycle. Every other subscriber is
        scheduled as a fire-and-forget task under the ``"state_store_notify"``
        owner, so a slow cross-view subscriber cannot stall the acting
        dispatch. Batched regimes (``BATCH_COMPLETE``
        with ``source=None``) fall through to pure fire-and-forget until
        ``BatchContext`` threads a source id through.
        """
        tasks = []
        acting_id = action.get("source")
        acting_coro = None

        # ``asyncio.create_task`` copies the current context at task-creation
        # time, so cross-view subscriber tasks would otherwise inherit the
        # live ``_CURRENT_INTERACTION`` set by the acting callback. Scope
        # the contextvar to ``None`` while the fan-out loop schedules the
        # background tasks, then restore the token before awaiting the
        # acting coro so ``refresh()`` can still route its edit through
        # the interaction-response fast path. Keeps the fast path naturally
        # scoped to the acting subscriber even if the message-id guard in
        # ``refresh()`` is later relaxed.
        interaction_token = _CURRENT_INTERACTION.set(None)
        try:
            for subscriber_id, (callback, action_filter, selector) in list(
                self.subscribers.items()
            ):
                # For BATCH_COMPLETE, check subscriber's filter against any batched action
                if action["type"] == "BATCH_COMPLETE":
                    if action_filter is not None:
                        batched_types = {a["type"] for a in action["payload"].get("actions", [])}
                        if (
                            not (action_filter & batched_types)
                            and "BATCH_COMPLETE" not in action_filter
                        ):
                            continue
                else:
                    # Normal action: skip if the subscriber has a filter and this action isn't in it.
                    # UNDO/REDO bypass the filter so cross-view subscribers see restored state.
                    if (
                        action_filter is not None
                        and action["type"] not in action_filter
                        and action["type"] not in ("UNDO", "REDO")
                    ):
                        continue

                # Skip if the subscriber has a selector and the selected value hasn't changed
                if selector is not None:
                    try:
                        new_value = selector(self.state)
                    except Exception:
                        new_value = self._SENTINEL  # On error, always notify
                    old_value = self._last_selected.get(subscriber_id, self._SENTINEL)
                    if (
                        new_value is not self._SENTINEL
                        and old_value is not self._SENTINEL
                        and new_value == old_value
                    ):
                        logger.debug(f"Skipping subscriber {subscriber_id}: selector unchanged")
                        continue
                    self._last_selected[subscriber_id] = new_value

                # Skip notifying the source to avoid loops ONLY if explicitly configured
                if action.get("source") == subscriber_id and action.get("skip_self_notify", False):
                    continue

                logger.debug(f"Notifying subscriber {subscriber_id} about action {action['type']}")
                # Bind the state snapshot at scheduling time so subscriber tasks
                # see state-as-of-this-dispatch even if later dispatches have
                # already reassigned ``self.state``. Safe under the shallow-spread
                # reducer pattern because the reducer returns a new top-level dict.
                state_snapshot = self.state

                if subscriber_id == acting_id:
                    # Acting view rides the interaction's own ack cycle. Hold the
                    # coroutine until after the fan-out loop so every other
                    # subscriber is already scheduled before this await yields.
                    acting_coro = self._safe_notify(subscriber_id, callback, action, state_snapshot)
                    continue

                task = self.task_manager.create_task(
                    "state_store_notify",
                    self._safe_notify(subscriber_id, callback, action, state_snapshot),
                )
                tasks.append(task)

            logger.debug(
                f"Notifying {len(tasks)}/{len(self.subscribers)} subscribers about action: {action['type']}"
            )
        finally:
            # Restore the live interaction before awaiting the acting coro
            # so ``refresh()`` in the acting subscriber sees the same value
            # that ``stateful_callback`` set. Cross-view tasks already
            # captured their context with ``None`` above.
            _CURRENT_INTERACTION.reset(interaction_token)

        # Fire-and-forget for cross-view subscribers: they are scheduled and
        # tracked under the "state_store_notify" owner but not awaited here,
        # so a slow peer never stalls the store. Errors surface through
        # ``_safe_notify`` + ``TaskManager._wrap_task`` logging.
        #
        # The acting view (if any) is awaited inline below so its refresh
        # lands flush with the button re-enable -- preserving visual
        # coherence without re-serializing the rest of the fan-out.
        #
        # Tests that assert on cross-view subscriber side effects must call
        # ``await store._flush_notifications()`` to drain the background tasks.
        if acting_coro is not None:
            await acting_coro

    async def _safe_notify(
        self,
        subscriber_id: str,
        callback: SubscriberFn,
        action: Action,
        state: StateData,
    ) -> None:
        """Safely call a subscriber callback with the dispatch-time state
        snapshot. Binding ``state`` at scheduling time keeps the subscriber
        contract intact under fire-and-forget: the handler sees the state
        that this dispatch produced, not whatever later dispatch has since
        reassigned ``self.state``.
        """
        perf = self._perf_enabled
        if perf:
            t0 = time.perf_counter()
        try:
            logger.debug(f"Executing notification callback for subscriber {subscriber_id}")
            await callback(state, action)
        except Exception as e:
            logger.error(f"Error notifying subscriber {subscriber_id}: {e}", exc_info=True)

            # Don't propagate the exception to avoid breaking notification chain
            # but log detailed error information for debugging
            import traceback

            logger.debug(f"Detailed error for {subscriber_id}:\n{traceback.format_exc()}")
        finally:
            # Record per-subscriber wall time even on exception -- a
            # crashing subscriber is still a subscriber whose cost counts
            # against the fan-out. Captured here rather than around the
            # ``callback`` call so the accounting survives the error path.
            if perf:
                self._notify_samples.append(
                    {
                        "subscriber_id": subscriber_id,
                        "action": action["type"],
                        "ms": (time.perf_counter() - t0) * 1000,
                        "timestamp": action["timestamp"],
                    }
                )

    def subscribe(
        self,
        subscriber_id: str,
        callback: SubscriberFn,
        action_filter: Optional[set] = None,
        selector: Optional[SelectorFn] = None,
    ) -> None:
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

    def _unsubscribe(self, subscriber_id: str) -> None:
        """Stop receiving state updates. Internal plumbing.

        Called from view teardown paths. User code that subscribed via
        ``subscribe()`` should retain the ``subscriber_id`` and call
        this from its own cleanup path through the view lifecycle.
        """
        if subscriber_id in self.subscribers:
            del self.subscribers[subscriber_id]
        self._last_selected.pop(subscriber_id, None)

    @property
    def perf_samples(self) -> List[Dict[str, Any]]:
        """Snapshot of dispatch-level perf samples with edit counters normalized.

        Internally, ``sample["edits"]`` is a mutable ``[int]`` list so
        subscriber tasks fired after ``dispatch()`` returns can still
        attribute their edits to the originating dispatch. This property
        returns a snapshot copy where ``edits`` is normalized to ``int``
        at read time, so external readers (devtools, external monitoring,
        custom inspectors) never see the live-reference shape.

        Safe to call at any time. Does not mutate the internal storage.
        If subscriber tasks are still in flight, late edits continue to
        mutate the internal list but will not appear in any snapshot
        already returned by this property. Await ``_flush_notifications()``
        first if exact totals matter.
        """
        snapshot: List[Dict[str, Any]] = []
        for sample in self._perf_samples:
            edits = sample.get("edits")
            if isinstance(edits, list):
                normalized = dict(sample)
                normalized["edits"] = edits[0] if edits else 0
                snapshot.append(normalized)
            else:
                snapshot.append(sample)
        return snapshot

    def _record_edit(self) -> None:
        """Increment the active dispatch's edit counter when profiling is on.

        Internal plumbing. Views call this from ``refresh()`` so perf
        samples attribute the ``message.edit()`` cost to the dispatch
        that triggered the refresh, even when the refresh runs inside a
        subscriber task that outlives ``dispatch()``. The counter is
        task-inherited via ``_CURRENT_EDIT_COUNTER`` (set at dispatch
        time, captured by ``asyncio.create_task``), with a fallback to
        the legacy ``_perf_edit_stack`` frame for sync test paths that
        push manually.

        No-op when profiling is off or when called outside a dispatch.
        """
        counter = _CURRENT_EDIT_COUNTER.get()
        if counter is not None:
            counter[0] += 1
            return
        if self._perf_edit_stack:
            top = self._perf_edit_stack[-1]
            if isinstance(top, list):
                top[0] += 1
            else:
                self._perf_edit_stack[-1] = top + 1

    async def _flush_notifications(self) -> None:
        """Await every in-flight subscriber notification. Internal plumbing.

        Subscriber callbacks scheduled by ``_notify_subscribers`` run
        fire-and-forget so a slow subscriber never stalls dispatch. Tests
        and internal code paths that need a "dispatch returns after all
        subscribers settle" contract call this helper to wait out the
        fan-out.

        After tasks drain, walks ``_perf_samples`` to finalize any live
        ``edits`` counter references into plain ints. This is what makes
        ``sample["edits"] == 1`` work for tests even though the sample
        dict was appended while the counter was still mutable.
        """
        await self.task_manager.wait_tasks("state_store_notify")
        for sample in self._perf_samples:
            edits = sample.get("edits")
            if isinstance(edits, list) and len(edits) == 1:
                sample["edits"] = edits[0]


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
