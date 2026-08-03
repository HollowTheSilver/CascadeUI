# // ========================================( Modules )======================================== // #


import copy
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .._batching import current_batch
from ..types import Action, StateData

logger = logging.getLogger(__name__)


# Sentinel marking "this slot did not exist pre-action" in an undo diff.
# The UNDO reducer reads ``_MISSING`` as an instruction to ``pop`` the
# slot from the current application dict (restoring the pre-action
# absence). The REDO reducer reads it as an instruction to delete the
# slot when re-applying a post-action state that did not contain it.
# Identity comparison (``is _MISSING``) is the contract -- do not
# substitute a string sentinel that could collide with a real slot value.
#
# The sentinel survives ``copy.deepcopy`` via ``__deepcopy__`` returning
# self. Without that, ``@cascade_reducer``'s state deep-copy boundary
# would replace stored ``_MISSING`` references inside undo-stack diffs
# with fresh ``object()`` instances, breaking the identity check in
# ``_apply_slot_diff`` -- the deep-copied bare object would land in the
# else branch and be stored AS the slot value, corrupting the slot for
# subsequent reducers (e.g. ``state["application"]["scoped"]`` becoming
# a bare object that fails ``key not in slot`` on the next dispatch).
class _MissingSentinel:
    """Singleton sentinel that preserves identity across copy/deepcopy."""

    _instance: Optional["_MissingSentinel"] = None

    def __new__(cls) -> "_MissingSentinel":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __deepcopy__(self, memo: Any) -> "_MissingSentinel":
        return self

    def __copy__(self) -> "_MissingSentinel":
        return self

    def __repr__(self) -> str:
        return "_MISSING"

    def __bool__(self) -> bool:
        return False


_MISSING: Any = _MissingSentinel()

# Actions that should NOT create undo snapshots (internal lifecycle).
#
# Prune actions (APPLICATION_SLOTS_PRUNED, REGISTRY_PRUNED) are
# observability signals fired after the persistence manager has already
# deleted rows on disk; there is no user-meaningful in-memory change to
# undo, and snapshotting every prune would waste memory on a routine
# maintenance path. INSPECTOR_PURGED_STALE mutates transient
# component/modal dispatch-log buffers (not ``application`` slots) and
# is a devtools maintenance sweep that no user would want rewound.
_SKIP_ACTIONS: Set[str] = {
    "VIEW_CREATED",
    "VIEW_UPDATED",
    "VIEW_DESTROYED",
    "SESSION_CREATED",
    "NAVIGATION_REPLACE",
    "NAVIGATION_PUSH",
    "NAVIGATION_POP",
    "PERSISTENT_VIEW_REGISTERED",
    "PERSISTENT_VIEW_UNREGISTERED",
    "MODAL_SUBMITTED",
    "BATCH_COMPLETE",
    "UNDO",
    "REDO",
    "COMPONENT_INTERACTION",
    "INSPECTOR_PURGED_STALE",
    "APPLICATION_SLOTS_PRUNED",
    "REGISTRY_PRUNED",
}


# // ========================================( Diff Helpers )======================================== // #


def _diff_application_slots(pre: dict, post: dict) -> Dict[str, Any]:
    """Return per-slot undo diff of what pre-state values need restoring.

    For each top-level slot name that differs between ``pre`` and
    ``post``:

    - Slot present in ``pre`` only (deleted by action): diff maps the
      name to a deepcopy of the pre-value. UNDO restores it.
    - Slot present in ``post`` only (added by action): diff maps the
      name to ``_MISSING``. UNDO pops it.
    - Slot in both but value changed: diff maps the name to a deepcopy
      of the pre-value. UNDO overwrites.
    - Slot unchanged: omitted entirely. Other views' concurrent writes
      to their own slots survive this view's undo.

    The pairing with :func:`_diff_current_slots` in the reducer closes
    the loop: when UNDO applies this diff, it captures the current
    post-values for the same slot names into a redo diff, so REDO can
    restore the post-state per-slot without clobbering siblings.
    """
    diff: Dict[str, Any] = {}
    for name in set(pre) | set(post):
        pre_val = pre.get(name, _MISSING)
        post_val = post.get(name, _MISSING)
        # Reducers shallow-spread, so a slot the action did not touch is the
        # SAME object on both sides. Dict equality has no identity shortcut,
        # so without this every undo-tracked action deep-compared the whole
        # application namespace to prove nothing had changed in it.
        if pre_val is post_val:
            continue
        if pre_val is _MISSING:
            diff[name] = _MISSING
        elif post_val is _MISSING:
            diff[name] = copy.deepcopy(pre_val)
        elif pre_val != post_val:
            diff[name] = copy.deepcopy(pre_val)
    return diff


# // ========================================( Middleware )======================================== // #


