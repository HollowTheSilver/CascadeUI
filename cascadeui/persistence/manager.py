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
import contextlib
import functools
import inspect
import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone
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
from ..utils.tasks import _bounded_wait
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


# Captured kwargs a registry row does not carry: ``persistence_key`` has its own
# column, and ``theme`` and ``bot`` are live objects (``bot`` returns through
# ``on_bind``). Stripped at write by the middleware, and at read by
# ``_reattach_one`` so the column's value is not passed twice.
_NON_PERSISTABLE_KWARGS: frozenset[str] = frozenset({"persistence_key", "theme", "bot"})

# How long a backend's close() may take before persistence closes without it.
# It runs inside the bot's close, so a close that never returns would keep the
# bot from ever exiting. Above the built-in SQL backends' own five-second wait.
_BACKEND_CLOSE_SECONDS = 10.0


def _bot_closed(bot: Any) -> bool:
    """Whether ``bot`` has closed. Read with ``is True``: a stand-in without a
    real ``is_closed`` reads as open."""
    is_closed = getattr(bot, "is_closed", None)
    return is_closed is not None and is_closed() is True


def _validate_prune_unreachable_after_days(value: Any, bot: Any) -> None:
    """Refuse a bad ``prune_unreachable_after_days`` where it is passed.

    A zero cutoff is refused although ``prune_unreachable(older_than_days=0)``
    accepts it: run automatically, it deletes a row on a single failed fetch.
    A sweep without a bot has nothing to re-verify against, and no reattach
    pass ever stamps a row for it to find.
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"prune_unreachable_after_days= must be None or a positive int, got {value!r}."
        )
    if bot is None:
        raise ValueError(
            "prune_unreachable_after_days= needs bot=: the sweep re-verifies each "
            "unreachable row against Discord before deleting it."
        )


async def _settle(task: asyncio.Task) -> None:
    """Wait for a task this close cancelled, without taking its outcome as the
    caller's: a cancel of the caller still reaches the caller."""
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()


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
        prune_unreachable_after_days: Optional[int] = None,
    ) -> None:
        _validate_prune_unreachable_after_days(prune_unreachable_after_days, bot)
        self._store = store
        self._bot = bot
        # Bounds per-row Discord round-trips across both restore phases; see
        # _run_post_ready_restore for the per-channel serialization the
        # repaint adds on top of this bound.
        self.restore_concurrency = restore_concurrency
        # Cutoff the daily unreachable sweep passes to prune_unreachable();
        # None leaves the sweep off.
        self.prune_unreachable_after_days = prune_unreachable_after_days
        # Held by every walk that can delete or re-attach registry rows
        # (a reattach pass and prune_unreachable), so a sweep cannot delete a
        # row a concurrent reattach just found reachable again. Taken
        # through _loop_lock("registry_walk").
        self._registry_walk_owner: Optional[asyncio.Task] = None

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
        self._loop_locks: dict[str, tuple] = {}
        # A close that began and was cut off before it finished still needs
        # a reopen, though _closed is set only at the end.
        self._close_started: bool = False
        self._registry_rows: list[dict[str, Any]] = []
        # The stored form of each application slot as last loaded or queued for
        # a write. PersistenceMiddleware routes through it and skips a slot whose
        # stored form has not changed.
        self._slot_payloads: dict[str, str] = {}
        # The expires_at of each slot with a TTL, as last loaded or queued, so an
        # expired slot can be dropped from the running bot as well as from disk.
        self._slot_expiry: dict[str, int] = {}
        # Slots already logged as having no registered policy.
        self._unpolicied_slots: set[str] = set()
        # Keys restored across reattach passes. reattach() re-drives the
        # reattach and skips these so an already-live panel is never re-fetched
        # or double-registered on a second pass.
        self._restored_keys: set[str] = set()
        # Which reattach pass is running. The first is the library's own,
        # during initialize(); later ones come from reattach(). A class
        # missing on the first is expected and on a later one is not.
        self._reattach_passes: int = 0
        # The most recent pass's summary, readable after setup_middleware
        # returns: REGISTRY_PRUNED fires inside it, before on_ready can listen.
        self.last_reattach_summary: Optional[dict[str, list[str]]] = None
        # Every key a pass has reported, under its most recent outcome; read
        # through total_reattach_summary. Outlives the attribute above across
        # a re-drive, which is what keeps a removed key reconcilable.
        self._reattach_outcomes: dict[str, str] = {}

        # Slot policy registry, seeded from ApplicationPersistence.slots
        # and extended at runtime by register_slot_policy.
        self._slot_policies: dict[str, SlotPolicy] = dict(self.application.slots)

        # persistent=True registers the slot as persistent_slots and access_slot
        # do. Here rather than in initialize(), which a pre-built manager skips.
        for slot_name, policy in self._slot_policies.items():
            if policy.persistent:
                self._register_persistent_slot(slot_name)

        # TTL sweeper task. Started during PersistenceMiddleware.initialize()
        # only when at least one slot declares ttl_days > 0. Cancelled by close().
        self._ttl_sweeper_task: Optional[asyncio.Task] = None
        # Daily unreachable-row sweep. Started with the TTL sweeper only when
        # prune_unreachable_after_days is set. Cancelled by close().
        self._unreachable_sweeper_task: Optional[asyncio.Task] = None
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

        ``initialize()`` starts the daily sweeper only when a registered
        policy has a TTL, so a TTL policy registered later starts it here.
        The sweep itself deletes every expired row, whatever its policy.
        Idempotent: ``_start_ttl_sweeper`` is a no-op when the task is
        already alive or when the application backend is opted out.
        """
        if not isinstance(name, str):
            raise TypeError(f"slot name must be str, got {type(name).__name__}")
        if not isinstance(policy, SlotPolicy):
            raise TypeError(f"policy must be a SlotPolicy, got {type(policy).__name__}")
        if name in self._slot_policies:
            raise ValueError(f"Slot policy already registered for {name!r}")
        self._slot_policies[name] = policy
        # The stored row was written under the fallback policy: forgetting its
        # form lets the next action that copies the state (any @cascade_reducer)
        # rewrite it with this one's expiry.
        self._slot_payloads.pop(name, None)
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
            # behavior; once per slot, since every write of the slot asks.
            if name not in self._unpolicied_slots:
                self._unpolicied_slots.add(name)
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
                # A second process booting alongside can commit the rename
                # between the probes and the transaction. Losing that race is
                # healthy; reported as a failure it would read as a GRANT problem.
                if await self._legacy_rename_already_done(backend, legacy_name, table):
                    logger.debug(
                        f"Another process renamed {legacy_name!r} to "
                        f"{physical_table(backend, table)!r} first; nothing to do."
                    )
                    continue
                # Wrapped like every other pipeline step: the driver's own type
                # names neither the table nor the step, and on a role that cannot
                # rename, this is the first DDL to fail.
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
            # information_schema filters by privilege, and that is required: a
            # role that cannot see the old table cannot rename it either, so it
            # is skipped. pg_class and pg_tables do not filter.
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
            # The driver's own vendor type makes this except broad, so it cannot
            # tell a missing column from a table a concurrent boot just renamed.
            # Existence is read again before the table is called a consumer's.
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
            # A rename keeps the index under its old name on both engines, and
            # the next boot's CREATE INDEX IF NOT EXISTS would add a second one.
            # An index is derived state, so it is dropped and recreated.
            await backend.execute(
                f"DROP INDEX IF EXISTS {physical_table(backend, legacy.old_index)}"
            )
            await backend.execute(apply_table_prefix(legacy.index_ddl, prefix))
            if old_ver is not None:
                # The version record moves with its data, or the renamed table
                # reads as unversioned and its migrators re-run. A record under
                # the current name can only describe the shell dropped above.
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
            # The rename takes ACCESS EXCLUSIVE, so any open read blocks it, and
            # unbounded it would hang the boot instead of warning. SET LOCAL
            # inside the transaction leaves nothing set on a pooled connection.
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
                # Rows without a version record walk forward from v1 (see the
                # docstring); an empty table is a fresh install. A wrong guess
                # only re-runs migrators, and every one is idempotent.
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
                    # With no raw-SQL surface the migrator cannot run its DDL,
                    # and skipping it is safe only for open-mapping rows. Unrefused,
                    # the first registry write carrying the new column would fail.
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
        # Under the write lock, so a rehydrate run again while the bot is up
        # waits for a write in flight instead of loading the row it replaces.
        ns = getattr(self._middleware, "_ns_application", None)
        async with ns.write_lock if ns is not None else contextlib.nullcontext():
            await self._load_application_rows()

    def _stored_under_current_policy(self, slot_name: str, row: dict) -> bool:
        """Whether ``row``'s expiry is the one the slot's current policy would write."""
        ttl_days = self.get_slot_policy(slot_name).ttl_days
        expires_at = row.get("expires_at")
        if ttl_days is None:
            return expires_at is None
        updated_at = row.get("updated_at")
        return (
            expires_at is not None
            and updated_at is not None
            and expires_at - updated_at == ttl_days * 86400
        )

    async def _load_application_rows(self) -> None:
        backend = self.application.backend
        assert backend is not None

        # Expired rows go before the select: loaded, an expired slot would be
        # served until the next daily sweep. Skipped when no persistent slot
        # declares a TTL.
        if any(p.ttl_days is not None and p.persistent for p in self._slot_policies.values()):
            cutoff = int(time.time())
            try:
                await backend.row_delete_where_lt(TABLE_APPLICATION_SLOTS, "expires_at", cutoff)
            except Exception as exc:
                # Prune failures should not block rehydrate -- the sweeper
                # will catch the rows on its next tick. Log and continue.
                logger.warning(f"Pre-rehydrate TTL prune failed: {exc}")
            else:
                # A rehydrate run again while the bot is up: the expired slots
                # the select no longer returns leave memory too.
                await self._drop_slots(expired_before=cutoff)

        try:
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        except Exception as exc:
            raise PersistenceRehydrateError(
                f"Failed to read {TABLE_APPLICATION_SLOTS}: {exc}"
            ) from exc

        slots = {}
        expiry = {}
        payloads = {}
        for row in rows:
            slot_name = row["slot_name"]
            try:
                slots[slot_name] = json.loads(row["payload"])
            except (TypeError, ValueError) as exc:
                raise PersistenceRehydrateError(
                    f"Corrupt payload for slot {slot_name!r}: {exc}"
                ) from exc
            if row.get("expires_at") is not None:
                expiry[slot_name] = row["expires_at"]
            # A row written under another TTL policy is left unrecorded, so the
            # next action that copies the state rewrites it with the current
            # policy's expiry.
            if self._stored_under_current_policy(slot_name, row):
                # Serialized again rather than taken from the row: a backend may
                # store its own normalized form, which would never match.
                payloads[slot_name] = json.dumps(slots[slot_name])
        loaded = []

        def write():
            # A change still waiting to be written is newer than its row, so a
            # rehydrate run again while the bot is up leaves that slot alone.
            pending = self._pending_slots()
            fresh = {name: value for name, value in slots.items() if name not in pending}
            self._store.state.setdefault("application", {}).update(fresh)
            self._slot_payloads.update((n, p) for n, p in payloads.items() if n in fresh)
            self._slot_expiry.update((n, at) for n, at in expiry.items() if n in fresh)
            loaded.append(len(fresh))

        # In the state turn: a reducer awaiting in another task would
        # otherwise commit a state without these slots.
        await self._store._write_in_turn(write, "rehydrating application slots")
        logger.info(f"Rehydrated {loaded[0]} application slot(s)")

    def _pending_slots(self) -> set:
        """The application slots with a write or delete still waiting."""
        ns = getattr(self._middleware, "_ns_application", None)
        if ns is None:
            return set()
        return set(ns.dirty_rows) | ns.deleted_keys

    async def _drop_slots(self, names=(), *, expired_before: Optional[int] = None) -> list:
        """Remove slots from the running bot, with any write still waiting for them.

        ``names`` drops those slots. ``expired_before`` drops every slot whose
        recorded expiry is earlier, read at the moment of the drop, so a slot
        changed since then carries its new expiry and stays. Runs in the state
        turn, so no action lands between the slot leaving memory and its
        waiting write being dropped. Returns the names that were in memory.
        """
        dropped: list = []

        def write():
            if expired_before is None:
                chosen = list(names)
            else:
                chosen = [name for name, at in self._slot_expiry.items() if at < expired_before]
            ns = getattr(self._middleware, "_ns_application", None)
            for name in chosen:
                self._slot_payloads.pop(name, None)
                if ns is not None:
                    ns.dirty_rows.pop(name, None)
            state = self._store.state
            application = state.get("application") or {}
            present = [name for name in chosen if name in application]
            if present:
                kept = {k: v for k, v in application.items() if k not in present}
                self._store.state = {**state, "application": kept}
            dropped.extend(present)

        await self._store._write_in_turn(write, "dropping pruned application slots")
        return dropped

    async def _rehydrate_registry(self) -> None:
        backend = self.registry.backend
        assert backend is not None
        try:
            rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        except Exception as exc:
            raise PersistenceRehydrateError(
                f"Failed to read {TABLE_PERSISTENT_VIEWS}: {exc}"
            ) from exc

        # The rows reattach_persistent_views restores from, so that pass needs
        # no backend read of its own.
        self._registry_rows: list[dict[str, Any]] = list(rows)

        # The store's mirror of every row, in the entry shape
        # reduce_persistent_view_registered writes (ids as strings). Without it a
        # restored view's exit() would change no state and its row would never
        # be deleted. A key a send already registered keeps its live entry.
        seeded = self._mirror_entries(rows)

        # Written in place, not dispatched: a dispatch would route these rows
        # back to disk as new registrations. Taken in the state turn, so an
        # awaiting reducer cannot commit over it.
        def write():
            state = self._store.state
            state["persistent_views"] = {**seeded, **state.get("persistent_views", {})}

        await self._store._write_in_turn(write, "rehydrating the registry")
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

        Serialized with :meth:`prune_unreachable`, the other walk that acts
        on registry rows. The return covers this pass;
        :attr:`total_reattach_summary` carries every pass, which is what a
        reconcile running after a re-drive needs.
        """
        summary = await self._serialized_walk("reattach_persistent_views", self._reattach_pass)
        for outcome, keys in summary.items():
            for key in keys:
                self._reattach_outcomes[key] = outcome
        return summary

    @staticmethod
    def _mirror_entries(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Registry rows in the entry shape the store's mirror holds.

        Ids are strings there and the row's are integers, and ``registered_at``
        is the ISO string an action timestamp carries, so a row cannot be put
        into the mirror as it comes off disk.
        """

        def snowflake(value):
            return None if value is None else str(value)

        def iso(epoch):
            return (
                None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).isoformat()
            )

        return {
            row["persistence_key"]: {
                "persistence_key": row["persistence_key"],
                "class_name": row.get("view_class"),
                "message_id": snowflake(row.get("message_id")),
                "channel_id": snowflake(row.get("channel_id")),
                "guild_id": snowflake(row.get("guild_id")),
                "user_id": snowflake(row.get("user_id")),
                "registered_at": iso(row.get("created_at")),
            }
            for row in rows
        }

    def reseed_registry_mirror(self) -> int:
        """Rebuild the store's registry mirror from the rows this process knows.

        For a caller that rebuilds state from scratch: the mirror is part of
        that state, and dropping it leaves stored rows nothing in the process
        knows about, so a later exit under one of those keys retires nothing
        and a re-send skips its orphan cleanup. Returns the entry count.
        """
        entries = self._mirror_entries(self._registry_rows)

        def write():
            self._store.state["persistent_views"] = entries

        # Lands after a reducer running in another task commits, which would
        # otherwise replace it.
        self._store._write_when_free(write)
        return len(entries)

    def _remember_registry_row(self, persistence_key: str, row: dict[str, Any]) -> None:
        """Record a row this process wrote into the boot mirror.

        The counterpart of ``_forget_registry_row``: without it the mirror only
        ever shrinks, so a registration handed back to a live predecessor sits
        on disk while this process's reattach candidates no longer name it, and
        only the next boot finds it again.
        """
        for i, existing in enumerate(self._registry_rows):
            if existing.get("persistence_key") == persistence_key:
                self._registry_rows[i] = dict(row)
                return
        self._registry_rows.append(dict(row))

    def _forget_registry_row(self, persistence_key: str) -> None:
        """Drop a row this process deleted from the boot mirror.

        The mirror is filled once at rehydrate and is what every reattach pass
        walks, so a row deleted afterwards is still a reattach candidate: a key
        left pending at boot, re-posted in this process and later retired, would
        be restored onto the message the first row named, with no row behind it.
        ``prune_registry`` already drops what it deletes; this is the other
        deletion, an ``exit()`` that unregisters. The middleware calls it for a
        removal the reducer accepted, so a superseded panel's exit, which the
        reducer refuses, leaves the mirror alone.
        """
        self._registry_rows = [
            r for r in self._registry_rows if r.get("persistence_key") != persistence_key
        ]

    @property
    def total_reattach_summary(self) -> dict[str, list[str]]:
        """Every key reattach has reported, under its most recent outcome.

        The same five buckets :meth:`reattach_persistent_views` returns, across
        every pass rather than the latest one. A key moves between buckets as
        later passes re-verdict it (``skipped`` on the boot pass, ``restored``
        once its class imports) and appears in exactly one.

        This is the read for post-boot reconciliation. ``removed`` is the case
        that needs it: only the pass that deletes a row can report it, so a
        consumer reading ``last_reattach_summary`` after a :meth:`reattach`
        re-drive sees an empty list and reconciles nothing.

        Reattach passes only. A row deleted later, by the unreachable sweep or
        by :meth:`prune_registry`, keeps whatever outcome its last pass gave
        it; those deletions arrive as ``REGISTRY_PRUNED``, which a subscriber
        registered after startup does receive. Between them the two cover every
        registration that goes away: this property for the boot passes a late
        subscriber missed, the action for everything after.
        """
        totals: dict[str, list[str]] = {
            "restored": [],
            "skipped": [],
            "failed": [],
            "removed": [],
            "unreachable": [],
        }
        for key, outcome in self._reattach_outcomes.items():
            totals.setdefault(outcome, []).append(key)
        return totals

    async def _serialized_walk(self, name: str, walk: Callable[[], Any]) -> Any:
        """Run one registry walk at a time, refusing a re-entry that would deadlock.

        Both walks dispatch ``REGISTRY_PRUNED`` while they hold the lock, and
        store hooks run inline, so a hook that starts another walk in the
        same task would wait forever on its own caller. It raises instead,
        naming the fix; a walk started from any other task simply waits.
        """
        current = asyncio.current_task()
        if current is not None and self._registry_walk_owner is current:
            raise RuntimeError(
                f"{name}() was called from inside a running reattach pass or "
                f"prune_unreachable() in the same task (a REGISTRY_PRUNED hook, "
                f"for example), where it would wait forever for the walk that "
                f"called it. Schedule it on its own task instead: "
                f"asyncio.create_task(...)."
            )
        async with self._loop_lock("registry_walk"):
            self._registry_walk_owner = current
            try:
                return await walk()
            finally:
                self._registry_walk_owner = None

    async def _reattach_pass(self) -> dict[str, list[str]]:
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
        bot = self._bot
        if bot is None:
            return summary
        # The view store the panels restore onto answers stale clicks too:
        # after the bot's clear() it is a new one, which no send has wrapped yet.
        self._store._answer_stale_clicks(bot)

        # Re-driven every pass, so a DynamicPersistentButton class imported later
        # routes clicks without a restart. discord.py keys dynamic items by their
        # template, so passing the full registry again is an idempotent write.
        # Lazy import breaks the components <-> persistence cycle.
        from ..components.base import _dynamic_button_classes

        if _dynamic_button_classes:
            self._bot.add_dynamic_items(*_dynamic_button_classes.values())

        # Keys a prior pass restored are skipped, and so are keys a live view
        # holds: a send under a key pending from boot rewrites its row, and
        # re-driving it would attach a second instance to the live panel's
        # message, or prune the key on a 404 for the message it replaced.
        live_keys = self._store._live_persistence_keys()
        rows = [
            r
            for r in self._registry_rows
            if r.get("persistence_key") not in self._restored_keys
            and r.get("persistence_key") not in live_keys
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
        # Fetch succeeded, whatever construction then did: a panel whose view
        # raised still has a reachable message, and its stamp must not age.
        reachable_keys: list[str] = []

        # Phase 1 (concurrent, bounded by restore_concurrency): resolve the
        # class, migrate kwargs, and fetch channel and message for each row; a
        # bad row lands in the summary and never aborts the fan-out. Fetches need
        # no per-channel order: read buckets report their limits in headers.
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
                    # Expected for a cog loading after setup_middleware, which
                    # reattach() picks up. The aggregate warning below carries
                    # the signal, so each row logs at DEBUG.
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
                if _bot_closed(bot):
                    # A fetch through a closed client raises; the row stays
                    # pending for the next bot rather than reading as failed.
                    logger.debug(f"Left {persistence_key!r} for the next bot: its bot closed")
                    return None
                logger.error(
                    f"Failed to prepare persistent view {persistence_key!r}: {exc}",
                    exc_info=True,
                )
                summary["failed"].append(persistence_key)
                return None

        prepared = await asyncio.gather(*(_prepare(row) for row in rows))

        # Phase 2 (serial): construct + register each prepared view. Store
        # mutation (add_view, _register_view) must stay serial -- concurrent
        # dispatches would race the shared registries.
        restored_views: list = []
        for item in prepared:
            if item is None:
                continue
            if _bot_closed(bot):
                break
            row, view_cls, init_kwargs, message = item
            # A send under the key while the fetches ran made its own panel;
            # a second one here would share the message it took.
            if self._store._holds_persistence_key(row["persistence_key"]):
                continue
            outcome = await self._reattach_one(
                row, view_cls, init_kwargs, message, row["view_class"], restored_views
            )
            if outcome is not None:
                summary[outcome].append(row["persistence_key"])

        # Rows whose channel or message disappeared while offline are pruned.
        # Nothing here may escape the pass: phase 2 already registered views,
        # and an abort would skip their post-ready repaint and raise out of
        # setup_middleware.
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
            # A rewritten row names a message this pass never fetched: reported
            # unreachable and kept for the next pass, with no stamp, since
            # nothing about the new message failed.
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

        # on_restore reads the bot cache, cold during setup_hook, so each render
        # waits for the gateway; the views already route clicks. A bot that
        # closed during the pass never becomes ready.
        if restored_views and not _bot_closed(bot):
            task = asyncio.create_task(self._run_post_ready_restore(restored_views))
            task.add_done_callback(self._on_post_ready_restore_done)
            self._post_ready_restore_tasks.add(task)

        # Remember what this pass restored so a later reattach() skips it,
        # but not a panel its bot's close released during the pass.
        self._restored_keys.update(
            key for key in summary["restored"] if self._store._holds_persistence_key(key)
        )

        # One aggregate summary (counts, not the full key lists) so a bot with
        # hundreds of persistent views does not flood startup; per-view detail
        # is at DEBUG.
        logger.info(
            f"Persistent view reattach complete: {len(summary['restored'])} restored, "
            f"{len(summary['skipped'])} skipped, {len(summary['failed'])} failed, "
            f"{len(summary['removed'])} removed, {len(summary['unreachable'])} unreachable"
        )
        # A missing class is news only after reattach() had its chance to import
        # it, so only a later pass warns. It names each stored string, which is
        # what a class must register under or session_class_key pins to.
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
        the rows this pass processed. A post-boot reconcile reads
        :attr:`total_reattach_summary` instead: this pass cannot re-report a
        row the boot pass deleted, so its ``removed`` list is empty of them.
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
            # InvalidData, RateLimited, and aiohttp's transport errors are not
            # HTTPException subclasses: uncaught they would log the row as failed,
            # with a traceback and no unreachable stamp. Transport errors are
            # likeliest at boot, before the host has its connection.
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
                    # One column only: a whole-row write would revert a registry
                    # flush landing between the read and the write, leaving the
                    # row on a replaced message for the next boot to prune.
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
                    # No await between the read and the write, so no flush lands
                    # between them. These backends replace on upsert, so the row
                    # goes whole: a partial one would drop its message reference.
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
    ) -> Optional[str]:
        """Construct + register a single view. Returns the summary
        bucket name (``"restored"`` or ``"failed"``), or ``None`` when the
        bot closed during it and the row stays pending for the next bot.

        ``on_restore`` is NOT run here. It reads the gateway cache, which is
        cold on the ``setup_hook`` critical path, so it is deferred to a
        post-ready background task (see :meth:`_run_post_ready_restore`). A
        successfully registered view is appended to ``restored_views`` for
        that deferred render.
        """
        persistence_key = row["persistence_key"]
        view = None
        # Set once SESSION_CREATED and VIEW_CREATED landed, so a later failure's
        # rollback knows to dispatch VIEW_DESTROYED.
        state_registered = False
        try:
            # The middleware drops these at write too; a row an older release
            # wrote can still carry them.
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
            view._registry_message_id = str(message.id)

            # The tree __init__ produced, before on_restore renders anything.
            # A teardown ahead of that render compares against it; see
            # _StatefulMixin._teardown_edit_target.
            view._reattach_baseline_digest = view._compute_tree_digest(ignore_disabled=True)

            # ``is not None`` (not truthy) so a stored ``user_id=0`` still
            # restores -- Discord doesn't mint zero snowflakes, but tests
            # and edge-case fixtures do, and the cost of the explicit form
            # is nothing.
            if row.get("user_id") is not None:
                view.user_id = int(row["user_id"])
            if row.get("guild_id") is not None:
                view.guild_id = int(row["guild_id"])

            # __init__ had no user_id, so no session was derived. The recorded
            # session is rejoined; a re-derived key would collide across one
            # user's panels of a class. A row without one derives it.
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

            # One BATCH_COMPLETE per restored view instead of three notification
            # cycles, sourced from the view as _send_pipeline's batch is.
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
            # An on_bind that reaches Discord through a closed bot raises.
            closed = _bot_closed(self._bot)
            if closed:
                logger.debug(f"Left {persistence_key!r} for the next bot: its bot closed")
            else:
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
                    # _destroy_view clears the registry entry only once state
                    # confirms the removal, and drops a session left memberless.
                    await self._store._destroy_view(view.id)
                else:
                    # State never registered; just drop the active entry.
                    self._store._unregister_view(view.id)
            return None if closed else "failed"

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

    def _start_sweepers(self) -> None:
        """Start every background sweep this configuration asks for. Idempotent."""
        self._start_ttl_sweeper()
        self._start_unreachable_sweeper()

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
        task.add_done_callback(
            functools.partial(self._on_sweeper_done, "TTL sweeper", "_ttl_sweeper_task")
        )
        self._ttl_sweeper_task = task

    def _start_unreachable_sweeper(self) -> None:
        """Spawn the daily unreachable-row sweep when
        ``prune_unreachable_after_days`` is set. Idempotent. No-op when the
        registry backend is opted out.
        """
        if self._unreachable_sweeper_task is not None:
            return
        if self.prune_unreachable_after_days is None or self.registry.backend is None:
            return
        task = asyncio.create_task(self._unreachable_sweeper_loop())
        task.add_done_callback(
            functools.partial(
                self._on_sweeper_done, "Unreachable sweeper", "_unreachable_sweeper_task"
            )
        )
        self._unreachable_sweeper_task = task

    def _on_sweeper_done(self, name: str, field: str, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"{name} crashed: {exc}", exc_info=exc)
        setattr(self, field, None)

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
        # Batches are task-scoped, so each panel's on_restore flushes its own.
        # Bounded by restore_concurrency, the fetch phase's ceiling.
        semaphore = asyncio.Semaphore(self.restore_concurrency)

        async def _render(view) -> bool:
            persistence_key = getattr(view, "_persistence_key", "?")
            async with semaphore:
                # Read after the wait: a panel can close or push to another
                # screen while it queues behind the others.
                if view.is_finished():
                    return False
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

        # Message edits bucket per channel, with sub-limits response headers do
        # not report, so a same-channel burst 429s however the HTTP layer paces.
        # A view whose message was deleted during the wait has no channel and
        # repaints in a group of its own.
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

        Errors are logged and the loop continues: transient backend hiccups
        must not silently kill the sweeper.
        """
        try:
            while not self._closed:
                try:
                    await asyncio.sleep(86400)
                except asyncio.CancelledError:
                    return
                if self._closed:
                    return
                if self.application.backend is None:
                    return
                try:
                    await self._sweep_expired_slots()
                except Exception as exc:
                    logger.error(f"TTL sweeper error: {exc}", exc_info=True)
        except asyncio.CancelledError:
            return

    async def _sweep_expired_slots(self) -> None:
        """Delete expired slot rows, then drop the expired slots from the running bot.

        Rows with ``expires_at=NULL`` are never touched by the backend
        contract, so slots without a TTL are safe.
        """
        cutoff = int(time.time())
        deleted = await self.application.backend.row_delete_where_lt(
            TABLE_APPLICATION_SLOTS, "expires_at", cutoff
        )
        dropped = await self._drop_slots(expired_before=cutoff)
        if deleted or dropped:
            await self._store.dispatch(
                "APPLICATION_SLOTS_PRUNED",
                ActionCreators.application_slots_pruned(deleted, cutoff=cutoff, slots=dropped),
            )

    async def _unreachable_sweeper_loop(self) -> None:
        """Run :meth:`prune_unreachable` once the gateway is ready, then daily.

        The first run waits for ready rather than running during
        ``setup_hook``: a fetch that fails while the gateway is up says the
        channel is unreachable, not the host, and every reattach re-drive a
        consumer issues in ``setup_hook`` has finished by then. Errors are
        logged and the loop continues, as the TTL sweeper does.
        """
        try:
            try:
                await self._bot.wait_until_ready()
            except Exception as exc:
                logger.warning(
                    f"Unreachable sweeper could not wait for the gateway ({exc}); "
                    f"no automatic prune runs this session."
                )
                return
            while not self._closed:
                try:
                    await self.prune_unreachable(older_than_days=self.prune_unreachable_after_days)
                except Exception as exc:
                    logger.error(f"Unreachable sweeper error: {exc}", exc_info=True)
                await asyncio.sleep(86400)
        except asyncio.CancelledError:
            return

    # // ========================================( Prune surface )======================================== // #

    async def prune_application(
        self,
        *,
        slot: Optional[str] = None,
        older_than_days: Optional[int] = None,
    ) -> int:
        """Delete application slots, from storage and from the running bot.

        When ``slot`` is given, removes that one slot (any age): its row, any
        write still waiting for it, and its value in memory. When
        ``older_than_days`` is given, deletes rows whose ``expires_at`` passed
        more than that many days ago and drops those slots from memory too. The two
        modes are mutually exclusive. Returns the number of rows deleted.
        """
        backend = self.application.backend
        if backend is None:
            return 0
        if slot is not None and older_than_days is not None:
            raise ValueError("prune_application: pass slot OR older_than_days, not both")

        if slot is not None:
            # Under the write lock, so a flush already holding the slot's row
            # lands, or puts it back after a failure, before the delete; the
            # slot leaves memory and its waiting write only once the row is
            # gone, so a delete that raises leaves everything as it was.
            ns = getattr(self._middleware, "_ns_application", None)
            async with ns.write_lock if ns is not None else contextlib.nullcontext():
                deleted = await backend.row_delete(TABLE_APPLICATION_SLOTS, {"slot_name": slot})
                dropped = await self._drop_slots([slot])
            cutoff = None
        elif older_than_days is not None:
            cutoff = int(time.time()) - (older_than_days * 86400)
            deleted = await backend.row_delete_where_lt(
                TABLE_APPLICATION_SLOTS, "expires_at", cutoff
            )
            dropped = await self._drop_slots(expired_before=cutoff)
        else:
            return 0

        await self._store.dispatch(
            "APPLICATION_SLOTS_PRUNED",
            ActionCreators.application_slots_pruned(deleted, cutoff=cutoff, slots=dropped),
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

        Rows are matched by key alone, so this deletes whatever holds a key
        at the moment it runs, including the row of a panel still running.
        It is meant for a key no live panel holds: retire a live one through
        its own ``exit()``, which retires the registration only while that
        panel owns it, and ask
        ``StateStore.get_active_view(persistence_key=...)`` whether there is
        one. A prune that does take a live panel's row logs a warning.

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
            # Matched by store ownership rather than by a lookup for the panel
            # instance, since a pushed child holds its panel's key through
            # _registry_message_id. Losing the row leaves the panel live now
            # and absent after the next restart, which nothing else reports.
            held = sorted(set(pruned) & self._store._live_persistence_keys())
            if held:
                logger.warning(
                    f"prune_registry deleted the registration of {len(held)} live "
                    f"panel(s) ({', '.join(map(repr, held))}); each stays on screen "
                    f"now and is not restored after a restart. prune_registry matches "
                    f"by key, so it is meant for a key no live panel holds: exit() "
                    f"retires a live one, and get_active_view(persistence_key=...) "
                    f"says whether there is one."
                )

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

        A row whose key a live panel in this process holds is kept: the panel
        owns the key, so a failed fetch says nothing about whether its row
        should go. Serialized with :meth:`reattach_persistent_views`, the other
        walk that acts on registry rows.

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

        return await self._serialized_walk(
            "prune_unreachable",
            lambda: self._prune_unreachable_pass(older_than_days, summary),
        )

    async def _prune_unreachable_pass(
        self, older_than_days: int, summary: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        backend = self.registry.backend
        # From disk, never the in-memory mirror: a re-send in this process
        # clears the stamp on disk without refreshing the copy, and the
        # destructive path has to read the authority.
        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        stamped = [r for r in rows if r.get("first_unreachable_at") is not None]
        live_keys = self._store._live_persistence_keys()
        summary["kept"].extend(
            r["persistence_key"] for r in stamped if r["persistence_key"] in live_keys
        )
        candidates = [r for r in stamped if r["persistence_key"] not in live_keys]
        if not candidates:
            return summary

        cutoff = int(time.time()) - older_than_days * 86400
        kill: list[str] = []

        # Bounded fan-out, the same shape the reattach pass uses for the same
        # two fetches: this walk holds the registry lock, and its candidate
        # set is largest exactly when an outage or a permission change stamped
        # rows in bulk. Verdicts stay serial below, in row order.
        sem = asyncio.Semaphore(self.restore_concurrency)

        async def _verify(row: dict[str, Any]) -> tuple[Optional[Any], list[str], bool]:
            removed_scratch: list[str] = []
            try:
                async with sem:
                    fetched = await self._fetch_restore_message(row, removed_scratch, [])
            except Exception as exc:
                # Reported as unverified, not as a failed fetch: an aged row
                # whose re-check never produced an answer would otherwise fall
                # through to the cutoff below and be deleted on no evidence.
                logger.warning(
                    f"Re-verifying {row.get('persistence_key')!r} for the unreachable "
                    f"sweep failed; keeping the row: {exc}",
                    exc_info=exc,
                )
                return None, [], False
            return fetched, removed_scratch, True

        verified = await asyncio.gather(*(_verify(row) for row in candidates))

        recovered: list[str] = []
        for row, (fetched, removed_scratch, checked) in zip(candidates, verified):
            key = row.get("persistence_key")
            if not checked:
                summary["kept"].append(key)
            elif fetched is not None:
                summary["recovered"].append(key)
                recovered.append(key)
            elif removed_scratch:
                # A definitive 404. Age is irrelevant; the message is gone.
                kill.append(key)
            elif row["first_unreachable_at"] <= cutoff:
                kill.append(key)
            else:
                summary["kept"].append(key)

        if recovered:
            # One write for the batch: a recovered row's stamp is cleared so
            # its next unreachable spell starts its own age window.
            await self._write_unreachable_stamps(recovered, None)

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
        sets its closed flag first, so a change arriving during the final
        flush is held for a reopen instead of scheduling a write.

        Safe to call twice, and from two tasks at once: the second waits
        for the first. One misbehaving backend is logged and does not
        block the others from closing cleanly.
        """
        async with self._lifecycle_lock():
            if self._closed:
                return
            await self._close_now()

    def _lifecycle_lock(self) -> asyncio.Lock:
        """The lock :meth:`close` and :meth:`_reopen` share."""
        return self._loop_lock("lifecycle")

    def _loop_lock(self, name: str) -> asyncio.Lock:
        """The lock kept under ``name``, made for the running loop.

        A bot run twice through ``asyncio.run`` uses two loops, and a lock
        one loop waited on refuses the other.
        """
        loop = asyncio.get_running_loop()
        held = self._loop_locks.get(name)
        if held is None or held[0] is not loop:
            held = (loop, asyncio.Lock())
            self._loop_locks[name] = held
        return held[1]

    async def _close_now(self) -> None:
        self._close_started = True
        # Cancel the sweepers first. Each sleeps between runs and a cancelled
        # prune loses nothing: it drains no buffer, and a partial prune leaves
        # rows the next boot re-seeds the store from.
        for field in ("_ttl_sweeper_task", "_unreachable_sweeper_task"):
            task = getattr(self, field)
            if task is None:
                continue
            task.cancel()
            await _settle(task)
            setattr(self, field, None)

        # Cancel any post-ready restore renders still waiting on the gateway
        # (or mid-render) when shutdown begins. Iterate a copy -- the done
        # callback discards from the set as each cancelled task settles.
        for task in list(self._post_ready_restore_tasks):
            task.cancel()
            await _settle(task)
        self._post_ready_restore_tasks.clear()

        if self._middleware is not None:
            try:
                await self._middleware.close()
            except Exception as exc:
                logger.error(f"Error closing persistence middleware: {exc}")

        await self._close_backends()

        self._closed = True
        logger.debug("PersistenceManager closed")

    async def _close_backends(self) -> None:
        for backend in self._unique_backends.values():
            try:
                await _bounded_wait(backend.close(), _BACKEND_CLOSE_SECONDS)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Closing {type(backend).__name__} timed out after at most "
                    f"{_BACKEND_CLOSE_SECONDS:g} seconds; persistence closed without it."
                )
            except Exception as exc:
                logger.error(f"Error closing {type(backend).__name__}: {exc}")

    async def _reopen(self) -> bool:
        """Open the backends again after :meth:`close`; ``True`` when it did.

        The middleware calls this when ``setup_middleware`` runs again (a bot
        closed and started again in the same process), when the bot connects
        again after its close, and when a close finds the bot started again
        while it ran. It waits out a close still running, since a restart
        command closes from its own task. State is still in memory, so
        nothing is rehydrated and no migration runs; the sweepers are
        restarted by the caller.
        """
        async with self._lifecycle_lock():
            if not (self._closed or self._close_started):
                return False
            self._initialized = False
            try:
                await self.initialize_backends()
            except BaseException:
                # A backend that opened before another failed would keep a
                # SQLite worker thread alive with nothing left to close it.
                await self._close_backends()
                raise
            self._closed = False
            self._close_started = False
        logger.debug("PersistenceManager reopened")
        return True
