# // ========================================( Modules )======================================== // #

import copy
from typing import Any, Dict

from .middleware.undo import _MISSING
from .types import Action, StateData

# // ========================================( Constants )======================================== // #

# Action types owned by the built-in reducers in this module. Registering a
# custom reducer for any of these via @cascade_reducer would silently shadow
# the library's own state machinery (sessions, navigation, undo stacks, etc.)
# and produce hard-to-trace breakage. The decorator raises ValueError at
# decoration time when a user attempts the collision -- see
# cascadeui/utils/decorators.py.
# Prune actions (APPLICATION_SLOTS_PRUNED, REGISTRY_PRUNED) are
# library-owned observability signals fired by the persistence manager
# after deleting rows on disk. The library ships no reducer for them --
# they are dispatch-only so subscribers and hooks can observe prunes.
# They are listed here so @cascade_reducer raises on collision; users
# who want to react to prunes should subscribe or use
# store.on("application_slots_pruned", ...) rather than shadowing the name.
_BUILTIN_REDUCER_ACTIONS = frozenset(
    {
        "VIEW_CREATED",
        "VIEW_UPDATED",
        "VIEW_DESTROYED",
        "SESSION_CREATED",
        "SESSION_UPDATED",
        "NAVIGATION_REPLACE",
        "NAVIGATION_PUSH",
        "NAVIGATION_POP",
        "SCOPED_UPDATE",
        "COMPONENT_INTERACTION",
        "MODAL_SUBMITTED",
        "PERSISTENT_VIEW_REGISTERED",
        "PERSISTENT_VIEW_UNREGISTERED",
        "UNDO",
        "REDO",
        "APPLICATION_SLOTS_PRUNED",
        "REGISTRY_PRUNED",
        "INSPECTOR_PURGED_STALE",
    }
)

# // ========================================( Coroutines )======================================== // #

#
# All reducers below follow the shallow-spread contract:
#
#   1. Return ``state`` unchanged (same identity) when the action is a no-op.
#   2. Otherwise return a new top-level dict, spreading intermediate dicts
#      and lists only along the mutation path.  Unchanged branches share
#      references with the input state.
#   3. Never mutate the input ``state`` or any nested structure reachable
#      from it.  The per-dispatch ``copy.deepcopy`` that used to gate this
#      contract is gone -- the shallow spread replaces it.
#
# User reducers registered through ``@cascade_reducer`` still receive a
# deep-copied ``state`` so the existing "mutate freely" contract is intact
# for user code.  The shallow-spread pattern here is an internal speedup.
#


async def reduce_view_created(action: Action, state: StateData) -> StateData:
    """Handle VIEW_CREATED actions."""
    payload = action["payload"]

    view_id = payload.get("view_id")
    if not view_id:
        return state

    views = state.get("views", {})
    new_view = {
        "id": view_id,
        "type": payload.get("view_type"),
        "user_id": payload.get("user_id"),
        "session_id": payload.get("session_id"),
        "created_at": action["timestamp"],
        "updated_at": action["timestamp"],
        "props": payload.get("props", {}),
        "message_id": payload.get("message_id"),
        "channel_id": payload.get("channel_id"),
    }
    new_views = {**views, view_id: new_view}

    new_state = {**state, "views": new_views}

    # Associate with session (spread session.members on write)
    session_id = payload.get("session_id")
    sessions = state.get("sessions", {})
    if session_id and session_id in sessions:
        session = sessions[session_id]
        members = session.get("members", [])
        if view_id not in members:
            new_session = {**session, "members": [*members, view_id]}
            new_state["sessions"] = {**sessions, session_id: new_session}

    return new_state


async def reduce_view_updated(action: Action, state: StateData) -> StateData:
    """Handle VIEW_UPDATED actions."""
    payload = action["payload"]

    view_id = payload.get("view_id")
    views = state.get("views", {})
    if not view_id or view_id not in views:
        return state

    old_view = views[view_id]
    new_view = {**old_view, "updated_at": action["timestamp"]}
    for key, value in payload.items():
        if key != "view_id":
            new_view[key] = value

    return {**state, "views": {**views, view_id: new_view}}


