# // ========================================( Modules )======================================== // #


import copy
from typing import Callable, Optional, Set

from ..utils.logging import AsyncLogger
from .types import Action, StateData

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")

# Actions that should NOT create undo snapshots (internal lifecycle)
_SKIP_ACTIONS: Set[str] = {
    "VIEW_CREATED",
    "VIEW_UPDATED",
    "VIEW_DESTROYED",
    "SESSION_CREATED",
    "SESSION_UPDATED",
    "NAVIGATION_REPLACE",
    "NAVIGATION_PUSH",
    "NAVIGATION_POP",
    "PERSISTENT_VIEW_REGISTERED",
    "PERSISTENT_VIEW_UNREGISTERED",
    "MODAL_SUBMITTED",
    "BATCH_COMPLETE",
    "UNDO",
    "REDO",
    "SCOPED_UPDATE",
    "COMPONENT_INTERACTION",
}


# // ========================================( Middleware )======================================== // #


class UndoMiddleware:
    """Middleware that captures state snapshots for undo/redo support.

    Only captures snapshots for views that have ``enable_undo = True``.
    Batched actions produce a single undo entry (snapshot taken before
    the first action in the batch).

    Usage:
        from cascadeui.state.undo import UndoMiddleware

        store = get_store()
        store.add_middleware(UndoMiddleware(store))
    """

    def __init__(self, store):
        self._store = store

    async def __call__(self, action: Action, state: StateData, next_fn: Callable) -> StateData:
        """Snapshot application state before the reducer runs, if applicable."""
        action_type = action["type"]
        source_id = action.get("source")

        should_snapshot = False

        # Don't snapshot lifecycle/internal actions
        if action_type not in _SKIP_ACTIONS and source_id:
            should_snapshot = self._source_has_undo(source_id)

        snapshot = None
        if should_snapshot and not self._store._batching:
            snapshot = copy.deepcopy(state.get("application", {}))

        # Run the rest of the chain (including reducer)
        result = await next_fn(action, state)

        # Push snapshot onto the result state (not the live store state)
        # so undo stack changes flow through the same state object that
        # dispatch will assign to the store
        if snapshot is not None and source_id:
            session_id = self._find_session_id(source_id, result)
            if session_id:
                limit = self._get_undo_limit(source_id)
                self._push_undo(result, session_id, snapshot, limit)

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

    def _push_undo(self, state: StateData, session_id: str, snapshot: dict, limit: int):
        """Push a snapshot onto the undo stack in the given state dict."""
        sessions = state.get("sessions", {})
        session = sessions.get(session_id)
        if session is None:
            return

        if "undo_stack" not in session:
            session["undo_stack"] = []
        if "redo_stack" not in session:
            session["redo_stack"] = []

        session["undo_stack"].append(snapshot)
        if len(session["undo_stack"]) > limit:
            session["undo_stack"] = session["undo_stack"][-limit:]

        # Clear redo stack on new action (standard undo/redo behavior)
        session["redo_stack"] = []

        logger.debug(
            f"Undo snapshot pushed for session {session_id} "
            f"(stack depth: {len(session['undo_stack'])})"
        )
