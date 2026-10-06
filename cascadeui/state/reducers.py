# // ========================================( Modules )======================================== // #

import copy
from typing import Any, Dict

from ._batching import next_commit_sequence
from .middleware.undo import _MISSING, _KeyedDiff
from .types import Action, StateData

# // ========================================( Constants )======================================== // #

# Action types the library owns; @cascade_reducer raises ValueError on any of
# them rather than let a custom reducer shadow the library's own. REGISTRY_PRUNED
# reduces, keeping the persistent_views mirror current; APPLICATION_SLOTS_PRUNED
# is dispatch-only, since the slots it names have left memory before it is sent.
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
        # Synthetic: the batch commit fires it straight at subscribers and
        # hooks, bypassing dispatch, so a reducer registered for it would
        # never run. Reserved so the decorator rejects it rather than
        # accepting a registration that does nothing.
        "BATCH_COMPLETE",
    }
)

# // ========================================( Coroutines )======================================== // #

# These reducers never mutate their input, which nothing deep-copies first: a
# no-op returns ``state`` itself, anything else a new dict that spreads only the
# dicts and lists on the mutation path. Reducers registered with
# ``@cascade_reducer`` still receive a deep copy they may mutate.


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
        "guild_id": payload.get("guild_id"),
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
    if not view_id:
        return state
    if view_id not in state.get("views", {}):
        return _without_empty_session(state, payload.get("session_id"))
    return _without_views(state, {view_id})


def _without_empty_session(state: StateData, session_id) -> StateData:
    """``state`` without session ``session_id`` when it has no member."""
    session = state.get("sessions", {}).get(session_id) if session_id else None
    if session is None or session.get("members"):
        return state
    sessions = {key: value for key, value in state["sessions"].items() if key != session_id}
    return {**state, "sessions": sessions}


def _without_views(state: StateData, view_ids) -> StateData:
    """``state`` without the given views, their component and modal entries, and
    their session membership; a session left with no member goes too."""
    ids = set(view_ids)
    views = state.get("views", {})
    gone = {view_id for view_id in ids if view_id in views}
    # An interaction recorded for a view whose row is already gone can still
    # leave an entry.
    modals = state.get("modals")
    stale_modals = bool(modals) and not ids.isdisjoint(modals)
    components = state.get("components")
    stale_components = bool(components) and any(
        c.get("view_id") in ids for c in components.values()
    )
    if not gone and not stale_modals and not stale_components:
        return state

    new_state = {**state}
    new_state["views"] = {k: v for k, v in views.items() if k not in gone}

    # Remove component interaction entries owned by these views
    if components:
        # Empty, not absent: ``components`` is a key _build_initial_state
        # establishes, and dropping it leaves the state shape depending on
        # whether a view happened to be destroyed.
        new_state["components"] = {
            cid: c for cid, c in components.items() if c.get("view_id") not in ids
        }

    # Remove modal submission entries owned by these views
    if stale_modals:
        new_modals = {k: v for k, v in modals.items() if k not in ids}
        if new_modals:
            new_state["modals"] = new_modals
        else:
            new_state.pop("modals", None)

    # Remove them from their sessions
    sessions = state.get("sessions", {})
    new_sessions = None
    for session_id in {views[view_id].get("session_id") for view_id in gone}:
        session = sessions.get(session_id) if session_id else None
        if not session:
            continue
        members = session.get("members", [])
        kept = [m for m in members if m not in gone]
        if len(kept) == len(members):
            continue
        if new_sessions is None:
            new_sessions = dict(sessions)
        if kept:
            new_sessions[session_id] = {**session, "members": kept}
        else:
            del new_sessions[session_id]
    if new_sessions is not None:
        new_state["sessions"] = new_sessions

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
        "guild_id": payload.get("guild_id"),
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