async def reduce_view_destroyed(action: Action, state: StateData) -> StateData:
    """Handle VIEW_DESTROYED actions."""
    payload = action["payload"]

    view_id = payload.get("view_id")
    views = state.get("views", {})
    if not view_id or view_id not in views:
        return state

    view_data = views[view_id]
    session_id = view_data.get("session_id")

    new_state = {**state}
    new_state["views"] = {k: v for k, v in views.items() if k != view_id}

    # Remove component interaction entries owned by this view
    components = state.get("components")
    if components:
        filtered = {cid: c for cid, c in components.items() if c.get("view_id") != view_id}
        if filtered:
            new_state["components"] = filtered
        else:
            new_state.pop("components", None)

    # Remove modal submission entries owned by this view
    modals = state.get("modals")
    if modals and view_id in modals:
        new_modals = {k: v for k, v in modals.items() if k != view_id}
        if new_modals:
            new_state["modals"] = new_modals
        else:
            new_state.pop("modals", None)

    # Remove from session if applicable
    if session_id:
        sessions = state.get("sessions", {})
        session = sessions.get(session_id)
        if session and view_id in session.get("members", []):
            new_members = [m for m in session["members"] if m != view_id]
            if new_members:
                new_session = {**session, "members": new_members}
                new_state["sessions"] = {**sessions, session_id: new_session}
            else:
                new_state["sessions"] = {k: v for k, v in sessions.items() if k != session_id}

    return new_state


async def reduce_session_created(action: Action, state: StateData) -> StateData:
    """Handle SESSION_CREATED actions."""
    payload = action["payload"]

    session_id = payload.get("session_id")
    if not session_id:
        return state

    sessions = state.get("sessions", {})
    # Duplicate session id -- no-op, preserve existing content
    if session_id in sessions:
        return state

    new_session = {
        "id": session_id,
        "user_id": payload.get("user_id"),
        "created_at": action["timestamp"],
        "updated_at": action["timestamp"],
        "members": [],
        "history": [],
        "shared_data": payload.get("shared_data", {}),
    }
    return {**state, "sessions": {**sessions, session_id: new_session}}


async def reduce_session_updated(action: Action, state: StateData) -> StateData:
    """Handle SESSION_UPDATED actions."""
    payload = action["payload"]

    session_id = payload.get("session_id")
    sessions = state.get("sessions", {})
    if not session_id or session_id not in sessions:
        return state

    old_session = sessions[session_id]
    new_session = {**old_session, "updated_at": action["timestamp"]}

    if "shared_data" in payload:
        new_session["shared_data"] = {
            **old_session.get("shared_data", {}),
            **payload["shared_data"],
        }

    return {**state, "sessions": {**sessions, session_id: new_session}}


async def reduce_navigation_replace(action: Action, state: StateData) -> StateData:
    """Handle NAVIGATION_REPLACE actions."""
    payload = action["payload"]

    source_id = action.get("source")
    dest_view_type = payload.get("destination")
    if not source_id or not dest_view_type:
        return state

    views = state.get("views", {})
    if source_id not in views:
        return state

    session_id = views[source_id].get("session_id")
    sessions = state.get("sessions", {})
    if not session_id or session_id not in sessions:
        return state

    old_session = sessions[session_id]
    history = old_session.get("history", [])
    new_event = {
        "from_view": source_id,
        "to_view_type": dest_view_type,
        "timestamp": action["timestamp"],
        "params": payload.get("params", {}),
    }
    new_session = {**old_session, "history": [*history, new_event]}
    return {**state, "sessions": {**sessions, session_id: new_session}}


async def reduce_component_interaction(action: Action, state: StateData) -> StateData:
    """Handle COMPONENT_INTERACTION actions."""
    payload = action["payload"]

    component_id = payload.get("component_id")
    view_id = payload.get("view_id")
    if not component_id or not view_id:
        return state

    components = state.get("components", {})
    existing = components.get(
        component_id,
        {
            "id": component_id,
            "view_id": view_id,
            "interactions": [],
        },
    )

    interactions = existing.get("interactions", [])
    new_interactions = [
        *interactions,
        {
            "user_id": payload.get("user_id"),
            "view_id": view_id,
            "value": payload.get("value"),
            "timestamp": action["timestamp"],
        },
    ]
    if len(new_interactions) > 50:
        new_interactions = new_interactions[-50:]

    new_component = {
        **existing,
        "interactions": new_interactions,
        "last_interaction": action["timestamp"],
    }

    return {**state, "components": {**components, component_id: new_component}}


async def reduce_modal_submitted(action: Action, state: StateData) -> StateData:
    """Handle MODAL_SUBMITTED actions."""
    payload = action["payload"]

    view_id = payload.get("view_id")
    if not view_id:
        return state

    modals = state.get("modals", {})
    existing = modals.get(view_id, {"submissions": []})

    submissions = existing.get("submissions", [])
    new_submissions = [
        *submissions,
        {
            "user_id": payload.get("user_id"),
            "values": payload.get("values", {}),
            "timestamp": action["timestamp"],
        },
    ]
    if len(new_submissions) > 50:
        new_submissions = new_submissions[-50:]

    new_modal = {
        **existing,
        "submissions": new_submissions,
        "last_submission": action["timestamp"],
    }

    return {**state, "modals": {**modals, view_id: new_modal}}


