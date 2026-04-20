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
prune, and schema migration are designed to produce the same end state
whether they run once or many times. This is deliberate: the Protocol
does not expose a transaction primitive, so crash-mid-operation recovery
relies on re-running the operation rather than rolling back. Future
additions to this surface must preserve the property -- any new method
that mutates state should either be safely repeatable or gated behind
an optional :class:`Capability` flag that transactional backends declare.

**The Protocol is backend-agnostic.** Methods operate on namespaces
(logical tables), rows (dicts), keys (strings), and values (bytes).
Raw SQL is not exposed. A :data:`Capability.RAW_SQL` flag plus optional
``execute(sql)`` method will be added when the first library migration
needs DDL -- until then, schema evolution lives in ``schema.py`` for
fresh installs and data rewrites via the row API for existing installs.
"""

# // ========================================( Modules )======================================== // #


from enum import Flag, auto
from typing import Any, AsyncIterator, ClassVar, Protocol, runtime_checkable

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
    """

    KV = auto()
    RELATIONAL = auto()
    TTL_INDEX = auto()
    SCHEMA_META = auto()


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
