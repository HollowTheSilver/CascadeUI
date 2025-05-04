
# // ========================================( Modules )======================================== // #

# Import logging at module level
from ..utils.logging import AsyncLogger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")

# Store singleton
_store_instance = None


# // ========================================( Functions )======================================== // #


def get_store():
    """Get the global state store instance."""
    global _store_instance
    if _store_instance is None:
        # Import here to avoid circular imports
        from .store import StateStore
        _store_instance = StateStore()
        logger.debug("Created new StateStore instance")
    return _store_instance