async def reduce_persistent_view_registered(action: Action, state: StateData) -> StateData:
    """Handle PERSISTENT_VIEW_REGISTERED actions."""
    payload = action["payload"]

    persistence_key = payload.get("persistence_key")
    if not persistence_key:
        return state

    persistent_views = state.get("persistent_views", {})
    new_entry = {
        "persistence_key": persistence_key,
        "class_name": payload.get("class_name"),
        "message_id": payload.get("message_id"),
        "channel_id": payload.get("channel_id"),
        "guild_id": payload.get("guild_id"),
        "user_id": payload.get("user_id"),
        "registered_at": action["timestamp"],
    }
    return {**state, "persistent_views": {**persistent_views, persistence_key: new_entry}}


async def reduce_persistent_view_unregistered(action: Action, state: StateData) -> StateData:
    """Handle PERSISTENT_VIEW_UNREGISTERED actions."""
    payload = action["payload"]

    persistence_key = payload.get("persistence_key")
    if not persistence_key:
        return state

    persistent_views = state.get("persistent_views", {})
    if persistence_key not in persistent_views:
        return state

    return {
        **state,
        "persistent_views": {k: v for k, v in persistent_views.items() if k != persistence_key},
    }


async def reduce_inspector_purged_stale(action: Action, state: StateData) -> StateData:
    """Handle INSPECTOR_PURGED_STALE -- drop component/modal entries not owned by the inspector.

    The DevTools inspector self-filters its own data from displayed aggregates so
    its observation does not poison the state it reports.  Purge sweeps the
    component/modal slots clean of stale (non-inspector) entries while keeping
    the inspector's own live entries intact -- the same self-filter property
    enforced by the read-side helpers in ``devtools.py``.

    ``inspector_id`` present and non-None preserves that inspector's rows;
    ``inspector_id`` present and None (CLI sledgehammer path) purges everything
    -- no row can match ``view_id == None`` in a well-formed state tree.
    Missing ``inspector_id`` key short-circuits as a defensive no-op against
    malformed payloads.
    """
    payload = action["payload"]

    if "inspector_id" not in payload:
        return state

    inspector_id = payload["inspector_id"]
    new_state = {**state}

    components = state.get("components")
    if components:
        kept = {cid: c for cid, c in components.items() if c.get("view_id") == inspector_id}
        if kept:
            new_state["components"] = kept
        else:
            new_state.pop("components", None)

    modals = state.get("modals")
    if modals:
        kept_modals = {k: v for k, v in modals.items() if k == inspector_id}
        if kept_modals:
            new_state["modals"] = kept_modals
        else:
            new_state.pop("modals", None)

    return new_state


# // ========================================( Navigation Stack )======================================== // #


async def reduce_navigation_push(action: Action, state: StateData) -> StateData:
    """Handle NAVIGATION_PUSH -- no-op reducer.

    Navigation stack is view-local (transferred at the Python object level
    by ``_navigate_to``).  The dispatch still fires for middleware and
    subscriber notification.
    """
    return state


async def reduce_navigation_pop(action: Action, state: StateData) -> StateData:
    """Handle NAVIGATION_POP -- no-op reducer.

    Navigation stack is view-local (transferred at the Python object level
    by ``_navigate_to``).  The dispatch still fires for middleware and
    subscriber notification.
    """
    return state


# // ========================================( State Scoping )======================================== // #


async def reduce_scoped_update(action: Action, state: StateData) -> StateData:
    """Handle SCOPED_UPDATE -- merge data into a scoped state slice.

    Delegates key construction to ``StateStore._build_scope_key`` so the
    write path and read path stay in sync. Returns state unchanged when the
    payload is malformed (missing scope / bad identifiers) rather than
    writing an unreachable key.
    """
    from .slots import read_slot
    from .store import StateStore  # lazy to avoid circular import

    payload = action["payload"]
    scope = payload.get("scope")
    if not scope:
        return state

    identifiers = payload.get("identifiers", {})
    data = payload.get("data", {})
    slot_name = payload.get("slot_name", "scoped")

    try:
        scope_key = StateStore._build_scope_key(scope, **identifiers)
    except ValueError:
        return state

    old_bucket = read_slot(state, slot_name)
    existing = old_bucket.get(scope_key, {})
    new_bucket = {**old_bucket, scope_key: {**existing, **data}}
    old_application = state.get("application", {})
    new_application = {**old_application, slot_name: new_bucket}

    return {**state, "application": new_application}


