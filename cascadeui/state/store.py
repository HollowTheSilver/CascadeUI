# // ========================================( Modules )======================================== // #


import asyncio
import contextlib
import contextvars
import copy
import logging
import time
import weakref
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Set, Tuple

from ..utils.errors import with_error_boundary
from ..utils.hooks import await_maybe
from ..utils.tasks import _bounded_wait, get_task_manager
from ._batching import _ACTIVE_BATCHES, current_batch, next_commit_sequence
from .actions import ActionCreators
from .slots import access_slot, read_slot
from .types import Action, HookFn, MiddlewareFn, ReducerFn, SelectorFn, StateData, SubscriberFn

logger = logging.getLogger(__name__)

# A wait for the state turn this long is logged, naming what holds it.
_STATE_TURN_WAIT_WARN_SECONDS = 30.0

# How long an undo or redo waits for another task's batch holding steps for the view.
_UNDO_BATCH_WAIT_SECONDS = 30.0


# The edit counter of the dispatch (or batch) being profiled. A subscriber task
# copies it at creation, so a refresh() it makes after the dispatch returns still
# counts there. A one-element list shared by reference with the recorded sample;
# ``_flush_notifications`` turns it into an int.
_CURRENT_EDIT_COUNTER: contextvars.ContextVar[Optional[List[int]]] = contextvars.ContextVar(
    "_CURRENT_EDIT_COUNTER", default=None
)


# The interaction being handled, bound around a click's or a modal submission's
# callback and its dispatch. ``refresh()`` reads it to answer that interaction with
# its edit in one request (``interaction.response.edit_message``). ``None``
# outside a callback.
_CURRENT_INTERACTION: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "_CURRENT_INTERACTION", default=None
)


# The reducer-time slot of the dispatch being profiled, a one-element list like
# the edit counter. ``None`` while profiling is off. A batched action times on a
# slot of its own, read only if the batch refuses it.
_CURRENT_REDUCER_MS: contextvars.ContextVar[Optional[List[float]]] = contextvars.ContextVar(
    "_CURRENT_REDUCER_MS", default=None
)

# Called at the commit of the dispatch whose chain is running, before anything
# else can run, with the store's state just before the commit and the state the
# reducer committed. A middleware that records what its action changed
# registers one, since another dispatch can commit while the middlewares around
# it await (see UndoMiddleware).
_ON_COMMIT: contextvars.ContextVar[Optional[List[Callable[[StateData, StateData], None]]]] = (
    contextvars.ContextVar("_ON_COMMIT", default=None)
)

# The view that dispatched the action whose chain is running, looked up when the
# chain began: a middleware that awaits can hold the action until a push has
# torn that view down and unregistered it (see UndoMiddleware).
_SOURCE_VIEW: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "_SOURCE_VIEW", default=None
)

# The store and state turn of the reducer running in this context. A task the
# reducer starts copies it, which is how a dispatch from that task is known.
_IN_REDUCER: contextvars.ContextVar[Optional[Tuple[Any, object]]] = contextvars.ContextVar(
    "_IN_REDUCER", default=None
)


