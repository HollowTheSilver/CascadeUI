"""SQLite-backed persistence backend.

Implements the full :class:`PersistenceBackend` Protocol against a local
SQLite database via ``aiosqlite``. WAL journal mode is enabled at
connection time for better concurrent-read behavior and to avoid
Windows file-locking surprises when a second process reads the DB.

Requires the ``aiosqlite`` extra::

    pip install pycascadeui[sqlite]

One physical database serves all three namespaces plus the generic KV
surface -- table names (``cascadeui/persistence/schema.py``) are the
partitioning key. A single persistent connection is opened in
:meth:`initialize` and reused; the :class:`~asyncio.Lock` on the
connection guards SQLite's single-writer semantics.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import time
from typing import Any, AsyncIterator, ClassVar

import aiosqlite  # hard import -- backends/__init__.py catches ImportError

import logging

from ..protocols import Capability
from ..schema import ALL_DDL, TABLE_KV, TABLE_SCHEMA_META

logger = logging.getLogger(__name__)


# // ========================================( Helpers )======================================== // #


def _quote_ident(name: str) -> str:
    """Quote an identifier (table or column) with double quotes and
    escape embedded double quotes. Safe against the only values the
    library ever passes through (constants from ``schema.py`` and row
    keys from namespace configs), but cheap insurance for user-authored
    namespace configs that might slip an odd name past review.
    """
    return '"' + name.replace('"', '""') + '"'


# LIKE wildcard characters need escaping when the caller-supplied prefix
# is treated as a literal. ``\\`` is declared as the escape via ``ESCAPE``
# in the query. Matches the set documented at sqlite.org/lang_expr.html.
_LIKE_SPECIALS = ("\\", "%", "_")


def _escape_like(prefix: str) -> str:
    """Escape LIKE wildcards in ``prefix`` using ``\\`` as the escape
    character. Paired with ``ESCAPE '\\\\'`` in the query."""
    out = prefix
    for ch in _LIKE_SPECIALS:
        out = out.replace(ch, "\\" + ch)
    return out


# // ========================================( Class )======================================== // #


class SQLiteBackend:
    """Persistent SQLite implementation of :class:`PersistenceBackend`.

    Opens one ``aiosqlite`` connection in :meth:`initialize` and holds
    it for the backend lifetime. A shared :class:`asyncio.Lock`
    serializes writes -- SQLite is single-writer anyway, but the lock
    keeps the Python-side queue orderly under high contention and gives
    deterministic error behavior when a transaction fails mid-flight.

    Declares every capability -- relational rows, TTL index support,
    schema metadata, KV surface -- so any namespace config works
    against it without configuration.
    """

    capabilities: ClassVar[Capability] = (
        Capability.KV
        | Capability.RELATIONAL
        | Capability.TTL_INDEX
        | Capability.SCHEMA_META
    )

    def __init__(self, db_path: str = "cascadeui.db") -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    # // ========================================( Lifecycle )======================================== // #

    async def initialize(self) -> None:
        """Open the connection, enable WAL, run every DDL statement.

        Safe to call more than once -- the second call is a no-op. DDL
        is ``CREATE TABLE IF NOT EXISTS`` throughout, so re-running it
        against a populated database does not drop data.
        """
        if self._conn is not None:
            return

        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row  # dict-like access via column name

        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")

        for stmt in ALL_DDL:
            await conn.execute(stmt)
        await conn.commit()

        self._conn = conn
        logger.debug(f"SQLiteBackend initialized: {self.db_path}")

    async def close(self) -> None:
        """Close the connection cleanly. Safe to call multiple times."""
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None
        logger.debug(f"SQLiteBackend closed: {self.db_path}")

    # // ========================================( Connection accessor )======================================== // #

    def _db(self) -> aiosqlite.Connection:
        """Return the live connection or raise if uninitialized. Every
        public method routes through here so the error points at the
        setup bug rather than a misleading ``AttributeError`` on
        ``NoneType.execute``."""
        if self._conn is None:
            raise RuntimeError(
                "SQLiteBackend used before initialize(). "
                "Install PersistenceMiddleware via setup_middleware() or "
                "await backend.initialize() first."
            )
        return self._conn

    # // ========================================( Key-value surface )======================================== // #

    async def kv_read(self, namespace: str, key: str) -> bytes | None:
        db = self._db()
        table = _quote_ident(TABLE_KV)
        cursor = await db.execute(
            f"SELECT value FROM {table} WHERE namespace = ? AND key = ?",
            (namespace, key),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return bytes(row[0]) if row is not None else None

    async def kv_write(self, namespace: str, key: str, value: bytes) -> None:
        db = self._db()
        table = _quote_ident(TABLE_KV)
        async with self._write_lock:
            await db.execute(
                f"""
                INSERT INTO {table} (namespace, key, value)
                VALUES (?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value
                """,
                (namespace, key, value),
            )
            await db.commit()

    async def kv_delete(self, namespace: str, key: str) -> None:
        db = self._db()
        table = _quote_ident(TABLE_KV)
        async with self._write_lock:
            await db.execute(
                f"DELETE FROM {table} WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            await db.commit()

    async def kv_scan(
        self, namespace: str, prefix: str = ""
    ) -> AsyncIterator[tuple[str, bytes]]:
        db = self._db()
        table = _quote_ident(TABLE_KV)
        if prefix:
            # LIKE with ESCAPE so a caller-supplied prefix containing %
            # or _ is treated literally. Paired with _escape_like above.
            pattern = _escape_like(prefix) + "%"
            cursor = await db.execute(
                f"""
                SELECT key, value FROM {table}
                WHERE namespace = ? AND key LIKE ? ESCAPE '\\'
                """,
                (namespace, pattern),
            )
        else:
            cursor = await db.execute(
                f"SELECT key, value FROM {table} WHERE namespace = ?",
                (namespace,),
            )

        # Snapshot before yielding so caller mutation during iteration
        # is safe (Protocol contract: scan-snapshot safety).
        rows = await cursor.fetchall()
        await cursor.close()

        for row in rows:
            yield row[0], bytes(row[1])

    # // ========================================( Relational surface )======================================== // #

    async def row_upsert(
        self,
        namespace: str,
        row: dict[str, Any],
        key_columns: list[str],
    ) -> None:
        if not row:
            raise ValueError("row_upsert requires at least one column")
        if not key_columns:
            raise ValueError("row_upsert requires at least one key column")

        table = _quote_ident(namespace)
        cols = list(row.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(_quote_ident(c) for c in cols)

        # Excluded-table upsert: every non-key column is overwritten on
        # conflict, every key column is left alone (they already match
        # by definition of being the conflict target).
        update_cols = [c for c in cols if c not in key_columns]
        if update_cols:
            set_clause = ", ".join(
                f"{_quote_ident(c)} = excluded.{_quote_ident(c)}" for c in update_cols
            )
            conflict_sql = (
                f"ON CONFLICT ({', '.join(_quote_ident(k) for k in key_columns)}) "
                f"DO UPDATE SET {set_clause}"
            )
        else:
            # All columns are key columns -- conflict means the row
            # already exists with identical values, no update needed.
            conflict_sql = (
                f"ON CONFLICT ({', '.join(_quote_ident(k) for k in key_columns)}) DO NOTHING"
            )

        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) {conflict_sql}"
        )

        db = self._db()
        async with self._write_lock:
            await db.execute(sql, tuple(row[c] for c in cols))
            await db.commit()

    async def row_select(
        self,
        namespace: str,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        db = self._db()
        table = _quote_ident(namespace)
        if where:
            clause = " AND ".join(f"{_quote_ident(c)} = ?" for c in where.keys())
            sql = f"SELECT * FROM {table} WHERE {clause}"
            params: tuple[Any, ...] = tuple(where.values())
        else:
            sql = f"SELECT * FROM {table}"
            params = ()

        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        # aiosqlite.Row -> plain dict so callers can mutate freely without
        # touching the cursor's backing buffer. Matches InMemoryBackend's
        # copy-on-return contract.
        return [dict(r) for r in rows]

    async def row_delete(
        self,
        namespace: str,
        where: dict[str, Any],
    ) -> int:
        if not where:
            raise ValueError("row_delete requires a non-empty where clause")

        db = self._db()
        table = _quote_ident(namespace)
        clause = " AND ".join(f"{_quote_ident(c)} = ?" for c in where.keys())
        sql = f"DELETE FROM {table} WHERE {clause}"
        async with self._write_lock:
            cursor = await db.execute(sql, tuple(where.values()))
            deleted = cursor.rowcount
            await cursor.close()
            await db.commit()
        return deleted or 0

    async def row_delete_where_lt(
        self,
        namespace: str,
        column: str,
        value: Any,
    ) -> int:
        # NULL-safe natively: SQLite treats ``NULL < anything`` as NULL
        # (never true), so rows without a timestamp are preserved. No
        # explicit null handling needed.
        db = self._db()
        table = _quote_ident(namespace)
        col = _quote_ident(column)
        sql = f"DELETE FROM {table} WHERE {col} < ?"
        async with self._write_lock:
            cursor = await db.execute(sql, (value,))
            deleted = cursor.rowcount
            await cursor.close()
            await db.commit()
        return deleted or 0

    # // ========================================( Schema metadata surface )======================================== // #

    async def get_schema_version(self, table: str) -> int:
        db = self._db()
        meta = _quote_ident(TABLE_SCHEMA_META)
        cursor = await db.execute(
            f"SELECT schema_version FROM {meta} WHERE table_name = ?",
            (table,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row is not None else 0

    async def set_schema_version(self, table: str, version: int) -> None:
        db = self._db()
        meta = _quote_ident(TABLE_SCHEMA_META)
        applied_at = int(time.time())
        async with self._write_lock:
            await db.execute(
                f"""
                INSERT INTO {meta} (table_name, schema_version, applied_at)
                VALUES (?, ?, ?)
                ON CONFLICT(table_name) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    applied_at = excluded.applied_at
                """,
                (table, version, applied_at),
            )
            await db.commit()