async def reduce_registry_pruned(action: Action, state: StateData) -> StateData:
    """Handle REGISTRY_PRUNED actions.

    A prune deletes stored rows, so the store's mirror of the registry is
    stale until it drops the same keys. Leaving it stale is not inert: the
    duplicate-key cleanup in ``_PersistentMixin._register_persistent`` reads
    this mapping, and a pruned key still listed there sends it down the
    orphan branch, which freezes or deletes the very message a caller pruned
    the row to leave standing.

    Reducing does not stop a subscriber or hook from observing the prune.
    Reducers run inside the middleware chain and both notification passes
    run after it, so the payload reaches every listener either way.
    """
    payload = action["payload"]

    keys = payload.get("keys") or []
    if not keys:
        return state

    persistent_views = state.get("persistent_views", {})
    pruned = set(keys)
    if not pruned & set(persistent_views):
        return state

    return {
        **state,
        "persistent_views": {k: v for k, v in persistent_views.items() if k not in pruned},
    }


async def reduce_persistent_view_unregistered(action: Action, state: StateData) -> StateData:
    """Handle PERSISTENT_VIEW_UNREGISTERED actions."""
    payload = action["payload"]

    persistence_key = payload.get("persistence_key")
    if not persistence_key:
        return state

    persistent_views = state.get("persistent_views", {})
    entry = persistent_views.get(persistence_key)
    if entry is None:
        return state
    # A superseded panel exiting must not take its successor's registration
    # with it: the key now points at a different message.
    message_id = payload.get("message_id")
    if message_id is not None and entry.get("message_id") not in (None, message_id):
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
        new_state["components"] = kept

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

    Values mapped to ``_MISSING`` delete the slot; a ``_KeyedDiff`` sets
    or deletes only the keys it names inside the slot, at any depth; any
    other value replaces the slot wholesale. Slots and keys absent from
    ``diff`` carry through unchanged so sibling views' concurrent writes
    survive.
    """
    return _apply_keyed(application, diff)


def _apply_keyed(target: dict, diff: Dict[str, Any]) -> dict:
    """``target`` with the entries of ``diff`` applied, as a new dict."""
    result = dict(target)
    for key, value in diff.items():
        if isinstance(value, _KeyedDiff):
            current = result.get(key, _MISSING)
            if current is not _MISSING and not isinstance(current, dict):
                # Another writer has since replaced the dict these keys lived
                # in; its value is kept.
                continue
            nested = _apply_keyed({} if current is _MISSING else current, value)
            if value.created and not nested:
                result.pop(key, None)
            else:
                result[key] = nested
        elif value is _MISSING:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def _build_inverse_diff(current_application: dict, diff: Dict[str, Any]) -> Dict[str, Any]:
    """Build the inverse of ``diff`` from the current application.

    The inverse of applying ``diff`` is "restore what it names to its
    current value (or delete it if currently absent)": whole slots for a
    slot entry, and only the named keys for a ``_KeyedDiff``. Captures
    current values by deepcopy so the inverse diff is self-contained, and
    maps absent slots and keys to ``_MISSING`` so the symmetric UNDO<->REDO
    round-trip re-deletes what was added after the partner action.
    """
    return dict(_invert_keyed(current_application, diff))


def _invert_keyed(current: Any, diff: Dict[str, Any]) -> _KeyedDiff:
    """What restores the entries ``diff`` names to their values in ``current``."""
    source = current if isinstance(current, dict) else {}
    inverse = _KeyedDiff(created=current is _MISSING)
    for key, entry in diff.items():
        now = source.get(key, _MISSING)
        if isinstance(entry, _KeyedDiff):
            inverse[key] = _invert_keyed(now, entry)
        else:
            inverse[key] = _MISSING if now is _MISSING else copy.deepcopy(now)
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

    redo_diff = _build_inverse_diff(current_application, undo_diff)
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

    undo_diff = _build_inverse_diff(current_application, redo_diff)
    # A redo is a new change: stamped like one, so a batch that began after it
    # places its own entry above this one when it ends.
    undo_snapshot = {
        "application_slots": undo_diff,
        "shared_data": copy.deepcopy(current_shared),
        "seq": next_commit_sequence(),
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
