# // ========================================( Modules )======================================== // #


from ..utils.logging import AsyncLogger
from .storage import StorageBackend

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")


# // ========================================( Migration )======================================== // #


async def migrate_storage(source: StorageBackend, target: StorageBackend) -> bool:
    """Migrate state data from one storage backend to another.

    Loads the full state from the source backend and saves it to the
    target backend. Both backends must implement the StorageBackend
    interface.

    Usage:
        from cascadeui.persistence import migrate_storage, FileStorageBackend
        from cascadeui.persistence.sqlite import SQLiteBackend

        await migrate_storage(
            source=FileStorageBackend("old.json"),
            target=SQLiteBackend("new.db"),
        )

    Returns:
        True if migration succeeded, False otherwise.
    """
    logger.info(
        f"Migrating state from {source.__class__.__name__} " f"to {target.__class__.__name__}"
    )

    state = await source.load_state()
    if state is None:
        logger.warning("Source backend returned no state — nothing to migrate")
        return False

    success = await target.save_state(state)
    if success:
        logger.info("Migration completed successfully")
    else:
        logger.error("Migration failed: target backend could not save state")

    return success
