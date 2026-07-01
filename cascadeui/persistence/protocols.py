"""Backend protocol and capability declaration for the persistence layer.

Backends implement :class:`PersistenceBackend` and declare their
capabilities as a class-level :class:`Capability` flag. Namespace
configs validate capabilities at setup time so misconfiguration
fails during bot startup, not mid-runtime.

The canonical import path is::

    from cascadeui.persistence import Capability, PersistenceBackend

Design contracts
----------------

**All library-owned persistence operations are idempotent.** Rehydrate,
prune, and schema migration produce the same end state whether they
run once or many times. The namespace API does not expose a transaction
primitive, so crash-mid-operation recovery relies on re-running the
operation rather than rolling back. Methods that mutate state are
safely repeatable; backends offering atomic grouping declare
:data:`Capability.RAW_SQL` and expose the explicit
:meth:`PersistenceBackend.transaction` context manager.

**The Protocol is backend-agnostic at the namespace level.** Methods
operate on namespaces (logical tables), rows (dicts), keys (strings),
and values (bytes). SQL backends that declare :data:`Capability.RAW_SQL`
expose an additional escape hatch (``execute``, ``fetch``,
``executemany``, ``fetch_one``, ``transaction()``) for user-managed
tables, vendor-specific features (PostgreSQL JSONB GIN queries, SQLite
FTS5), and ad-hoc DDL. Backends without ``Capability.RAW_SQL``
(in-memory, key-value-only) do not implement this surface; user code
that requires raw SQL checks the capability flag first.
"""

# // ========================================( Modules )======================================== // #


from contextlib import AbstractAsyncContextManager
from enum import Flag, auto
from typing import Any, AsyncIterator, ClassVar, Optional, Protocol, runtime_checkable

# // ========================================( Capability Flag )======================================== // #


class Capability(Flag):
    """Feature flags a backend declares to the manager at setup time.

    The manager validates required capabilities against each namespace
    config before any backend method is called. A backend missing a
    required capability raises :class:`PersistenceConfigError` during
    :meth:`PersistenceMiddleware.initialize`.

    Flag semantics:

    - ``KV`` -- basic key-value surface. All backends declare this.
    - ``RELATIONAL`` -- row-level upsert/select/delete with WHERE clauses.
      Needed for registry and scoped namespaces.
    - ``TTL_INDEX`` -- indexed TTL column for efficient prune. Needed
      when a namespace config sets ``ttl_days=N``.
    - ``SCHEMA_META`` -- supports the ``cascadeui_schema`` metadata
      table for migrator bookkeeping. All library-shipped backends
      declare this.
    - ``RAW_SQL`` -- exposes the raw-SQL escape hatch (``execute``,
      ``fetch``, ``executemany``, ``fetch_one``, ``transaction()``)
      for user-managed tables and vendor-specific features. SQL
      backends declare this; in-memory and key-value-only backends
      do not.
    """

    KV = auto()
    RELATIONAL = auto()
    TTL_INDEX = auto()
    SCHEMA_META = auto()
    RAW_SQL = auto()


# // ========================================( Backend Protocol )======================================== // #


