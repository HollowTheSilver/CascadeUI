# // ========================================( Modules )======================================== // #


from typing import Any, Dict, List, Optional

from .types import ComponentId, GuildId, SessionId, UserId, ViewId

# Type alias for action payloads
ActionPayload = Dict[str, Any]


# // ========================================( Classes )======================================== // #


class ActionCreators:
    """Helper methods to create standardized actions."""

    @staticmethod
    def view_created(
        view_id: ViewId,
        view_type: str,
        user_id: Optional[UserId] = None,
        session_id: Optional[SessionId] = None,
        guild_id: GuildId = None,
        **props,
    ) -> ActionPayload:
        """Create a VIEW_CREATED action payload."""
        return {
            "view_id": view_id,
            "view_type": view_type,
            "user_id": user_id,
            "session_id": session_id,
            "guild_id": guild_id,
            "props": props,
        }

    @staticmethod
    def view_updated(view_id: ViewId, **updates) -> ActionPayload:
        """Create a VIEW_UPDATED action payload."""
        return {"view_id": view_id, **updates}

    @staticmethod
    def view_destroyed(view_id: ViewId) -> ActionPayload:
        """Create a VIEW_DESTROYED action payload."""
        return {"view_id": view_id}

    @staticmethod
    def session_created(
        session_id: SessionId,
        user_id: Optional[UserId] = None,
        guild_id: GuildId = None,
        **data,
    ) -> ActionPayload:
        """Create a SESSION_CREATED action payload."""
        return {
            "session_id": session_id,
            "user_id": user_id,
            "guild_id": guild_id,
            "shared_data": data,
        }

    @staticmethod
    def session_updated(session_id: SessionId, **data) -> ActionPayload:
        """Create a SESSION_UPDATED action payload."""
        return {"session_id": session_id, "shared_data": data}

    @staticmethod
    def navigation_replace(destination: str, **params) -> ActionPayload:
        """Create a NAVIGATION_REPLACE action payload."""
        return {"destination": destination, "params": params}

    @staticmethod
    def component_interaction(
        component_id: ComponentId, view_id: ViewId, user_id: Optional[UserId] = None, **values
    ) -> ActionPayload:
        """Create a COMPONENT_INTERACTION action payload."""
        return {
            "component_id": component_id,
            "view_id": view_id,
            "user_id": user_id,
            "value": values,
        }

    @staticmethod
    def persistent_view_registered(
        persistence_key: str,
        class_name: str,
        message_id: str,
        channel_id: str,
        guild_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> ActionPayload:
        """Create a PERSISTENT_VIEW_REGISTERED action payload."""
        return {
            "persistence_key": persistence_key,
            "class_name": class_name,
            "message_id": message_id,
            "channel_id": channel_id,
            "guild_id": guild_id,
            "user_id": user_id,
        }

    @staticmethod
    def persistent_view_unregistered(persistence_key: str) -> ActionPayload:
        """Create a PERSISTENT_VIEW_UNREGISTERED action payload."""
        return {
            "persistence_key": persistence_key,
        }

    @staticmethod
    def navigation_push(
        session_id: SessionId,
        class_name: str,
        module: Optional[str] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        state_snapshot: Optional[Any] = None,
    ) -> ActionPayload:
        """Create a NAVIGATION_PUSH action payload."""
        return {
            "session_id": session_id,
            "class_name": class_name,
            "module": module,
            "kwargs": kwargs or {},
            "state_snapshot": state_snapshot,
        }

    @staticmethod
    def navigation_pop(session_id: SessionId) -> ActionPayload:
        """Create a NAVIGATION_POP action payload."""
        return {
            "session_id": session_id,
        }

    @staticmethod
    def application_slots_pruned(deleted: int, cutoff: Optional[int] = None) -> ActionPayload:
        """Create an APPLICATION_SLOTS_PRUNED action payload.

        Fires after the persistence manager deletes expired rows from the
        application_slots namespace. ``deleted`` is the row count removed;
        ``cutoff`` is the ``expires_at`` threshold used (epoch seconds) or
        ``None`` if prune was manual.
        """
        return {"deleted": deleted, "cutoff": cutoff}

    @staticmethod
    def registry_pruned(
        deleted: int, reason: str, keys: Optional[List[str]] = None
    ) -> ActionPayload:
        """Create a REGISTRY_PRUNED action payload.

        Fires after the persistence manager deletes rows from the
        persistent_views namespace. ``deleted`` is the row count removed,
        ``keys`` is the list of ``persistence_key`` values actually pruned (so
        a subscriber can reconcile its own records surgically), and ``reason``
        is a short tag (``"explicit"`` for a targeted prune, ``"clear_all"``
        for a full wipe) indicating what motivated the prune.
        """
        return {"deleted": deleted, "keys": keys or [], "reason": reason}

    @staticmethod
    def scoped_update(
        scope: str,
        identifiers: Dict[str, Any],
        data: Dict[str, Any],
        *,
        slot_name: str = "scoped",
    ) -> ActionPayload:
        """Create a SCOPED_UPDATE action payload.

        ``identifiers`` carries the kwargs consumed by
        ``StateStore._build_scope_key`` for the given ``scope``:

            "user"       -> {"user_id": ...}
            "guild"      -> {"guild_id": ...}
            "user_guild" -> {"user_id": ..., "guild_id": ...}
            "global"     -> {}

        ``slot_name`` routes the write to a named bucket under
        ``state["application"]``. Defaults to the shared ``"scoped"``
        bucket so generic callers keep working; views with a
        ``scoped_slot`` class attribute supply their own bucket for
        subsystem isolation.
        """
        return {
            "scope": scope,
            "identifiers": identifiers,
            "data": data,
            "slot_name": slot_name,
        }
