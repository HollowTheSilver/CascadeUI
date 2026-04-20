# // ========================================( Modules )======================================== // #

import logging

logger = logging.getLogger(__name__)

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
