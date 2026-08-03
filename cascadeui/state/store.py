# // ========================================( Modules )======================================== // #


import asyncio
import contextvars
import copy
import inspect
import logging
import time
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set, Tuple, Union

from ..utils.errors import with_error_boundary
from ..utils.hooks import await_maybe
from ..utils.tasks import get_task_manager
from ._batching import _ACTIVE_BATCHES, current_batch, next_batch_sequence
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


# Contextvar holding the reducer-time slot for the current dispatch, so
# ``run_reducer`` reports into the sample belonging to its own dispatch.
# Sibling of the edit counter above and stored the same way, as a
# single-element list the reducer writes in place. ``None`` means nothing is
# collecting: profiling is off, or the action is batched and accounts for
# itself under the batch's own sample.
_CURRENT_REDUCER_MS: contextvars.ContextVar[Optional[List[float]]] = contextvars.ContextVar(
    "_CURRENT_REDUCER_MS", default=None
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

    Membership follows the TASK, not the store. Two tasks that each open a
    batch collect and flush independently; a batch opened inside another in
    the same task still absorbs into it. A task spawned inside a batch
    inherits the lineage as it stood at spawn, so its dispatches join the
    innermost entry still open and fall through to an immediate notification
    once they have all closed.
    """

    def __init__(self, store: "StateStore", source_id: Optional[str] = None):
        self._store = store
        # This batch's own queued entries as ``(sequence, action)``. Per-batch
        # rather than per-store, so an abort drops only what this batch queued
        # and a concurrent batch's actions are untouchable from here.
        self._entries: List[Tuple[int, Action]] = []
        # Per-action undo records, accumulated by UndoMiddleware while this is
        # the innermost open batch and merged into one diff per view at flush.
        self._undo_records: List[
            Tuple[int, str, Dict[str, Any], Optional[str], Optional[dict], Dict[str, Any]]
        ] = []
        self._token = None
        self._closed = False
        # Propagated into ``BATCH_COMPLETE["source"]`` so _notify_subscribers
        # can award the inline slot to the acting view even in batched regimes
        # (push/pop, _send_pipeline, _cleanup_attached_children). ``None``
        # keeps the pre-source-threading fan-out behavior. Exposed publicly
        # and mutable so callers whose acting view is created mid-batch
        # (e.g. ``_navigate_to``'s new_view) can rebind after construction:
        # ``async with store.batch() as batch: batch.source_id = new_view.id``
        self.source_id = source_id

    @property
    def closed(self) -> bool:
        """Whether this batch has exited and stopped accepting entries."""
        return self._closed

    def add_entry(self, action: Action) -> bool:
        """Queue an action, or report that this batch has already closed.

        A dispatch resolves its batch before running the middleware chain and
        queues afterwards, so a chain that genuinely suspends can outlive the
        batch it started in. Its reducer has committed by then; appending to
        a drained buffer would leave that change unannounced, which is the
        shape this batch redesign exists to remove. Refusing lets the caller
        notify immediately instead.
        """
        if self._closed:
            return False
        self._entries.append((next_batch_sequence(), action))
        return True

    def add_undo_record(
        self,
        source_id: str,
        diff: Dict[str, Any],
        session_id: Optional[str],
        shared: Optional[dict],
        post_application: Dict[str, Any],
    ) -> bool:
        """Record one action's undo diff, or report that this batch closed.

        Stamped on arrival rather than at queue time: undo runs inside the
        middleware chain, before the action itself is queued, so its records
        carry earlier stamps than the actions they describe. Only the order
        among the records matters, and that is reduction order either way.

        ``post_application`` is the state this action left behind, kept so
        the merge can drop slots the batch wrote and then restored. Refusal
        mirrors :meth:`add_entry`: a chain that outlived its batch pushes
        its own snapshot rather than losing it.
        """
        if self._closed:
            return False
        self._undo_records.append(
            (next_batch_sequence(), source_id, diff, session_id, shared, post_application)
        )
        return True

    def _absorb(self, child: "BatchContext") -> None:
        """Adopt a nested batch's entries and undo records on its exit."""
        self._entries.extend(child._entries)
        self._undo_records.extend(child._undo_records)

    async def __aenter__(self):
        # Both shapes are broken, and differently: re-entering while open
        # would leave a stale duplicate in the lineage, and re-entering after
        # the close would collect onto a buffer that has already flushed,
        # where ``current_batch`` skips it and the dispatches escape.
        if self._token is not None or self._closed:
            raise RuntimeError(
                "A BatchContext cannot be re-entered. Call store.batch() again "
                "for a second batch."
            )
        self._token = _ACTIVE_BATCHES.set(_ACTIVE_BATCHES.get() + (self,))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Close and leave the lineage BEFORE anything awaits below. The flush
        # schedules background subscriber tasks, and ``create_task`` copies the
        # context: a task spawned while this batch was still listed would
        # inherit it and queue onto a buffer that is already being drained.
        self._closed = True
        if self._token is not None:
            try:
                _ACTIVE_BATCHES.reset(self._token)
            except ValueError:
                # Entered and exited under different contexts -- an async
                # generator holding a batch across a yield and finalized from
                # another task does this. The tuple is per-context, so the
                # entry goes away with the context that created it.
                pass
            self._token = None

        # An abort takes the same path as a clean exit, deliberately. An entry
        # is queued only after its reducer has committed, so the queue is not a
        # speculative sequence to discard -- it is exactly what already
        # happened. Dropping it left state changed with no subscriber told and
        # no undo entry to revert it, which is a silent desync rather than a
        # rollback. The exception still propagates; what changes is that the
        # committed prefix is announced and undoable.
        #
        # Nested batches absorb into the nearest ancestor still open in this
        # task. Reading it after the reset above means this batch is already
        # out of the lineage, so the scan cannot return self.
        parent = current_batch()
        if parent is not None:
            parent._absorb(self)
            return False

        entries = sorted(self._entries, key=lambda entry: entry[0])
        self._entries = []
        actions = [action for _, action in entries]

        # Undo captures per action during a batch and pushes nothing until
        # here, so one entry lands on each participating view's stack instead
        # of N. Delegate the merge to the middleware so _SKIP_ACTIONS and the
        # snapshot shape stay owned in one place.
        #
        # Runs BEFORE the empty-batch return: a dispatch whose chain suspends
        # past its own batch has its action refused by ``add_entry`` and
        # notified immediately, while ``UndoMiddleware`` re-resolves the
        # lineage afterwards and lands its record on a still-open ancestor.
        # That ancestor can hold records without holding a single action of
        # its own, and returning first dropped the snapshot for a state change
        # that had already committed.
        undo_mw = self._store._undo_middleware
        if undo_mw is not None and self._undo_records:
            undo_mw.finalize_batch(sorted(self._undo_records, key=lambda rec: rec[0]))
        self._undo_records = []

        if not actions:
            return False

        batch_action = {
            "type": "BATCH_COMPLETE",
            "payload": {"actions": actions},
            "source": self.source_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
                # Removed by identity, not popped: two batches flushing
                # concurrently interleave their awaits, and a positional pop
                # would take the other one's frame.
                try:
                    store._perf_edit_stack.remove(edit_counter)
                except ValueError:
                    pass
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
        # Subscribers whose selector has already been reported as raising.
        # A broken selector raises on every dispatch, and the failure is a
        # property of the selector rather than of any one action, so one
        # line says everything a flood would.
        self._selector_failed: Set[str] = set()
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

        # Batch membership is task-scoped and lives in ``_batching.py``, not
        # on the store: two tasks batching at once must not share one depth
        # counter and one buffer, which read as a single nested batch and let
        # whichever exited last flush both.

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
            # slot this dispatch bound, so the dispatch site can subtract it
            # from the chain total to derive ``middleware_ms``. Reached through
            # a contextvar rather than a shared stack: a batched action binds
            # no slot, and writing to the top of a shared one overwrote the
            # timing of whichever unbatched dispatch was running concurrently.
            reducer_slot = _CURRENT_REDUCER_MS.get()
            perf = self._perf_enabled and reducer_slot is not None
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
                reducer_slot[0] = (time.perf_counter() - r0) * 1000
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

    # // ========================================( Batching )======================================== // #

    def batch(self, source_id: Optional[str] = None) -> BatchContext:
        """Start an atomic batch of dispatches.

        All ``store.dispatch()`` calls made while the batch is active queue
        into the batch, including transitive ones from view helpers like
        ``update_session()``, ``push()``, and ``_register_state()``. One
        ``BATCH_COMPLETE`` notification fires at the outermost exit.

        Membership is per task. A batch opened inside another in the same
        task absorbs into it, while two tasks batching at the same time
        collect and flush separately, so concurrent work (restoring many
        persistent panels, say) does not fold into one notification under
        a single ``source_id``.

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
                await await_maybe(hook(action, self.state))
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
        uid = identifiers.get("user_id")
        gid = identifiers.get("guild_id")
        key = StateStore.scope_key(scope, user_id=uid, guild_id=gid)
        if key is not None:
            return key
        if scope == "user":
            raise ValueError("user_id is required for 'user' scope")
        if scope == "guild":
            raise ValueError("guild_id is required for 'guild' scope")
        if scope == "user_guild":
            raise ValueError("user_id and guild_id are both required for 'user_guild' scope")
        raise ValueError(f"Unknown scope: {scope!r}")

    @staticmethod
    def scope_key(scope: str, *, user_id=None, guild_id=None) -> Optional[str]:
        """Build a scope key, or ``None`` when the scope's ids are missing.

        The single writer of the scope-key format. Every other site that
        needs one (the strict :meth:`_build_scope_key`, the instance
        index, the sync availability pre-check) routes through here, so
        the format is defined once and a change cannot desync one caller
        from the rest.

        Missing means ``None``, not falsy. ``0`` is a value a caller can
        legitimately hold (a sentinel account, a DM standing in for a
        guild), and treating it as absent would drop the write rather than
        reject it. Callers that want falsy ids treated as absent normalize
        with ``or None`` before calling.

        Key formats:
            user        -> "user:{user_id}"
            guild       -> "guild:{guild_id}"
            user_guild  -> "user_guild:{user_id}:{guild_id}"
            global      -> "global"
        """
        if scope == "user":
            return None if user_id is None else f"user:{user_id}"
        if scope == "guild":
            return None if guild_id is None else f"guild:{guild_id}"
        if scope == "user_guild":
            if user_id is None or guild_id is None:
                return None
            return f"user_guild:{user_id}:{guild_id}"
        if scope == "global":
            return "global"
        return None

    # // ========================================( View Registry )======================================== // #

    def _add_to_instance_index(self, view_id: str, view_type: str, scope_key: str) -> None:
        """Add a view ID to the instance index under the given type+scope key."""
        key = (view_type, scope_key)
        self._instance_index.setdefault(key, []).append(view_id)

    def _remove_from_instance_index(self, view_id: str, view_type: str, scope_key: str) -> None:
        """Remove a view ID from the instance index for the given type+scope key."""
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
        rejects mutation: all bookkeeping goes through the store's own
        ``_register_view`` / ``_destroy_view`` seams.
        """
        return MappingProxyType(self._active_views)

    def _register_participant(self, view, user_id: int) -> None:
        """Add a participant's scope key to the instance index for a view.

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
        """Remove a participant's scope key from the instance index for a view."""
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
                instance index). Only affects "user" and "user_guild" scopes.
        """
        # The instance index treats a falsy id as no id: an unindexed view
        # is simply exempt from the limit, where a "user:0" bucket would
        # silently pool every such view together. The scoped-state writer
        # keeps the stricter is-None rule, since dropping a write is worse
        # than skipping an index entry.
        uid = user_id if user_id is not None else view.user_id
        return StateStore.scope_key(
            view.instance_scope,
            user_id=uid or None,
            guild_id=view.guild_id or None,
        )

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
                    await await_maybe(view.on_message_delete())
                    break

        @bot.listen("on_raw_bulk_message_delete")
        async def _cascadeui_bulk_message_cleanup(payload):
            deleted_ids = set(payload.message_ids)
            for view in list(store._active_views.values()):
                if view._message and view._message.id in deleted_ids:
                    await await_maybe(view.on_message_delete())

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
        # The Redux idiom most callers arrive with is ``dispatch(action)``,
        # and reducers here receive exactly that dict, so handing one to this
        # is the natural mistake. It reached the reducer lookup below and
        # failed on "cannot use 'dict' as a dict key", which names neither
        # the parameter nor the shape. A non-string type is quieter still:
        # nothing raises, and an action nothing can route sits in state and
        # history under a type no reducer or subscriber will ever match.
        if not isinstance(action_type, str):
            got = type(action_type).__name__
            hint = (
                "\n  Fix: pass the type and payload separately, e.g. "
                'dispatch(action["type"], action["payload"]).'
                if isinstance(action_type, dict)
                else "\n  Fix: pass the action's type as a non-empty string."
            )
            raise TypeError(
                f"dispatch() expects an action type string, got {got}: {action_type!r}{hint}"
            )
        # A string carrying no name is the right type with a wrong value,
        # which is where the two stdlib exceptions divide.
        if not action_type:
            raise ValueError(
                "dispatch() received an empty action type. Nothing routes to the "
                "empty string, so the action would sit in state and history under "
                "a type no reducer or subscriber can match."
                "\n  Fix: pass the action's type, e.g. dispatch('SCORE_CHANGED', ...)."
            )

        # Create the action object
        action = {
            "type": action_type,
            "payload": payload or {},
            "source": source_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
        batch = current_batch()
        chain_ran = False
        if batch is not None:
            await self._run_middleware_chain(action, reducer)
            if batch.add_entry(action):
                return self.state
            # The batch closed while this dispatch's chain was suspended, so
            # there is no longer a flush that will announce this action. The
            # reducer has already committed, so the immediate path below is
            # what keeps the change from going unannounced.
            chain_ran = True

        # Opt-in profiling. The hot path is a single bool check when
        # disabled; no timestamps, no sample dict, no deque append.
        if self._perf_enabled:
            # Shared mutable counter: refresh() in any subscriber task (which
            # captured the contextvar at task creation time) increments this
            # list in place, and the sample dict stores the same reference so
            # late arrivals are still attributed to the right dispatch.
            edit_counter: List[int] = [0]
            reducer_slot: List[float] = [0.0]
            self._perf_edit_stack.append(edit_counter)
            token = _CURRENT_EDIT_COUNTER.set(edit_counter)
            reducer_token = _CURRENT_REDUCER_MS.set(reducer_slot)
            try:
                t0 = time.perf_counter()
                if not chain_ran:
                    await self._run_middleware_chain(action, reducer)
                t1 = time.perf_counter()
                logger.debug(f"Notifying subscribers about {action_type}")
                await self._notify_subscribers(action)
                t2 = time.perf_counter()
                await self._fire_hooks(action)
                t3 = time.perf_counter()
            finally:
                _CURRENT_EDIT_COUNTER.reset(token)
                _CURRENT_REDUCER_MS.reset(reducer_token)
                # By identity: a batch flushing concurrently interleaves its
                # awaits with this one, and a positional pop takes its frame.
                try:
                    self._perf_edit_stack.remove(edit_counter)
                except ValueError:
                    pass
            reducer_ms = reducer_slot[0]
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
            if not chain_ran:
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
        # Built once rather than per subscriber: the set is the same for every
        # one of them, and rebuilding it inside the loop made a batch commit
        # cost O(subscribers x actions) for a value that never varies.
        batched_types = (
            {a["type"] for a in action["payload"].get("actions", [])}
            if action["type"] == "BATCH_COMPLETE"
            else frozenset()
        )
        try:
            for subscriber_id, (callback, action_filter, selector) in list(
                self.subscribers.items()
            ):
                # For BATCH_COMPLETE, check subscriber's filter against any batched action
                if action["type"] == "BATCH_COMPLETE":
                    if action_filter is not None:
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
                    except Exception as e:
                        # Degrade to notify-always, which is the safe answer
                        # for "cannot tell whether this changed". Report it
                        # once: silence here left a permanently broken
                        # selector looking exactly like a subscriber that
                        # legitimately wants every action.
                        if subscriber_id not in self._selector_failed:
                            self._selector_failed.add(subscriber_id)
                            logger.warning(
                                f"Selector for subscriber {subscriber_id} raised "
                                f"{type(e).__name__}: {e}. Notifying on every action "
                                f"until it stops raising."
                            )
                        new_value = self._SENTINEL
                    old_value = self._last_selected.get(subscriber_id, self._SENTINEL)
                    # Identity first: a selector returning the same object is the
                    # common case for a bare whole-bucket read, and dict/list
                    # equality has no identity shortcut -- it walks every entry
                    # to prove what `is` already answered. Views are immune
                    # either way (``_build_selector`` returns a tuple, and tuple
                    # comparison does shortcut identical elements), so this pays
                    # for direct ``store.subscribe`` callers with fat selectors.
                    # The one behavioral edge is a selector returning NaN, which
                    # flips from notify-always to notify-never; pathological.
                    if (
                        new_value is not self._SENTINEL
                        and old_value is not self._SENTINEL
                        and (new_value is old_value or new_value == old_value)
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
        # ``_safe_notify`` + ``TaskManager._on_task_done`` logging.
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
            await await_maybe(callback(state, action))
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
            callback: Callable receiving (state, action). Sync or async.
            action_filter: Optional set of action types to listen for.
                           If None, the subscriber receives all actions.
            selector: Optional synchronous function that extracts a slice
                      of state. When set, the subscriber is only notified
                      when the selected value changes between dispatches.
        """
        if selector is not None:
            if not callable(selector):
                raise TypeError(
                    f"subscribe({subscriber_id!r}) selector= must be a callable taking "
                    f"the state and returning the slice to watch; got "
                    f"{type(selector).__name__}: {selector!r}"
                )
            # The comparison that decides whether to notify runs inline in
            # dispatch and cannot await. An async selector returns a fresh
            # coroutine every time, which never equals the last one, so the
            # subscriber is notified on every action, the exact opposite
            # of what passing a selector asks for, and each unawaited
            # coroutine warns from the user's console.
            if inspect.iscoroutinefunction(selector) or inspect.iscoroutinefunction(
                getattr(selector, "__call__", None)
            ):
                raise TypeError(
                    f"subscribe({subscriber_id!r}) selector= must be synchronous; the "
                    f"change check runs inline in dispatch and cannot await. Read the "
                    f"slice from the state argument, and do async work in the callback."
                )
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
        self._selector_failed.discard(subscriber_id)

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
        the ``_perf_edit_stack`` frame the store itself pushes for edits
        that land outside a task-inherited context.

        No-op when profiling is off or when called outside a dispatch.
        """
        counter = _CURRENT_EDIT_COUNTER.get()
        if counter is not None:
            counter[0] += 1
            return
        # Last resort only: the contextvar above is the attributed path. Under
        # concurrent batches the top of this stack belongs to whichever frame
        # pushed last, so an edit reaching here is credited approximately.
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