# // ========================================( Undo / Redo )======================================== // #


def _apply_slot_diff(application: dict, diff: Dict[str, Any]) -> dict:
    """Apply a per-slot diff to an application dict and return a new dict.

    Values mapped to ``_MISSING`` delete the slot; any other value
    replaces the slot wholesale. Slots absent from ``diff`` carry
    through unchanged so sibling views' concurrent writes survive.
    """
    new_application = dict(application)
    for name, target_value in diff.items():
        if target_value is _MISSING:
            new_application.pop(name, None)
        else:
            new_application[name] = target_value
    return new_application


def _build_inverse_diff(current_application: dict, diff_keys: Any) -> Dict[str, Any]:
    """Build the inverse diff from the current application for the same slot names.

    The inverse of applying ``diff`` is "restore these slots to their
    current values (or delete if currently absent)." Captures current
    slot values by deepcopy so the inverse diff is self-contained, and
    maps absent slots to ``_MISSING`` so the symmetric UNDO<->REDO
    round-trip re-deletes slots that were added after the partner
    action.
    """
    inverse: Dict[str, Any] = {}
    for name in diff_keys:
        current_val = current_application.get(name, _MISSING)
        if current_val is _MISSING:
            inverse[name] = _MISSING
        else:
            inverse[name] = copy.deepcopy(current_val)
    return inverse


async def reduce_undo(action: Action, state: StateData) -> StateData:
    """Handle UNDO -- pop view's undo stack, apply per-slot diff, push inverse to redo."""
    payload = action["payload"]

    view_id = payload.get("view_id")
    session_id = payload.get("session_id")
    views = state.get("views", {})
    if not view_id or view_id not in views:
        return state

    view = views[view_id]
    undo_stack = view.get("undo_stack", [])
    if not undo_stack:
        return state

    sessions = state.get("sessions", {})
    session = sessions.get(session_id) if session_id else None

    snapshot = undo_stack[-1]
    new_undo_stack = undo_stack[:-1]
    undo_diff: Dict[str, Any] = snapshot.get("application_slots", {})

    current_application = state.get("application", {})
    current_shared = session.get("shared_data", {}) if session else {}

    redo_diff = _build_inverse_diff(current_application, undo_diff.keys())
    redo_snapshot = {
        "application_slots": redo_diff,
        "shared_data": copy.deepcopy(current_shared),
    }
    redo_stack = view.get("redo_stack", [])
    new_redo_stack = [*redo_stack, redo_snapshot]

    new_application = _apply_slot_diff(current_application, undo_diff)

    new_view = {**view, "undo_stack": new_undo_stack, "redo_stack": new_redo_stack}
    new_state = {
        **state,
        "views": {**views, view_id: new_view},
        "application": new_application,
    }

    if session is not None:
        new_session = {**session, "shared_data": snapshot.get("shared_data", {})}
        new_state["sessions"] = {**sessions, session_id: new_session}

    return new_state


async def reduce_redo(action: Action, state: StateData) -> StateData:
    """Handle REDO -- pop view's redo stack, apply per-slot diff, push inverse to undo."""
    payload = action["payload"]

    view_id = payload.get("view_id")
    session_id = payload.get("session_id")
    views = state.get("views", {})
    if not view_id or view_id not in views:
        return state

    view = views[view_id]
    redo_stack = view.get("redo_stack", [])
    if not redo_stack:
        return state

    sessions = state.get("sessions", {})
    session = sessions.get(session_id) if session_id else None

    snapshot = redo_stack[-1]
    new_redo_stack = redo_stack[:-1]
    redo_diff: Dict[str, Any] = snapshot.get("application_slots", {})

    current_application = state.get("application", {})
    current_shared = session.get("shared_data", {}) if session else {}

    undo_diff = _build_inverse_diff(current_application, redo_diff.keys())
    undo_snapshot = {
        "application_slots": undo_diff,
        "shared_data": copy.deepcopy(current_shared),
    }
    undo_stack = view.get("undo_stack", [])
    new_undo_stack = [*undo_stack, undo_snapshot]

    new_application = _apply_slot_diff(current_application, redo_diff)

    new_view = {**view, "undo_stack": new_undo_stack, "redo_stack": new_redo_stack}
    new_state = {
        **state,
        "views": {**views, view_id: new_view},
        "application": new_application,
    }

    if session is not None:
        new_session = {**session, "shared_data": snapshot.get("shared_data", {})}
        new_state["sessions"] = {**sessions, session_id: new_session}

    return new_state
