"""PostgreSQL-backed persistence backend.

Implements the full :class:`PersistenceBackend` Protocol against
PostgreSQL via ``asyncpg``. The Protocol-conformance suite at
``tests/test_backends.py`` runs against both this backend and
``SQLiteBackend`` without an adapter layer.

Requires the ``asyncpg`` extra::

    pip install pycascadeui[postgres]

Connection model: an :class:`asyncpg.Pool` for queries plus a separate
dedicated :class:`asyncpg.Connection` for ``LISTEN``/``NOTIFY``. The
listener stays outside the pool because ``LISTEN`` registrations are
session-scoped (per ``postgresql.org/docs/current/sql-listen.html``,
"A session's listen registrations are automatically cleared when the
session ends") -- a pooled connection released back to rotation would
unsubscribe immediately.

Cross-process invalidation: writes to the KV and application-slots
surfaces broadcast a ``cascadeui_invalidation`` notification with a
JSON payload naming the namespace and key. Other CascadeUI processes
listening on the same database receive the notification and can drop
cached state. The library ships the broadcast machinery; consumer-
side cache invalidation hooks register via
:meth:`set_invalidation_callback`.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import json
import logging
import time
from contextvars import ContextVar
from typing import Any, AsyncIterator, Callable, ClassVar, Optional

import asyncpg  # hard import -- backends/__init__.py catches ImportError

from ..protocols import Capability
from ..schema import TABLE_KV, TABLE_SCHEMA_META
from ..schema_postgres import ALL_DDL_PG, JSONB_COLUMNS

logger = logging.getLogger(__name__)


# // ========================================( Constants )======================================== // #


CHANNEL_INVALIDATION: str = "cascadeui_invalidation"
"""Single library-owned LISTEN/NOTIFY channel name. Hard-coded so no
identifier-injection vector crosses the seam from user-supplied state."""


_NOTIFY_PAYLOAD_LIMIT_BYTES: int = 7900
"""Conservative ceiling under PostgreSQL's documented 8000-byte payload
limit (``postgresql.org/docs/current/sql-notify.html``: "In the default
configuration it must be shorter than 8000 bytes"). Margin absorbs UTF-8
multi-byte expansion in slot keys."""


_CURRENT_TXN_CONN: ContextVar[Optional[asyncpg.Connection]] = ContextVar(
    "cascadeui_pg_txn_conn", default=None
)
"""Per-task pointer to the asyncpg Connection held by an active
``backend.transaction()`` block. Raw-SQL methods read this contextvar:
when set, operations route onto the transaction's connection rather
than acquiring a fresh one from the pool. ContextVar isolation per
asyncio Task means concurrent transactions in different tasks correctly
isolate."""


# // ========================================( Helpers )======================================== // #


def _quote_ident(name: str) -> str:
    """Double-quote a PostgreSQL identifier, escaping embedded double
    quotes by doubling them. The SQL-99 quoting rule is identical to
    SQLite's.

    Rejects NUL bytes at this seam so the error surfaces as a Python
    ``ValueError`` rather than propagating to the PostgreSQL engine
    (which rejects NUL identifiers with a UTF-8 encoding error). Mirrors
    the same guard in :func:`cascadeui.persistence.backends.sqlite._quote_ident`.
    """
    if "\x00" in name:
        raise ValueError(f"identifier contains NUL byte: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def _escape_like(prefix: str) -> str:
    """Escape LIKE wildcards in ``prefix`` using ``\\`` as the escape
    character. Paired with ``ESCAPE '\\\\'`` in the query. PostgreSQL's
    LIKE/ESCAPE grammar matches SQLite's per the SQL standard.
    """
    out = prefix
    for ch in ("\\", "%", "_"):
        out = out.replace(ch, "\\" + ch)
    return out


def _coerce_jsonb_for_write(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Coerce JSONB-bound dict values to JSON strings for asyncpg.

    PersistenceManager pre-stringifies JSON payloads before calling
    row_upsert (see manager.py's ``_capture_registry_row`` etc.), so most
    incoming values are already strings. This guard catches the case
    where a caller passes a dict directly: asyncpg's default JSONB
    encoder accepts strings, not dicts, so the conversion happens here
    rather than at the asyncpg seam where the error would surface as a
    cryptic encoder failure.
    """
    cols = JSONB_COLUMNS.get(table, frozenset())
    if not cols:
        # Copy even when no coercion is needed so the backend never holds
        # a reference to the caller's dict (copy-on-store, matching the
        # JSONB branch below).
        return dict(row)
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k in cols and isinstance(v, dict):
            out[k] = json.dumps(v)
        else:
            out[k] = v
    return out


def _decode_jsonb_for_read(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Normalize JSONB columns to JSON strings for cross-backend Protocol
    compatibility.

    SQLiteBackend stores JSON payloads as TEXT and returns plain strings.
    asyncpg's default JSONB decoder returns the raw JSON string (per the
    asyncpg docs: ``json, jsonb [convert to] str``). The pass-through
    branch handles this default case. The ``isinstance(v, (dict, list))``
    branch handles the case where a custom JSONB codec has been registered
    on the connection (returning Python dicts/lists) -- the function
    re-serializes those back to JSON strings so row_select's output shape
    matches SQLiteBackend's regardless of asyncpg codec configuration.
    """
    cols = JSONB_COLUMNS.get(table, frozenset())
    if not cols:
        return row
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k in cols and isinstance(v, (dict, list)):
            out[k] = json.dumps(v)
        else:
            out[k] = v
    return out


# // ========================================( Class )======================================== // #


class PostgresBackend:
    """Persistent PostgreSQL implementation of :class:`PersistenceBackend`.

    Opens an :class:`asyncpg.Pool` in :meth:`initialize` plus a separate
    listener connection for ``LISTEN``/``NOTIFY``. Both close cleanly on
    :meth:`close`.

    Declares every capability -- relational rows, TTL index support,
    schema metadata, KV surface -- so any namespace config works against
    it without configuration.

    Cross-process invalidation: writes broadcast on the
    ``cascadeui_invalidation`` channel. Register a callback with
    :meth:`set_invalidation_callback` to consume notifications.
    """

    capabilities: ClassVar[Capability] = (
        Capability.KV
        | Capability.RELATIONAL
        | Capability.TTL_INDEX
        | Capability.SCHEMA_META
        | Capability.RAW_SQL
    )

    placeholder_style: ClassVar[str] = "numeric"

    # Listener loop tunables. Heartbeat is the interval between health
    # checks on the dedicated listener connection; retry is the delay
    # before reconnecting after a caught exception. Subclasses override
    # these to trade failover responsiveness against PG round-trip cost
    # (a connection that is healthy answers ``is_closed()`` from local
    # state, so the heartbeat cost is local; a torn connection only
    # surfaces on the next health check).
    listener_poll_seconds: ClassVar[float] = 10.0
    listener_retry_seconds: ClassVar[float] = 5.0

    def __init__(
        self,
        dsn: str,
        *,
        pool_kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        """Construct a PostgresBackend.

        :param dsn: PostgreSQL connection string. Supports the standard
            libpq URL format (``postgresql://user:pass@host:port/db?sslmode=...``).
            See ``postgresql.org/docs/current/libpq-connect.html`` for the
            full parameter list. Production deployments should use
            ``sslmode=verify-full``.
        :param pool_kwargs: Extra keyword arguments forwarded to
            :func:`asyncpg.create_pool`. Library defaults
            (``min_size=2``, ``max_size=10``, ``statement_cache_size=1024``)
            apply first; ``pool_kwargs`` overrides on a per-key basis.
            Set ``statement_cache_size=0`` when running behind pgbouncer
            in transaction or statement mode -- asyncpg's prepared
            statement cache is incompatible with those modes.
        """
        if not isinstance(dsn, str):
            raise TypeError(f"dsn must be str, got {type(dsn).__name__}")
        if not dsn:
            raise ValueError("dsn must be non-empty")
        if pool_kwargs is not None and not isinstance(pool_kwargs, dict):
            raise TypeError(f"pool_kwargs must be dict, got {type(pool_kwargs).__name__}")

        self._dsn = dsn
        self._pool_kwargs: dict[str, Any] = {
            "min_size": 2,
            "max_size": 10,
            "statement_cache_size": 1024,
            **(pool_kwargs or {}),
        }

        self._pool: Optional[asyncpg.Pool] = None
        self._listen_conn: Optional[asyncpg.Connection] = None
        # Background task set follows the PersistenceMiddleware convention:
        # ``add_done_callback(set.discard)`` keeps the set self-pruning when
        # tasks exit normally, and ``close()`` iterates the live set to
        # cancel survivors.
        self._tasks: set[asyncio.Task] = set()
        self._invalidation_callback: Optional[Callable[[str, str], None]] = None
        self._closing: bool = False

    # // ========================================( Lifecycle )======================================== // #

    async def initialize(self) -> None:
        """Open the pool, run DDL, start the LISTEN/NOTIFY listener.

        Safe to call more than once -- the second call is a no-op. DDL
        is ``CREATE TABLE IF NOT EXISTS`` throughout, so re-running it
        against a populated database does not drop data.

        Startup ordering follows the LISTEN race-condition rule from
        ``postgresql.org/docs/current/sql-listen.html`` notes:
        listener connection is opened and ``LISTEN`` committed before
        rehydrate inspects database state. Subsequent NOTIFY events fire
        after the LISTEN commit point, so the consumer-side rehydrate
        path observes a consistent snapshot.
        """
        if self._pool is not None:
            return

        self._pool = await asyncpg.create_pool(self._dsn, **self._pool_kwargs)

        async with self._pool.acquire() as conn:
            for stmt in ALL_DDL_PG:
                await conn.execute(stmt)

        # Listener connection is separate from the pool. add_listener
        # auto-issues LISTEN and commits.
        self._listen_conn = await asyncpg.connect(self._dsn)
        await self._listen_conn.add_listener(CHANNEL_INVALIDATION, self._on_notify)
        listen_task = asyncio.create_task(self._listen_loop())
        self._tasks.add(listen_task)
        listen_task.add_done_callback(self._tasks.discard)

        logger.debug(
            f"PostgresBackend initialized: pool min/max="
            f"{self._pool_kwargs['min_size']}/{self._pool_kwargs['max_size']}, "
            f"listener attached"
        )

    async def close(self) -> None:
        """Close the listener task, listener connection, and pool. Safe
        to call multiple times.
        """
        if self._pool is None and self._listen_conn is None:
            return

        self._closing = True

        # Snapshot the task set before cancelling: the done-callback
        # discards entries while iteration is in progress, and a
        # cancelled task can complete fast enough to mutate the set
        # mid-loop. Iterating a copy keeps the cancel + await pass
        # ordering deterministic.
        if self._tasks:
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            self._tasks.clear()

        if self._listen_conn is not None:
            try:
                await self._listen_conn.close()
            except Exception as exc:
                logger.debug(f"PostgresBackend listener close: {exc}")
            self._listen_conn = None

        if self._pool is not None:
            await self._pool.close()
            self._pool = None

        logger.debug("PostgresBackend closed")

    # // ========================================( Pool accessor )======================================== // #

    def _pool_or_raise(self) -> asyncpg.Pool:
        """Return the live pool or raise if uninitialized. Every public
        method routes through here so the error points at the setup bug
        rather than a misleading ``AttributeError`` on a NoneType.
        """
        if self._pool is None:
            raise RuntimeError(
                "PostgresBackend used before initialize(). "
                "Install PersistenceMiddleware via setup_middleware() or "
                "await backend.initialize() first."
            )
        return self._pool

    # // ========================================( Key-value surface )======================================== // #

    async def kv_read(self, namespace: str, key: str) -> Optional[bytes]:
        pool = self._pool_or_raise()
        table = _quote_ident(TABLE_KV)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT value FROM {table} WHERE namespace = $1 AND key = $2",
                namespace,
                key,
            )
        return bytes(row["value"]) if row is not None else None

    async def kv_write(self, namespace: str, key: str, value: bytes) -> None:
        pool = self._pool_or_raise()
        table = _quote_ident(TABLE_KV)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"""
                    INSERT INTO {table} (namespace, key, value)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value
                    """,
                    namespace,
                    key,
                    value,
                )
                await self._notify_invalidation(conn, namespace, key)

    async def kv_delete(self, namespace: str, key: str) -> None:
        pool = self._pool_or_raise()
        table = _quote_ident(TABLE_KV)
        async with pool.acquire() as conn:
            async with conn.transaction():
                result: str = await conn.execute(
                    f"DELETE FROM {table} WHERE namespace = $1 AND key = $2",
                    namespace,
                    key,
                )
                # Skip NOTIFY when no row was deleted -- the Protocol's
                # "silent no-op if the row does not exist" contract
                # forbids spurious cross-process invalidations.
                if _parse_command_tag_count(result) > 0:
                    await self._notify_invalidation(conn, namespace, key)

    async def kv_scan(self, namespace: str, prefix: str = "") -> AsyncIterator[tuple[str, bytes]]:
        pool = self._pool_or_raise()
        table = _quote_ident(TABLE_KV)
        async with pool.acquire() as conn:
            if prefix:
                pattern = _escape_like(prefix) + "%"
                rows = await conn.fetch(
                    f"""
                    SELECT key, value FROM {table}
                    WHERE namespace = $1 AND key LIKE $2 ESCAPE '\\'
                    """,
                    namespace,
                    pattern,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT key, value FROM {table} WHERE namespace = $1",
                    namespace,
                )

        # Snapshot before yielding so caller mutation during iteration
        # is safe (Protocol contract: scan-snapshot safety).
        for row in rows:
            yield row["key"], bytes(row["value"])

    # // ========================================( Relational surface )======================================== // #

    def _build_upsert_sql(self, namespace: str, cols: list[str], key_columns: list[str]) -> str:
        """Assemble an excluded-table upsert INSERT for ``cols`` with ``$n``
        placeholders. Non-key columns are overwritten on conflict; an
        all-key-column row resolves to DO NOTHING."""
        table = _quote_ident(namespace)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
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

        coerced = _coerce_jsonb_for_write(namespace, row)
        cols = list(coerced.keys())
        sql = self._build_upsert_sql(namespace, cols, key_columns)

        pool = self._pool_or_raise()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql, *(coerced[c] for c in cols))
                # Broadcast invalidation keyed by namespace + first key
                # column value. Consumer-side filtering decides whether
                # the row is interesting.
                await self._notify_invalidation(
                    conn, namespace, str(coerced.get(key_columns[0], ""))
                )

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

        # Coerce up front and group by column signature so each executemany
        # shares one statement. Flush rows are homogeneous, so this is
        # usually a single group; a mixed-shape batch produces one group per
        # column set.
        coerced_rows: list[dict[str, Any]] = []
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for row in rows:
            if not row:
                raise ValueError("row_upsert_many requires at least one column per row")
            coerced = _coerce_jsonb_for_write(namespace, row)
            coerced_rows.append(coerced)
            groups.setdefault(tuple(coerced.keys()), []).append(coerced)

        pool = self._pool_or_raise()
        # One connection acquire + one transaction for the whole batch
        # instead of one per row. The NOTIFYs ride the same connection and
        # deliver on commit. The listener contract is keyed per (namespace,
        # key), so each row still emits its own invalidation.
        async with pool.acquire() as conn:
            async with conn.transaction():
                for cols, group in groups.items():
                    sql = self._build_upsert_sql(namespace, list(cols), key_columns)
                    await conn.executemany(sql, [tuple(r[c] for c in cols) for r in group])
                for coerced in coerced_rows:
                    await self._notify_invalidation(
                        conn, namespace, str(coerced.get(key_columns[0], ""))
                    )

    async def row_select(
        self,
        namespace: str,
        where: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        pool = self._pool_or_raise()
        table = _quote_ident(namespace)
        if where:
            cols = list(where.keys())
            clause = " AND ".join(f"{_quote_ident(c)} = ${i + 1}" for i, c in enumerate(cols))
            sql = f"SELECT * FROM {table} WHERE {clause}"
            params = tuple(where[c] for c in cols)
        else:
            sql = f"SELECT * FROM {table}"
            params = ()

        async with pool.acquire() as conn:
            records = await conn.fetch(sql, *params)

        # asyncpg.Record -> plain dict so callers can mutate freely without
        # touching the cursor's backing buffer. Matches SQLiteBackend's
        # copy-on-return contract. JSONB columns are restringified so the
        # output shape matches SQLiteBackend's TEXT-stored JSON columns.
        return [_decode_jsonb_for_read(namespace, dict(r)) for r in records]

    async def row_delete(
        self,
        namespace: str,
        where: dict[str, Any],
    ) -> int:
        if not where:
            raise ValueError("row_delete requires a non-empty where clause")

        pool = self._pool_or_raise()
        table = _quote_ident(namespace)
        cols = list(where.keys())
        clause = " AND ".join(f"{_quote_ident(c)} = ${i + 1}" for i, c in enumerate(cols))
        sql = f"DELETE FROM {table} WHERE {clause}"
        async with pool.acquire() as conn:
            async with conn.transaction():
                result: str = await conn.execute(sql, *(where[c] for c in cols))
                count = _parse_command_tag_count(result)
                # Notify when rows actually changed so cross-process
                # listeners observe the deletion and invalidate caches.
                if count > 0:
                    key_repr = str(where[cols[0]]) if cols else ""
                    await self._notify_invalidation(conn, namespace, key_repr)
        return count

    async def row_delete_where_lt(
        self,
        namespace: str,
        column: str,
        value: Any,
    ) -> int:
        # NULL-safe natively: PostgreSQL treats ``NULL < anything`` as NULL
        # (never true), so rows without a value in ``column`` are preserved.
        # Same SQL standard semantic SQLiteBackend relies on.
        pool = self._pool_or_raise()
        table = _quote_ident(namespace)
        col = _quote_ident(column)
        sql = f"DELETE FROM {table} WHERE {col} < $1"
        async with pool.acquire() as conn:
            async with conn.transaction():
                result: str = await conn.execute(sql, value)
                count = _parse_command_tag_count(result)
                # Bulk delete may affect many rows; emit a namespace-only
                # NOTIFY (empty key = "invalidate everything in this
                # namespace") so listeners drop their caches without
                # us tracking individual row keys.
                if count > 0:
                    await self._notify_invalidation(conn, namespace, "")
        return count

    # // ========================================( Schema metadata surface )======================================== // #

    async def get_schema_version(self, table: str) -> int:
        pool = self._pool_or_raise()
        meta = _quote_ident(TABLE_SCHEMA_META)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT schema_version FROM {meta} WHERE table_name = $1",
                table,
            )
        return int(row["schema_version"]) if row is not None else 0

    async def set_schema_version(self, table: str, version: int) -> None:
        pool = self._pool_or_raise()
        meta = _quote_ident(TABLE_SCHEMA_META)
        applied_at = int(time.time())
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"""
                    INSERT INTO {meta} (table_name, schema_version, applied_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (table_name) DO UPDATE SET
                        schema_version = excluded.schema_version,
                        applied_at = excluded.applied_at
                    """,
                    table,
                    version,
                    applied_at,
                )

    # // ========================================( Raw SQL surface )======================================== // #

    async def execute(self, sql: str, *params: Any) -> int:
        """Execute an SQL statement. Caller-supplied SQL runs verbatim
        with the provided positional ``params`` bound through asyncpg.
        Use ``$1``, ``$2`` placeholders to match PostgreSQL's parameter
        style.

        Returns the affected-row count parsed from asyncpg's command tag
        string. DDL statements (CREATE/ALTER/DROP) return ``0``. Inside
        a ``transaction()`` block the call routes onto the transaction's
        connection via ``_CURRENT_TXN_CONN``; outside one a connection
        is acquired from the pool for the duration of the call.
        """
        if not sql:
            raise ValueError("execute requires a non-empty sql string")
        txn_conn = _CURRENT_TXN_CONN.get()
        if txn_conn is not None:
            result: str = await txn_conn.execute(sql, *params)
        else:
            pool = self._pool_or_raise()
            async with pool.acquire() as conn:
                result = await conn.execute(sql, *params)
        return _parse_command_tag_count(result)

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        """Execute an SQL query and return all rows as dicts. Returns
        an empty list for queries that yield no rows. Each dict's keys
        are column names from the query (or aliases via ``AS``); rows
        are defensive copies independent of asyncpg's record buffers.
        """
        if not sql:
            raise ValueError("fetch requires a non-empty sql string")
        txn_conn = _CURRENT_TXN_CONN.get()
        if txn_conn is not None:
            records = await txn_conn.fetch(sql, *params)
        else:
            pool = self._pool_or_raise()
            async with pool.acquire() as conn:
                records = await conn.fetch(sql, *params)
        return [dict(r) for r in records]

    async def executemany(self, sql: str, params_list: list[tuple]) -> int:
        """Execute an SQL statement against multiple parameter sets in
        one round trip. Returns ``len(params_list)`` as a best-effort
        approximation -- asyncpg's executemany does not report
        per-statement row counts. Empty ``params_list`` is a no-op
        returning ``0``. Inside a transaction the call routes onto the
        transaction's connection.
        """
        if not sql:
            raise ValueError("executemany requires a non-empty sql string")
        if not params_list:
            return 0
        txn_conn = _CURRENT_TXN_CONN.get()
        if txn_conn is not None:
            await txn_conn.executemany(sql, params_list)
        else:
            pool = self._pool_or_raise()
            async with pool.acquire() as conn:
                await conn.executemany(sql, params_list)
        return len(params_list)

    async def fetch_one(self, sql: str, *params: Any) -> Optional[dict[str, Any]]:
        """Execute an SQL query and return the first row as a dict, or
        ``None`` if the query yields no rows. The empty-result return
        is the contract; callers enforce single-row constraints
        explicitly.
        """
        if not sql:
            raise ValueError("fetch_one requires a non-empty sql string")
        txn_conn = _CURRENT_TXN_CONN.get()
        if txn_conn is not None:
            record = await txn_conn.fetchrow(sql, *params)
        else:
            pool = self._pool_or_raise()
            async with pool.acquire() as conn:
                record = await conn.fetchrow(sql, *params)
        return dict(record) if record is not None else None

    def transaction(self) -> "_PostgresTransaction":
        """Open an explicit transaction context.

        Outermost entries acquire a connection from the pool and bind
        it to the ``_CURRENT_TXN_CONN`` ContextVar; raw-SQL methods
        within the block route onto that connection. Nested entries
        reuse the outer connection and create savepoints via asyncpg's
        native ``Connection.transaction()`` nesting support.

        Only the raw-SQL methods (``execute``, ``fetch``,
        ``executemany``, ``fetch_one``) participate in the transaction.
        Namespace API methods (``row_upsert``, ``row_select``,
        ``kv_*``) acquire their own connections and auto-commit per
        call.

        The connection is held for the lifetime of the ``async with``
        block. Long-running transactions starve the pool; keep
        transaction bodies short.
        """
        return _PostgresTransaction(self)

    # // ========================================( LISTEN / NOTIFY )======================================== // #

    def set_invalidation_callback(self, callback: Optional[Callable[[str, str], None]]) -> None:
        """Register a callback that fires for each cross-process
        invalidation notification.

        The callback receives ``(namespace, key)`` as positional args.
        Pass ``None`` to clear any registered callback.

        :raises TypeError: if ``callback`` is neither None nor callable.
        """
        if callback is not None and not callable(callback):
            raise TypeError(f"callback must be callable or None, got {type(callback).__name__}")
        self._invalidation_callback = callback

    async def _notify_invalidation(
        self, conn: asyncpg.Connection, namespace: str, key: str
    ) -> None:
        """Emit a NOTIFY for the (namespace, key) pair. Called inside the
        same transaction as the write so consumers observe the post-write
        state when the notification arrives.

        Per ``postgresql.org/docs/current/sql-notify.html``: "if a NOTIFY
        is executed inside a transaction, the notify events are not
        delivered until and unless the transaction is committed."
        """
        payload = json.dumps({"namespace": namespace, "key": key})
        if len(payload.encode("utf-8")) > _NOTIFY_PAYLOAD_LIMIT_BYTES:
            # Fallback to namespace-only payload; consumers treat empty
            # key as "invalidate everything in this namespace."
            payload = json.dumps({"namespace": namespace, "key": ""})
        await conn.execute("SELECT pg_notify($1, $2)", CHANNEL_INVALIDATION, payload)

    def _on_notify(
        self,
        conn: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """Async-listener callback invoked by asyncpg on each NOTIFY.

        Parses the JSON payload and forwards ``(namespace, key)`` to the
        registered consumer callback. Malformed payloads are logged and
        dropped -- they do not propagate exceptions back into asyncpg's
        listener machinery.
        """
        if self._invalidation_callback is None:
            return
        try:
            data = json.loads(payload)
            namespace = data.get("namespace", "")
            key = data.get("key", "")
            self._invalidation_callback(namespace, key)
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            logger.warning(f"PostgresBackend dropped malformed invalidation payload: {exc}")

    async def _listen_loop(self) -> None:
        """Maintain the listener connection across drops.

        On reconnect, re-issues ``LISTEN`` via ``add_listener``. The race
        rule from ``postgresql.org/docs/current/sql-listen.html`` --
        first commit LISTEN, then inspect database state -- holds at
        startup. After the brief drop/reconnect window, missed
        invalidations show as cache misses on the next read; the
        rehydrate path remains the source of truth.
        """
        while not self._closing:
            try:
                if self._listen_conn is None or self._listen_conn.is_closed():
                    # Reconnect via a local variable so a cancel-during-
                    # reconnect (close() racing with the connect or the
                    # add_listener await) does not orphan the new
                    # connection on the instance attribute. Ownership
                    # transfers to ``self._listen_conn`` only after
                    # ``add_listener`` returns successfully; on any
                    # exception (including CancelledError) the local
                    # ref is closed before propagating.
                    new_conn = None
                    try:
                        new_conn = await asyncpg.connect(self._dsn)
                        await new_conn.add_listener(CHANNEL_INVALIDATION, self._on_notify)
                        self._listen_conn = new_conn
                        new_conn = None
                        logger.info("PostgresBackend listener reconnected")
                    except BaseException:
                        if new_conn is not None:
                            try:
                                await new_conn.close()
                            except Exception:
                                pass
                        raise
                await asyncio.sleep(self.listener_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"PostgresBackend listener error: {exc}; "
                    f"retrying in {self.listener_retry_seconds}s"
                )
                await asyncio.sleep(self.listener_retry_seconds)


# // ========================================( Helpers )======================================== // #


def _parse_command_tag_count(tag: str) -> int:
    """Parse asyncpg's ``execute()`` return string into a row count.

    asyncpg returns SQL command tags like ``DELETE 3``, ``UPDATE 1``,
    ``INSERT 0 5``. The integer count is the last whitespace-separated
    token. Returns 0 when the tag has no numeric tail.
    """
    if not tag:
        return 0
    parts = tag.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


# // ========================================( Transaction helper )======================================== // #


class _PostgresTransaction:
    """PostgreSQL transaction context. Outermost entries acquire a pool
    connection and start an asyncpg transaction; nested entries reuse
    the outer connection and create savepoints natively (asyncpg's
    ``Connection.transaction()`` supports nesting via savepoints).

    Concurrent transactions in different asyncio Tasks isolate
    correctly: each Task that constructs its own ``backend.transaction()``
    acquires its own pool connection.

    .. warning::
        Spawning a child task with ``asyncio.create_task`` INSIDE a
        ``backend.transaction()`` block produces a child that inherits
        the parent task's ``_CURRENT_TXN_CONN`` ContextVar (per
        ``asyncio.create_task`` semantics: child tasks copy the parent's
        context at spawn time). Raw-SQL calls in the child route onto
        the parent's transaction connection, producing undefined
        ordering on a single connection and use-after-release if the
        parent commits before the child completes. Do not spawn child
        tasks inside a transaction body. Either issue all raw-SQL calls
        sequentially within the block, or fan out work BEFORE entering
        the transaction.
    """

    def __init__(self, backend: "PostgresBackend") -> None:
        self._backend = backend
        self._conn: Optional[asyncpg.Connection] = None
        self._txn: Optional[Any] = None
        self._token: Optional[Any] = None
        self._pool_acquire: Optional[Any] = None
        self._is_outermost: bool = False

    async def __aenter__(self) -> "_PostgresTransaction":
        existing = _CURRENT_TXN_CONN.get()
        if existing is not None:
            self._conn = existing
            self._is_outermost = False
        else:
            pool = self._backend._pool_or_raise()
            self._pool_acquire = pool.acquire()
            self._conn = await self._pool_acquire.__aenter__()
            self._is_outermost = True
            self._token = _CURRENT_TXN_CONN.set(self._conn)
        try:
            self._txn = self._conn.transaction()
            await self._txn.__aenter__()
        except Exception:
            if self._is_outermost:
                if self._token is not None:
                    _CURRENT_TXN_CONN.reset(self._token)
                if self._pool_acquire is not None:
                    await self._pool_acquire.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        try:
            if self._txn is not None:
                await self._txn.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            if self._is_outermost:
                if self._token is not None:
                    _CURRENT_TXN_CONN.reset(self._token)
                if self._pool_acquire is not None:
                    await self._pool_acquire.__aexit__(exc_type, exc_val, exc_tb)
