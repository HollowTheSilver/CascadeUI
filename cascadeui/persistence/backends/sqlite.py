"""SQLite-backed persistence backend.

Implements the full :class:`PersistenceBackend` Protocol against a local
SQLite database via ``aiosqlite``. WAL journal mode is enabled at
connection time, so a second process can read the database while this
one writes, without Windows file-locking surprises.

Requires the ``aiosqlite`` extra::

    pip install pycascadeui[sqlite]

One physical database serves both namespaces plus the generic KV
surface; shared table-name constants are the partitioning key. A single
persistent connection is opened in :meth:`initialize` and reused; the
:class:`~asyncio.Lock` on it gives tasks turns on that one connection, for
reads and writes alike.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import contextlib
import logging
import time
from contextvars import ContextVar
from typing import Any, AsyncIterator, ClassVar, Optional

import aiosqlite  # hard import -- backends/__init__.py catches ImportError

from ...utils.tasks import _bounded_wait
from ..protocols import Capability
from ..schema import (
    ALL_DDL,
    TABLE_KV,
    TABLE_SCHEMA_META,
    apply_table_prefix,
    validate_table_prefix,
)

logger = logging.getLogger(__name__)

_CLOSE_WAIT_SECONDS: float = 5.0
"""How long ``close()`` lets a transaction, read, or write already running on
the connection finish before closing it anyway."""

_CURRENT_TXN: ContextVar[tuple] = ContextVar("cascadeui_sqlite_txn", default=(None, None))
"""The backend and innermost transaction the current task is inside, so a
transaction belongs to the task that opened it. A read or write from another
task waits for the write lock instead of joining it, and so does one from a
task spawned inside a transaction that has since ended."""


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


async def _roll_back(db: aiosqlite.Connection) -> None:
    """Roll back what a write or BEGIN that raised or was cancelled left open.

    Not gated on ``in_transaction``: a cancel can land while the statement is
    still queued on aiosqlite's worker thread, which runs it anyway and runs
    this rollback after it.
    """
    try:
        await db.rollback()
    except Exception as exc:
        logger.debug(f"SQLiteBackend rollback after a failed write: {exc}")


# // ========================================( Class )======================================== // #


class SQLiteBackend:
    """Persistent SQLite implementation of :class:`PersistenceBackend`.

    Opens one ``aiosqlite`` connection in :meth:`initialize` and holds
    it for the backend lifetime. A shared :class:`asyncio.Lock` gives
    tasks turns on that connection for reads and writes alike, so a read
    never sees another task's open transaction and writes queue in order.

    Declares relational rows, TTL index support, schema metadata, the KV
    surface, and raw SQL, so any namespace config works against it
    without configuration.
    """

    capabilities: ClassVar[Capability] = (
        Capability.KV
        | Capability.RELATIONAL
        | Capability.TTL_INDEX
        | Capability.SCHEMA_META
        | Capability.RAW_SQL
    )

    placeholder_style: ClassVar[str] = "qmark"

    def __init__(self, db_path: str = "cascadeui.db", *, table_prefix: str = "") -> None:
        self.db_path = db_path
        validate_table_prefix(table_prefix, "SQLiteBackend")
        self.table_prefix = table_prefix
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    def _table(self, name: str) -> str:
        """Quote ``name`` under this backend's table prefix.

        Every table this backend reads or writes resolves here, including the
        namespace a row operation is given, since a namespace names its own
        table. The default names all carry the ``cascadeui_`` prefix; the
        ``table_prefix`` kwarg keeps two CascadeUI deployments sharing one
        database apart.
        """
        return _quote_ident(f"{self.table_prefix}{name}")

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

        # ``close()`` cannot reach this connection until setup succeeds, and its
        # aiosqlite worker thread is not a daemon, so a failure here closes it or
        # it blocks interpreter exit (a non-SQLite file, a consumer table failing
        # the index DDL, a lock held past busy_timeout, a full disk).
        try:
            # WAL mode requires SQLite 3.7.0+. The PRAGMA returns the new
            # journal mode as a string -- "wal" on success, the prior mode
            # if the engine could not switch. Fail loud rather than running
            # silently in DELETE mode with degraded concurrency.
            cursor = await conn.execute("PRAGMA journal_mode=WAL")
            mode_row = await cursor.fetchone()
            await cursor.close()
            if mode_row is None or str(mode_row[0]).lower() != "wal":
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
                await conn.execute(apply_table_prefix(stmt, self.table_prefix))
            await conn.commit()
        except BaseException:
            # BaseException so a cancelled initialize releases it too.
            try:
                await conn.close()
            except Exception:
                pass
            raise

        # A lock a waiter touched is bound to that event loop, and a bot run
        # twice through asyncio.run reopens on another one.
        self._write_lock = asyncio.Lock()
        self._conn = conn
        logger.debug(f"SQLiteBackend initialized: {self.db_path}")

    async def close(self) -> None:
        """Close the connection cleanly. Safe to call multiple times, and from two tasks at once.

        A transaction, read, or write already running on the connection gets
        five seconds to finish. One still running after that is cut off:
        nothing it wrote is committed, and it raises on exit.
        """
        conn = self._conn
        if conn is None:
            return
        lock = self._write_lock
        try:
            await _bounded_wait(lock.acquire(), _CLOSE_WAIT_SECONDS)
            held = True
        except asyncio.TimeoutError:
            held = False
            logger.warning(
                f"SQLiteBackend closed with a call still running after "
                f"{_CLOSE_WAIT_SECONDS:g}s; what it had not committed was lost: "
                f"{self.db_path}"
            )
        try:
            # Claimed before the await: a second close() of an aiosqlite
            # connection queues a stop its already-stopped worker never answers.
            if self._conn is not conn:
                return
            self._conn = None
            if not held:
                # The transaction cut off still holds the lock; the next
                # connection starts with one of its own.
                self._write_lock = asyncio.Lock()
                # A statement running on the worker thread would otherwise
                # hold the close until it finished, however long.
                await conn.interrupt()
            await conn.close()
            logger.debug(f"SQLiteBackend closed: {self.db_path}")
        finally:
            if held:
                lock.release()

    # // ========================================( Connection accessor )======================================== // #

    def _db(self) -> aiosqlite.Connection:
        """Return the live connection or raise if uninitialized. Every
        public method routes through here so the error points at the
        setup bug rather than a misleading ``AttributeError`` on
        ``NoneType.execute``."""
        if self._conn is None:
            raise RuntimeError(
                "SQLiteBackend used before initialize() or after close(). "
                "Install PersistenceMiddleware via setup_middleware() or "
                "await backend.initialize() first."
            )
        return self._conn

    def _txn_conn(self) -> Optional[aiosqlite.Connection]:
        """The connection this task's open transaction runs on, or ``None`` outside one."""
        backend, txn = _CURRENT_TXN.get()
        if backend is not self or txn is None or not txn._active:
            return None
        if txn._conn is not self._conn:
            raise RuntimeError(
                "SQLiteBackend closed while this transaction was open; nothing it "
                "wrote was committed."
            )
        return txn._conn

    async def _acquire_write_lock(self) -> asyncio.Lock:
        """Take the open connection's write lock, which a close or reopen may
        replace while this waits; returns the lock held."""
        while True:
            lock = self._write_lock
            await lock.acquire()
            if self._write_lock is lock:
                return lock
            lock.release()

    @contextlib.asynccontextmanager
    async def _writing(self, method: Optional[str] = None) -> AsyncIterator[aiosqlite.Connection]:
        """Hold the write lock and yield the open connection.

        ``method`` names a namespace call, which is refused inside this task's
        own transaction: SQLite has one connection, so the call would wait
        forever on the lock that transaction holds.

        A write that raises or is cancelled before its commit is rolled back
        here, so nothing it sent stays open on the connection for the next
        commit to take along.
        """
        if method is not None and self._txn_conn() is not None:
            raise RuntimeError(
                f"SQLiteBackend.{method}() cannot run inside this task's transaction(): "
                f"it takes the write lock the transaction holds. Use execute() inside "
                f"the transaction, or call {method}() after it."
            )
        lock = await self._acquire_write_lock()
        try:
            db = self._db()
            try:
                yield db
            except BaseException:
                await _roll_back(db)
                raise
        finally:
            lock.release()

    @contextlib.asynccontextmanager
    async def _reading(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield the connection for a read.

        Inside this task's transaction the read runs on it and sees its own
        writes. Otherwise it waits for another task's open transaction to end:
        SQLite has one connection, so a read beside that transaction would see
        rows a rollback then discards.
        """
        conn = self._txn_conn()
        if conn is not None:
            yield conn
            return
        lock = await self._acquire_write_lock()
        try:
            yield self._db()
        finally:
            lock.release()

    # // ========================================( Key-value surface )======================================== // #

    async def kv_read(self, namespace: str, key: str) -> bytes | None:
        table = self._table(TABLE_KV)
        async with self._reading() as db:
            cursor = await db.execute(
                f"SELECT value FROM {table} WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return bytes(row[0]) if row is not None else None

    async def kv_write(self, namespace: str, key: str, value: bytes) -> None:
        table = self._table(TABLE_KV)
        async with self._writing("kv_write") as db:
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
        table = self._table(TABLE_KV)
        async with self._writing("kv_delete") as db:
            await db.execute(
                f"DELETE FROM {table} WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            await db.commit()

    async def kv_scan(self, namespace: str, prefix: str = "") -> AsyncIterator[tuple[str, bytes]]:
        table = self._table(TABLE_KV)
        if prefix:
            # LIKE with ESCAPE so a caller-supplied prefix containing %
            # or _ is treated literally. Paired with _escape_like above.
            sql = f"""
                SELECT key, value FROM {table}
                WHERE namespace = ? AND key LIKE ? ESCAPE '\\'
                """
            params: tuple[Any, ...] = (namespace, _escape_like(prefix) + "%")
        else:
            sql = f"SELECT key, value FROM {table} WHERE namespace = ?"
            params = (namespace,)

        # Snapshot before yielding so caller mutation during iteration
        # is safe (Protocol contract: scan-snapshot safety), and so the
        # lock is not held while the caller runs.
        async with self._reading() as db:
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            await cursor.close()

        for row in rows:
            yield row[0], bytes(row[1])

    # // ========================================( Relational surface )======================================== // #

    def _build_upsert_sql(self, namespace: str, cols: list[str], key_columns: list[str]) -> str:
        """Assemble an excluded-table upsert INSERT for ``cols``.

        Every non-key column is overwritten on conflict; key columns are
        excluded from the SET clause (they are the conflict target). When
        every column is a key column the conflict is a DO NOTHING -- the row
        already exists with identical values.
        """
        table = self._table(namespace)
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(_quote_ident(c) for c in cols)

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
            conflict_sql = (
                f"ON CONFLICT ({', '.join(_quote_ident(k) for k in key_columns)}) DO NOTHING"
            )

        return f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) {conflict_sql}"

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

        cols = list(row.keys())
        sql = self._build_upsert_sql(namespace, cols, key_columns)

        async with self._writing("row_upsert") as db:
            await db.execute(sql, tuple(row[c] for c in cols))
            await db.commit()

    async def row_upsert_many(
        self,
        namespace: str,
        rows: list[dict[str, Any]],
        key_columns: list[str],
    ) -> None:
        if not rows:
            return
        if not key_columns:
            raise ValueError("row_upsert_many requires at least one key column")

        # Group by column signature so each executemany shares one SQL
        # statement. Flush rows are homogeneous, so this is usually a single
        # group; a mixed-shape batch produces one group per column set.
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for row in rows:
            if not row:
                raise ValueError("row_upsert_many requires at least one column per row")
            groups.setdefault(tuple(row.keys()), []).append(row)

        # One write lock and one commit for the whole batch -- the commit is
        # a single fsync instead of one per row. _writing() rolls back on a
        # raise or a cancel, which keeps the batch atomic: it commits whole or
        # not at all, whichever group (or row inside one executemany) failed.
        async with self._writing("row_upsert_many") as db:
            for cols, group in groups.items():
                sql = self._build_upsert_sql(namespace, list(cols), key_columns)
                await db.executemany(sql, [tuple(r[c] for c in cols) for r in group])
            await db.commit()

    async def row_select(
        self,
        namespace: str,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        table = self._table(namespace)
        if where:
            clause = " AND ".join(f"{_quote_ident(c)} = ?" for c in where.keys())
            sql = f"SELECT * FROM {table} WHERE {clause}"
            params: tuple[Any, ...] = tuple(where.values())
        else:
            sql = f"SELECT * FROM {table}"
            params = ()

        async with self._reading() as db:
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

        table = self._table(namespace)
        clause = " AND ".join(f"{_quote_ident(c)} = ?" for c in where.keys())
        sql = f"DELETE FROM {table} WHERE {clause}"
        async with self._writing("row_delete") as db:
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
        table = self._table(namespace)
        col = _quote_ident(column)
        sql = f"DELETE FROM {table} WHERE {col} < ?"
        async with self._writing("row_delete_where_lt") as db:
            cursor = await db.execute(sql, (value,))
            deleted = cursor.rowcount
            await cursor.close()
            await db.commit()
        return deleted or 0

    # // ========================================( Schema metadata surface )======================================== // #

    async def get_schema_version(self, table: str) -> int:
        meta = self._table(TABLE_SCHEMA_META)
        async with self._reading() as db:
            cursor = await db.execute(
                f"SELECT schema_version FROM {meta} WHERE table_name = ?",
                (table,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row is not None else 0

    async def set_schema_version(self, table: str, version: int) -> None:
        meta = self._table(TABLE_SCHEMA_META)
        applied_at = int(time.time())
        async with self._writing("set_schema_version") as db:
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
        txn = self._txn_conn()
        if txn is not None:
            cursor = await txn.execute(sql, params)
            rowcount = cursor.rowcount
            await cursor.close()
        else:
            async with self._writing() as db:
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
        async with self._reading() as db:
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
        txn = self._txn_conn()
        if txn is not None:
            cursor = await txn.executemany(sql, params_list)
            await cursor.close()
        else:
            async with self._writing() as db:
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
        async with self._reading() as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
            await cursor.close()
        return dict(row) if row is not None else None

    def transaction(self) -> "_SQLiteTransaction":
        """Open an explicit transaction context. Outermost entries issue
        ``BEGIN``/``COMMIT``/``ROLLBACK`` and hold the backend's write
        lock; nested entries issue ``SAVEPOINT``/``RELEASE
        SAVEPOINT``/``ROLLBACK TO SAVEPOINT``.

        The raw-SQL methods (``execute``, ``fetch``, ``executemany``,
        ``fetch_one``) participate in the transaction. A namespace write
        (``row_upsert``, ``kv_write``, ...) inside it raises
        ``RuntimeError``, since it takes the write lock the transaction
        holds. Namespace reads inside it see its writes. From another task,
        reads and writes wait for it to end.
        """
        return _SQLiteTransaction(self)


# // ========================================( Transaction helper )======================================== // #


class _SQLiteTransaction:
    """SQLite transaction context. Outermost entries take the backend's
    write lock and issue BEGIN/COMMIT; nested entries issue SAVEPOINT
    statements that compose with asyncpg-style nesting (rollback to
    savepoint isolates inner failures from the outer transaction).

    A transaction belongs to the task that opened it (``_CURRENT_TXN``):
    nesting counts only within that task, and another task's reads and
    writes wait for the lock rather than joining it. A task spawned inside
    the block writes as part of it while it is open, and on its own once it
    ends. The spawned task cannot open a transaction of its own while the
    block is open. The write lock is held for the lifetime of the outermost
    transaction, so a long one holds up every other task's database calls.
    Keep transaction bodies short.
    """

    def __init__(self, backend: "SQLiteBackend") -> None:
        self._backend = backend
        self._savepoint_name: str | None = None
        self._lock: Optional[asyncio.Lock] = None
        self._conn: Optional[aiosqlite.Connection] = None
        self._depth: int = 0
        self._active: bool = False
        self._task: Optional[asyncio.Task] = None
        self._token = None

    async def __aenter__(self) -> "_SQLiteTransaction":
        backend = self._backend
        outer_conn = backend._txn_conn()
        outer = _CURRENT_TXN.get()[1] if outer_conn is not None else None
        task = asyncio.current_task()
        if outer is not None and outer._task is not task:
            # A savepoint on another task's transaction commits with it, and
            # its release fails once that transaction has moved on.
            raise RuntimeError(
                "A task spawned inside an SQLiteBackend transaction cannot open one "
                "of its own while the outer one is open. Open it after the outer "
                "transaction ends, or run its statements as part of the outer one."
            )
        depth = outer._depth if outer is not None else 0
        if depth == 0:
            lock = await backend._acquire_write_lock()
            try:
                db = backend._db()
                try:
                    await db.execute("BEGIN")
                except BaseException:
                    await _roll_back(db)
                    raise
            except BaseException:
                lock.release()
                raise
            self._lock = lock
        else:
            db = outer_conn
            self._savepoint_name = f"cascadeui_sp_{depth}"
            await db.execute(f"SAVEPOINT {self._savepoint_name}")
        self._conn = db
        self._depth = depth + 1
        self._task = task
        self._active = True
        self._token = _CURRENT_TXN.set((backend, self))
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        # A task spawned inside the block still holds this transaction in its
        # copied context; once it ends, that task writes on its own.
        self._active = False
        try:
            await self._finish(exc_type)
        finally:
            try:
                _CURRENT_TXN.reset(self._token)
            except ValueError:
                # Exited from another context, as when the loop finalizes an
                # abandoned async generator; that context never set the marker.
                pass

    async def _finish(self, exc_type: Optional[type]) -> None:
        if self._backend._conn is not self._conn:
            # close() cut this transaction off: there is nothing left to
            # commit to, and the lock it held is not the open connection's.
            if self._lock is not None:
                self._lock.release()
                self._lock = None
            if exc_type is None:
                raise RuntimeError(
                    "SQLiteBackend closed while this transaction was open; nothing it "
                    "wrote was committed."
                )
            return
        db = self._conn
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
            if self._lock is not None:
                self._lock.release()
                self._lock = None