@runtime_checkable
class PersistenceBackend(Protocol):
    """Library-owned persistence backend contract.

    Implement this Protocol (no inheritance required) to add a custom
    backend. Declare supported capabilities as a class-level
    :class:`Capability` flag; the manager checks required capabilities
    when :class:`PersistenceMiddleware` initializes.

    The Protocol uses ``@runtime_checkable`` so ``isinstance(backend,
    PersistenceBackend)`` validates method presence at setup. Users who
    prefer abstract-base semantics can subclass the Protocol directly.
    """

    capabilities: ClassVar[Capability]
    placeholder_style: ClassVar[str]
    """PEP 249 paramstyle the backend's raw-SQL methods expect.

    Meaningful only when ``Capability.RAW_SQL`` is declared. Standard
    values: ``"qmark"`` (``?`` placeholders, SQLite via aiosqlite),
    ``"numeric"`` (``$1``/``$2`` placeholders, PostgreSQL via asyncpg).
    Backends without raw-SQL support set this to ``"n/a"`` for
    informational consistency.

    Portable user code reads this property and formats SQL accordingly::

        ph = backend.placeholder_style
        sql = (
            "INSERT INTO t VALUES (?, ?)" if ph == "qmark"
            else "INSERT INTO t VALUES ($1, $2)"
        )
        await backend.execute(sql, val1, val2)
    """

    # Lifecycle

    async def initialize(self) -> None:
        """Open connections and create tables. Called once per backend
        instance during :meth:`PersistenceMiddleware.initialize`."""
        ...

    async def close(self) -> None:
        """Close connections cleanly. Called on bot shutdown."""
        ...

    # Key-value surface (Capability.KV)

    async def kv_read(self, namespace: str, key: str) -> bytes | None:
        """Return the raw value stored under ``(namespace, key)``, or
        ``None`` if absent."""
        ...

    async def kv_write(self, namespace: str, key: str, value: bytes) -> None:
        """Store ``value`` under ``(namespace, key)``. Overwrites any
        existing value."""
        ...

    async def kv_delete(self, namespace: str, key: str) -> None:
        """Delete the row at ``(namespace, key)``. Silent no-op if the
        row does not exist."""
        ...

    async def kv_scan(self, namespace: str, prefix: str = "") -> AsyncIterator[tuple[str, bytes]]:
        """Iterate all ``(key, value)`` pairs in ``namespace`` whose
        keys start with ``prefix``. Empty prefix yields everything."""
        ...

    # Relational surface (Capability.RELATIONAL)

    async def row_upsert(
        self,
        namespace: str,
        row: dict[str, Any],
        key_columns: list[str],
    ) -> None:
        """Upsert ``row`` into ``namespace``. ``key_columns`` names the
        primary-key columns used to detect conflicts."""
        ...

    async def row_upsert_many(
        self,
        namespace: str,
        rows: list[dict[str, Any]],
        key_columns: list[str],
    ) -> None:
        """Upsert ``rows`` into ``namespace`` in one batch. ``key_columns``
        names the primary-key columns used to detect conflicts. Each row is
        copied on store and resolved with the same conflict semantics as
        :meth:`row_upsert`.

        SQL backends collapse the batch into a single round-trip (one
        connection acquire + one transaction + ``executemany``). The
        persistence middleware calls ``row_upsert`` per row when a backend
        does not implement this method, so a backend may omit it. An empty
        ``rows`` list is a no-op."""
        ...

    async def row_select(
        self,
        namespace: str,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return all rows from ``namespace`` matching the optional
        equality ``where`` clause. Empty clause returns every row."""
        ...

    async def row_delete(
        self,
        namespace: str,
        where: dict[str, Any],
    ) -> int:
        """Delete rows matching the equality ``where`` clause. Returns
        the number of rows deleted."""
        ...

    async def row_delete_where_lt(
        self,
        namespace: str,
        column: str,
        value: Any,
    ) -> int:
        """Delete rows where ``column < value``. Returns the number of
        rows deleted. Used for TTL prune against indexed timestamp
        columns (``Capability.TTL_INDEX``)."""
        ...

    # Schema metadata surface (Capability.SCHEMA_META)

    async def get_schema_version(self, table: str) -> int:
        """Return the on-disk schema version for ``table``. Returns
        ``0`` when the table has no recorded version (fresh install)."""
        ...

    async def set_schema_version(self, table: str, version: int) -> None:
        """Record ``version`` as the current on-disk schema version for
        ``table``. Written atomically with the migration that produced
        it."""
        ...

    # Raw SQL surface (Capability.RAW_SQL)

    async def execute(self, sql: str, *params: Any) -> int:
        """Execute an SQL statement. Returns the affected-row count for
        ``INSERT``/``UPDATE``/``DELETE``; returns ``0`` for DDL
        statements that do not report row counts.

        The placeholder style of ``sql`` must match the backend's
        :attr:`placeholder_style` ClassVar. Caller-supplied SQL is
        executed verbatim; identifier quoting, SQL injection prevention,
        and dialect compatibility are caller responsibilities.

        Raises :class:`ValueError` if ``sql`` is empty. Backend-specific
        exceptions on SQL errors propagate unwrapped so callers can
        pattern-match on vendor error codes.
        """
        ...

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        """Execute an SQL query and return all rows as a list of dicts.

        Empty result returns an empty list. Each dict's keys are the
        column names from the query (or aliases via ``AS``). Rows are
        defensive copies -- caller mutation of the returned list cannot
        affect backend state.
        """
        ...

    async def executemany(self, sql: str, params_list: list[tuple]) -> int:
        """Execute an SQL statement against multiple parameter sets in
        a single round trip. Returns ``len(params_list)`` as a
        best-effort approximation of affected rows.

        Drivers do not consistently expose per-batch row counts (aiosqlite
        reports the last-row count only; asyncpg returns no count from
        ``executemany``), so implementations return the input length
        rather than an aggregate from the engine. All parameter tuples
        must have the same arity. Empty ``params_list`` is a no-op
        returning ``0``. Intended for bulk ``INSERT``/``UPDATE``/``DELETE``
        patterns; DDL statements that take no parameters use
        :meth:`execute` instead.
        """
        ...

    async def fetch_one(self, sql: str, *params: Any) -> Optional[dict[str, Any]]:
        """Execute an SQL query and return the first row as a dict, or
        ``None`` if the query returns no rows. The empty-result return
        is the contract; callers enforce single-row constraints
        explicitly.
        """
        ...

    def transaction(self) -> AbstractAsyncContextManager["Transaction"]:
        """Open an explicit transaction for atomic raw-SQL grouping.

        Operations within the ``async with`` block run on the same
        underlying connection and commit or roll back atomically.
        Nested transactions create savepoints natively (asyncpg and
        aiosqlite both support this).

        The returned object is an async context manager. Users do not
        inspect it directly; consumption is via ``async with``::

            async with backend.transaction():
                await backend.execute("INSERT INTO t VALUES (?, ?)", 1, "a")
                await backend.execute("INSERT INTO t VALUES (?, ?)", 2, "b")
            # Both committed atomically; either raised => both rolled back.

        Constraint: only the raw-SQL methods (``execute``, ``fetch``,
        ``executemany``, ``fetch_one``) participate in the transaction.
        The namespace API (``row_upsert``, ``row_select``, ``kv_*``)
        continues to auto-commit per call. To group namespace operations
        atomically, use raw SQL inside the transaction body.

        Raises :class:`NotImplementedError` if the backend does not
        declare ``Capability.RAW_SQL``.
        """
        ...


# // ========================================( Transaction Protocol )======================================== // #


@runtime_checkable
class Transaction(Protocol):
    """Async context manager returned by :meth:`PersistenceBackend.transaction`.

    Backends return implementations of this Protocol from their
    :meth:`~PersistenceBackend.transaction` method. Users consume them
    via ``async with``; the object itself has no public methods beyond
    the context-manager dunder pair.
    """

    async def __aenter__(self) -> "Transaction": ...

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None: ...