class _ChainRun:
    """What one pass through a dispatch's middleware chain did."""

    __slots__ = ("reduced", "stamp", "error")

    def __init__(self) -> None:
        self.reduced = False
        self.stamp: Optional[int] = None
        self.error: Optional[Exception] = None


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
        # rather than per-store, so a concurrent batch's actions are
        # untouchable from here.
        self._entries: List[Tuple[int, Action]] = []
        # Per-action undo records, accumulated by UndoMiddleware while this is
        # the innermost open batch and merged into one diff per view at flush.
        self._undo_records: List[
            Tuple[int, str, Dict[str, Any], Optional[str], Optional[dict], Dict[str, Any], Any]
        ] = []
        self._token = None
        self._closed = False
        # Set once this batch no longer holds undo steps (handed to its parent
        # or written at its exit), for an undo waiting on it.
        self._settled = asyncio.Event()
        # Set once an undo has waited the whole bound for this batch, so no
        # later wait on the same undo, or another, starts the bound again.
        self._undo_wait_expired = False
        # The records taken at the exit, until their step is placed: another
        # batch placing its own, or an undo waiting, still has to see them.
        self._placing: List = []
        # Records of batches that placed their steps while this one was open,
        # for its own placing: a batch whose step pruned to nothing leaves no
        # entry on the stack to read them from.
        self._handed: List[list] = []
        # Becomes ``BATCH_COMPLETE["source"]``, so the acting view keeps its
        # inline notification; ``None`` notifies every subscriber in the
        # background. Mutable for an acting view created mid-batch:
        # ``async with store.batch() as batch: batch.source_id = new_view.id``
        self.source_id = source_id

    @property
    def closed(self) -> bool:
        """Whether this batch has exited and stopped accepting entries."""
        return self._closed

    def add_entry(self, action: Action, stamp: Optional[int] = None) -> bool:
        """Queue an action, or report that this batch has already closed.

        A dispatch resolves its batch before running the middleware chain and
        queues afterwards, so a chain that genuinely suspends can outlive the
        batch it started in. Its reducer has committed by then; appending to
        a drained buffer would leave that change unannounced, which is the
        shape this batch redesign exists to remove. Refusing lets the caller
        notify immediately instead.

        ``stamp`` is the sequence taken when the action's reducer committed,
        so actions are announced in the order the store applied them.
        """
        if self._closed:
            return False
        self._entries.append((next_commit_sequence() if stamp is None else stamp, action))
        return True

    def add_undo_record(
        self,
        source_id: str,
        diff: Dict[str, Any],
        session_id: Optional[str],
        shared: Optional[dict],
        post_application: Dict[str, Any],
        view: Any = None,
    ) -> None:
        """Record one action's undo diff.

        Called as the action's reducer commits, so the stamps follow the order
        the store committed. That is earlier than the action itself is
        queued, which is harmless: only the order among the records matters.
        The caller reads this batch from ``current_batch()`` and records with
        nothing awaited in between, so the batch is always still open here.

        ``post_application`` is the state this action left behind, kept so
        the merge can drop slots the batch wrote and then restored. ``view``
        is the source view, kept so the entry can follow its panel when the
        batch pushes or pops away from it.
        """
        self._undo_records.append(
            (next_commit_sequence(), source_id, diff, session_id, shared, post_application, view)
        )
        self._store._note_undo_batch(self)

    def _absorb(self, child: "BatchContext") -> None:
        """Adopt a nested batch's entries and undo records on its exit."""
        self._entries.extend(child._entries)
        self._undo_records.extend(child._undo_records)
        if child._undo_records:
            self._store._note_undo_batch(self)

    def _holds_steps_for(self, view_id: str) -> bool:
        """Whether a step this batch adds at its exit goes on ``view_id``'s history."""
        return self._store._records_step_on((*self._undo_records, *self._placing), view_id)

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

        # An abort takes the clean exit's path: an entry is queued only after its
        # reducer committed, and dropping it would leave state changed with no
        # subscriber told. A nested batch joins the nearest ancestor still open;
        # this one left the lineage above, so the scan cannot return it.
        parent = current_batch()
        if parent is not None:
            parent._absorb(self)
            self._store._forget_undo_batch(self)
            return False

        entries = sorted(self._entries, key=lambda entry: entry[0])
        self._entries = []
        actions = [action for _, action in entries]
        batch_action = None
        if actions:
            batch_action = {
                "type": "BATCH_COMPLETE",
                "payload": {"actions": actions},
                "source": self.source_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # One undo entry per participating view, merged by the middleware. Runs
        # before the empty-batch return: a dispatch whose chain outlived its own
        # batch can leave its undo record on an ancestor holding no action.
        undo_mw = self._store._undo_middleware
        records = sorted(self._undo_records, key=lambda rec: rec[0])
        self._undo_records = []
        self._placing = records
        try:
            if undo_mw is not None and records:
                try:
                    # Waits for a reducer running in another task, so the views
                    # the batch notifies render with the entry in place.
                    await self._store._write_in_turn(
                        lambda: undo_mw.finalize_batch(records), "a batch's undo entry"
                    )
                except asyncio.CancelledError:
                    # The entry lands at that reducer's commit; the actions have
                    # committed, so subscribers are still told.
                    if batch_action is not None:
                        self._store.task_manager.create_task(
                            "state_store_notify", self._store._notify_subscribers(batch_action)
                        )
                    raise
        finally:
            # A cut-off write is queued ahead of the next reducer, so an undo
            # waiting on this batch still finds the entry in place.
            self._placing = []
            self._handed = []
            self._store._forget_undo_batch(self)

        if batch_action is None:
            return False

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
                await store._fire_hooks(*actions, batch_action)
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
            await store._announce(batch_action, *actions, batch_action)

        # Persistence for the batch is driven entirely by
        # PersistenceMiddleware (installed via setup_middleware). The store
        # no longer carries a fallback writer.
        return False


def _in_channel(message: Any, channel_id: int) -> bool:
    """Whether ``message`` sits in the channel ``channel_id``, or in a thread under it.

    Deleting a channel deletes its threads, and a thread message's channel
    carries the parent as ``parent_id``. A text channel's category is
    ``category_id`` instead, so a deleted category matches nothing, as the
    channels inside it survive.
    """
    channel = getattr(message, "channel", None)
    return (
        getattr(channel, "id", None) == channel_id
        or getattr(channel, "parent_id", None) == channel_id
    )


# // ========================================( Class )======================================== // #


class _Registration:
    """One ``on()`` call's hook.

    Two registrations of one callback stay distinct, and firing compares
    registrations by identity, never by calling a callback's ``__eq__``.
    """

    __slots__ = ("callback",)

    def __init__(self, callback: HookFn) -> None:
        self.callback = callback


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
            "modals": {},
            "application": {},
            # Mirrors every stored registry row, so a rebuild that dropped it
            # would leave rows on disk that nothing in this process knows about.
            "persistent_views": {},
        }

    def __init__(self):
        if self._initialized:
            return

        # Scoped slices live under ``state["application"]["scoped"]``, so
        # ``persistent_slots = ("scoped",)`` persists them like any other slot.
        self.state: StateData = self._build_initial_state()

        # Callbacks for state changes: {id: (callback, action_filter, selector)}
        self.subscribers: Dict[
            str, Tuple[SubscriberFn, Optional[Set[str]], Optional[SelectorFn]]
        ] = {}
        # Ids subscribed by views. A view's subscription is what marks it live,
        # so the public subscribe() and unsubscribe() refuse these ids.
        self._view_subscriptions: Set[str] = set()

        # Memoized selector results for change detection
        self._last_selected: Dict[str, Any] = {}
        # Subscribers whose selector has already been reported as raising.
        # A broken selector raises on every dispatch, and the failure is a
        # property of the selector rather than of any one action, so one
        # line says everything a flood would.
        self._selector_failed: Set[str] = set()
        # The same, for a selected value that cannot be compared.
        self._selector_uncomparable: Set[str] = set()
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
        self._hooks: Dict[str, List[_Registration]] = {}

        # Computed values registry: {name: ComputedValue}
        # Seeded from the module-level @computed registry so decorators that
        # ran at import time survive a store reset (e.g. between tests).
        self._computed: Dict[str, Any] = {}
        from .computed import _COMPUTED_REGISTRY, ComputedValue

        for _name, (_selector, _fn) in _COMPUTED_REGISTRY.items():
            self._computed[_name] = ComputedValue(_name, _selector, _fn)

        # Batch membership lives in ``_batching.py``, per task: two tasks
        # batching at once must not share one buffer.

        # Views that have undo enabled: {view_id: undo_limit}
        # Populated by StatefulView.__init__ when enable_undo = True
        self._undo_enabled_views: Dict[str, int] = {}

        # Active view instance registry: view_id -> view instance
        self._active_views: Dict[str, Any] = {}

        # Instance index: (view_type, scope_key) -> [view_id, ...] oldest-first
        self._instance_index: Dict[tuple, list] = {}
        # view id -> the (view_type, scope_key) keys it is filed under, so it
        # leaves the ones it was filed under even after its scope changes.
        self._instance_keys: Dict[str, set] = {}

        # Which registered views hold a persistence key, and which carry a
        # registration's message id, oldest-first. The second is what a view
        # pushed from a panel holds instead of the key.
        self._views_by_key: Dict[str, list] = {}
        self._views_by_message: Dict[str, list] = {}

        # The bots the gateway listeners are installed on; a restart that
        # builds a new bot object adds one (see _install_message_cleanup).
        self._cleanup_listener_bots: "weakref.WeakSet" = weakref.WeakSet()
        # The discord.py view stores that answer a click from an earlier
        # render (see _answer_stale_clicks); a bot's clear() replaces its store.
        self._stale_click_stores: "weakref.WeakSet" = weakref.WeakSet()
        # Views a bot's close released that code may still hold, for linking
        # to the panel restored in their place (see _release_views_of).
        self._released_refs: "weakref.WeakSet" = weakref.WeakSet()
        # Ids of every view a bot's close released (see _drop_released).
        self._released_ids: set = set()
        # Set when a release left persistent panels for a restart to restore.
        self._restore_owed = False
        # The state turn (see _state_turn): its lock, made for the running
        # loop, and the task holding it with what it is doing.
        self._state_lock: Optional[Tuple[asyncio.AbstractEventLoop, asyncio.Lock, List[int]]] = None
        self._state_holder: Optional["asyncio.Task"] = None
        self._state_holder_label: Optional[str] = None
        self._state_holder_action: Optional[str] = None
        # Identifies one turn, so a task a reducer started is known only
        # while that reducer runs.
        self._state_token: Optional[object] = None
        # Writes outside a reducer held until the holder gives the turn back
        # (see _write_when_free).
        self._state_pending: List[Callable[[], None]] = []
        # Open batches holding undo steps; an undo of a view whose history one
        # of them adds to waits for it (see _wait_for_undo_batches).
        self._undo_batches: List["BatchContext"] = []

        # Task management
        self.task_manager = get_task_manager()

        # Per-dispatch profiling, off by default (see ``enable_perf``).
        import collections as _collections

        self._perf_enabled: bool = False
        self._perf_samples: _collections.deque = _collections.deque(maxlen=100)
        # Parallel deque for view-refresh timings. Populated by
        # ``_StatefulMixin.refresh`` when perf is enabled. Kept separate
        # from ``_perf_samples`` because the record shape is different
        # (per-view Discord edit, not per-dispatch).
        self._refresh_samples: _collections.deque = _collections.deque(maxlen=100)
        # Edit-count frames of profiled dispatches, pushed as one starts and
        # removed by identity when it ends. ``_CURRENT_EDIT_COUNTER`` carries
        # the attribution; ``_record_edit`` falls back to the top frame.
        self._perf_edit_stack: list = []
        # Per-subscriber timings from ``_safe_notify`` while profiling, each
        # {subscriber_id, action, ms, timestamp}; longer than ``_perf_samples``,
        # since one dispatch fans out to many subscribers.
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
        (capped at 500), one entry per subscriber per dispatch.
        Overhead while enabled is a handful of ``time.perf_counter()``
        calls per dispatch: negligible relative to a REST round-trip
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
            reduce_registry_pruned,
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
            "REGISTRY_PRUNED": reduce_registry_pruned,
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

    async def _take_state_turn(
        self, label: Optional[str] = None, action: Optional[str] = None
    ) -> Optional[asyncio.Lock]:
        """Take the store's state for one writer at a time; ``None`` if this task holds it.

        A reducer runs inside the turn, from reading the state to committing
        its result, so a reducer that awaits cannot return a state from before
        its await over a change committed meanwhile. The other writers outside
        a reducer, the undo entries a batch records and devtools' reset, take
        it too. The taker is named by ``label``, or by the ``action`` whose
        reducer runs. A wait of ``_STATE_TURN_WAIT_WARN_SECONDS`` is logged,
        naming the holder. Give the lock back with :meth:`_give_state_turn`.
        """
        current = asyncio.current_task()
        if current is not None and current is self._state_holder:
            return None
        loop = asyncio.get_running_loop()
        if self._state_lock is None or self._state_lock[0] is not loop:
            # A bot run twice through asyncio.run uses two loops, and a lock
            # one loop waited on refuses the other.
            self._state_lock = (loop, asyncio.Lock(), [0])
        _, lock, waiting = self._state_lock
        # Waiters are counted because a released lock reads free before the
        # waiter it woke takes it, and acquire() still queues then.
        if lock.locked() or waiting[0]:
            waiting[0] += 1
            watchdog = loop.call_later(
                _STATE_TURN_WAIT_WARN_SECONDS, self._warn_long_state_wait, label, action
            )
            try:
                await lock.acquire()
            finally:
                waiting[0] -= 1
                watchdog.cancel()
        else:
            await lock.acquire()
        self._state_holder = current
        self._state_holder_label, self._state_holder_action = label, action
        self._state_token = object()
        return lock

    def _give_state_turn(self, lock: Optional[asyncio.Lock]) -> None:
        """Give back a turn :meth:`_take_state_turn` took, after the writes held for it."""
        if lock is None or self._state_lock is None or lock is not self._state_lock[1]:
            # A turn taken on a loop that has since closed: the store has a new
            # lock, and whoever holds that turn is not this caller.
            return
        try:
            while self._state_pending:
                write = self._state_pending.pop(0)
                try:
                    write()
                except Exception:
                    logger.exception("A state write held for the state turn failed")
        finally:
            self._state_holder = self._state_holder_label = self._state_holder_action = None
            self._state_token = None
            lock.release()

    def _held_elsewhere(self) -> bool:
        """Whether a reducer in another task of the running loop holds the turn.

        A holder from a loop that has since closed holds nothing.
        """
        holder = self._state_holder
        if holder is None:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        return holder.get_loop() is loop and holder is not asyncio.current_task()

    def _records_step_on(self, records, view_id: str) -> bool:
        """Whether any of a batch's undo records puts a step on ``view_id``'s history.

        The rule UndoMiddleware places the step by: on the source view, or on
        the view it has since handed its panel on to.
        """
        for _seq, source_id, *_record, view in records:
            if source_id in self._released_ids:
                continue
            if view is not None and view._successor is not None:
                if view._last_successor().id == view_id:
                    return True
            elif source_id == view_id:
                return True
        return False

    def _note_undo_batch(self, batch: "BatchContext") -> None:
        """Record that ``batch`` holds undo steps."""
        if not any(held is batch for held in self._undo_batches):
            self._undo_batches.append(batch)

    def _forget_undo_batch(self, batch: "BatchContext") -> None:
        """Drop ``batch`` once its steps are placed, and release waiters."""
        self._undo_batches = [held for held in self._undo_batches if held is not batch]
        batch._settled.set()

    def _pending_undo_batches(self, view_id: str) -> List["BatchContext"]:
        """Other tasks' open batches holding undo steps for ``view_id``.

        A batch in the caller's own lineage is left out, since it cannot exit
        before the caller does.
        """
        lineage = _ACTIVE_BATCHES.get()
        return [
            batch
            for batch in self._undo_batches
            if not batch._undo_wait_expired
            and not any(batch is own for own in lineage)
            and batch._holds_steps_for(view_id)
        ]

    async def _wait_for_undo_batches(self, view_id: str) -> bool:
        """Wait for other tasks' open batches holding undo steps for ``view_id``.

        A batch's steps join the view's history only at its exit, so an undo
        run meanwhile would undo an older step over the batch's writes and
        leave the batch's step restoring an undone value. Past
        ``_UNDO_BATCH_WAIT_SECONDS`` the undo goes ahead and says so, and this
        returns ``False``.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _UNDO_BATCH_WAIT_SECONDS
        while True:
            pending = self._pending_undo_batches(view_id)
            if not pending:
                return True
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await _bounded_wait(pending[0]._settled.wait(), remaining)
            except asyncio.TimeoutError:
                for batch in pending:
                    batch._undo_wait_expired = True
                logger.warning(
                    f"An undo or redo of view {view_id} waited {_UNDO_BATCH_WAIT_SECONDS:g}s "
                    f"for a batch holding its steps to finish, and goes ahead without it."
                )
                return False

    def _write_when_free(self, write: Callable[[], None]) -> None:
        """Run ``write``, a synchronous state write, now or after the turn's holder commits.

        For writers outside a reducer that cannot wait for the turn: while a
        reducer in another task holds it, a write made now would be replaced
        by that reducer's result, so it runs just before the turn is given
        back instead.
        """
        if self._held_elsewhere():
            self._state_pending.append(write)
        else:
            write()

    async def _write_in_turn(self, write: Callable[[], None], label: str) -> None:
        """Run ``write`` in the state turn, waiting while another task holds it.

        For a write something reads right after, which :meth:`_write_when_free`
        would leave waiting behind the holder. A cancel during the wait leaves
        it to run as the holder gives the turn back.
        """
        if not self._held_elsewhere():
            write()
            return
        try:
            lock = await self._take_state_turn(label)
        except asyncio.CancelledError:
            self._write_when_free(write)
            raise
        try:
            write()
        finally:
            self._give_state_turn(lock)

    @contextlib.asynccontextmanager
    async def _state_turn(self, label: str):
        """Hold the state turn (see :meth:`_take_state_turn`) for a block."""
        lock = await self._take_state_turn(label)
        try:
            yield
        finally:
            self._give_state_turn(lock)

    @staticmethod
    def _turn_taker(label: Optional[str], action: Optional[str]) -> str:
        return label or f"the reducer for {action!r}"

    def _state_holder_name(self) -> str:
        return self._turn_taker(self._state_holder_label, self._state_holder_action)

    def _warn_long_state_wait(self, label: Optional[str], action: Optional[str]) -> None:
        """Log a state-turn wait that has lasted ``_STATE_TURN_WAIT_WARN_SECONDS``."""
        holder = self._state_holder_name() if self._state_holder else "another reducer"
        logger.warning(
            f"{self._turn_taker(label, action)} has waited "
            f"{_STATE_TURN_WAIT_WARN_SECONDS:g}s for {holder} to return. Reducers "
            f"run one at a time, so a reducer that awaits holds up every dispatch "
            f"until it returns, and one waiting on a dispatch of its own (from a "
            f"task it created or awaits) never returns. Do slow work before "
            f"dispatching and pass its result in the payload."
        )

    async def _run_middleware_chain(self, action: Action, reducer_fn, run: "_ChainRun") -> None:
        """Build and execute the middleware chain ending at the reducer.

        ``run`` records whether the reducer step ran, the batch sequence
        taken then, and an exception a middleware raised after it, which the
        caller re-raises once the action is announced.
        """

        # The state last handed down the chain. A middleware that awaits before
        # next_fn passes on what the store held when it was called, and reducing
        # that would undo a commit made since, so the live state replaces it. A
        # mapping a middleware built is reduced as written.
        handed = [self.state]
        on_commit: List[Callable[[StateData, StateData], None]] = []

        async def run_reducer(act, state):
            if self._state_holder is not None and self._state_holder is asyncio.current_task():
                # Waiting would never end: this task holds the turn.
                raise RuntimeError(
                    f"dispatch({act['type']!r}) was called from inside "
                    f"{self._state_holder_name()}. A reducer's result replaces the "
                    f"state, so an action dispatched from inside one is lost. "
                    f"Dispatch it after the first dispatch returns."
                )
            inside = _IN_REDUCER.get()
            if inside is not None and inside[0] is self and inside[1] is self._state_token:
                # A task the running reducer started: the dispatch would wait
                # for the reducer, which never returns if it awaits the task.
                raise RuntimeError(
                    f"dispatch({act['type']!r}) was called from a task started inside "
                    f"{self._state_holder_name()} while it runs. The dispatch waits "
                    f"for that reducer, so a reducer awaiting it never returns. "
                    f"Dispatch it after the first dispatch returns, or from a "
                    f"store.on() hook."
                )
            lock = await self._take_state_turn(action=act["type"])
            skipped = False
            try:
                if act["type"] in ("UNDO", "REDO"):
                    # undo() waits before dispatching, but a batch can open while
                    # the middlewares above await, so the check is made again
                    # here, holding the turn, where no batch records a step unseen.
                    payload = act.get("payload")
                    undone = payload.get("view_id") if isinstance(payload, dict) else None
                    # A view that hands its panel on during a wait takes its
                    # history along, so the undo follows it there.
                    following = self._active_views.get(undone)
                    if following is None:
                        named = _SOURCE_VIEW.get()
                        following = named if getattr(named, "id", None) == undone else None
                    if following is not None and following._successor is not None:
                        following = None
                    while self._pending_undo_batches(undone):
                        if state is not handed[0]:
                            # A middleware built this state before the batch's
                            # writes, and reducing it after the wait erases them.
                            skipped = True
                            logger.warning(
                                f"Skipped {act['type']} for view {undone}: another task's batch "
                                f"on the view is open, and a middleware passed on a state built "
                                f"before its writes. Undo again once the batch has ended."
                            )
                            break
                        self._give_state_turn(lock)
                        lock = None
                        clear = await self._wait_for_undo_batches(undone)
                        lock = await self._take_state_turn(action=act["type"])
                        if following is not None and following._successor is not None:
                            following = following._last_successor()
                            undone = following.id
                            payload = {
                                **payload,
                                "view_id": undone,
                                "session_id": following.session_id,
                            }
                            act = {**act, "payload": payload}
                        if not clear:
                            break
                if state is handed[0]:
                    state = self.state
                # The reducer's own time goes to the slot this dispatch bound, so
                # the dispatch can derive ``middleware_ms``. A contextvar, since a
                # shared slot would take a concurrent dispatch's timing.
                reducer_slot = _CURRENT_REDUCER_MS.get()
                perf = self._perf_enabled and reducer_slot is not None
                if perf:
                    r0 = time.perf_counter()
                if skipped:
                    pass
                elif reducer_fn:
                    try:
                        reducing = _IN_REDUCER.set((self, self._state_token))
                        try:
                            new_state = await reducer_fn(act, state)
                        finally:
                            try:
                                _IN_REDUCER.reset(reducing)
                            except ValueError:
                                # Closed from another context, as collecting a
                                # reducer left on a closed loop does.
                                pass
                            # The reducer alone: the commit callbacks below are
                            # middleware work.
                            if perf:
                                reducer_slot[0] = (time.perf_counter() - r0) * 1000
                                perf = False
                        # A reducer that declines an action returns the object it
                        # was handed. Rebinding it would revert whatever committed
                        # while this dispatch was suspended in the chain above.
                        if new_state is not state:
                            prior = self.state
                            self.state = new_state
                            for callback in list(on_commit):
                                try:
                                    callback(prior, new_state)
                                except Exception:
                                    logger.exception(f"Commit callback failed for {act['type']}")
                        logger.debug(f"State updated by reducer for {act['type']}")
                    except Exception as e:
                        logger.error(f"Error in reducer for {act['type']}: {e}", exc_info=True)
                else:
                    # Lazy import, same cycle as _load_core_reducers: reducers.py
                    # reaches back into this module via the middleware package.
                    from .reducers import _BUILTIN_REDUCER_ACTIONS

                    action_type = act["type"]
                    if action_type in _BUILTIN_REDUCER_ACTIONS:
                        # The real property is "library-declared dispatch-only",
                        # never a typo. "Has a listener" is a proxy that fails
                        # here: subscriber filters default to an empty set, so the
                        # manager's prune signals arrive listener-less by default.
                        logger.debug(f"No reducer for {action_type} (dispatch-only built-in)")
                    else:
                        # With no reducer, an action something listens for (a hook
                        # or a subscriber filter) is a broadcast; one nobody
                        # listens for is likely a typo and keeps the warning.
                        has_listener = action_type in self._hooks or any(
                            flt is None or action_type in flt
                            for _, flt, _ in self.subscribers.values()
                        )
                        if has_listener:
                            logger.debug(f"No reducer for {action_type} (broadcast-only)")
                        else:
                            logger.warning(f"No reducer found for action type {action_type}")
                if perf:
                    reducer_slot[0] = (time.perf_counter() - r0) * 1000
                # From here the action is announced whatever the chain does next.
                # The stamp is taken whether or not the reducer committed, so a
                # batch announces its actions in the order the store applied them.
                run.reduced = True
                if run.stamp is None:
                    run.stamp = next_commit_sequence()
                return self.state
            finally:
                self._give_state_turn(lock)

        # Build the chain from inside out: last middleware wraps the reducer,
        # second-to-last wraps that, etc. Default args capture loop variables.
        chain = run_reducer
        for mw in reversed(self._middleware):

            def wrap(middleware=mw, next_fn=chain):
                async def step(act, state):
                    if state is handed[0]:
                        state = handed[0] = self.state
                    return await middleware(act, state, next_fn)

                return step

            chain = wrap()

        token = _ON_COMMIT.set(on_commit)
        # A view a push or pop has torn down is gone from the registry, and its
        # own dispatch() names it instead.
        named = _SOURCE_VIEW.get()
        source = _SOURCE_VIEW.set(
            self._active_views.get(action.get("source"))
            or (named if getattr(named, "id", None) == action.get("source") else None)
        )
        try:
            await chain(action, self.state)
        except asyncio.CancelledError:
            # Cancelled after the reducer step: a change it committed stands,
            # so the action is still announced, by the batch collecting it or
            # from a task.
            if run.reduced:
                self._announce_later(action, run)
            raise
        except Exception as exc:
            # Raised by a middleware after the reducer step: the action is
            # announced before the error reaches the caller.
            if not run.reduced:
                raise
            run.error = exc
        finally:
            _SOURCE_VIEW.reset(source)
            _ON_COMMIT.reset(token)

    def _announce_later(self, action: Action, run: "_ChainRun") -> None:
        """Tell subscribers about an action whose dispatch was cancelled after its reducer step.

        The batch collecting it announces it in full. Otherwise its
        subscribers are told from a task, and its hooks, which had not
        started, do not run.
        """
        batch = current_batch()
        if batch is not None and batch.add_entry(action, run.stamp):
            return
        self.task_manager.create_task("state_store_notify", self._notify_subscribers(action))

    async def _announce(self, action: Action, *hook_actions: Action) -> None:
        """Tell subscribers about ``action``, then fire the hooks of ``hook_actions``.

        ``hook_actions`` is ``action`` alone unless given; a batch passes its
        actions followed by its ``BATCH_COMPLETE``.
        """
        await self._notify_subscribers(action)
        await self._fire_hooks(*(hook_actions or (action,)))

    @staticmethod
    def _log_held_error(run: "_ChainRun", action_type: str) -> None:
        """Log a middleware's error the announcement's own exception replaces."""
        if run.error is not None:
            logger.error(
                f"A middleware raised after {action_type} was applied: {run.error}",
                exc_info=run.error,
            )

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
        self._hooks[action_type].append(_Registration(callback))

    def off(self, hook_name: str, callback: HookFn) -> None:
        """Remove a previously registered hook."""
        action_type = self._HOOK_ACTION_MAP.get(hook_name, hook_name)
        if action_type in self._hooks:
            registrations = self._hooks[action_type]
            for index, registration in enumerate(registrations):
                if registration.callback is callback or registration.callback == callback:
                    del registrations[index]
                    break
            if not self._hooks[action_type]:
                del self._hooks[action_type]

    async def _fire_hooks(self, *actions: Action) -> None:
        """Fire the hooks registered for each action, in order.

        Each action's hooks are read when its turn comes, and a registration
        ``off()`` removed before its call is skipped, so a batch calls the
        hooks its actions dispatched one by one would. A hook added during an
        action's turn is first called for the next action. A cancel ends the
        firing: the hooks not yet started do not run.
        """
        for action in actions:
            for hook in list(self._hooks.get(action["type"], ())):
                if not any(entry is hook for entry in self._hooks.get(action["type"], ())):
                    continue
                try:
                    await await_maybe(hook.callback(action, self.state))
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
        """Set scoped state directly (prefer dispatch for tracked changes).

        A reducer running in another task commits first, and this write lands
        after it.
        """
        scope_key = self._build_scope_key(scope, **identifiers)

        def write():
            access_slot(self.state, slot_name)[scope_key] = data

        self._write_when_free(write)

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

    @staticmethod
    def _index_add(index: dict, key, view_id: str) -> None:
        """Append a view id to one bucket of a view index."""
        index.setdefault(key, []).append(view_id)

    @staticmethod
    def _index_remove(index: dict, key, view_id: str) -> None:
        """Remove a view id from one bucket of a view index, dropping it when empty."""
        ids = index.get(key, [])
        if view_id in ids:
            ids.remove(view_id)
            if not ids:
                del index[key]

    def _indexed_views(self, index: dict, key) -> list:
        """The registered views in one bucket of a view index, newest first."""
        ids = index.get(key, ())
        return [self._active_views[vid] for vid in reversed(ids) if vid in self._active_views]

    def _add_to_instance_index(self, view_id: str, view_type: str, scope_key: str) -> None:
        """Add a view ID to the instance index under the given type+scope key."""
        self._index_add(self._instance_index, (view_type, scope_key), view_id)
        self._instance_keys.setdefault(view_id, set()).add((view_type, scope_key))

    def _remove_from_instance_index(self, view_id: str, view_type: str, scope_key: str) -> None:
        """Remove a view ID from the instance index for the given type+scope key."""
        self._index_remove(self._instance_index, (view_type, scope_key), view_id)
        keys = self._instance_keys.get(view_id)
        if keys is not None:
            keys.discard((view_type, scope_key))
            if not keys:
                del self._instance_keys[view_id]

    def _reindex_instance(self, view) -> None:
        """File a registered view again under the keys its current scope gives."""
        if view.id not in self._active_views:
            return
        for view_type, scope_key in list(self._instance_keys.get(view.id, ())):
            self._remove_from_instance_index(view.id, view_type, scope_key)
        scope_key = self._build_instance_scope_key(view)
        if scope_key is not None:
            view_type = (
                getattr(view, "_instance_root_class", None) or view.__class__._class_session_key()
            )
            self._add_to_instance_index(view.id, view_type, scope_key)
        for user_id in getattr(view, "_participants", set()):
            self._register_participant(view, user_id)

    def _reindex_registry_message(self, view, previous, current) -> None:
        """Move a registered view between registration-message buckets.

        A send stamps the id after the view registers, and a navigation's
        commit or rollback clears it on a registered view, so the index is kept
        current where the id is written rather than only at registration.
        """
        if view.id not in self._active_views:
            return
        if previous is not None:
            self._index_remove(self._views_by_message, previous, view.id)
        if current is not None:
            self._index_add(self._views_by_message, current, view.id)

    def _register_view(self, view) -> None:
        """Register a live view instance. Internal plumbing. Idempotent.

        Uses ``view._instance_root_class`` (if set) as the type key so that
        navigated sub-views are tracked under the root view's class name.
        Called from the view pipeline, never directly by user code.
        """
        already_registered = view.id in self._active_views
        self._active_views[view.id] = view
        if not already_registered:
            key = getattr(view, "_persistence_key", None)
            if key is not None:
                self._index_add(self._views_by_key, key, view.id)
            message_id = getattr(view, "_registry_message_id", None)
            if message_id is not None:
                self._index_add(self._views_by_message, message_id, view.id)
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
            key = getattr(view, "_persistence_key", None)
            if key is not None:
                self._index_remove(self._views_by_key, key, view_id)
            message_id = getattr(view, "_registry_message_id", None)
            if message_id is not None:
                self._index_remove(self._views_by_message, message_id, view_id)

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
        and the active-registry entry was retained. A view a bot's close
        released left without an action, and is removed the same way here.
        """
        if view_id in self._released_ids:
            self._drop_released({view_id})
            return True
        payload = ActionCreators.view_destroyed(view_id)
        if view_id not in self.state.get("views", {}):
            # A send rolled back before its VIEW_CREATED landed: the session
            # its SESSION_CREATED made goes too, unless another registered view
            # (a concurrent send not yet in it) names it.
            view = self._active_views.get(view_id)
            session_id = getattr(view, "session_id", None)
            if session_id is not None and not any(
                other is not view and getattr(other, "session_id", None) == session_id
                for other in self._active_views.values()
            ):
                payload = ActionCreators.view_destroyed(view_id, session_id=session_id)
        try:
            await self.dispatch("VIEW_DESTROYED", payload, source_id=source_id)
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
            # The active entry clears only once state confirms the removal, and
            # in the finally, so a cancel landing after the reducer removed the
            # state entry still clears it.
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
        """Return active view instances for a type+scope, oldest-first. Internal plumbing.

        Counts each panel once. A push or pop destination still arriving is
        left out, since its source stands for the panel until the edit
        lands, and so is a view already torn down (a source that handed its
        message over, a discarded destination, an exiting view), whose slot
        is free before its registration is removed.
        """
        key = (view_type, scope_key)
        ids = self._instance_index.get(key, [])
        views = [self._active_views[vid] for vid in ids if vid in self._active_views]
        return [
            view
            for view in views
            if getattr(view, "_arriving_from", None) is None and view.id in self._view_subscriptions
        ]

    def get_active_views(self) -> Mapping[str, Any]:
        """Read-only view of the active view registry (view_id -> view instance).

        Callers outside the store (devtools, diagnostics, test harnesses)
        read through this accessor instead of reaching for ``_active_views``
        directly. The returned mapping reflects registrations live but
        rejects mutation: all bookkeeping goes through the store's own
        ``_register_view`` / ``_destroy_view`` seams.
        """
        return MappingProxyType(self._active_views)

    def get_active_view(self, *, persistence_key: str) -> Optional[Any]:
        """The live view holding ``persistence_key``, or ``None``.

        Matches the ``persistence_key=`` a view was constructed with; a view
        that was given none holds no key, even though its ``persistence_key``
        property falls back to its id. A finished view is never returned.
        When several unfinished views hold the key (a swap under
        ``retire_previous_on_send = False``, or the moment inside a send
        before the new panel registers), the one the stored registration
        points at is returned, which is the panel on screen; with no
        registration, the most recently registered holder is.

        A panel that navigated away with ``push()`` holds its key through the
        view now on its message: navigation replaces the instance, and the
        destination carries the registration id without the key. That view is
        returned, so this answers "what is live on this key's registration"
        for every shape, which is the question a swap and a prune pre-flight
        both ask. It is not always a persistent view. A view a ``push()`` or
        ``pop()`` is still bringing in is not returned until its edit lands:
        the view it would replace is still the one on screen.

        The argument is keyword-only because ``get_active_views()`` is keyed
        by view id, and a view id passed here would match nothing.
        """
        entry = self.state.get("persistent_views", {}).get(persistence_key)
        owner_message = entry.get("message_id") if entry else None
        newest = None
        for view in self._views_for_key(persistence_key):
            if view.is_finished() or getattr(view, "_arriving_from", None) is not None:
                continue
            if (
                owner_message is not None
                and getattr(view, "_registry_message_id", None) == owner_message
            ):
                return view
            if newest is None:
                newest = view
        # The owner is preferred over any key holder, so a panel superseded
        # under the key does not shadow the view sitting on the registered
        # message. Only reached when no key holder carried that id.
        if owner_message is not None:
            for view in self._views_for_message(owner_message):
                if not view.is_finished() and getattr(view, "_arriving_from", None) is None:
                    return view
        return newest

    def _views_for_key(self, persistence_key: str) -> list:
        """Every registered view holding ``persistence_key``, newest first, finished included."""
        return self._indexed_views(self._views_by_key, persistence_key)

    def _views_for_message(self, message_id: str) -> list:
        """Every registered view carrying ``message_id`` as its registration, most
        recently stamped first, finished included."""
        return self._indexed_views(self._views_by_message, message_id)

    def _persistence_keys_for_message(self, message_id: str) -> list:
        """Every registration whose message is ``message_id``.

        The reverse of the usual lookup, for a view that carries a
        registration's message id without its key: after a ``push()`` the
        panel's key rides the destination that way. A key renamed in place
        leaves the old row naming the same message, so there can be two.
        """
        return [
            key
            for key, entry in self.state.get("persistent_views", {}).items()
            if entry.get("message_id") == message_id
        ]

    def _live_persistence_keys(self) -> set:
        """The registry keys a live persistent panel in this process owns.

        Reattach re-drives and the unreachable prune skip these rows: a live
        panel owns its key, so a row under it is never re-attached a second
        time or deleted on the strength of a fetch that failed.

        A key is owned by an unfinished persistent view that holds it, or by
        any unfinished view carrying the message id the key's registration
        points at. The second covers a panel that has pushed a child: the
        child shows the panel's message and carries its registration id, but
        not its key. A non-persistent view built with a matching key owns no
        registry row, so it protects none.
        """
        keys = set(self._views_by_key) | set(self.state.get("persistent_views", {}))
        return {key for key in keys if self._holds_persistence_key(key)}

    def _holds_persistence_key(self, key: str) -> bool:
        """Whether a live panel owns ``key`` (see :meth:`_live_persistence_keys`)."""
        if any(
            getattr(view, "_persistent", False) and not view.is_finished()
            for view in self._views_for_key(key)
        ):
            return True
        message_id = self.state.get("persistent_views", {}).get(key, {}).get("message_id")
        return message_id is not None and any(
            not view.is_finished() for view in self._views_for_message(message_id)
        )

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
        # A falsy id counts as no id: the view is exempt from the limit rather
        # than pooled into one "user:0" bucket. The scoped-state writer keeps
        # is-None, since a dropped write is worse than a skipped index entry.
        uid = user_id if user_id is not None else view.user_id
        # A view pushed onto a chain is keyed by the root's scope, so the
        # root's limit finds it.
        return StateStore.scope_key(
            getattr(view, "_instance_root_scope", None) or view.instance_scope,
            user_id=uid or None,
            guild_id=view.guild_id or None,
        )

    # // ========================================( Message Cleanup )======================================== // #

    def _answer_stale_clicks(self, bot) -> None:
        """Have the bot's view store answer a click sent from an earlier render.

        A click carries the ``custom_id`` of the render its user saw. When a
        re-render changes that id, as a generated id does when the button's
        label counts something, discord.py finds no item for it and drops the
        click unanswered, so the user sees "This interaction failed". On a
        message a live view owns, such a click is acknowledged and logged as
        dropped instead; nothing runs. Clicks on other messages, and dynamic
        items, route as discord.py routes them. Once per view store.
        """
        from discord.ui.view import ViewStore

        view_store = getattr(getattr(bot, "_connection", None), "_view_store", None)
        if not isinstance(view_store, ViewStore) or view_store in self._stale_click_stores:
            return
        self._stale_click_stores.add(view_store)
        store = self

        def dispatch_view(component_type, custom_id, interaction):
            try:
                stale = store._stale_click_view(view_store, component_type, custom_id, interaction)
            except Exception:
                # The check reads discord.py's private tables; if a release
                # changes them, clicks still route as discord.py routes them.
                logger.exception("Could not check a click against the view on its message")
                stale = None
            # Through the class, so ViewStore tracing, which patches it there, still runs.
            type(view_store).dispatch_view(view_store, component_type, custom_id, interaction)
            if stale is not None:
                stale._answer_stale_click(custom_id, interaction)

        view_store.dispatch_view = dispatch_view

    def _stale_click_view(self, view_store, component_type, custom_id, interaction):
        """The live view on a click's message when nothing there takes the click, else ``None``.

        Read before discord.py dispatches, with the same lookups: the
        message's items, the items stored without a message, and the
        dynamic item patterns. An item whose view is gone (``remove_item()``
        detaches it at once, while the store keeps it until the edit lands)
        takes nothing, and discord.py discards the click with a warning.
        """
        message = interaction.message
        items = view_store._views.get(message.id) if message is not None else None
        if not items:
            return None
        key = (component_type, custom_id)
        item = items.get(key)
        if item is None:
            item = view_store._views.get(None, {}).get(key)
        if item is not None and item.view is not None:
            return None
        if any(pattern.fullmatch(custom_id) for pattern in view_store._dynamic_items):
            return None
        for item in items.values():
            view = item.view
            if view is not None and self._active_views.get(getattr(view, "id", None)) is view:
                return view
        return None

    def _install_message_cleanup(self, bot) -> None:
        """Register gateway listeners that clean up views when their message is deleted.

        A message goes with a delete or a bulk purge, which Discord reports
        only to a bot holding the message intents, and with the channel or
        thread it sits in, which Discord reports under the ``guilds`` intent
        and never as message deletions. Each routes to
        ``view.on_message_delete()`` for every view on a deleted message; one
        view's raising override does not stop the others. A bot without
        message intents, or a plain :class:`discord.Client`, which has no
        listeners to add, is still covered for a lone delete by the next
        edit, which finds the message gone and tears the view down itself.

        Once per bot object. Called automatically from ``send()`` and from
        :meth:`PersistenceMiddleware.initialize` when ``bot=`` is supplied.
        The bot's views are also released when it closes (see
        :meth:`_release_views_of`), and its view store answers a click sent
        from an earlier render (see :meth:`_answer_stale_clicks`).
        """
        # Before the once-per-bot return: a bot's clear() replaces its store.
        self._answer_stale_clicks(bot)
        if bot in self._cleanup_listener_bots:
            return
        self._cleanup_listener_bots.add(bot)
        # Before the new bot restores a panel or registers a send.
        self._drop_released()
        self._release_with(bot)
        if not hasattr(bot, "listen"):
            return

        store = self

        @bot.listen("on_raw_message_delete")
        async def _cascadeui_message_cleanup(payload):
            await store._clean_up_deleted(lambda m: m.id == payload.message_id)

        @bot.listen("on_raw_bulk_message_delete")
        async def _cascadeui_bulk_message_cleanup(payload):
            deleted_ids = set(payload.message_ids)
            await store._clean_up_deleted(lambda m: m.id in deleted_ids)

        @bot.listen("on_guild_channel_delete")
        async def _cascadeui_channel_cleanup(channel):
            await store._clean_up_deleted(lambda m: _in_channel(m, channel.id))

        @bot.listen("on_raw_thread_delete")
        async def _cascadeui_thread_cleanup(payload):
            await store._clean_up_deleted(lambda m: _in_channel(m, payload.thread_id))

        logger.debug("Message deletion cleanup listener installed")

    def _release_with(self, bot) -> None:
        """Release the views sent through ``bot`` when it closes.

        discord.py has no shutdown event, so ``close()`` is wrapped on the
        instance, and the release runs after it, so a ``close()`` override
        on the bot's class still acts on live views.
        """
        closes = bot.close
        store = self

        async def close(*args, **kwargs):
            try:
                return await closes(*args, **kwargs)
            finally:
                store._release_views_of(bot)

        # Another wrapper's marker stays readable through this one.
        close.__dict__.update(getattr(closes, "__dict__", {}))
        bot.close = close

    def _release_views_of(self, bot) -> None:
        """Release, as a process restart would, the views sent through ``bot``.

        A closed bot never routes a click again: discord.py cannot log it in
        again, so a restart in the same process builds a new bot object. Each
        view sent through it stops, with its timer and tasks, and nothing is
        edited. Code awaiting a view's ``wait()`` ends as it does when the
        process ends. The views leave the registry and the state, their
        sessions with them, and nothing is dispatched: a process restart runs
        no hook or middleware for the old process's views either, and one run
        here would run at every shutdown or ahead of the next bot's boot. A
        persistent panel keeps its registration, and is restored when a new
        bot's ``setup_hook`` runs :func:`~cascadeui.setup_middleware` again.
        """
        if not bot.is_closed():
            return
        manager = getattr(self, "persistence_manager", None)
        keys_by_message: Dict[Any, List[str]] = {}
        for key, entry in self.state.get("persistent_views", {}).items():
            keys_by_message.setdefault(entry.get("message_id"), []).append(key)
        released = []
        for view in list(self._active_views.values()):
            if view._client() is not bot:
                continue
            keys = set()
            if getattr(view, "persistence_key", None):
                keys.add(view.persistence_key)
            registered = getattr(view, "_registry_message_id", None)
            if registered is not None:
                keys.update(keys_by_message.get(registered, ()))
            try:
                view._release_for_restart()
            except Exception as exc:
                logger.error(
                    f"{type(view).__name__} could not be released when "
                    f"{type(bot).__name__} closed: {exc}",
                    exc_info=exc,
                )
                continue
            view._released_keys = keys
            released.append(view)
            self._released_refs.add(view)
            if keys:
                self._restore_owed = True
            if manager is not None:
                # Reattach skips a key it restored before.
                manager._restored_keys.difference_update(keys)
        if released:
            ids = {view.id for view in released}
            self._released_ids |= ids
            for view in released:
                self._unregister_view(view.id)
            # A view whose VIEW_CREATED had not landed is no member of the
            # session its send made; that session goes unless a view still
            # registered names it.
            named = {getattr(view, "session_id", None) for view in self._active_views.values()}
            sessions = {view.session_id for view in released} - named
            self._drop_released(ids, empty_sessions=sessions)

    def _drop_released(self, view_ids=None, empty_sessions=()) -> None:
        """Remove from the state the views a bot's close released, or ``view_ids``.

        Work in flight at the close can write a released view back: a
        dispatch suspended in the chain commits a state read before the
        release, and a send registering the view reduces its ``VIEW_CREATED``
        after it. Wiring a new bot drops them again, so the restart starts
        without them. A reducer awaiting in another task commits first, and
        the drop lands after it. Each of ``empty_sessions`` goes too when it
        has no member.
        """
        ids = frozenset(self._released_ids if view_ids is None else view_ids)
        if ids:
            from .reducers import _without_empty_session, _without_views

            def drop():
                state = _without_views(self.state, ids)
                for session_id in empty_sessions:
                    state = _without_empty_session(state, session_id)
                self.state = state

            self._write_when_free(drop)

    def _link_released(self) -> None:
        """Point a released panel held by code at the panel restored in its place."""
        for view in list(self._released_refs):
            for key in getattr(view, "_released_keys", ()):
                restored = self.get_active_view(persistence_key=key)
                if restored is not None:
                    view._successor = restored
                    break

    async def _clean_up_deleted(self, message_matches: Callable[[Any], bool]) -> None:
        """Run ``on_message_delete`` for every live view whose message matches."""
        views = [
            view
            for view in list(self._active_views.values())
            if view._message is not None and message_matches(view._message)
        ]
        # Views being sent go last, so one slow send holds up no other view.
        views.sort(key=lambda view: view._lifecycle_sending)
        for view in views:
            # A send of the view in flight may be moving it to a new message,
            # and the deleted one is then a message it has left.
            await view._send_finished()
            # Re-checked per view, not at snapshot time: one view's hook can
            # tear another down (a parent exits its attached children, a panel
            # exits its sibling), and the snapshot predates every hook and wait.
            if view._message is None or not message_matches(view._message):
                continue
            # Marked even when another view's hook closed it first (the view a
            # push handed this message to): the deletion is why it closed.
            view._message_deleted = True
            if view._torn_down():
                continue
            try:
                await await_maybe(view.on_message_delete())
            except Exception as exc:
                logger.error(
                    f"on_message_delete failed for {type(view).__name__}: {exc}",
                    exc_info=exc,
                )

    # // ========================================( Dispatch )======================================== // #

    @with_error_boundary("dispatch")
    async def dispatch(
        self, action_type: str, payload: Any = None, source_id: Optional[str] = None
    ) -> StateData:
        """
        Process an action by updating state and notifying subscribers.

        When called inside ``async with store.batch()``, the reducer runs
        inline but notification and hooks defer to the outer batch exit
        (``PersistenceMiddleware`` queues its write at the commit). This is
        what makes ``batch()`` work transitively for view-level helpers that
        route through ``store.dispatch()``.
        """
        # ``dispatch(action)`` is the Redux idiom callers arrive with; it would
        # fail in the reducer lookup naming neither the parameter nor the shape.
        # Any other hashable non-string type raises nothing and matches nothing.
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

        # Batched: the reducer runs inline, and notification and hooks wait
        # for the outer batch exit (persistence queues at the commit). No
        # per-action profiling sample, since its notify_ms would read zero.
        batch = current_batch()
        chain_ran = False
        run = _ChainRun()
        batched_slot: Optional[List[float]] = None
        batched_chain_ms = 0.0
        if batch is not None:
            # Timed on a slot of its own, since one bound here belongs to the
            # dispatch whose chain this one runs inside. The timing is used
            # only when the batch refuses the action below.
            if self._perf_enabled:
                batched_slot = [0.0]
            slot_token = _CURRENT_REDUCER_MS.set(batched_slot)
            c0 = time.perf_counter() if self._perf_enabled else 0.0
            try:
                await self._run_middleware_chain(action, reducer, run)
            finally:
                _CURRENT_REDUCER_MS.reset(slot_token)
            if self._perf_enabled:
                batched_chain_ms = (time.perf_counter() - c0) * 1000
            if not run.reduced:
                # A middleware returned without calling next_fn: the action is
                # blocked, and nothing is told about it.
                return self.state
            if batch.add_entry(action, run.stamp):
                if run.error is not None:
                    raise run.error
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
                    await self._run_middleware_chain(action, reducer, run)
                t1 = time.perf_counter()
                if run.reduced:
                    logger.debug(f"Notifying subscribers about {action_type}")
                    await self._notify_subscribers(action)
                t2 = time.perf_counter()
                if run.reduced:
                    await self._fire_hooks(action)
                t3 = time.perf_counter()
            except BaseException:
                self._log_held_error(run, action_type)
                raise
            finally:
                _CURRENT_EDIT_COUNTER.reset(token)
                _CURRENT_REDUCER_MS.reset(reducer_token)
                # By identity: a batch flushing concurrently interleaves its
                # awaits with this one, and a positional pop takes its frame.
                try:
                    self._perf_edit_stack.remove(edit_counter)
                except ValueError:
                    pass
            if chain_ran:
                reducer_ms = batched_slot[0] if batched_slot is not None else 0.0
                chain_ms = batched_chain_ms
            else:
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
                    "total_ms": (t3 - t0) * 1000 + (batched_chain_ms if chain_ran else 0.0),
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
                await self._run_middleware_chain(action, reducer, run)
            if run.reduced:
                logger.debug(f"Notifying subscribers about {action_type}")
                try:
                    await self._announce(action)
                except BaseException:
                    self._log_held_error(run, action_type)
                    raise
        if run.error is not None:
            raise run.error

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

        # A task copies the current context, so background subscribers would
        # inherit the acting callback's interaction. It is unset while they are
        # scheduled and restored before the acting subscriber runs.
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
                # For BATCH_COMPLETE, check subscriber's filter against any batched
                # action. A batched UNDO/REDO bypasses it as an unbatched one does.
                if action["type"] == "BATCH_COMPLETE":
                    if action_filter is not None:
                        if (
                            not (action_filter & batched_types)
                            and "BATCH_COMPLETE" not in action_filter
                            and not (batched_types & {"UNDO", "REDO"})
                        ):
                            continue
                else:
                    # Normal action: skip if the subscriber has a filter and this
                    # action isn't in it. UNDO/REDO bypass the filter so cross-view
                    # subscribers see restored state.
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
                        # Notify-always is the safe answer when a change cannot
                        # be judged. Reported once, or a broken selector reads as
                        # a subscriber that wants every action.
                        if subscriber_id not in self._selector_failed:
                            self._selector_failed.add(subscriber_id)
                            logger.warning(
                                f"Selector for subscriber {subscriber_id} raised "
                                f"{type(e).__name__}: {e}. Notifying on every action "
                                f"until it stops raising."
                            )
                        new_value = self._SENTINEL
                    old_value = self._last_selected.get(subscriber_id, self._SENTINEL)
                    # Identity first: dict and list equality walk every entry to
                    # prove what ``is`` answers at once for a selector returning
                    # the same object. A selector returning the same NaN object
                    # is notified only for the first action.
                    unchanged = False
                    if new_value is not self._SENTINEL and old_value is not self._SENTINEL:
                        try:
                            unchanged = bool(new_value is old_value or new_value == old_value)
                        except Exception as e:
                            # An array-like value compares elementwise and
                            # refuses bool(); notify, as for a raising selector.
                            if subscriber_id not in self._selector_uncomparable:
                                self._selector_uncomparable.add(subscriber_id)
                                logger.warning(
                                    f"Selector value for subscriber {subscriber_id} could not "
                                    f"be compared ({type(e).__name__}: {e}). Notifying on every "
                                    f"action until it can."
                                )
                    if unchanged:
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

        # Other subscribers run as "state_store_notify" tasks, not awaited, so a
        # slow one never stalls the store; the acting view is awaited so its
        # refresh lands with the click. A test asserting on the background ones
        # drains them with ``await store._flush_notifications()``.
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

        Subscribing again under the same id replaces the earlier registration.
        Remove it with :meth:`unsubscribe`.

        Args:
            subscriber_id: Unique ID for this subscriber.
            callback: Callable receiving (state, action). Sync or async.
            action_filter: Optional set of action types to listen for.
                           If None, the subscriber receives all actions.
            selector: Optional synchronous function that extracts a slice
                      of state. When set, the subscriber is only notified
                      when the selected value changes between dispatches.

        Raises:
            ValueError: ``subscriber_id`` is a view's id. Replacing a view's
                subscription stops it rendering on state changes.
        """
        if subscriber_id in self._view_subscriptions:
            raise ValueError(
                f"subscribe({subscriber_id!r}) names a view's subscription; replacing "
                f"it stops the view rendering on state changes. Subscribe under an id "
                f"of your own, or set the view's subscribed_actions to change what it "
                f"renders on."
            )
        self._add_subscriber(subscriber_id, callback, action_filter, selector)

    def unsubscribe(self, subscriber_id: str) -> None:
        """Stop sending state updates to a subscriber added with :meth:`subscribe`.

        An id that is not subscribed is ignored, so a cleanup path can call
        this more than once.

        Raises:
            ValueError: ``subscriber_id`` is a view's id. A view leaves the
                store when it closes; call its ``exit()`` instead.
        """
        if subscriber_id in self._view_subscriptions:
            raise ValueError(
                f"unsubscribe({subscriber_id!r}) names a view's subscription, which "
                f"marks the view live: without it the view is never torn down. Call "
                f"view.exit() to close the view."
            )
        self._unsubscribe(subscriber_id)

    def _subscribe_view(
        self,
        view_id: str,
        callback: SubscriberFn,
        action_filter: Optional[set],
        selector: Optional[SelectorFn],
    ) -> None:
        """Subscribe a view under its own id, which the public methods then refuse."""
        self._add_subscriber(view_id, callback, action_filter, selector)
        self._view_subscriptions.add(view_id)

    def _add_subscriber(
        self,
        subscriber_id: str,
        callback: SubscriberFn,
        action_filter: Optional[set],
        selector: Optional[SelectorFn],
    ) -> None:
        if selector is not None:
            if not callable(selector):
                raise TypeError(
                    f"subscribe({subscriber_id!r}) selector= must be a callable taking "
                    f"the state and returning the slice to watch; got "
                    f"{type(selector).__name__}: {selector!r}"
                )
            # The change check runs inline and cannot await: an async selector
            # returns a new coroutine each time, never equal to the last, so the
            # subscriber would be notified on every action.
            from ..utils.hooks import is_async_callable

            if is_async_callable(selector):
                raise TypeError(
                    f"subscribe({subscriber_id!r}) selector= must be synchronous; the "
                    f"change check runs inline in dispatch and cannot await. Read the "
                    f"slice from the state argument, and do async work in the callback."
                )
        # A registration replacing another starts fresh: the old selector's
        # last value says nothing about the new one's.
        self._forget_selected(subscriber_id)
        self.subscribers[subscriber_id] = (callback, action_filter, selector)

    def _unsubscribe(self, subscriber_id: str) -> None:
        """Remove a subscriber, a view's included.

        View teardown calls this; user code calls :meth:`unsubscribe`.
        """
        if subscriber_id in self.subscribers:
            del self.subscribers[subscriber_id]
        self._view_subscriptions.discard(subscriber_id)
        self._forget_selected(subscriber_id)

    def _forget_selected(self, subscriber_id: str) -> None:
        """Drop what the change check remembers about a subscriber's selector."""
        self._last_selected.pop(subscriber_id, None)
        self._selector_failed.discard(subscriber_id)
        self._selector_uncomparable.discard(subscriber_id)

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
