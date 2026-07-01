"""Dict-backed persistence backend for testing and stateless deployments.

:class:`InMemoryBackend` implements the full :class:`PersistenceBackend`
Protocol using plain Python dicts as storage. Declares every capability
flag, so any namespace config works against it without configuration.

Two intended uses:

- **Tests.** Zero fixtures, zero disk I/O, zero cleanup. Construct a
  fresh backend per test or per session; data dies with the instance.
- **Reference implementation.** Every Protocol method is implemented
  in the most direct way possible, so new backend authors can read this
  file to see what each method is supposed to do.

Data is lost on process exit. For bots that need state to survive a
restart, use :class:`SQLiteBackend` or a custom Protocol implementation.
"""

# // ========================================( Modules )======================================== // #


from typing import Any, AsyncIterator, ClassVar

from ..protocols import Capability

# // ========================================( Class )======================================== // #


class InMemoryBackend:
    """Dict-backed implementation of :class:`PersistenceBackend`.

    Declares every capability flag so namespace configs that require
    ``RELATIONAL`` or ``TTL_INDEX`` work unchanged. Single-writer asyncio
    semantics are assumed -- concurrent access from multiple event loops
    is not supported.

    Storage layout:

    - ``_kv[namespace][key]`` holds bytes values for the KV surface.
    - ``_rows[namespace]`` holds a list of row dicts for the relational
      surface. Rows are plain dicts; the backend performs no type coercion.
    - ``_schema_versions[table]`` holds the current recorded schema version
      per table. Fresh instances return ``0`` for unknown tables, matching
      the Protocol contract.
    """

    capabilities: ClassVar[Capability] = (
        Capability.KV
        | Capability.RELATIONAL
        | Capability.TTL_INDEX
        | Capability.SCHEMA_META
        # Capability.RAW_SQL deliberately omitted -- in-memory storage
        # has no SQL engine to escape to. Code paths that require raw SQL
        # must check `Capability.RAW_SQL in backend.capabilities` first.
    )

    placeholder_style: ClassVar[str] = "n/a"

    def __init__(self) -> None:
        self._kv: dict[str, dict[str, bytes]] = {}
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self._schema_versions: dict[str, int] = {}

    # // ========================================( Lifecycle )======================================== // #

    async def initialize(self) -> None:
        """No-op. In-memory storage needs no setup."""
        return

    async def close(self) -> None:
        """No-op. In-memory storage needs no teardown."""
        return

    # // ========================================( Key-value surface )======================================== // #

    async def kv_read(self, namespace: str, key: str) -> bytes | None:
        return self._kv.get(namespace, {}).get(key)

    async def kv_write(self, namespace: str, key: str, value: bytes) -> None:
        self._kv.setdefault(namespace, {})[key] = value

    async def kv_delete(self, namespace: str, key: str) -> None:
        self._kv.get(namespace, {}).pop(key, None)

    async def kv_scan(self, namespace: str, prefix: str = "") -> AsyncIterator[tuple[str, bytes]]:
        # Snapshot the items list so the caller can mutate the namespace
        # through kv_write/kv_delete mid-iteration without RuntimeError.
        for key, value in list(self._kv.get(namespace, {}).items()):
            if key.startswith(prefix):
                yield key, value

    # // ========================================( Relational surface )======================================== // #

    async def row_upsert(
        self,
        namespace: str,
        row: dict[str, Any],
        key_columns: list[str],
    ) -> None:
        rows = self._rows.setdefault(namespace, [])
        for idx, existing in enumerate(rows):
            if all(existing.get(col) == row.get(col) for col in key_columns):
                # Copy-on-store so caller mutation to the input dict
                # does not leak into the backing row.
                rows[idx] = dict(row)
                return
        rows.append(dict(row))

    async def row_upsert_many(
        self,
        namespace: str,
        rows: list[dict[str, Any]],
        key_columns: list[str],
    ) -> None:
        # No round-trip to batch for the in-memory store -- per-row upsert
        # is O(1) amortized, and delegating keeps copy-on-store and conflict
        # semantics identical to row_upsert.
        for row in rows:
            await self.row_upsert(namespace, row, key_columns)

    async def row_select(
        self,
        namespace: str,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._rows.get(namespace, [])
        if not where:
            return [dict(r) for r in rows]
        return [dict(r) for r in rows if all(r.get(col) == val for col, val in where.items())]

    async def row_delete(
        self,
        namespace: str,
        where: dict[str, Any],
    ) -> int:
        rows = self._rows.get(namespace, [])
        if not rows:
            return 0
        kept: list[dict[str, Any]] = []
        deleted = 0
        for r in rows:
            if all(r.get(col) == val for col, val in where.items()):
                deleted += 1
            else:
                kept.append(r)
        self._rows[namespace] = kept
        return deleted

    async def row_delete_where_lt(
        self,
        namespace: str,
        column: str,
        value: Any,
    ) -> int:
        rows = self._rows.get(namespace, [])
        if not rows:
            return 0
        kept: list[dict[str, Any]] = []
        deleted = 0
        for r in rows:
            col_val = r.get(column)
            # NULL/missing values are kept (mirrors SQL's NULL comparison
            # semantics: NULL < anything evaluates to NULL, never true).
            if col_val is not None and col_val < value:
                deleted += 1
            else:
                kept.append(r)
        self._rows[namespace] = kept
        return deleted

    # // ========================================( Schema metadata surface )======================================== // #

    async def get_schema_version(self, table: str) -> int:
        return self._schema_versions.get(table, 0)

    async def set_schema_version(self, table: str, version: int) -> None:
        self._schema_versions[table] = version

    # // ========================================( Raw SQL opt-out stubs )======================================== // #

    # Capability.RAW_SQL is deliberately not declared on this backend --
    # in-memory storage has no SQL engine to escape to. The five methods
    # below raise NotImplementedError with a clear remediation message
    # rather than producing AttributeError, so callers who reach for the
    # escape hatch against InMemoryBackend get a directed error pointing
    # them at SQLiteBackend or PostgresBackend.

    _RAW_SQL_ERROR: ClassVar[str] = (
        "InMemoryBackend does not support Capability.RAW_SQL. "
        "Use SQLiteBackend or PostgresBackend for raw-SQL operations, "
        "or check `Capability.RAW_SQL in backend.capabilities` before "
        "reaching for the escape hatch."
    )

    async def execute(self, sql: str, *params: Any) -> int:
        raise NotImplementedError(self._RAW_SQL_ERROR)

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        raise NotImplementedError(self._RAW_SQL_ERROR)

    async def executemany(self, sql: str, params_list: list[tuple]) -> int:
        raise NotImplementedError(self._RAW_SQL_ERROR)

    async def fetch_one(self, sql: str, *params: Any) -> dict[str, Any] | None:
        raise NotImplementedError(self._RAW_SQL_ERROR)

    def transaction(self) -> Any:
        raise NotImplementedError(self._RAW_SQL_ERROR)
