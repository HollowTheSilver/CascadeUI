"""Session management for CascadeUI."""

# // ========================================( Modules )======================================== // #


from typing import List, Optional, Union, Tuple, Type, TypeVar, TYPE_CHECKING
from datetime import datetime

# Import the logger
from .utils.logger import AsyncLogger

# Import type references
from .types import CascadeViewObj

# Create logger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")

# Import only for type checking
if TYPE_CHECKING:
    from .view import CascadeView


# // ========================================( Class )======================================== // #


class UISession:
    """Manages view sessions for users."""

    def __init__(self, uid: int) -> None:
        self.uid: int = uid
        self.views: List[Optional[CascadeViewObj]] = list()
        self.last_interaction_time = datetime.now()
        self.view_history: List[Tuple[str, Type[CascadeViewObj]]] = []  # Store name and class

    def add_to_history(self, view: CascadeViewObj) -> None:
        """Track view in history for navigation."""
        # Add view class to history for potential navigation
        self.view_history.append((view.name, view.__class__))

        # Keep history manageable (limit to 10 entries)
        if len(self.view_history) > 10:
            self.view_history.pop(0)
            logger.debug(f"Trimmed view history in session {self.uid}")

        logger.debug(f"Added view '{view.name}' to history in session {self.uid}")

    def get(self, view: Union[CascadeViewObj, str]) -> Optional[CascadeViewObj]:
        """Get a view from the session by object or name."""
        # Local import to avoid circular imports
        from .view import CascadeView

        if not view:
            raise TypeError("Parameter 'view' required.")

        # If given a string, find by name
        if isinstance(view, str):
            for v in self.views:
                if v and v.name == view:
                    return v
            return None

        # If given a view object
        elif isinstance(view, CascadeView):
            if view in self.views:
                return view

            # Try by name as fallback
            for v in self.views:
                if v and v.name == view.name:
                    return v

        return None

    def set(self, view: CascadeViewObj) -> Optional[CascadeViewObj]:
        """
        Add or update a view in the session.

        Parameters:
        -----------
        view: CascadeViewObj
            The view to add or update

        Returns:
        --------
        Optional[CascadeViewObj]
            The added/updated view or None
        """
        # Local import to avoid circular imports
        from .view import CascadeView

        if not view:
            exception: str = "Parameter 'view' required. Must provide a cascade view object to set."
            raise TypeError(exception)

        if not isinstance(view, CascadeView):
            exception: str = \
                f"Invalid type '{type(view)}' provided. Parameter 'view' only accepts a cascade view object."
            raise TypeError(exception)

        # For better debugging, log the current state
        current_views = [f"{v.name} (id: {id(v)})" for v in self.views if v]
        logger.debug(f"Session {self.uid} before update: {current_views}")

        # Check if this exact view instance is already in the list
        if view in self.views:
            logger.debug(f"View '{view.name}' (id: {id(view)}) already in session {self.uid}")
            # Update last interaction time
            self.last_interaction_time = datetime.now()
            return view

        # Remove any existing view with the same name that isn't this instance
        for i, v in enumerate(list(self.views)):
            if v and v.name == view.name and v is not view:
                logger.debug(f"Removing older view '{v.name}' (id: {id(v)}) with same name")
                self.views.pop(i)
                break

        # Add the new view to the list
        self.views.append(view)
        logger.debug(f"Added view '{view.name}' (id: {id(view)}) to session {self.uid}")

        # Update last interaction time
        self.last_interaction_time = datetime.now()

        return view

    def delete(self, view: Union[CascadeViewObj, str]) -> Optional[CascadeViewObj]:
        """
        Remove a view from the session.

        Parameters:
        -----------
        view: Union[CascadeViewObj, str]
            The view to remove

        Returns:
        --------
        Optional[CascadeViewObj]
            The removed view or None
        """
        # Local import to avoid circular imports
        from .view import CascadeView

        if not view:
            exception: str = "Parameter 'view' required. Must provide a cascade view object or name to delete."
            raise TypeError(exception)

        # If given a string, find by name
        if isinstance(view, str):
            for i, v in enumerate(self.views[:]):  # Create a copy to avoid modification during iteration
                if v and v.name == view:
                    logger.debug(f"Removing view '{view}' by name from session {self.uid}")
                    return self.views.pop(i)
            return None

        # If given a view object, find by identity
        elif isinstance(view, CascadeView):
            try:
                i = self.views.index(view)
                logger.debug(f"Removing view '{view.name}' (id: {id(view)}) by identity from session {self.uid}")
                return self.views.pop(i)
            except ValueError:
                # If not found by identity, try by name as fallback
                view_name = view.name
                for i, v in enumerate(self.views[:]):  # Create a copy to avoid modification during iteration
                    if v and v.name == view_name:
                        logger.debug(f"Removing view '{view_name}' (id: {id(v)}) by name from session {self.uid}")
                        return self.views.pop(i)

        return None

    async def contains(self, view: CascadeViewObj = None, uid: int = None) -> bool:
        """
        Check if a view exists in the session.

        Parameters:
        -----------
        view: Optional[CascadeViewObj]
            The view to check
        uid: Optional[int]
            The view ID to check

        Returns:
        --------
        bool
            True if the view exists, False otherwise
        """
        # Local import to avoid circular imports
        from .view import CascadeView
        try:
            if isinstance(view, CascadeView):
                return True if (view in self.views) else False
            elif isinstance(uid, int):
                return True if uid in [id(v) for v in self.views if v] else False
        except ValueError:
            ...
        return False
