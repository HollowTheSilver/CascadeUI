"""Persistence coordinator for the two-namespace architecture.

:class:`PersistenceManager` owns the lifecycle of every configured
backend and coordinates the two namespaces (registry, application
slots) against the store. The manager is constructed by
:class:`~cascadeui.state.middleware.PersistenceMiddleware` from the
two namespace configs and a store reference; callers interact with
the manager for explicit prunes, slot-policy registration, and
shutdown.

When any registered :class:`SlotPolicy` carries ``ttl_days``, the
manager starts a daily background TTL sweeper during
:meth:`PersistenceMiddleware.initialize`. The sweeper calls
``row_delete_where_lt(TABLE_APPLICATION_SLOTS, "expires_at", now)``
once per 24 hours, so rows with ``expires_at=NULL`` (no TTL) are
never touched by contract. ``expires_at`` is an absolute wall-clock
timestamp written at write-time, so TTL does not restart across
bot restarts. :meth:`rehydrate` issues one prune pass before reading
so rows that expired while the bot was offline are dropped rather
than loaded into memory.

Lifecycle phases, driven by :meth:`PersistenceMiddleware.initialize`
when the middleware is installed via :func:`setup_middleware`:

1. :meth:`initialize_backends` -- dedup by backend identity, call
   :meth:`~PersistenceBackend.initialize` once per unique instance.
2. :meth:`apply_migrations` -- for each configured namespace, run any
   registered schema migrators to bring the table from the on-disk
   version to :data:`CURRENT_SCHEMA_VERSIONS`.
3. :meth:`rehydrate` -- blocking read from each namespace into the
   in-memory store. Returns only when the store is fully restored.
4. :meth:`reattach_persistent_views` -- (requires ``bot``) walks the
   registry, re-fetches messages, reconstructs view instances.

Runtime surface: :meth:`prune_application`, :meth:`prune_registry`,
:meth:`register_slot_policy`, :meth:`close`. Each prune dispatches its
corresponding bookkeeping action (:data:`APPLICATION_SLOTS_PRUNED`,
:data:`REGISTRY_PRUNED`) so subscribers observe the event.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import inspect
import json
import logging
import time
from collections import Counter
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..exceptions import (
    PersistenceConfigError,
    PersistenceInitError,
    PersistenceRehydrateError,
    PersistenceSchemaError,
)
from ..state.actions import ActionCreators
from ..utils.hooks import await_maybe
from ..utils.responses import DISCORD_CALL_ERRORS
from .config import (
    NAMESPACE_APPLICATION,
    NAMESPACE_REGISTRY,
    ApplicationPersistence,
    RegistryPersistence,
    SlotPolicy,
)
from .migrations import get_kwargs_migrator, get_schema_migrator, physical_table
from .protocols import Capability, PersistenceBackend
from .schema import (
    CURRENT_SCHEMA_VERSIONS,
    LEGACY_TABLE_RENAMES,
    TABLE_APPLICATION_SLOTS,
    TABLE_PERSISTENT_VIEWS,
    TABLE_SCHEMA_META,
    apply_table_prefix,
)

if TYPE_CHECKING:
    from ..state.store import StateStore

logger = logging.getLogger(__name__)


# Kwargs captured by ``__init_subclass__`` that are also surfaced as
# their own registry-row column (``persistence_key``) or are not safely
# round-trippable through JSON (``theme`` is a live ``Theme`` object;
# ``bot`` is a live client injected via ``on_bind`` on restore).
# The middleware strips them at write so the registry stays clean, and
# ``_reattach_one`` strips them at read so the row column wins on
# reconstruction without a duplicate-keyword crash.
_NON_PERSISTABLE_KWARGS: frozenset[str] = frozenset({"persistence_key", "theme", "bot"})


# // ========================================( Manager )======================================== // #


class PersistenceManager:
    """Owns backend lifecycle and namespace routing for the store."""

    def __init__(
        self,
        store: "StateStore",
        registry: Optional[RegistryPersistence] = None,
        application: Optional[ApplicationPersistence] = None,
        bot: Any = None,
        restore_concurrency: int = 8,
    ) -> None:
        self._store = store
        self._bot = bot
        # Bounds per-row Discord round-trips across both restore phases; see
        # _run_post_ready_restore for the per-channel serialization the
        # repaint adds on top of this bound.
        self.restore_concurrency = restore_concurrency

        # Default to opted-out configs so every namespace has a config
        # object. Avoids None-branching at every call site.
        self.registry: RegistryPersistence = registry or RegistryPersistence(backend=None)
        self.application: ApplicationPersistence = application or ApplicationPersistence(
            backend=None
        )

        # Dedup backends by identity. A single SQLiteBackend serving
        # both namespaces should receive one initialize() call, not two.
        self._unique_backends: dict[int, PersistenceBackend] = {}
        for ns_cfg in (self.registry, self.application):
            if ns_cfg.backend is not None:
                self._unique_backends[id(ns_cfg.backend)] = ns_cfg.backend

        self._initialized: bool = False
        self._rehydrated: bool = False
        self._closed: bool = False
        self._registry_rows: list[dict[str, Any]] = []
        # Keys restored across reattach passes. reattach() re-drives the
        # reattach and skips these so an already-live panel is never re-fetched
        # or double-registered on a second pass.
        self._restored_keys: set[str] = set()
        # Which reattach pass is running. The first is the library's own,
        # during initialize(); later ones come from reattach(). A class
        # missing on the first is expected and on a later one is not.
        self._reattach_passes: int = 0
        # Summary from the most recent reattach_persistent_views() (restored /
        # skipped / failed / removed key lists). Stashed so a consumer can read
        # which persistence_keys were pruned for gone messages after
        # setup_middleware returns: REGISTRY_PRUNED fires once during reattach,
        # inside setup_middleware, so code that subscribes only after setup_hook
        # (e.g. on_ready) misses it. None until the first reattach.
        self.last_reattach_summary: Optional[dict[str, list[str]]] = None

        # Slot policy registry, seeded from ApplicationPersistence.slots
        # and extended at runtime by register_slot_policy.
        self._slot_policies: dict[str, SlotPolicy] = dict(self.application.slots)

        # A policy declaring persistent=True is an opt-in, so it registers
        # the slot the same way persistent_slots and access_slot do. Seeding
        # here rather than in PersistenceMiddleware.initialize() covers both
        # construction paths: a caller passing a pre-built manager marks the
        # middleware initialized, so initialize() short-circuits and never
        # runs. Lazy import matches the other two seeding sites.
        for slot_name, policy in self._slot_policies.items():
            if policy.persistent:
                self._register_persistent_slot(slot_name)

        # TTL sweeper task. Started during PersistenceMiddleware.initialize()
        # only when at least one slot declares ttl_days > 0. Cancelled by close().
        self._ttl_sweeper_task: Optional[asyncio.Task] = None
        # Post-ready on_restore render tasks. Each reattach call schedules its
        # own and tracks it here (mirrors PersistenceMiddleware._tasks), so a
        # second reattach never drops a batch. Cancelled by close().
        self._post_ready_restore_tasks: set = set()

        # Observability hooks for operators/devtools. Read by the
        # persistence middleware via _fire_hook(). Empty by default;
        # users register via register_hook().
        self._hooks: dict[str, list[Callable[..., Any]]] = {}

        # Middleware handle, set by PersistenceMiddleware.initialize once it
        # has resolved this manager. Stays None only for a manager built
        # directly in a test or a custom harness, where flush_all() and
        # close() then have no buffers to drain.
        self._middleware: Any = None

    # // ========================================( Introspection )======================================== // #

    @property
    def is_rehydrated(self) -> bool:
        return self._rehydrated

    @property
    def namespaces(self) -> dict[str, Any]:
        return {
            NAMESPACE_REGISTRY: self.registry,
            NAMESPACE_APPLICATION: self.application,
        }

    @property
    def backends(self) -> tuple[PersistenceBackend, ...]:
        """Unique backend instances in registration order."""
        return tuple(self._unique_backends.values())

    # // ========================================( Slot policy )======================================== // #

    def register_slot_policy(self, name: str, policy: SlotPolicy) -> None:
        """Register a slot policy at runtime. Raises :class:`ValueError`
        on re-registration so accidental overwrites surface immediately.

        When the new policy declares ``ttl_days`` and the daily sweeper
        is not yet running, bootstrap it here: the sweeper inspects
        ``_slot_policies`` once at start time and never re-checks, so a
        policy registered after it starts is invisible to it without this
        bootstrap. Idempotent: ``_start_ttl_sweeper`` is a no-op when the
        task is already alive or when the application backend is opted out.
        """
        if not isinstance(name, str):
            raise TypeError(f"slot name must be str, got {type(name).__name__}")
        if not isinstance(policy, SlotPolicy):
            raise TypeError(f"policy must be a SlotPolicy, got {type(policy).__name__}")
        if name in self._slot_policies:
            raise ValueError(f"Slot policy already registered for {name!r}")
        self._slot_policies[name] = policy
        if policy.persistent:
            self._register_persistent_slot(name)

        if policy.persistent and policy.ttl_days is not None:
            self._start_ttl_sweeper()

    @staticmethod
    def _register_persistent_slot(name: str) -> None:
        """Add ``name`` to the sticky opt-in set the middleware scans."""
        from ..state.slots import _PERSISTENT_SLOTS

        _PERSISTENT_SLOTS.add(name)

    def get_slot_policy(self, name: str) -> SlotPolicy:
        """Return the policy for ``name`` or the :class:`SlotPolicy`
        default when unregistered. First-write-without-policy: the slot
        is accepted under the namespace default TTL until an explicit
        policy registers; the fallback case is logged at DEBUG.
        """
        policy = self._slot_policies.get(name)
        if policy is None:
            # DEBUG because users who never declare slots still get sane
            # behavior; noise-free unless someone is actively auditing.
            logger.debug(f"No slot policy registered for {name!r}; using SlotPolicy()")
            return SlotPolicy()
        return policy

    # // ========================================( Lifecycle )======================================== // #

    async def initialize_backends(self) -> None:
        """Call :meth:`initialize` on every unique backend exactly once.

        Raises :class:`PersistenceInitError` wrapping the original
        exception so callers see a persistence-domain error rather
        than a raw connection failure.
        """
        if self._initialized:
            return
        for backend in self._unique_backends.values():
            try:
                await backend.initialize()
            except Exception as exc:
                raise PersistenceInitError(
                    f"Failed to initialize {type(backend).__name__}: {exc}"
                ) from exc
        self._initialized = True
        logger.debug(f"Initialized {len(self._unique_backends)} backend(s)")

    async def _reconcile_legacy_table_names(self) -> None:
        """Rename tables created under the pre-prefix names to the current ones.

        The registry and slots tables shipped as ``persistent_views`` and
        ``application_slots``; both now carry the ``cascadeui_`` prefix. A
        database created under the old names still holds every row a posted
        panel needs to reattach, so each configured namespace is checked
        before schema versions resolve -- this cannot be a versioned
        migrator, because migrators are keyed by table name and the name is
        what changes.

        Per table, the rename runs only when the old name exists, the old
        table carries the library's column signature, and the current name
        is absent or empty. ``initialize()`` runs the DDL before this, so on
        the first boot after an upgrade the current name always exists as a
        just-created empty shell; a table with zero rows is dropped to make
        way, which loses nothing by construction. Shell drop, rename, index
        swap, and version-row move commit as one transaction: a crash leaves
        either the old database or the finished rename, never a half state.

        The other combinations are left alone. An old table without the
        signature columns is a consumer's own and is never touched -- the
        case the prefixed names exist to protect. A populated table under
        both names is ambiguous: the library uses the current name and logs
        at WARNING so an operator can reconcile. No old table means a fresh
        install or a database already renamed, and nothing happens.
        """
        for ns_cfg, table in (
            (self.registry, TABLE_PERSISTENT_VIEWS),
            (self.application, TABLE_APPLICATION_SLOTS),
        ):
            backend = ns_cfg.backend
            if backend is None:
                continue
            if Capability.RAW_SQL not in backend.capabilities:
                # No SQL surface means no tables to rename: InMemoryBackend
                # holds nothing across restarts, and a custom non-SQL backend
                # keys rows by namespace string, which its author moves.
                logger.debug(
                    f"Skipping legacy-name reconciliation for {table} on "
                    f"{type(backend).__name__} (no raw-SQL surface)"
                )
                continue
            try:
                await self._reconcile_one_legacy_table(backend, table)
            except Exception as exc:
                legacy_name = physical_table(backend, LEGACY_TABLE_RENAMES[table].old_name)
                # The probes run before the transaction opens, so a second
                # process booting alongside this one can commit the rename in
                # between and this one fails on a table that is already gone.
                # Losing that race is the healthy outcome, and reporting it as
                # a rename failure would send an operator after GRANTs.
                if await self._legacy_rename_already_done(backend, legacy_name, table):
                    logger.debug(
                        f"Another process renamed {legacy_name!r} to "
                        f"{physical_table(backend, table)!r} first; nothing to do."
                    )
                    continue
                # Every other step of the pipeline wraps its failures in a
                # persistence-domain error; unwrapped, the driver's own type
                # leaves setup_hook naming neither the table nor the step. This
                # runs before the version loop, so on a role that cannot rename
                # it is the first DDL to fail and the one an operator sees.
                raise PersistenceSchemaError(
                    f"Renaming {legacy_name!r} to {physical_table(backend, table)!r} "
                    f"failed: {type(exc).__name__}: {exc}. The rename commits as one "
                    f"transaction, so the database is unchanged and it retries on the "
                    f"next start. A rename runs DDL: on PostgreSQL that needs table "
                    f"ownership, which the usual SELECT/INSERT/UPDATE/DELETE grants do "
                    f"not confer."
                ) from exc
            # Outside the rename path on purpose: a database renamed under an
            # earlier release early-returns at the old_exists probe and would
            # never reach a repair living inside that transaction.
            await self._normalize_pkey_name(backend, table)

    async def _legacy_rename_already_done(
        self, backend: PersistenceBackend, legacy_name: str, table: str
    ) -> bool:
        """Report whether another process completed this rename first.

        Answered by the state on disk rather than by the exception, because
        the drivers spell a missing table differently and a message match
        would go stale on a driver upgrade. Any failure here answers False,
        so the caller reports the original error rather than swallowing it
        behind a diagnostic that could not run.
        """
        try:
            if backend.placeholder_style == "qmark":
                sql = "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?"
            else:
                sql = (
                    "SELECT 1 AS present FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_type = 'BASE TABLE' AND table_name = $1"
                )
            old_gone = await backend.fetch_one(sql, legacy_name) is None
            new_there = await backend.fetch_one(sql, physical_table(backend, table)) is not None
            return old_gone and new_there
        except Exception:
            return False

    async def _reconcile_one_legacy_table(self, backend: PersistenceBackend, table: str) -> None:
        """Probe one table's rename preconditions, then rename atomically.

        See :meth:`_reconcile_legacy_table_names` for the decision table.
        """
        legacy = LEGACY_TABLE_RENAMES[table]
        prefix = getattr(backend, "table_prefix", "")
        phys_old = physical_table(backend, legacy.old_name)
        phys_new = physical_table(backend, table)
        qmark = backend.placeholder_style == "qmark"

        # Probes run before the transaction opens: a failed statement inside
        # a PostgreSQL transaction aborts the whole block, and the signature
        # probe below fails by design on a consumer's table.
        if qmark:
            exists_sql = "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?"
        else:
            # information_schema filters by privilege, which is load-bearing
            # rather than incidental: a role that cannot see the old table also
            # cannot rename it, so the skip below is correct instead of lucky.
            # pg_class and pg_tables do not filter, and reading either here
            # turns a correct silent skip into a wrong one.
            exists_sql = (
                "SELECT 1 AS present FROM information_schema.tables "
                "WHERE table_schema = current_schema() "
                "AND table_type = 'BASE TABLE' AND table_name = $1"
            )
        old_exists = await backend.fetch_one(exists_sql, phys_old) is not None
        if not old_exists:
            return
        new_exists = await backend.fetch_one(exists_sql, phys_new) is not None

        cols = ", ".join(legacy.signature_columns)
        try:
            await backend.fetch(f"SELECT {cols} FROM {phys_old} LIMIT 0")
        except Exception:
            # The driver raises its own vendor type unwrapped, so the except is
            # broad, and a broad except cannot tell a missing column from a
            # table that stopped existing between the check above and here.
            # Re-read existence before naming a cause: a concurrent boot that
            # renamed it is the one other way in, and reporting the library's
            # own table as a consumer's would send an operator after the wrong
            # one. Anything else resurfaces on the next statement.
            if await backend.fetch_one(exists_sql, phys_old) is None:
                logger.debug(
                    f"Table {phys_old!r} went away between the existence check "
                    f"and the column probe; another process reconciled it."
                )
                return
            logger.info(
                f"Table {phys_old!r} exists but does not carry the library's "
                f"columns ({cols}); leaving it alone. The library uses "
                f"{phys_new!r}."
            )
            return

        if new_exists and await backend.fetch_one(f"SELECT 1 AS present FROM {phys_new} LIMIT 1"):
            logger.warning(
                f"Both {phys_old!r} and {phys_new!r} exist and hold rows. The "
                f"library reads and writes {phys_new!r} and will not touch "
                f"{phys_old!r}. Reconcile manually: move any rows still needed "
                f"into {phys_new!r}, then drop {phys_old!r}."
            )
            return

        ph1 = "?" if qmark else "$1"
        ph2 = "?" if qmark else "$2"
        meta = physical_table(backend, TABLE_SCHEMA_META)
        ver_sql = f"SELECT schema_version FROM {meta} WHERE table_name = {ph1}"
        old_ver = await backend.fetch_one(ver_sql, legacy.old_name)
        new_ver = await backend.fetch_one(ver_sql, table)

        async with backend.transaction():
            if new_exists:
                # The empty shell this boot's CREATE TABLE IF NOT EXISTS just
                # created; its index drops with it.
                await backend.execute(f"DROP TABLE {phys_new}")
            await backend.execute(f"ALTER TABLE {phys_old} RENAME TO {phys_new}")
            # A rename keeps the table's index under its old name on both
            # engines. Drop it and recreate from the current DDL: an index is
            # derived state, so the swap is safe, and left in place the next
            # boot's CREATE INDEX IF NOT EXISTS would add a second index over
            # the same columns under the current name.
            await backend.execute(
                f"DROP INDEX IF EXISTS {physical_table(backend, legacy.old_index)}"
            )
            await backend.execute(apply_table_prefix(legacy.index_ddl, prefix))
            if old_ver is not None:
                # Move the version record with the data it describes, or the
                # renamed table reads as unversioned and its migrators re-run.
                # A record already keyed by the current name loses to the
                # moved one: the only table it can describe is the shell
                # dropped above.
                if new_ver is not None:
                    await backend.execute(f"DELETE FROM {meta} WHERE table_name = {ph1}", table)
                await backend.execute(
                    f"UPDATE {meta} SET table_name = {ph1} WHERE table_name = {ph2}",
                    table,
                    legacy.old_name,
                )
        logger.info(f"Renamed legacy table {phys_old!r} to {phys_new!r}")

    async def _normalize_pkey_name(self, backend: PersistenceBackend, table: str) -> None:
        """Rename a primary-key constraint left under a table's legacy name.

        ``ALTER TABLE ... RENAME TO`` renames the table and nothing else on
        PostgreSQL: the inline PRIMARY KEY's auto-named constraint keeps
        ``persistent_views_pkey``, so an upgraded database and a fresh
        install diverge in the catalog by upgrade path alone. SQLite's
        autoindex follows the table through a rename, so only PostgreSQL
        needs this. ``RENAME CONSTRAINT`` renames the backing index with
        the constraint and needs the same table ownership the table rename
        needed, so it rides the same grant story.

        Failures log and return rather than raise: the table rename guards
        data reachability, but a constraint name guards only catalog
        uniformity (no library SQL names the primary-key index), so a
        stale name degrades nothing at runtime, and the normalization
        retries on the next start.
        """
        # placeholder_style stands in for "speaks pg_catalog SQL", the same
        # engine discriminator the existence probes above read. Positive
        # match, so an unknown engine skips rather than receiving this SQL.
        if backend.placeholder_style != "numeric":
            return

        phys = physical_table(backend, table)
        # PostgreSQL truncates an identifier at 63 bytes, so a long
        # table_prefix makes the name it stores shorter than the one built
        # here. Comparing the untruncated form would never match, and this
        # would try to rename on every boot and warn each time.
        desired = f"{phys}_pkey".encode()[:63].decode(errors="ignore")
        # Keyed on the table's own relation, never on a hardcoded old name:
        # PostgreSQL uniquifies auto-names (persistent_views_pkey1) when the
        # plain one is taken, and those must normalize too.
        conname_sql = (
            "SELECT c.conname AS conname "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE c.contype = 'p' AND t.relname = $1 "
            "AND n.nspname = current_schema()"
        )
        try:
            row = await backend.fetch_one(conname_sql, phys)
            if row is None:
                # Table absent, or a consumer's table with no primary key.
                return
            conname = row["conname"]
            if conname == desired:
                return
            # Index names are relations, so a squatter on the desired name
            # makes the rename fail; warn and leave the legacy name in place.
            taken = await backend.fetch_one(
                "SELECT 1 AS present FROM pg_class r "
                "JOIN pg_namespace n ON n.oid = r.relnamespace "
                "WHERE r.relname = $1 AND n.nspname = current_schema()",
                desired,
            )
            if taken is not None:
                logger.warning(
                    f"The primary-key constraint on {phys!r} keeps its legacy "
                    f"name {conname!r}: another relation already holds "
                    f"{desired!r} in this schema. Rename or drop that relation "
                    f"and the next start normalizes the constraint."
                )
                return
        except Exception as exc:
            logger.warning(
                f"Could not read the primary-key constraint name on {phys!r}: "
                f"{type(exc).__name__}: {exc}. Retried on the next start."
            )
            return

        # The discovered name is catalog data; quote it the way the backends
        # quote identifiers. The desired name is library-constructed.
        quoted = '"' + conname.replace('"', '""') + '"'
        try:
            # The rename takes ACCESS EXCLUSIVE, so any open read against the
            # table blocks it: a long query, a running dump, an outgoing
            # process still shutting down. Without a bound it waits rather
            # than failing, and a step designed to warn and retry would hang
            # the boot instead. The transaction scopes the timeout to this
            # statement, so nothing is left set on a pooled connection.
            async with backend.transaction():
                await backend.execute("SET LOCAL lock_timeout = '3s'")
                await backend.execute(f"ALTER TABLE {phys} RENAME CONSTRAINT {quoted} TO {desired}")
        except Exception as exc:
            # Same shape as _legacy_rename_already_done: a second booting
            # process can commit the rename between the probe and the ALTER.
            try:
                again = await backend.fetch_one(conname_sql, phys)
            except Exception:
                again = None
            if again is not None and again["conname"] == desired:
                logger.debug(
                    f"Another process renamed the primary-key constraint on "
                    f"{phys!r} to {desired!r} first; nothing to do."
                )
                return
            logger.warning(
                f"Renaming the primary-key constraint on {phys!r} from "
                f"{conname!r} to {desired!r} failed: {type(exc).__name__}: "
                f"{exc}. The stale name affects only catalog uniformity and "
                f"the rename retries on the next start. Renaming a constraint "
                f"needs table ownership, the same grant the table rename "
                f"needed."
            )
            return
        logger.info(f"Renamed primary-key constraint {conname!r} to {desired!r} on {phys!r}")

    async def apply_migrations(self) -> None:
        """Run registered schema migrators up to current version for
        each configured namespace. No-op when on-disk version equals
        current.

        Migrators are pulled from :mod:`cascadeui.persistence.migrations`
        via :func:`get_schema_migrator`. The loop advances one version
        per iteration so multi-step upgrades (v1 -> v3) run v1->v2
        then v2->v3 sequentially.

        A pending migration also checks the backend's capability
        declaration: :data:`Capability.OPEN_ROWS` means the migrator can
        skip its DDL, :data:`Capability.RAW_SQL` means it can run it, and
        a backend declaring neither raises
        :class:`~cascadeui.exceptions.PersistenceConfigError` here rather
        than rejecting registry writes after the version is recorded. The
        check never fires for a backend with nothing to migrate.

        A missing version row is not automatically a fresh install: a
        table with rows predates the version record (a partial restore, a
        hand-edited schema table) and is assumed to sit at v1, walked
        forward by the migrator chain from there; an empty table is a
        genuine fresh install, recorded directly at the current version.
        The row probe that tells them apart runs only on the boot that
        writes the record.

        Version resolution is keyed by table name, so
        :meth:`_reconcile_legacy_table_names` runs first: a database
        created under the pre-prefix table names is renamed, version
        record and all, before anything below reads it.
        """
        await self._reconcile_legacy_table_names()

        for ns_cfg, table in (
            (self.registry, TABLE_PERSISTENT_VIEWS),
            (self.application, TABLE_APPLICATION_SLOTS),
        ):
            if ns_cfg.backend is None:
                continue
            current = CURRENT_SCHEMA_VERSIONS[table]
            on_disk = await ns_cfg.backend.get_schema_version(table)

            if on_disk == 0:
                # Rows predate the version record and walk forward from v1
                # (see the docstring); an empty table is a fresh install. A
                # wrong guess only costs a re-run: every migrator is
                # idempotent (SQLite probes for the column, PostgreSQL uses
                # IF NOT EXISTS, an OPEN_ROWS backend skips its DDL entirely).
                if await ns_cfg.backend.row_select(table):
                    on_disk = 1
                    await ns_cfg.backend.set_schema_version(table, on_disk)
                else:
                    await ns_cfg.backend.set_schema_version(table, current)
                    continue

            while on_disk < current:
                migrator = get_schema_migrator(table, on_disk)
                if migrator is None:
                    raise PersistenceSchemaError(
                        f"No migrator registered for {table} "
                        f"v{on_disk} -> v{on_disk + 1}; cannot upgrade"
                    )
                caps = ns_cfg.backend.capabilities
                if not caps & (Capability.OPEN_ROWS | Capability.RAW_SQL):
                    # The migrator cannot run DDL without a raw-SQL surface,
                    # and skipping it is only safe when rows are open
                    # mappings. Refusing here is what keeps the failure at
                    # the misconfiguration instead of at the first registry
                    # write carrying a column the table does not have.
                    raise PersistenceConfigError(
                        f"{table} has a pending schema migration "
                        f"(v{on_disk} -> v{on_disk + 1}), and backend "
                        f"{type(ns_cfg.backend).__name__} declares neither "
                        f"Capability.OPEN_ROWS nor Capability.RAW_SQL. Declare "
                        f"OPEN_ROWS if rows are open mappings (unknown columns "
                        f"round-trip through row_upsert/row_select and a missing "
                        f"column reads as None), or declare RAW_SQL and implement "
                        f"the raw-SQL surface so the migrator can alter the table."
                    )
                try:
                    await migrator(ns_cfg.backend)
                except PersistenceSchemaError:
                    raise
                except Exception as exc:
                    # Unwrapped, the driver's own error leaves setup_hook
                    # naming neither the table nor the step; initialize_backends
                    # already wraps its failures this way, and this seam had
                    # nothing to wrap until the library shipped a migrator.
                    raise PersistenceSchemaError(
                        f"Migrating {table} from v{on_disk} to v{on_disk + 1} failed: "
                        f"{type(exc).__name__}: {exc}. The on-disk version is unchanged, "
                        f"so the migration retries on the next start. A migrator runs DDL: "
                        f"on PostgreSQL that needs table ownership, which the usual "
                        f"SELECT/INSERT/UPDATE/DELETE grants do not confer."
                    ) from exc
                on_disk += 1
                await ns_cfg.backend.set_schema_version(table, on_disk)
                logger.info(f"Migrated {table} to v{on_disk}")

            if on_disk > current:
                # DB was written by a newer CascadeUI release. Refuse
                # to run rather than silently downgrade data.
                raise PersistenceSchemaError(
                    f"{table} on-disk schema v{on_disk} is newer than "
                    f"library version v{current}; upgrade CascadeUI."
                )

    # // ========================================( Rehydrate )======================================== // #

    async def rehydrate(self) -> None:
        """Restore every configured namespace into the in-memory store.

        Blocking by design: callers downstream of
        :meth:`~cascadeui.state.middleware.PersistenceMiddleware.initialize`
        see a store with all persisted state already present.
        """
        if self.application.backend is not None:
            await self._rehydrate_application()
        if self.registry.backend is not None:
            await self._rehydrate_registry()
        self._rehydrated = True

    async def _rehydrate_application(self) -> None:
        backend = self.application.backend
        assert backend is not None

        # Drop expired rows before the select. expires_at is an absolute
        # timestamp written at write-time, so TTL does not restart across
        # restarts -- but the daily sweeper's first tick is 24h away, and
        # without this pass an already-expired slot would reappear in
        # memory for up to a day. Gated on "at least one TTL slot exists"
        # so zero-TTL deployments pay nothing.
        if any(p.ttl_days is not None and p.persistent for p in self._slot_policies.values()):
            try:
                await backend.row_delete_where_lt(
                    TABLE_APPLICATION_SLOTS, "expires_at", int(time.time())
                )
            except Exception as exc:
                # Prune failures should not block rehydrate -- the sweeper
                # will catch the rows on its next tick. Log and continue.
                logger.warning(f"Pre-rehydrate TTL prune failed: {exc}")

        try:
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        except Exception as exc:
            raise PersistenceRehydrateError(
                f"Failed to read {TABLE_APPLICATION_SLOTS}: {exc}"
            ) from exc

        state = self._store.state
        app = state.setdefault("application", {})
        restored = 0
        for row in rows:
            slot_name = row["slot_name"]
            try:
                app[slot_name] = json.loads(row["payload"])
            except (TypeError, ValueError) as exc:
                raise PersistenceRehydrateError(
                    f"Corrupt payload for slot {slot_name!r}: {exc}"
                ) from exc
            restored += 1
        logger.info(f"Rehydrated {restored} application slot(s)")

    async def _rehydrate_registry(self) -> None:
        backend = self.registry.backend
        assert backend is not None
        try:
            rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        except Exception as exc:
            raise PersistenceRehydrateError(
                f"Failed to read {TABLE_PERSISTENT_VIEWS}: {exc}"
            ) from exc

        # Registry rehydrate seeds the store's restored-registry buffer;
        # actual re-attachment (fetch_message + construct view) happens
        # in reattach_persistent_views once the bot is ready. Storing
        # the raw row list keeps the reattach step free of its own
        # backend read.
        self._registry_rows: list[dict[str, Any]] = list(rows)
        logger.info(f"Rehydrated {len(rows)} persistent view row(s)")

    # // ========================================( Reattach )======================================== // #

    async def reattach_persistent_views(self) -> dict[str, list[str]]:
        """Reconstruct PersistentView instances from the rows rehydrated
        by :meth:`_rehydrate_registry`.

        Returns a summary dict with five lists of ``persistence_key`` values:

        - ``restored`` -- reattached successfully.
        - ``skipped`` -- view class not imported OR missing kwargs
          migrator. Row stays on disk so the next restart can pick it
          up once the import or migrator is fixed.
        - ``failed`` -- construction or a migrator raised during reattach, or
          a 404-verdicted row could not be re-read or pruned because the
          backend raised. (``on_restore`` runs later, in a post-ready
          background task; its failures are logged there, not reflected in
          this bucket.) Row stays on disk; the next pass retries it.
        - ``removed`` -- channel or message returned a definitive 404
          (``discord.NotFound``). Row deleted from disk via :meth:`prune_registry`
          and its bookkeeping action dispatched.
        - ``unreachable`` -- channel or message could not be fetched for a
          transient reason (``Forbidden``, ``HTTPException``, or a non-messageable
          channel), or the row was rewritten mid-pass (re-posted under its
          stable key while this pass held a 404 verdict against the message it
          replaced) and points at a message this pass never fetched. The row is
          left on disk so a clean restart retries; nothing is pruned, and a
          rewritten row is not stamped.

        Requires ``self._bot``. No-op when bot is absent (data-only
        persistence mode). Every pass also re-registers the full
        ``DynamicPersistentButton`` registry with the bot via
        ``add_dynamic_items``, so dynamic-item dispatch stays current
        across re-drives.
        """
        summary: dict[str, list[str]] = {
            "restored": [],
            "skipped": [],
            "failed": [],
            "removed": [],
            "unreachable": [],
        }
        # Publish the summary now (a live reference, mutated in place below) so
        # it is reachable even on the early returns. A consumer reads
        # last_reattach_summary["removed"] after setup_middleware to reconcile
        # records for messages deleted while the bot was down.
        self.last_reattach_summary = summary
        if self._bot is None:
            return summary

        # Re-drive dynamic-item registration on every pass so a
        # DynamicPersistentButton subclass imported after the initial
        # reattach (a cog loaded later, a hot-reloaded extension) routes
        # clicks without a restart -- the same recovery reattach() gives
        # late-imported view classes. The full registry is passed each
        # time: discord.py keys dynamic items on their compiled template,
        # so re-registration is an idempotent dict write and a reloaded
        # class cleanly replaces its predecessor. Lazy import breaks the
        # components <-> persistence cycle.
        from ..components.base import _dynamic_button_classes

        if _dynamic_button_classes:
            self._bot.add_dynamic_items(*_dynamic_button_classes.values())

        # Skip keys already restored on a prior pass so reattach() only
        # processes rows that are new (a class imported after the initial
        # reattach) or still pending (skipped / unreachable / failed).
        rows = [
            r for r in self._registry_rows if r.get("persistence_key") not in self._restored_keys
        ]
        if not rows:
            return summary

        # Late import breaks the views <-> persistence cycle. The class
        # registry is populated by _PersistentMixin.__init_subclass__ at
        # import time, so user modules must be imported before this
        # method runs (standard setup_hook ordering).
        from ..views.persistent import _persistent_view_classes

        removed_keys: list[str] = []
        unreachable_keys: list[str] = []
        # Stored view_class strings no imported class registered under, counted
        # per string. summary["skipped"] also collects rows a kwargs migrator
        # turned away, so it cannot answer "which class is missing" on its own.
        missing_classes: Counter = Counter()
        # Fetch SUCCEEDED, whatever construction then did. Clearing on
        # restore-success instead would leave a stamp ageing on a panel
        # whose message is perfectly reachable and whose view merely
        # raised -- and that panel is live, so pruning it is the exact
        # outcome the stamp exists to prevent.
        reachable_keys: list[str] = []

        # Phase 1 (concurrent): resolve the view class, migrate kwargs, and
        # fetch the Discord channel + message for every row. These are the
        # per-row Discord round-trips that dominate startup; running them
        # concurrently (bounded by ``restore_concurrency``) overlaps the
        # network latency within Discord's rate budget instead of paying it
        # serially on the setup_hook critical path. The per-row helpers
        # already isolate failures (skipped / removed / failed land in the
        # summary), so a bad row never aborts the fan-out. No per-channel
        # ordering here, unlike the post-ready repaint: read buckets report
        # their limits in response headers, so the HTTP layer paces a
        # same-channel fetch burst itself. Message edits carry
        # header-invisible sub-limits and get grouped in
        # _run_post_ready_restore.
        self._reattach_passes += 1
        sem = asyncio.Semaphore(self.restore_concurrency)

        async def _prepare(row: dict[str, Any]) -> Optional[tuple]:
            # ``persistence_key`` is read defensively for the failure log;
            # the strict ``row[...]`` reads happen inside the try so a
            # malformed row (missing key) lands in summary["failed"] instead
            # of escaping the gather and aborting every other row's reattach.
            persistence_key = row.get("persistence_key", "<unknown>")
            try:
                persistence_key = row["persistence_key"]
                class_name = row["view_class"]
                view_cls = _persistent_view_classes.get(class_name)
                if view_cls is None:
                    # Not yet imported is the state the two-pass design exists
                    # to absorb: a cog loading after setup_middleware lands its
                    # panels here, and reattach() picks them up. Reporting each
                    # one at warning level put a fault on the sanctioned path
                    # and, at one line per row, taught an operator to skim the
                    # level. The aggregate below carries the signal, and says
                    # what to do about it.
                    logger.debug(
                        f"Persistent view class {class_name!r} not imported yet for "
                        f"persistence_key {persistence_key!r}; leaving it for reattach()."
                    )
                    summary["skipped"].append(persistence_key)
                    # Keyed by str: an OPEN_ROWS backend can store a NULL here,
                    # and a mixed-type Counter cannot be sorted for the message.
                    missing_classes[str(class_name)] += 1
                    return None

                migrated = await self._migrate_init_kwargs(row, view_cls, summary)
                if migrated is None:
                    # Summary list already populated by _migrate_init_kwargs.
                    return None

                async with sem:
                    fetched = await self._fetch_restore_message(row, removed_keys, unreachable_keys)
                if fetched is None:
                    # Already appended to removed_keys or unreachable_keys.
                    return None
                _channel, message = fetched
                reachable_keys.append(persistence_key)
                return (row, view_cls, migrated, message)
            except Exception as exc:
                logger.error(
                    f"Failed to prepare persistent view {persistence_key!r}: {exc}",
                    exc_info=True,
                )
                summary["failed"].append(persistence_key)
                return None

        prepared = await asyncio.gather(*(_prepare(row) for row in rows))

        # Phase 2 (serial): construct + register each prepared view. Store
        # mutation (add_view, _register_view) must stay serial -- concurrent
        # dispatches would race the shared registries. Batch state is
        # task-scoped and is no longer a reason on its own.
        restored_views: list = []
        for item in prepared:
            if item is None:
                continue
            row, view_cls, init_kwargs, message = item
            outcome = await self._reattach_one(
                row, view_cls, init_kwargs, message, row["view_class"], restored_views
            )
            summary[outcome].append(row["persistence_key"])

        # Delete rows whose channel or message disappeared while the bot
        # was offline. prune_registry dispatches REGISTRY_PRUNED so
        # subscribers observe the bookkeeping action. Nothing in this block
        # may escape the pass: phase 2 already registered views, and an
        # abort here skips the warm repaint and the _restored_keys update,
        # so a later reattach() re-drive would construct a second view for
        # a message that already has a live one.
        if removed_keys:
            verdicted = {r.get("persistence_key"): r for r in rows}
            confirmed, rewritten, unverified = await self._confirm_unchanged(
                removed_keys, verdicted
            )
            if confirmed:
                try:
                    await self.prune_registry(persistence_keys=confirmed)
                    summary["removed"].extend(confirmed)
                except Exception as exc:
                    logger.warning(
                        f"Pruning {len(confirmed)} gone row(s) failed mid-reattach: {exc}. "
                        f"The rows stay on disk and the next pass re-verdicts them."
                    )
                    summary["failed"].extend(confirmed)
            # A rewritten row points at a message this pass never fetched: it
            # is reported unreachable and left on disk with its mirror already
            # refreshed, so the next pass fetches the new coordinates. It is
            # kept out of the stamp write below -- nothing about the new
            # message failed, and stamping it would age a live panel.
            summary["unreachable"].extend(rewritten)
            summary["failed"].extend(unverified)
        # Transiently unreachable rows are NOT pruned -- they stay on disk so a
        # clean restart retries the fetch. Reported separately so a consumer
        # does not mistake a momentary glitch for a definitive deletion.
        if unreachable_keys:
            summary["unreachable"].extend(unreachable_keys)
            await self._write_unreachable_stamps(unreachable_keys, int(time.time()))
        if reachable_keys:
            await self._write_unreachable_stamps(reachable_keys, None)

        # Defer every restored view's on_restore render to after the gateway
        # is ready. on_restore reads the bot cache (avatars, members,
        # channels), cold on the setup_hook critical path; rendering there
        # paints defaults. Registration above is live, so the views route
        # interactions immediately while their render waits.
        if restored_views:
            task = asyncio.create_task(self._run_post_ready_restore(restored_views))
            task.add_done_callback(self._on_post_ready_restore_done)
            self._post_ready_restore_tasks.add(task)

        # Remember what this pass restored so a later reattach() skips it.
        self._restored_keys.update(summary["restored"])

        # One aggregate summary (counts, not the full key lists) so a bot with
        # hundreds of persistent views does not flood startup; per-view detail
        # is at DEBUG.
        logger.info(
            f"Persistent view reattach complete: {len(summary['restored'])} restored, "
            f"{len(summary['skipped'])} skipped, {len(summary['failed'])} failed, "
            f"{len(summary['removed'])} removed, {len(summary['unreachable'])} unreachable"
        )
        # Skipped rows are only news once the caller has had its chance to
        # import them. reattach() is that chance, so the first pass says
        # nothing louder than the count above and a later pass, finding the
        # class still absent, reports it as the real problem it is by then.
        # The stored string is the fix itself: what a class must register
        # under, or what session_class_key pins to after a move. The warning
        # names each one rather than counting them; distinct classes number
        # in the single digits however many rows they own.
        if missing_classes and self._reattach_passes > 1:
            unresolved = ", ".join(
                f"{name} ({count} row{'s' if count != 1 else ''})"
                for name, count in sorted(missing_classes.items(), key=lambda kv: str(kv[0]))
            )
            logger.warning(
                f"{sum(missing_classes.values())} persistent view row(s) still have "
                f"no imported class after a reattach() pass; their messages stay dead "
                f"until a class registers under the stored name. Unresolved: "
                f"{unresolved}. A moved or renamed class re-registers by setting "
                f"session_class_key to the stored name. Keys at DEBUG on this logger."
            )
        if summary["unreachable"]:
            logger.warning(
                f"{len(summary['unreachable'])} persistent view(s) could not be reached "
                f"(missing permissions, a channel that is gone, or a row re-posted "
                f"mid-pass); the rows are kept and retried next restart. Keys at DEBUG "
                f"on this logger."
            )
        return summary

    async def reattach(self) -> dict[str, list[str]]:
        """Re-drive persistent-view reattach after the initial pass.

        The initial reattach (during :meth:`PersistenceMiddleware.initialize`)
        can only attach view classes already imported at that point; a class
        whose module loads later (a cog loaded after ``setup_middleware``) lands
        in ``summary["skipped"]`` and its posted message stays dead until the
        next restart. Call this once the classes are imported, e.g. at the end
        of ``setup_hook`` after every cog loads, to attach those panels
        without a restart, so import order stops mattering.

        Idempotent: keys restored on a prior pass are skipped, so an
        already-live panel is never re-fetched or double-registered.
        Transiently ``unreachable`` / ``failed`` rows are retried. Each pass
        also re-drives dynamic-item registration, so a late-imported
        ``DynamicPersistentButton`` subclass recovers the same way. Returns the
        same summary shape as :meth:`reattach_persistent_views`, covering only
        the rows this pass processed.
        """
        return await self.reattach_persistent_views()

    async def _migrate_init_kwargs(
        self,
        row: dict[str, Any],
        view_cls: type,
        summary: dict[str, list[str]],
    ) -> Optional[dict[str, Any]]:
        """Walk the kwargs migrator chain from stored version to class
        version. Returns the migrated kwargs dict, or ``None`` when a
        migrator is missing or raised (summary already updated)."""
        persistence_key = row["persistence_key"]
        class_name = row["view_class"]

        raw = row.get("init_kwargs") or "{}"
        try:
            init_kwargs: dict[str, Any] = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.error(f"Corrupt init_kwargs for {persistence_key!r}: {exc}")
            summary["failed"].append(persistence_key)
            return None
        if not isinstance(init_kwargs, dict):
            logger.error(
                f"init_kwargs for {persistence_key!r} is not a JSON object "
                f"(got {type(init_kwargs).__name__})"
            )
            summary["failed"].append(persistence_key)
            return None

        row_version = int(row.get("kwargs_schema_version") or 1)
        class_version = int(getattr(view_cls, "kwargs_schema_version", 1))

        while row_version < class_version:
            migrator = get_kwargs_migrator(class_name, row_version)
            if migrator is None:
                logger.warning(
                    f"No kwargs migrator registered for {class_name} "
                    f"v{row_version} -> v{row_version + 1}; skipping "
                    f"{persistence_key!r}"
                )
                summary["skipped"].append(persistence_key)
                return None
            try:
                init_kwargs = await migrator(init_kwargs)
            except Exception as exc:
                logger.error(
                    f"Kwargs migrator {class_name} v{row_version} raised "
                    f"for {persistence_key!r}: {exc}",
                    exc_info=True,
                )
                summary["failed"].append(persistence_key)
                return None
            if not isinstance(init_kwargs, dict):
                logger.error(
                    f"Kwargs migrator {class_name} v{row_version} returned "
                    f"{type(init_kwargs).__name__}, expected dict"
                )
                summary["failed"].append(persistence_key)
                return None
            row_version += 1
        return init_kwargs

    async def _fetch_restore_message(
        self,
        row: dict[str, Any],
        removed_keys: list[str],
        unreachable_keys: list[str],
    ) -> Optional[tuple[Any, Any]]:
        """Fetch the target channel and message for a registry row.

        Returns ``None`` when the channel or message cannot be fetched. A
        definitive ``discord.NotFound`` appends to ``removed_keys`` and the row
        is pruned. A transient failure (``Forbidden``, ``HTTPException``, or a
        non-messageable channel) appends to ``unreachable_keys`` instead, which
        leaves the row on disk so a clean restart can retry it. A momentary
        permission change or 5xx during the startup mass-fetch must not delete a
        still-existing panel."""
        import discord

        persistence_key = row["persistence_key"]
        channel_id = row["channel_id"]
        message_id = row["message_id"]

        try:
            channel = self._bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self._bot.fetch_channel(int(channel_id))
        except discord.NotFound:
            logger.warning(f"Channel {channel_id} for {persistence_key!r} is gone; pruning entry.")
            removed_keys.append(persistence_key)
            return None
        except (
            discord.Forbidden,
            discord.InvalidData,
            *DISCORD_CALL_ERRORS,
        ) as exc:
            # RateLimited, InvalidData, and aiohttp's transport errors are all
            # siblings of HTTPException rather than subclasses; uncaught they
            # would land the row in "failed" (never retried) instead of
            # "unreachable" (retried next restart). Transport matters most
            # here: reattach runs at boot, which is exactly when the host may
            # not have its connection yet.
            logger.debug(
                f"Could not reach channel {channel_id} for {persistence_key!r} "
                f"({type(exc).__name__}); leaving the entry for the next restart."
            )
            unreachable_keys.append(persistence_key)
            return None

        if not isinstance(channel, discord.abc.Messageable):
            logger.warning(
                f"Channel {channel_id} for {persistence_key!r} is not messageable "
                f"({type(channel).__name__}); leaving the entry for the next restart."
            )
            unreachable_keys.append(persistence_key)
            return None

        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            logger.warning(f"Message {message_id} for {persistence_key!r} is gone; pruning entry.")
            removed_keys.append(persistence_key)
            return None
        except (discord.Forbidden, *DISCORD_CALL_ERRORS) as exc:
            logger.warning(
                f"Could not reach message {message_id} for {persistence_key!r} "
                f"({type(exc).__name__}); leaving the entry for the next restart."
            )
            unreachable_keys.append(persistence_key)
            return None

        return channel, message

    async def _confirm_unchanged(
        self, keys: list[str], verdicted: dict[str, dict]
    ) -> tuple[list[str], list[str], list[str]]:
        """Split keys into confirmed, rewritten, and unverified against live rows.

        Every reachability verdict costs a Discord fetch, so by the time one is
        acted on the snapshot behind it can be old -- minutes old on a real
        backlog. A panel re-posted under its stable key in that window writes a
        fresh row the verdict knows nothing about, and deleting by key alone
        would remove a live panel because the message it replaced returned a
        404. Every destructive path re-reads before it acts.

        A rewritten key's mirror row is refreshed with the fresh coordinates,
        so a later pass fetches the message the row now points at instead of
        re-404ing the one the verdict was about, warning per pass that it is
        pruning an entry it never prunes.

        A re-read that fails is logged and the key returned as unverified,
        never confirmed. The caller is mid-pass and still owes its summary,
        and the safe direction is the one that does not delete: the row stays
        on disk and the next pass re-verdicts it from scratch.

        A key whose row vanished from disk between the verdict and this
        re-read appears in none of the three lists: the deletion it was headed
        for already happened by another hand, and that hand did its own
        bookkeeping. Its mirror copy is dropped so later passes stop
        re-fetching a row that no longer exists.
        """
        backend = self.registry.backend
        if backend is None:
            return list(keys), [], []

        confirmed: list[str] = []
        rewritten: list[str] = []
        unverified: list[str] = []
        for key in keys:
            before = verdicted.get(key)
            try:
                current = await backend.row_select(TABLE_PERSISTENT_VIEWS, {"persistence_key": key})
            except Exception as exc:
                logger.warning(
                    f"Could not re-read {key!r} to confirm its prune: {exc}. "
                    f"The row is kept; the next pass re-verdicts it."
                )
                unverified.append(key)
                continue
            if not current:
                self._registry_rows = [
                    r for r in self._registry_rows if r.get("persistence_key") != key
                ]
                logger.debug(
                    f"Row for {key!r} vanished from disk before its prune; "
                    f"whoever deleted it did the bookkeeping."
                )
                continue
            if before is not None and (
                current[0].get("channel_id") != before.get("channel_id")
                or current[0].get("message_id") != before.get("message_id")
            ):
                logger.info(
                    f"Skipping prune of {key!r}: the row was rewritten while this pass "
                    f"was running, so the verdict describes a message it no longer "
                    f"points at."
                )
                # Adopt the fresh coordinates in place. Absent keys are not
                # appended: the mirror bounds what reattach re-drives walk,
                # and a row this process never mirrored stays outside it.
                fresh = dict(current[0])
                for i, r in enumerate(self._registry_rows):
                    if r.get("persistence_key") == key:
                        self._registry_rows[i] = fresh
                        break
                rewritten.append(key)
                continue
            confirmed.append(key)
        return confirmed, rewritten, unverified

    async def _write_unreachable_stamps(self, keys: list[str], stamp: Optional[int]) -> None:
        """Set or clear ``first_unreachable_at`` on registry rows.

        ``stamp=None`` clears. Setting preserves an existing value, because
        the column records the FIRST failure: refreshing it on every boot
        would reset the age of exactly the rows that have earned one.

        Each row is read back from disk and written whole. A partial upsert
        is not an option: ``InMemoryBackend`` replaces the stored row, so a
        two-key dict would delete the panel's message reference, and a SQL
        backend would fail the row's NOT NULL columns on a missing key.

        A write that fails is logged and skipped rather than raised. The
        caller is a reattach pass that still owes its summary, and a lost
        stamp self-heals: the row is still unreachable next boot, so it is
        stamped then, one boot later, which only prunes more conservatively.
        """
        backend = self.registry.backend
        if backend is None or not keys:
            return

        mirror = {r.get("persistence_key"): r for r in self._registry_rows}

        for key in keys:
            cached = mirror.get(key)
            if cached is not None:
                # Cheap agreement check first: the common boot writes nothing.
                if (stamp is None) == (cached.get("first_unreachable_at") is None):
                    continue
            try:
                found = await backend.row_select(TABLE_PERSISTENT_VIEWS, {"persistence_key": key})
                if not found:
                    continue
                current = found[0]

                # The verdict was rendered against the row read at rehydrate.
                # A re-post under the same key writes a new channel/message to
                # disk without refreshing that copy, so stamping blind would
                # age a panel that is live somewhere else.
                if cached is not None and (
                    current.get("channel_id") != cached.get("channel_id")
                    or current.get("message_id") != cached.get("message_id")
                ):
                    logger.debug(
                        f"Skipping unreachable stamp for {key!r}: the row was rewritten "
                        f"since this pass read it."
                    )
                    continue

                if (stamp is None) == (current.get("first_unreachable_at") is None):
                    continue

                if Capability.RAW_SQL in backend.capabilities:
                    # Touch the one column. Reading the row and writing it back
                    # whole would carry the rest of the snapshot with it, and a
                    # registry flush landing in the gap between that read and
                    # that write is silently reverted -- leaving the registry
                    # pointing at a message that was already replaced, which the
                    # next boot then 404-prunes. One UPDATE cannot lose a column
                    # it never names.
                    ph = "?" if backend.placeholder_style == "qmark" else "$1"
                    ph2 = "?" if backend.placeholder_style == "qmark" else "$2"
                    # Raw SQL does no prefixing of its own; the logical name
                    # targets the wrong table under a table_prefix.
                    stamp_table = physical_table(backend, TABLE_PERSISTENT_VIEWS)
                    await backend.execute(
                        f"UPDATE {stamp_table} SET first_unreachable_at = {ph} "
                        f"WHERE persistence_key = {ph2}",
                        stamp,
                        key,
                    )
                else:
                    # No SQL, and no await between the read and the write on a
                    # dict-shaped backend, so there is no gap for a flush to
                    # land in. Whole-row is required here: these backends
                    # replace on upsert, so a partial dict would delete the
                    # panel's own message reference.
                    updated = dict(current)
                    updated["first_unreachable_at"] = stamp
                    await backend.row_upsert(TABLE_PERSISTENT_VIEWS, updated, ["persistence_key"])
                if cached is not None:
                    cached["first_unreachable_at"] = stamp
            except Exception as exc:
                logger.warning(f"Could not update the unreachable stamp for {key!r}: {exc}")

    @property
    def unreachable_since(self) -> dict[str, int]:
        """Registry keys currently carrying an unreachable stamp, epoch seconds.

        Read from this process's view of the rows, so it reflects rehydrate
        plus whatever this process has observed since. That can over-report
        a row another process has already recovered; it cannot under-report
        one, and nothing destructive reads it -- :meth:`prune_unreachable`
        goes to disk.
        """
        return {
            r["persistence_key"]: r["first_unreachable_at"]
            for r in self._registry_rows
            if r.get("persistence_key") and r.get("first_unreachable_at") is not None
        }

    async def _reattach_one(
        self,
        row: dict[str, Any],
        view_cls: type,
        init_kwargs: dict[str, Any],
        message: Any,
        class_name: str,
        restored_views: list,
    ) -> str:
        """Construct + register a single view. Returns the summary
        bucket name (``"restored"`` or ``"failed"``).

        ``on_restore`` is NOT run here. It reads the gateway cache, which is
        cold on the ``setup_hook`` critical path, so it is deferred to a
        post-ready background task (see :meth:`_run_post_ready_restore`). A
        successfully registered view is appended to ``restored_views`` for
        that deferred render.
        """
        persistence_key = row["persistence_key"]
        view = None
        # Tracks whether ``_register_state`` succeeded -- if a downstream
        # step (``_update_message_state``, ``register_view``, ``on_bind``)
        # raises, the rollback dispatches ``VIEW_DESTROYED`` to undo the
        # ``SESSION_CREATED`` + ``VIEW_CREATED`` actions and prevent
        # zombie entries.
        state_registered = False
        try:
            # Strip kwargs that are surfaced via dedicated columns
            # (``persistence_key``) or that are never safely round-tripped
            # through JSON (``theme``). The middleware also drops these
            # at write, but defending the read seam keeps reattach
            # tolerant of older rows captured before the write-side
            # filter shipped.
            init_kwargs = {k: v for k, v in init_kwargs.items() if k not in _NON_PERSISTABLE_KWARGS}
            view = view_cls(persistence_key=persistence_key, **init_kwargs)

            # Validate custom_ids before discord.py attaches the view so
            # broken subclass updates fail here instead of swallowing
            # interactions silently.
            view._validate_custom_ids()

            # Set _message directly (not via the property setter) to
            # avoid firing a VIEW_UPDATED dispatch before _register_state
            # runs.
            view._message = message

            # ``is not None`` (not truthy) so a stored ``user_id=0`` still
            # restores -- Discord doesn't mint zero snowflakes, but tests
            # and edge-case fixtures do, and the cost of the explicit form
            # is nothing.
            if row.get("user_id") is not None:
                view.user_id = int(row["user_id"])
            if row.get("guild_id") is not None:
                view.guild_id = int(row["guild_id"])

            # __init__ ran with user_id=None so session auto-derivation was
            # skipped. Prefer the session_id captured at registration so a
            # restored view rejoins its original session -- isolated or
            # continuity, whichever it had -- rather than a re-derived
            # suffix-free key that collides across same-class same-user panels.
            # Fall back to deriving for rows written before the session_id
            # column was persisted.
            if not view.session_id:
                persisted = row.get("session_id")
                if persisted:
                    view.session_id = persisted
                elif view.user_id is not None:
                    view.session_id = f"{type(view)._class_session_key()}:user_{view.user_id}"

            self._bot.add_view(view, message_id=message.id)

            # _register_view first so the instance index is complete before
            # _register_state's VIEW_CREATED notifies subscribers. Mirrors
            # the order used by _send_pipeline -- subscribers and instance
            # limit checks see a consistent state row at every step.
            self._store._register_view(view)

            # Batch the registration dispatches so the three startup actions
            # (SESSION_CREATED + VIEW_CREATED + VIEW_UPDATED) collapse into a
            # single BATCH_COMPLETE notification per restored view. Without the
            # batch, a project with N persistent views fires 3N+ standalone
            # notification cycles at startup. Batch source_id is the view's id
            # so the BATCH_COMPLETE rides the acting-view inline-notification
            # path, matching the _send_pipeline contract.
            async with self._store.batch(source_id=view.id):
                await view._register_state()
                state_registered = True
                await view._update_message_state(message)
                # Stamped before the hook so the view can reach the client
                # even when the override does not store it. A restored view
                # has no interaction and no context, so this is the only
                # route to the bot's own allowed_mentions on later refreshes.
                view._bot = self._bot
                # on_bind runs here so runtime deps are available before the
                # deferred on_restore render. Supports sync or async overrides.
                bind_result = view.on_bind(self._bot)
                if inspect.isawaitable(bind_result):
                    await bind_result

            # on_restore is deferred to _run_post_ready_restore (after
            # wait_until_ready) so its gateway-cache reads land warm.
            # Registration above already ran, so the view routes interactions
            # immediately; only the render waits.
            restored_views.append(view)
            # DEBUG, not INFO: at scale (hundreds of guilds x many views) a
            # per-view INFO line floods startup. reattach_persistent_views
            # logs one aggregate summary instead.
            logger.debug(f"Reattached persistent view {persistence_key!r} ({class_name})")
            return "restored"

        except Exception as exc:
            logger.error(
                f"Failed to restore persistent view {persistence_key!r}: {exc}",
                exc_info=True,
            )
            if view is not None:
                # Mirror the v2 rollback: __init__ already installed a
                # subscriber + registry entry + undo-tracking row, all
                # of which must go back before the next row is tried.
                self._store._unsubscribe(view.id)
                self._store._undo_enabled_views.pop(view.id, None)
                if state_registered:
                    # State registration landed SESSION_CREATED + VIEW_CREATED,
                    # so tear them down through the atomic seam: _destroy_view
                    # dispatches VIEW_DESTROYED, then clears the active-registry
                    # entry only after state confirms the removal. A failed
                    # dispatch leaves both registries intact instead of
                    # stranding a ghost; _destroy_view catches and logs the
                    # dispatch failure internally. ``reduce_view_destroyed``
                    # cleans up the session entry too when its members empty.
                    await self._store._destroy_view(view.id)
                else:
                    # State never registered; just drop the active entry.
                    self._store._unregister_view(view.id)
            return "failed"

    def register_hook(self, name: str, callback: Callable[..., Any]) -> None:
        """Register an observability hook.

        Supported names: ``on_flush`` (fires after a successful flush
        with ``namespace, upsert_count, delete_count``) and ``on_error``
        (fires on flush exception with ``namespace, exc``). Hooks run
        under the middleware's write lock; keep them fast and
        non-blocking.
        """
        if not isinstance(name, str):
            raise TypeError(f"hook name must be str, got {type(name).__name__}")
        if not callable(callback):
            raise TypeError(f"hook callback must be callable, got {type(callback).__name__}")
        self._hooks.setdefault(name, []).append(callback)

    # // ========================================( TTL sweeper )======================================== // #

    def _start_ttl_sweeper(self) -> None:
        """Spawn the daily TTL sweeper task when at least one persistent
        slot declares ``ttl_days``. Idempotent: second call is a no-op
        if the task is already running. No-op when the application
        backend is opted out or no slot needs sweeping.
        """
        if self._ttl_sweeper_task is not None:
            return
        if self.application.backend is None:
            return
        if not any(p.ttl_days is not None and p.persistent for p in self._slot_policies.values()):
            return
        task = asyncio.create_task(self._ttl_sweeper_loop())
        task.add_done_callback(self._on_sweeper_done)
        self._ttl_sweeper_task = task

    def _on_sweeper_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"TTL sweeper crashed: {exc}", exc_info=exc)
        self._ttl_sweeper_task = None

    # // ========================================( Post-ready restore )======================================== // #

    async def _run_post_ready_restore(self, views: list) -> None:
        """Run ``on_restore`` for reattached views once the gateway is ready.

        ``on_restore`` is documented for post-restore rendering and fresh-data
        fetch, both of which read the gateway cache (``get_user``, members,
        channels). That cache is empty on the ``setup_hook`` critical path
        where reattach runs, so rendering there paints cold -- default avatars,
        missing member names. This waits for ``wait_until_ready`` off the
        critical path (in a background task, so ``setup_hook`` returns and the
        gateway can connect), then renders warm. Each view is already
        registered, so interactions route during the wait. ``on_restore``
        failures are logged per view and never abort the rest.

        Repaints group by channel: panels sharing a channel render one at a
        time, in registry row order (message edits rate-bucket per channel),
        while panels in distinct channels render concurrently under
        ``restore_concurrency``.
        """
        try:
            await self._bot.wait_until_ready()
        except Exception as e:
            # Every restored view stays registered and interactive; only the
            # warm re-render is skipped. Silence here made that look like a
            # restore that had simply found nothing to render.
            logger.warning(
                f"Post-ready restore skipped: waiting for the gateway raised "
                f"{type(e).__name__}: {e}. {len(views)} restored view(s) keep "
                f"their cold render until something re-renders them."
            )
            return
        start = time.monotonic()
        # Batch membership is task-scoped, so each panel's on_restore collects
        # and flushes its own batch: rendering concurrently no longer folds
        # independent panels into one false-nested batch where whichever
        # finished last flushed them all. Bounded by ``restore_concurrency``,
        # the ceiling the reattach fetch phase already uses, so a large
        # install does not open its entire repaint against Discord at once.
        semaphore = asyncio.Semaphore(self.restore_concurrency)

        async def _render(view) -> bool:
            if view.is_finished():
                return False
            persistence_key = getattr(view, "_persistence_key", "?")
            async with semaphore:
                try:
                    async with self._store.batch(source_id=view.id):
                        await await_maybe(view.on_restore(self._bot))
                    return True
                except Exception as exc:
                    logger.error(
                        f"on_restore failed for persistent view {persistence_key!r}: {exc}",
                        exc_info=exc,
                    )
                    return False

        # A repaint is a message edit, and Discord buckets message edits per
        # channel (channel id is a Route major parameter), with sub-limits
        # that response headers do not report -- so a same-channel burst 429s
        # no matter how the HTTP layer paces on headers. Panels sharing a
        # channel therefore repaint serially, in registry row order; panels in
        # distinct channels are distinct buckets and still fan out under the
        # semaphore. A view with no message ref (deleted out from under it
        # during the ready wait: on_message_delete nulls ``_message`` before
        # ``exit()``) derives no channel and repaints in its own group, so it
        # neither crashes the grouping nor serializes unrelated panels.
        groups: dict[Any, list] = {}
        for view in views:
            channel = getattr(getattr(view, "_message", None), "channel", None)
            channel_id = getattr(channel, "id", None)
            groups.setdefault(channel_id if channel_id is not None else object(), []).append(view)

        async def _render_channel(channel_views: list) -> int:
            count = 0
            for view in channel_views:
                if await _render(view):
                    count += 1
            return count

        rendered = sum(await asyncio.gather(*(_render_channel(g) for g in groups.values())))
        # The repaint runs on already-interactive views, so its duration is
        # surfaced rather than guarded. Startup is unaffected (this runs
        # post-ready).
        if rendered:
            logger.info(
                f"Post-ready restore rendered {rendered} view(s) in "
                f"{time.monotonic() - start:.1f}s"
            )

    def _on_post_ready_restore_done(self, task: asyncio.Task) -> None:
        self._post_ready_restore_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Post-ready restore render crashed: {exc}", exc_info=exc)

    async def _ttl_sweeper_loop(self) -> None:
        """24-hour sleep loop that sweeps expired application slots.

        Each tick issues one
        ``row_delete_where_lt(TABLE_APPLICATION_SLOTS, "expires_at", now)``
        call. Rows with ``expires_at=NULL`` are never touched by the
        backend contract, so non-TTL slots are safe. Errors are logged
        and the loop continues: transient backend hiccups must not
        silently kill the sweeper.
        """
        try:
            while not self._closed:
                try:
                    await asyncio.sleep(86400)
                except asyncio.CancelledError:
                    return
                if self._closed:
                    return
                backend = self.application.backend
                if backend is None:
                    return
                try:
                    cutoff = int(time.time())
                    deleted = await backend.row_delete_where_lt(
                        TABLE_APPLICATION_SLOTS,
                        "expires_at",
                        cutoff,
                    )
                    if deleted:
                        await self._store.dispatch(
                            "APPLICATION_SLOTS_PRUNED",
                            ActionCreators.application_slots_pruned(deleted, cutoff=cutoff),
                        )
                except Exception as exc:
                    logger.error(f"TTL sweeper error: {exc}", exc_info=True)
        except asyncio.CancelledError:
            return

    # // ========================================( Prune surface )======================================== // #

    async def prune_application(
        self,
        *,
        slot: Optional[str] = None,
        older_than_days: Optional[int] = None,
    ) -> int:
        """Delete cascadeui_application_slots rows.

        When ``slot`` is given, deletes that one slot (any age). When
        ``older_than_days`` is given, deletes rows whose ``expires_at``
        is older than the cutoff. The two modes are mutually exclusive.
        """
        backend = self.application.backend
        if backend is None:
            return 0
        if slot is not None and older_than_days is not None:
            raise ValueError("prune_application: pass slot OR older_than_days, not both")

        if slot is not None:
            deleted = await backend.row_delete(TABLE_APPLICATION_SLOTS, {"slot_name": slot})
            cutoff = None
        elif older_than_days is not None:
            cutoff = int(time.time()) - (older_than_days * 86400)
            deleted = await backend.row_delete_where_lt(
                TABLE_APPLICATION_SLOTS, "expires_at", cutoff
            )
        else:
            return 0

        await self._store.dispatch(
            "APPLICATION_SLOTS_PRUNED",
            ActionCreators.application_slots_pruned(deleted, cutoff=cutoff),
        )
        return deleted

    async def prune_registry(
        self,
        *,
        persistence_keys: Optional[list[str]] = None,
        reason: Optional[str] = None,
    ) -> int:
        """Delete cascadeui_persistent_views rows. When ``persistence_keys`` is given,
        only those rows are removed; otherwise clears the whole
        registry (destructive, rarely wanted).

        ``reason`` labels the ``REGISTRY_PRUNED`` dispatch so a subscriber can
        tell why a row went. Left unset it defaults to ``"explicit"`` for a
        targeted prune and ``"clear_all"`` for a full wipe."""
        backend = self.registry.backend
        if backend is None:
            return 0

        deleted = 0
        # Collect the keys actually removed (row_delete reported a row) so a
        # subscriber can reconcile its own records surgically, instead of
        # sweeping its whole domain against the registry. A key passed in but
        # absent on disk is not reported.
        pruned: list[str] = []
        if persistence_keys is None:
            # Clear all: row_select + row_delete per row. Callers who
            # hit this path are rare (test cleanup, admin tooling).
            rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
            for r in rows:
                if await backend.row_delete(
                    TABLE_PERSISTENT_VIEWS, {"persistence_key": r["persistence_key"]}
                ):
                    pruned.append(r["persistence_key"])
                    deleted += 1
        else:
            for sk in persistence_keys:
                if await backend.row_delete(TABLE_PERSISTENT_VIEWS, {"persistence_key": sk}):
                    pruned.append(sk)
                    deleted += 1

        # Drop the pruned rows from this process's mirror. Without it a later
        # reattach() re-drive walks rows whose messages are already gone and
        # spends one HTTP fetch per dead row per pass, which is the same cost
        # the unreachable stamp exists to stop paying.
        if pruned:
            gone = set(pruned)
            self._registry_rows = [
                r for r in self._registry_rows if r.get("persistence_key") not in gone
            ]

        await self._store.dispatch(
            "REGISTRY_PRUNED",
            ActionCreators.registry_pruned(
                deleted,
                reason or ("explicit" if persistence_keys else "clear_all"),
                keys=pruned,
            ),
        )
        return deleted

    async def prune_unreachable(self, *, older_than_days: int) -> dict[str, list[str]]:
        """Delete registry rows that have stayed unreachable past a cutoff.

        Candidates are rows carrying a ``first_unreachable_at`` stamp, set
        when a reattach pass cannot fetch the row's channel or message for a
        non-definitive reason and cleared the moment a fetch succeeds. Every
        candidate is re-verified against Discord before anything is deleted:

        - the fetch succeeds: the row is kept and its stamp cleared
          (``recovered``);
        - ``discord.NotFound``: deleted whatever its age (``pruned``), the
          same definitive verdict a reattach pass already prunes on;
        - still unreachable: deleted only when the stamp predates the cutoff
          (``pruned``), kept otherwise (``kept``). A row rewritten mid-pass,
          or one whose pre-delete re-read failed at the backend, is kept too:
          neither verdict describes the row as it stands.

        Re-verifying is what makes this safe to run. A stamp records one
        observation, so a host that simply has not rebooted for a month
        carries a month-old stamp on the strength of a single failure. The
        second look turns that into "unreachable then, and unreachable now",
        and guarantees a row that answers today cannot be deleted.

        Returns ``{"pruned": [...], "recovered": [...], "kept": [...]}`` of
        persistence keys. Deletions route through :meth:`prune_registry`, so
        ``REGISTRY_PRUNED`` fires with ``reason="unreachable"``.

        Raises ``ValueError`` for a negative or non-integer
        ``older_than_days`` and ``RuntimeError`` when the middleware was
        built without ``bot=``, since re-verification needs the client. A
        registry with no backend returns the empty summary.
        """
        if isinstance(older_than_days, bool) or not isinstance(older_than_days, int):
            raise ValueError(
                f"prune_unreachable older_than_days must be an int; "
                f"got {type(older_than_days).__name__}."
            )
        if older_than_days < 0:
            raise ValueError(
                f"prune_unreachable older_than_days must be zero or greater; "
                f"got {older_than_days}."
            )

        summary: dict[str, list[str]] = {"pruned": [], "recovered": [], "kept": []}
        backend = self.registry.backend
        if backend is None:
            return summary
        if self._bot is None:
            raise RuntimeError(
                "prune_unreachable() requires a middleware constructed with bot=; "
                "without a client there is nothing to re-verify against, and without "
                "one no reattach pass runs, so no rows are ever stamped."
            )

        # From disk, never the in-memory mirror: a re-send in this process
        # clears the stamp on disk without refreshing the copy, and the
        # destructive path has to read the authority.
        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        candidates = [r for r in rows if r.get("first_unreachable_at") is not None]
        if not candidates:
            return summary

        cutoff = int(time.time()) - older_than_days * 86400
        kill: list[str] = []

        for row in candidates:
            key = row.get("persistence_key")
            removed_scratch: list[str] = []
            unreachable_scratch: list[str] = []
            fetched = await self._fetch_restore_message(row, removed_scratch, unreachable_scratch)

            if fetched is not None:
                summary["recovered"].append(key)
                await self._write_unreachable_stamps([key], None)
            elif removed_scratch:
                # A definitive 404. Age is irrelevant; the message is gone.
                kill.append(key)
            elif row["first_unreachable_at"] <= cutoff:
                kill.append(key)
            else:
                summary["kept"].append(key)

        if kill:
            # Re-read before deleting -- see _confirm_unchanged for why. The
            # reattach pass guards this same shape on its own 404s; this is
            # the second destructive caller and needs the identical guard.
            verdicted = {r["persistence_key"]: r for r in candidates}
            confirmed, rewritten, unverified = await self._confirm_unchanged(kill, verdicted)
            summary["kept"].extend(rewritten)
            # Unverified means the re-read itself failed, so the verdict was
            # never checked against the live row. Kept, not pruned: deleting
            # on an unread verdict is the blindness the re-read prevents.
            summary["kept"].extend(unverified)
            if confirmed:
                await self.prune_registry(persistence_keys=confirmed, reason="unreachable")
                summary["pruned"].extend(confirmed)

        logger.info(
            f"prune_unreachable: {len(summary['pruned'])} pruned, "
            f"{len(summary['recovered'])} recovered, {len(summary['kept'])} still within "
            f"the {older_than_days}d cutoff."
        )
        logger.debug(f"prune_unreachable keys: {summary}")
        return summary

    # // ========================================( Shutdown )======================================== // #

    async def flush_all(self) -> None:
        """Drain every namespace's pending writes synchronously.

        Thin passthrough to :meth:`PersistenceMiddleware.flush_all`. Call
        from devtools or operator-facing commands that want an immediate
        disk write without tearing the manager down. No-op for a manager
        built outside the middleware, which owns the buffers this drains.
        """
        if self._middleware is not None:
            await self._middleware.flush_all()

    async def close(self) -> None:
        """Flush the middleware, then close every unique backend.

        Ordering matters: the middleware must drain its dirty buffers
        *before* the backends close, or in-flight writes get lost when
        the connection underneath them goes away. ``PersistenceMiddleware.close``
        sets its closed flag first so no new dispatches enqueue work
        while the final flush runs.

        Safe to call twice. One misbehaving backend is logged and does
        not block the others from closing cleanly.
        """
        if self._closed:
            return

        # Cancel the TTL sweeper first. It only sleeps + one backend
        # call per tick; cancellation is immediate and idempotent.
        if self._ttl_sweeper_task is not None:
            self._ttl_sweeper_task.cancel()
            try:
                await self._ttl_sweeper_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ttl_sweeper_task = None

        # Cancel any post-ready restore renders still waiting on the gateway
        # (or mid-render) when shutdown begins. Iterate a copy -- the done
        # callback discards from the set as each cancelled task settles.
        for task in list(self._post_ready_restore_tasks):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._post_ready_restore_tasks.clear()

        if self._middleware is not None:
            try:
                await self._middleware.close()
            except Exception as exc:
                logger.error(f"Error closing persistence middleware: {exc}")

        for backend in self._unique_backends.values():
            try:
                await backend.close()
            except Exception as exc:
                logger.error(f"Error closing {type(backend).__name__}: {exc}")

        self._closed = True
        logger.debug("PersistenceManager closed")
