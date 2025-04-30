"""Manager class for CascadeUI."""

# // ========================================( Modules )======================================== // #


from discord import Interaction
from typing import Dict, Optional, Union, Callable, List, Self, TYPE_CHECKING
from datetime import datetime

# Import the logger
from .utils.logger import AsyncLogger

# Import type references to avoid circular imports
from .types import UIManagerObj, UISessionObj, CascadeViewObj

# Create logger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")

# Only import for type checking
if TYPE_CHECKING:
    from .session import UISession
    from .view import CascadeView


# // ========================================( Class )======================================== // #


class UIManager(object):
    """
    Discord ui instance manager to support efficient dynamic view | embed chaining.

    ... VersionAdded:: 1.0
    """

    __instance: Optional[Self] = None

    def __init__(self):
        self.sessions: Dict[Optional[int], Optional[UISessionObj]] = dict()
        self.sessions.setdefault(None)
        self.middlewares: List[Callable] = []
        self._last_cleanup = datetime.now()

    def __new__(cls, *args, **kwargs) -> UIManagerObj:
        if not cls.__instance:
            cls.__instance: UIManagerObj = super().__new__(cls)
            logger.info(msg="Instantiated successfully.")
        return cls.__instance

    def get(self, session: Union[UISessionObj, int]) -> Optional[UISessionObj]:
        """
        Get a session by object or user ID.

        Parameters:
        -----------
        session: Union[UISessionObj, int]
            The session object or user ID to retrieve

        Returns:
        --------
        Optional[UISessionObj]
            The retrieved session or None if not found
        """
        from .session import UISession
        if not session:
            exception: str = \
                "Parameter 'session' required. Must provide a UI session object or unique id to retrieve."
            raise TypeError(exception)
        sid: int = session.uid if isinstance(session, UISession) else session
        if not isinstance(sid, int):
            exception: str = \
                (f"Invalid type '{type(sid)}' provided. Parameter 'session' only accepts a "  # NOQA
                 f"UI session object or unique int id.")
            raise TypeError(exception)
        if cached := self.sessions.get(sid):
            return cached
        return

    def set(self, session: UISessionObj) -> Optional[UISessionObj]:
        """
        Store a session.

        Parameters:
        -----------
        session: UISessionObj
            The session to store

        Returns:
        --------
        Optional[UISessionObj]
            The stored session or None if failed
        """
        from .session import UISession
        if not session:
            exception: str = "Parameter 'session' required. Must provide a UI session object to set."
            raise TypeError(exception)
        sid: int = session.uid if isinstance(session, UISession) else session
        if not isinstance(sid, int):
            exception: str = \
                (f"Invalid type '{type(sid)}' provided. Parameter 'session' only accepts a "  # NOQA
                 f"UI session object.")
            raise TypeError(exception)
        elif isinstance(session, UISession):
            self.sessions[sid] = session
            return session
        return

    def delete(self, session: Union[int, UISessionObj]) -> Optional[UISessionObj]:
        """
        Delete a session.

        Parameters:
        -----------
        session: Union[int, UISessionObj]
            The session or user ID to delete

        Returns:
        --------
        Optional[UISessionObj]
            The deleted session or None if not found
        """
        from .session import UISession
        if not session:
            exception: str = "Parameter 'session' required. Must provide a UI session object to delete."
            raise TypeError(exception)
        sid: int = session.uid if isinstance(session, UISession) else session
        if not isinstance(sid, int):
            exception: str = \
                (f"Invalid type '{type(sid)}' provided. Parameter 'session' only accepts a "  # NOQA
                 f"UI session object.")
            raise TypeError(exception)
        try:
            deleted_session = self.sessions[sid]
            del self.sessions[sid]
            return deleted_session
        except KeyError:
            return

    async def contains(self, session: UISessionObj = None, uid: int = None) -> bool:
        """
        Check if a session exists.

        Parameters:
        -----------
        session: Optional[UISessionObj]
            The session to check
        uid: Optional[int]
            The user ID to check

        Returns:
        --------
        bool
            True if the session exists, False otherwise
        """
        from .session import UISession
        try:
            if isinstance(session, UISession):
                return True if (session.uid in self.sessions.keys()) or (session in self.sessions.values()) else False
            elif isinstance(uid, int):
                return True if uid in self.sessions.keys() else False
        except ValueError:
            ...
        return False

    # Add this method to the UIManager class
    def check_for_existing_view(self, view: CascadeViewObj) -> Optional[CascadeViewObj]:
        """
            Check if there's already a view of the same class for this user.
            If found, update that view's interaction instead of creating a new one.
            This is called internally from the view's __init__ method.
        """
        if not view.interaction:
            return None  # No interaction to check

        user_id = view.interaction.user.id
        view_class_name = view.__class__.__name__

        # Try to get an existing session
        session = self.get(session=user_id)
        if not session:
            return None  # No existing session

        # Look for an existing view of the same class
        for existing_view in session.views:
            if existing_view.__class__.__name__ == view_class_name:
                # Found an existing view, update it
                existing_view.interaction = view.interaction
                # Mark the new view as superseded
                view._is_duplicate = True
                return existing_view

        return None  # No existing view found

    def add_middleware(self, middleware: Callable) -> None:
        """
        Add a middleware function to process interactions.

        Parameters:
        -----------
        middleware: Callable[[Interaction], Coroutine[Any, Any, Interaction]]
            Function that takes and returns an interaction
        """
        self.middlewares.append(middleware)

    async def process_interaction(self, interaction: Interaction) -> Interaction:
        """Process interaction through all middlewares."""
        processed = interaction
        for middleware in self.middlewares:
            try:
                processed = await middleware(processed)
            except Exception as e:
                logger.error(f"Middleware error: {e}", exc_info=True)
        return processed

    async def cleanup_old_sessions(self, max_age_minutes: int = 60) -> int:
        """
        Clean up inactive sessions.

        Parameters:
        -----------
        max_age_minutes: int
            Maximum age in minutes before a session is considered inactive

        Returns:
        --------
        int
            Number of sessions cleaned up
        """
        now = datetime.now()

        # Only run cleanup every 5 minutes
        if (now - self._last_cleanup).total_seconds() < 300:  # 5 minutes in seconds
            return 0

        self._last_cleanup = now
        sessions_to_remove = []

        for uid, session in self.sessions.items():
            if uid is None:
                continue  # Skip default None session

            if session and hasattr(session, 'last_interaction_time'):
                age = (now - session.last_interaction_time).total_seconds() / 60
                if age > max_age_minutes:
                    sessions_to_remove.append(uid)

        count = 0
        for uid in sessions_to_remove:
            if self.delete(session=uid):
                count += 1
                logger.debug(f"Cleaned up inactive session '{uid}'")

        return count