class UndoMiddleware:
    """Middleware that captures state snapshots for undo/redo support.

    Only captures snapshots for views that have ``enable_undo = True``.
    Batched actions produce a single undo entry: a diff is captured per
    action while the batch is open and merged first-write-wins when it
    commits, so the entry restores the state the batch started from.

    Usage:
        from cascadeui import setup_middleware
        from cascadeui.state.middleware import UndoMiddleware

        await setup_middleware(UndoMiddleware())
    """

    def __init__(self) -> None:
        self._store = None

    async def initialize(self, store) -> None:
        """Bind the middleware to its store. Idempotent."""
        self._store = store

    async def __call__(self, action: Action, state: StateData, next_fn: Callable) -> StateData:
        """Snapshot a per-slot application diff plus session shared_data.

        Captures pre-state references before the chain runs, then diffs
        against the post-reducer state so only slots this action
        actually touched are recorded. Sibling slots written by other
        views in parallel dispatches survive this view's undo path.
        """
        action_type = action["type"]
        source_id = action.get("source")

        should_snapshot = False

        if action_type not in _SKIP_ACTIONS and source_id:
            should_snapshot = self._source_has_undo(source_id)

        pre_application: Optional[dict] = None
        pre_shared: Optional[dict] = None
        pre_session_id: Optional[str] = None
        if should_snapshot:
            # Hold the pre-state reference by identity -- reducers
            # shallow-spread, so the nested application dict survives
            # reducer execution unmutated and the diff pass can read it
            # back for comparison with the post-state.
            pre_application = state.get("application", {})
            pre_session_id = self._find_session_id(source_id, state)
            if pre_session_id:
                session = state.get("sessions", {}).get(pre_session_id, {})
                pre_shared = copy.deepcopy(session.get("shared_data", {}))
            else:
                pre_shared = {}

        result = await next_fn(action, state)

        if pre_application is not None and source_id:
            post_application = result.get("application", {})
            diff = _diff_application_slots(pre_application, post_application)
            batch = current_batch()
            if batch is not None and batch.add_undo_record(
                source_id, diff, pre_session_id, pre_shared, post_application
            ):
                # Inside a batch the push waits for the commit, so one entry
                # lands on the view's stack instead of one per action. The
                # diff is taken here because this frame is the only one
                # holding the pre-state for this action. A refused record
                # means the batch closed under a suspended chain, so the
                # snapshot is pushed below rather than dropped.
                return result
            if not diff and self._restores_nothing(pre_shared, pre_session_id, result):
                # An entry that can put nothing back still occupies a slot on
                # a bounded stack, so a run of actions that write no slot
                # evicts the history a user actually wants to reach: with
                # undo_limit at 3, four of them clear it. Skipping applies
                # only when the shared_data is unchanged as well, since an
                # empty diff on its own says nothing about the session.
                return result
            snapshot = {
                "application_slots": diff,
                "shared_data": pre_shared if pre_shared is not None else {},
            }
            limit = self._get_undo_limit(source_id)
            new_views = self._views_with_undo_pushed(result, source_id, snapshot, limit)
            if new_views is not None:
                # Writing the rebuilt ``views`` mapping onto ``result`` is the
                # idiomatic middleware transform. What makes it safe is not
                # that ``result`` is always a fresh dict -- a reducer that
                # raised leaves ``run_reducer`` without a new state to bind, so
                # ``result`` is then the live previous state and this writes
                # into it. It is safe because the helper rebuilds the mapping
                # and every view/undo_stack dict inside it, so no structure
                # another holder can see is mutated in place.
                result["views"] = new_views

        return result

    def _source_has_undo(self, source_id: str) -> bool:
        """Check if the source view has enable_undo set."""
        return source_id in self._store._undo_enabled_views

    def _get_undo_limit(self, source_id: str) -> int:
        """Get the undo stack limit for a given view."""
        return self._store._undo_enabled_views.get(source_id, 20)

    def _find_session_id(self, source_id: str, state: StateData = None) -> Optional[str]:
        """Find the session_id for a given view source_id."""
        target = state if state is not None else self._store.state
        views = target.get("views", {})
        view_data = views.get(source_id, {})
        return view_data.get("session_id")

    def _restores_nothing(
        self, pre_shared: Optional[dict], session_id: Optional[str], state: StateData
    ) -> bool:
        """Whether a snapshot with an empty slot diff would revert anything.

        Only the ``shared_data`` half is left to check, and the question is
        whether it *changed*, not whether it holds anything. Those come apart
        on the case that matters: an action creating a session's first
        shared_data records a pre-value of ``{}``, which is both falsy and
        exactly what an UNDO has to put back. Reading the emptiness instead of
        the change drops that entry and the creation becomes unrevertable.
        """
        post_shared: dict = {}
        if session_id:
            post_shared = state.get("sessions", {}).get(session_id, {}).get("shared_data", {})
        return (pre_shared if pre_shared is not None else {}) == post_shared

    def finalize_batch(
        self,
        records: List[
            Tuple[int, str, Dict[str, Any], Optional[str], Optional[dict], Dict[str, Any]]
        ],
    ) -> None:
        """Push a single per-slot undo diff onto each participating view's stack.

        Called by ``BatchContext.__aexit__`` on clean outermost commit with
        the per-action records captured while the batch was open, in
        reduction order. Slots merge first-write-wins, so the template holds
        each slot's value from before the batch first touched it, and every
        participating view receives that template because they all saw the
        same slot changes roll up at the same boundary.

        Diffing per action rather than entry-state against live state is what
        keeps concurrent batches apart: live state holds every open batch's
        writes, so a whole-window diff would put one batch's slots into
        another's undo entry and its UNDO would revert them.

        ``shared_data`` is cached per session because session-mates share a
        single shared_data timeline.
        """
        if not records:
            return

        diff_template: Dict[str, Any] = {}
        shared_data_cache: Dict[str, dict] = {}
        session_by_source: Dict[str, str] = {}

        for _sequence, source_id, diff, session_id, shared, _post in records:
            for name, value in diff.items():
                # First writer wins: the earliest record carries the value
                # from before the batch, which is what an UNDO restores.
                if name not in diff_template:
                    diff_template[name] = value
            cache_key = session_id or ""
            session_by_source.setdefault(source_id, cache_key)
            if cache_key not in shared_data_cache:
                shared_data_cache[cache_key] = shared if shared is not None else {}

        # Drop slots the batch wrote and then put back. Per-action diffs each
        # report a change, so a v0 -> tmp -> v0 sequence merges to "restore
        # v0" and an UNDO would then overwrite whatever a sibling view wrote
        # to that slot afterwards -- the cross-view contamination per-slot
        # diffs exist to prevent. Compared against the last record's own
        # post-state rather than live state, which holds every concurrent
        # batch's writes too.
        final_application = records[-1][5]
        for name, pre_value in list(diff_template.items()):
            present = name in final_application
            if pre_value is _MISSING:
                if not present:
                    del diff_template[name]
            elif present and final_application[name] == pre_value:
                del diff_template[name]

        live_state = self._store.state

        for source_id, cache_key in session_by_source.items():
            # Per-view copy of the diff so a future mutation of one
            # view's undo entry cannot corrupt another's. ``_MISSING``
            # is skipped past ``deepcopy`` deliberately: ``deepcopy`` on
            # a bare ``object()`` returns a new instance, which would
            # break the ``is _MISSING`` identity check in the reducer.
            view_diff = {
                name: value if value is _MISSING else copy.deepcopy(value)
                for name, value in diff_template.items()
            }
            if not view_diff and self._restores_nothing(
                shared_data_cache[cache_key], cache_key or None, live_state
            ):
                # Same reasoning as the per-dispatch path: a batch whose slot
                # writes all pruned, against unchanged shared_data, has
                # nothing to give back and must not push a bounded stack.
                continue
            snapshot = {
                "application_slots": view_diff,
                "shared_data": shared_data_cache[cache_key],
            }
            limit = self._get_undo_limit(source_id)
            new_views = self._views_with_undo_pushed(live_state, source_id, snapshot, limit)
            if new_views is not None:
                # Batch-commit bookkeeping runs after the batch's reducers
                # have committed and before BATCH_COMPLETE notifies, so there
                # is no middleware return to thread. Rebind the store's state
                # reference (the same reference-swap the dispatch path uses),
                # never an in-place key write on the live dict. ``live_state``
                # is rebound too so the next source_id reads this push.
                live_state = {**live_state, "views": new_views}
                self._store.state = live_state

    def _views_with_undo_pushed(
        self, state: StateData, view_id: str, snapshot: dict, limit: int
    ) -> Optional[dict]:
        """Return a fresh ``views`` mapping with ``snapshot`` pushed onto
        ``view_id``'s undo stack, or ``None`` when the view is absent.

        Pure: constructs fresh undo_stack / view / views dicts and returns
        the new ``views`` mapping without touching ``state`` or any nested
        structure. Reducers shallow-spread, so the input view dict and its
        undo_stack list may be shared with prior state versions; rebuilding
        avoids corrupting them. The caller decides how to apply the result:
        the dispatch path writes it onto its own freshly-reduced state
        (``result["views"] = ...``), the batch-commit path rebinds the live
        store reference (``store.state = {**state, "views": ...}``) rather
        than mutating the committed dict in place.
        """
        views = state.get("views", {})
        view = views.get(view_id)
        if view is None:
            return None

        undo_stack = view.get("undo_stack", [])
        new_undo_stack = [*undo_stack, snapshot]
        if len(new_undo_stack) > limit:
            new_undo_stack = new_undo_stack[-limit:]

        # Clear redo stack on new action (standard undo/redo behavior)
        new_view = {**view, "undo_stack": new_undo_stack, "redo_stack": []}

        logger.debug(
            f"Undo snapshot pushed for view {view_id} " f"(stack depth: {len(new_undo_stack)})"
        )
        return {**views, view_id: new_view}
