
# // ========================================( Modules )======================================== // #


from typing import Dict, Any, Optional

# Type aliases
ActionPayload = Dict[str, Any]


# // ========================================( Classes )======================================== // #


class ActionCreators:
    """Helper methods to create standardized actions."""

    @staticmethod
    def view_created(view_id: str, view_type: str, user_id: Optional[int] = None,
                     session_id: Optional[str] = None, **props) -> ActionPayload:
        """Create a VIEW_CREATED action payload."""
        return {
            "view_id": view_id,
            "view_type": view_type,
            "user_id": user_id,
            "session_id": session_id,
            "props": props
        }

    @staticmethod
    def view_updated(view_id: str, **updates) -> ActionPayload:
        """Create a VIEW_UPDATED action payload."""
        return {
            "view_id": view_id,
            **updates
        }

    @staticmethod
    def view_destroyed(view_id: str) -> ActionPayload:
        """Create a VIEW_DESTROYED action payload."""
        return {
            "view_id": view_id
        }

    @staticmethod
    def session_created(session_id: str, user_id: Optional[int] = None,
                        **data) -> ActionPayload:
        """Create a SESSION_CREATED action payload."""
        return {
            "session_id": session_id,
            "user_id": user_id,
            "data": data
        }

    @staticmethod
    def session_updated(session_id: str, **data) -> ActionPayload:
        """Create a SESSION_UPDATED action payload."""
        return {
            "session_id": session_id,
            "data": data
        }

    @staticmethod
    def navigation(destination: str, **params) -> ActionPayload:
        """Create a NAVIGATION action payload."""
        return {
            "destination": destination,
            "params": params
        }

    @staticmethod
    def component_interaction(component_id: str, view_id: str,
                              user_id: Optional[int] = None,
                              **values) -> ActionPayload:
        """Create a COMPONENT_INTERACTION action payload."""
        return {
            "component_id": component_id,
            "view_id": view_id,
            "user_id": user_id,
            "value": values
        }
