# // ========================================( Modules )======================================== // #


import asyncio
import json
from typing import Any, Dict, Optional

from ..utils.logging import AsyncLogger
from .serialization import StateSerializer
from .storage import StorageBackend

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")


# // ========================================( Class )======================================== // #


class SQLiteBackend(StorageBackend):
    """Persist state in a local SQLite database using aiosqlite.

    Uses WAL journal mode for better concurrent-read performance and to
    avoid WinError 32 (file-in-use) issues on Windows.

    Requires the ``aiosqlite`` package (optional dependency):
        pip install pycascadeui[sqlite]

    Usage:
        from cascadeui.persistence import SQLiteBackend
        await setup_persistence(bot, backend=SQLiteBackend("cascadeui.db"))
    """

    def __init__(self, db_path: str = "cascadeui.db"):
        self.db_path = db_path
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_table(self):
        """Create the state table if it doesn't exist."""
        if self._initialized:
            return

        async with self._init_lock:
            # Double-check after acquiring lock
            if self._initialized:
                return

            try:
                import aiosqlite
            except ImportError:
                raise ImportError(
                    "aiosqlite is required for SQLiteBackend. "
                    "Install it with: pip install pycascadeui[sqlite]"
                )

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS cascadeui_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        data TEXT NOT NULL,
                        updated_at TEXT DEFAULT (datetime('now'))
                    )
                """)
                await db.commit()

            self._initialized = True

    async def save_state(self, state: Dict[str, Any]) -> bool:
        """Save state to SQLite."""
        await self._ensure_table()

        try:
            import aiosqlite

            data = StateSerializer.serialize(state)

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO cascadeui_state (id, data, updated_at)
                       VALUES (1, ?, datetime('now'))
                       ON CONFLICT(id) DO UPDATE SET
                           data = excluded.data,
                           updated_at = excluded.updated_at""",
                    (data,),
                )
                await db.commit()

            logger.debug(f"State saved to SQLite: {self.db_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving state to SQLite: {e}")
            return False

    async def load_state(self) -> Optional[Dict[str, Any]]:
        """Load state from SQLite."""
        await self._ensure_table()

        try:
            import aiosqlite

            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT data FROM cascadeui_state WHERE id = 1")
                row = await cursor.fetchone()

            if row is None:
                logger.info(f"No state found in SQLite: {self.db_path}")
                return None

            state = StateSerializer.deserialize(row[0])
            logger.info(f"State loaded from SQLite: {self.db_path}")
            return state
        except Exception as e:
            logger.error(f"Error loading state from SQLite: {e}")
            return None
