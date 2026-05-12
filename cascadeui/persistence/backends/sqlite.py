"""SQLite-backed persistence backend.

Implements the full :class:`PersistenceBackend` Protocol against a local
SQLite database via ``aiosqlite``. WAL journal mode is enabled at
connection time for better concurrent-read behavior and to avoid
Windows file-locking surprises when a second process reads the DB.

Requires the ``aiosqlite`` extra::

    pip install pycascadeui[sqlite]

One physical database serves all three namespaces plus the generic KV
surface; shared table-name constants are the partitioning key. A single
persistent connection is opened in :meth:`initialize` and reused; the
:class:`~asyncio.Lock` on the connection guards SQLite's single-writer
semantics.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import logging
import time
from typing import Any, AsyncIterator, ClassVar, Optional

import aiosqlite  # hard import -- backends/__init__.py catches ImportError

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

    Rejects NUL bytes outright -- SQLite silently accepts NUL in
    identifiers and produces a corrupt schema, while PostgreSQL
    rejects them. The ValueError surfaces at the seam rather than
    letting the bad identifier propagate to the engine.
    """
    if "\x00" in name:
        raise ValueError(f"identifier contains NUL byte: {name!r}")
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
        | Capability.RAW_SQL
    )

    placeholder_style: ClassVar[str] = "qmark"

    def __init__(self, db_path: str = "cascadeui.db") -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._txn_depth: int = 0

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

        # WAL mode requires SQLite 3.7.0+. The PRAGMA returns the new
        # journal mode as a string -- "wal" on success, the prior mode
        # if the engine could not switch. Fail loud rather than running
        # silently in DELETE mode with degraded concurrency.
        cursor = await conn.execute("PRAGMA journal_mode=WAL")
        mode_row = await cursor.fetchone()
        await cursor.close()
        if mode_row is None or str(mode_row[0]).lower() != "wal":
            await conn.close()
            raise RuntimeError(
                f"SQLiteBackend could not enable WAL journal mode "
                f"(got {mode_row[0] if mode_row else None!r}). "
                f"WAL requires SQLite 3.7.0+ (released 2010-07-21)."
            )
        await conn.execute("PRAGMA foreign_keys=ON")
        # synchronous=NORMAL is corruption-safe under WAL per
        # sqlite.org/pragma.html#pragma_synchronous and substantially
        # faster than the FULL default for commit-heavy workloads.
        await conn.execute("PRAGMA synchronous=NORMAL")
        # busy_timeout=5000 (5s) lets concurrent writers wait for the
        # lock instead of raising OperationalError immediately. Important
        # when an external SQLite process (devtools, ad-hoc scripts)
        # holds the write lock briefly.
        await conn.execute("PRAGMA busy_timeout=5000")

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

    async def kv_scan(self, namespace: str, prefix: str = "") -> AsyncIterator[tuple[str, bytes]]:
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

        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) {conflict_sql}"

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

    # // ========================================( Raw SQL surface )======================================== // #

    async def execute(self, sql: str, *params: Any) -> int:
        """Execute an SQL statement. Caller-supplied SQL runs verbatim
        with the provided positional ``params`` bound through aiosqlite.
        Use ``?`` placeholders to match SQLite's parameter style.

        Returns the affected-row count for INSERT/UPDATE/DELETE; returns
        ``0`` for DDL (CREATE/ALTER/DROP) statements that don't report
        row counts. Inside a ``transaction()`` block the call participates
        in the transaction; outside one it auto-commits under the write
        lock.
        """
        if not sql:
            raise ValueError("execute requires a non-empty sql string")
        db = self._db()
        if self._txn_depth > 0:
            cursor = await db.execute(sql, params)
            rowcount = cursor.rowcount
            await cursor.close()
        else:
            async with self._write_lock:
                cursor = await db.execute(sql, params)
                rowcount = cursor.rowcount
                await cursor.close()
                await db.commit()
        return rowcount or 0

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        """Execute an SQL query and return all rows as dicts. Returns an
        empty list for queries that yield no rows. Each dict's keys are
        column names from the query (or aliases via ``AS``); rows are
        defensive copies independent of cursor state.
        """
        if not sql:
            raise ValueError("fetch requires a non-empty sql string")
        db = self._db()
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    async def executemany(self, sql: str, params_list: list[tuple]) -> int:
        """Execute an SQL statement against multiple parameter sets in
        one call. Returns ``len(params_list)`` as a best-effort
        approximation -- aiosqlite's ``cursor.rowcount`` after
        ``executemany`` reflects only the LAST statement in the batch
        (per Python's ``sqlite3`` module behavior), not the aggregate.
        Empty ``params_list`` is a no-op returning ``0``. Inside a
        transaction the call participates in the transaction; outside
        one it auto-commits under the write lock.

        The return value matches ``PostgresBackend.executemany`` for
        cross-backend consistency.
        """
        if not sql:
            raise ValueError("executemany requires a non-empty sql string")
        if not params_list:
            return 0
        db = self._db()
        if self._txn_depth > 0:
            cursor = await db.executemany(sql, params_list)
            await cursor.close()
        else:
            async with self._write_lock:
                cursor = await db.executemany(sql, params_list)
                await cursor.close()
                await db.commit()
        return len(params_list)

    async def fetch_one(self, sql: str, *params: Any) -> Optional[dict[str, Any]]:
        """Execute an SQL query and return the first row as a dict, or
        ``None`` if the query yields no rows. The empty-result return is
        the contract; callers enforce single-row constraints explicitly.
        """
        if not sql:
            raise ValueError("fetch_one requires a non-empty sql string")
        db = self._db()
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    def transaction(self) -> "_SQLiteTransaction":
        """Open an explicit transaction context. Outermost entries issue
        ``BEGIN``/``COMMIT``/``ROLLBACK`` and hold the backend's write
        lock; nested entries issue ``SAVEPOINT``/``RELEASE
        SAVEPOINT``/``ROLLBACK TO SAVEPOINT``.

        Only the raw-SQL methods (``execute``, ``fetch``, ``executemany``,
        ``fetch_one``) participate in the transaction. Namespace API
        methods (``row_upsert``, ``row_select``, ``kv_*``) auto-commit
        per call regardless of transaction state.
        """
        return _SQLiteTransaction(self)


