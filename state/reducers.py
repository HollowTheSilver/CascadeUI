
# // ========================================( Modules )======================================== // #

import copy
from typing import Dict, Any

from .types import Action, StateData

# // ========================================( Coroutines )======================================== // #


async def reduce_view_created(action: Action, state: StateData) -> StateData:
    """Handle VIEW_CREATED actions."""
    new_state = copy.deepcopy(state)
    payload = action["payload"]

    view_id = payload.get("view_id")
    if not view_id:
        return state

    # Initialize views if needed
    if "views" not in new_state:
        new_state["views"] = {}

    # Add the new view
    new_state["views"][view_id] = {
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

    # Associate with session
    session_id = payload.get("session_id")
    if session_id and "sessions" in new_state and session_id in new_state["sessions"]:
        if "views" not in new_state["sessions"][session_id]:
            new_state["sessions"][session_id]["views"] = []

        if view_id not in new_state["sessions"][session_id]["views"]:
            new_state["sessions"][session_id]["views"].append(view_id)

    return new_state


async def reduce_view_updated(action: Action, state: StateData) -> StateData:
    """Handle VIEW_UPDATED actions."""
    new_state = copy.deepcopy(state)
    payload = action["payload"]

    view_id = payload.get("view_id")
    if not view_id or "views" not in new_state or view_id not in new_state["views"]:
        return state

    # Update the view
    view_data = new_state["views"][view_id]
    view_data["updated_at"] = action["timestamp"]

    # Update individual fields
    for key, value in payload.items():
        if key != "view_id":
            view_data[key] = value

    return new_state


async def reduce_view_destroyed(action: Action, state: StateData) -> StateData:
    """Handle VIEW_DESTROYED actions."""
    new_state = copy.deepcopy(state)
    payload = action["payload"]

    view_id = payload.get("view_id")
    if not view_id or "views" not in new_state or view_id not in new_state["views"]:
        return state

    # Get session info before removing view
    view_data = new_state["views"][view_id]
    session_id = view_data.get("session_id")

    # Remove view from state
    del new_state["views"][view_id]

    # Remove from session if applicable
    if session_id and "sessions" in new_state and session_id in new_state["sessions"]:
        if "views" in new_state["sessions"][session_id]:
            if view_id in new_state["sessions"][session_id]["views"]:
                new_state["sessions"][session_id]["views"].remove(view_id)

    return new_state


async def reduce_session_created(action: Action, state: StateData) -> StateData:
    """Handle SESSION_CREATED actions."""
    new_state = copy.deepcopy(state)
    payload = action["payload"]

    session_id = payload.get("session_id")
    if not session_id:
        return state

    # Initialize sessions if needed
    if "sessions" not in new_state:
        new_state["sessions"] = {}

    # Only create if session doesn't exist already
    if session_id not in new_state["sessions"]:
        # Add the new session
        new_state["sessions"][session_id] = {
            "id": session_id,
            "user_id": payload.get("user_id"),
            "created_at": action["timestamp"],
            "updated_at": action["timestamp"],
            "views": [],
            "history": [],
            "data": payload.get("data", {}),
        }

    return new_state


async def reduce_session_updated(action: Action, state: StateData) -> StateData:
    """Handle SESSION_UPDATED actions."""
    new_state = copy.deepcopy(state)
    payload = action["payload"]

    session_id = payload.get("session_id")
    if not session_id or "sessions" not in new_state or session_id not in new_state["sessions"]:
        return state

    # Update the session
    session_data = new_state["sessions"][session_id]
    session_data["updated_at"] = action["timestamp"]

    # Update data field if provided
    if "data" in payload:
        session_data["data"] = {
            **session_data.get("data", {}),
            **payload["data"]
        }

    return new_state


async def reduce_navigation(action: Action, state: StateData) -> StateData:
    """Handle NAVIGATION actions."""
    new_state = copy.deepcopy(state)
    payload = action["payload"]

    source_id = action.get("source")
    dest_view_type = payload.get("destination")

    if not source_id or not dest_view_type:
        return state

    # Update navigation history
    if "views" in new_state and source_id in new_state["views"]:
        view_data = new_state["views"][source_id]
        session_id = view_data.get("session_id")

        if session_id and "sessions" in new_state and session_id in new_state["sessions"]:
            session = new_state["sessions"][session_id]

            # Initialize history if needed
            if "history" not in session:
                session["history"] = []

            # Add navigation event
            session["history"].append({
                "from_view": source_id,
                "to_view_type": dest_view_type,
                "timestamp": action["timestamp"],
                "params": payload.get("params", {})
            })

    return new_state


async def reduce_component_interaction(action: Action, state: StateData) -> StateData:
    """Handle COMPONENT_INTERACTION actions."""
    new_state = copy.deepcopy(state)
    payload = action["payload"]

    component_id = payload.get("component_id")
    view_id = payload.get("view_id")

    if not component_id or not view_id:
        return state

    # Initialize components if needed
    if "components" not in new_state:
        new_state["components"] = {}

    # Record the interaction
    if component_id not in new_state["components"]:
        new_state["components"][component_id] = {
            "id": component_id,
            "interactions": []
        }

    # Add the interaction
    new_state["components"][component_id]["interactions"].append({
        "user_id": payload.get("user_id"),
        "view_id": view_id,
        "value": payload.get("value"),
        "timestamp": action["timestamp"]
    })

    # Update the last interaction timestamp
    new_state["components"][component_id]["last_interaction"] = action["timestamp"]

    return new_state
