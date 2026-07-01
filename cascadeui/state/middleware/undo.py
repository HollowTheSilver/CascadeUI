# // ========================================( Modules )======================================== // #


import copy
import logging
from typing import Any, Callable, Dict, List, Optional, Set

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
    Batched actions produce a single undo entry (snapshot taken before
    the first action in the batch).

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
        if should_snapshot and not self._store._batching:
            # Hold the pre-state reference by identity -- reducers
            # shallow-spread, so the nested application dict survives
            # reducer execution unmutated and the diff pass can read it
            # back for comparison with the post-state.
            pre_application = state.get("application", {})
            session_id = self._find_session_id(source_id, state)
            if session_id:
                session = state.get("sessions", {}).get(session_id, {})
                pre_shared = copy.deepcopy(session.get("shared_data", {}))
            else:
                pre_shared = {}

        result = await next_fn(action, state)

        if pre_application is not None and source_id:
            post_application = result.get("application", {})
            diff = _diff_application_slots(pre_application, post_application)
            snapshot = {
                "application_slots": diff,
                "shared_data": pre_shared if pre_shared is not None else {},
            }
            limit = self._get_undo_limit(source_id)
            new_views = self._views_with_undo_pushed(result, source_id, snapshot, limit)
            if new_views is not None:
                # ``result`` is this dispatch's freshly-reduced state (the
                # store has already bound it to ``self.state`` in run_reducer),
                # so writing the rebuilt ``views`` mapping onto it is the
                # idiomatic middleware transform -- the nested view/undo_stack
                # dicts are fresh (built by the helper), not shared structures.
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

    def finalize_batch(self, pre_batch_state: StateData, actions: List[Action]) -> None:
        """Push a single per-slot undo diff onto each participating view's stack.

        Called by ``BatchContext.__aexit__`` on clean outermost commit.
        Diffs ``pre_batch_state.application`` against the post-batch
        ``self._store.state.application`` once, then shares the diff
        across every participating view because every view in this batch
        saw the same set of slot changes roll up at the same boundary.

        ``shared_data`` is cached per session because session-mates
        share a single shared_data timeline.
        """
        source_ids: Set[str] = set()
        for action in actions:
            if action["type"] in _SKIP_ACTIONS:
                continue
            sid = action.get("source")
            if sid and self._source_has_undo(sid):
                source_ids.add(sid)

        if not source_ids:
            return

        live_state = self._store.state
        pre_application = pre_batch_state.get("application", {})
        post_application = live_state.get("application", {})
        diff_template = _diff_application_slots(pre_application, post_application)

        shared_data_cache: Dict[str, dict] = {}

        for source_id in source_ids:
            session_id = self._find_session_id(source_id, pre_batch_state)
            cache_key = session_id or ""
            if cache_key not in shared_data_cache:
                session = pre_batch_state.get("sessions", {}).get(cache_key, {})
                shared_data_cache[cache_key] = copy.deepcopy(session.get("shared_data", {}))
            # Per-view copy of the diff so a future mutation of one
            # view's undo entry cannot corrupt another's. ``_MISSING``
            # is skipped past ``deepcopy`` deliberately: ``deepcopy`` on
            # a bare ``object()`` returns a new instance, which would
            # break the ``is _MISSING`` identity check in the reducer.
            view_diff = {
                name: value if value is _MISSING else copy.deepcopy(value)
                for name, value in diff_template.items()
            }
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