# // ========================================( Transaction helper )======================================== // #


class _SQLiteTransaction:
    """SQLite transaction context. Outermost entries take the backend's
    write lock and issue BEGIN/COMMIT; nested entries issue SAVEPOINT
    statements that compose with asyncpg-style nesting (rollback to
    savepoint isolates inner failures from the outer transaction).

    Depth tracking lives on ``backend._txn_depth`` (the connection is a
    singleton on SQLiteBackend, so per-instance state is correct). The
    write lock is held for the lifetime of the outermost transaction --
    long-running transactions starve concurrent writes; keep transaction
    bodies short.
    """

    def __init__(self, backend: "SQLiteBackend") -> None:
        self._backend = backend
        self._savepoint_name: str | None = None
        self._holds_lock: bool = False

    async def __aenter__(self) -> "_SQLiteTransaction":
        depth = self._backend._txn_depth
        db = self._backend._db()
        if depth == 0:
            await self._backend._write_lock.acquire()
            self._holds_lock = True
            try:
                await db.execute("BEGIN")
            except Exception:
                self._backend._write_lock.release()
                self._holds_lock = False
                raise
        else:
            self._savepoint_name = f"cascadeui_sp_{depth}"
            await db.execute(f"SAVEPOINT {self._savepoint_name}")
        self._backend._txn_depth = depth + 1
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        self._backend._txn_depth -= 1
        db = self._backend._db()
        try:
            if exc_type is None:
                if self._savepoint_name is None:
                    await db.execute("COMMIT")
                else:
                    await db.execute(f"RELEASE SAVEPOINT {self._savepoint_name}")
            else:
                if self._savepoint_name is None:
                    await db.execute("ROLLBACK")
                else:
                    await db.execute(f"ROLLBACK TO SAVEPOINT {self._savepoint_name}")
                    await db.execute(f"RELEASE SAVEPOINT {self._savepoint_name}")
        finally:
            if self._holds_lock:
                self._backend._write_lock.release()
                self._holds_lock = False
