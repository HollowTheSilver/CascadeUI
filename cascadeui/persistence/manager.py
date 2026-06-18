"""Persistence coordinator for the two-namespace architecture.

:class:`PersistenceManager` owns the lifecycle of every configured
backend and coordinates the two namespaces (registry, application
slots) against the store. The manager is constructed by
:class:`~cascadeui.state.middleware.PersistenceMiddleware` from the
two namespace configs and a store reference; callers interact with
the manager for explicit prunes, slot-policy registration, and
shutdown.

When any registered :class:`SlotPolicy` carries ``ttl_days``, the
manager starts a daily background TTL sweeper at
:meth:`install_middleware` time. The sweeper calls
``row_delete_where_lt(TABLE_APPLICATION_SLOTS, "expires_at", now)``
once per 24 hours, so rows with ``expires_at=NULL`` (no TTL) are
never touched by contract. ``expires_at`` is an absolute wall-clock
timestamp written at write-time, so TTL does not restart across
bot restarts. :meth:`rehydrate` issues one prune pass before reading
so rows that expired while the bot was offline are dropped rather
than loaded into memory.

Lifecycle phases:

1. :meth:`initialize_backends` -- dedup by backend identity, call
   :meth:`~PersistenceBackend.initialize` once per unique instance.
2. :meth:`apply_migrations` -- for each configured namespace, run any
   registered schema migrators to bring the table from the on-disk
   version to :data:`CURRENT_SCHEMA_VERSIONS`.
3. :meth:`rehydrate` -- blocking read from each namespace into the
   in-memory store. Returns only when the store is fully restored.
4. :meth:`reattach_persistent_views` -- (requires ``bot``) walks the
   registry, re-fetches messages, reconstructs view instances.
5. :meth:`install_middleware` -- installs the write-through middleware
   on the store. The manager tracks which namespaces are write-enabled.

Runtime surface: :meth:`prune_application`, :meth:`prune_registry`,
:meth:`register_slot_policy`, :meth:`close`. Each prune dispatches its
corresponding bookkeeping action (:data:`APPLICATION_SLOTS_PRUNED`,
:data:`REGISTRY_PRUNED`) so subscribers observe the event.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..exceptions import PersistenceInitError, PersistenceRehydrateError
from .config import (
    NAMESPACE_APPLICATION,
    NAMESPACE_REGISTRY,
    ApplicationPersistence,
    RegistryPersistence,
    SlotPolicy,
)
from .migrations import get_kwargs_migrator, get_schema_migrator
from .protocols import PersistenceBackend
from .schema import (
    CURRENT_SCHEMA_VERSIONS,
    TABLE_APPLICATION_SLOTS,
    TABLE_PERSISTENT_VIEWS,
)

if TYPE_CHECKING:
    from ..state.store import StateStore

logger = logging.getLogger(__name__)


# Kwargs captured by ``__init_subclass__`` that are also surfaced as
# their own registry-row column (``persistence_key``) or are not safely
# round-trippable through JSON (``theme`` -- a live ``Theme`` object).
# The middleware strips them at write so the registry stays clean, and
# ``_reattach_one`` strips them at read so the row column wins on
# reconstruction without a duplicate-keyword crash.
_NON_PERSISTABLE_KWARGS: frozenset[str] = frozenset({"persistence_key", "theme"})


# // ========================================( Manager )======================================== // #


class PersistenceManager:
    """Owns backend lifecycle and namespace routing for the store."""

    def __init__(
        self,
        store: "StateStore",
        registry: Optional[RegistryPersistence] = None,
        application: Optional[ApplicationPersistence] = None,
        bot: Any = None,
    ) -> None:
        self._store = store
        self._bot = bot

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

        # Slot policy registry, seeded from ApplicationPersistence.slots
        # and extended at runtime by register_slot_policy.
        self._slot_policies: dict[str, SlotPolicy] = dict(self.application.slots)

        # TTL sweeper task. Started by install_middleware() only when at
        # least one slot declares ttl_days > 0. Cancelled by close().
        self._ttl_sweeper_task: Optional[asyncio.Task] = None

        # Observability hooks for operators/devtools. Read by the
        # persistence middleware via _fire_hook(). Empty by default;
        # users register via register_hook().
        self._hooks: dict[str, list[Callable[..., Any]]] = {}

        # Middleware handle populated by install_middleware(). Stays
        # None when every namespace is opted out (no backend).
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
        is not yet running, bootstrap it. Without this, a slot policy
        registered after :meth:`install_middleware` would be invisible
        to the sweeper -- it inspects ``_slot_policies`` once at install
        time and never re-checks. Idempotent: ``_start_ttl_sweeper`` is
        a no-op when the task is already alive or when the application
        backend is opted out.
        """
        if not isinstance(name, str):
            raise TypeError(f"slot name must be str, got {type(name).__name__}")
        if not isinstance(policy, SlotPolicy):
            raise TypeError(f"policy must be a SlotPolicy, got {type(policy).__name__}")
        if name in self._slot_policies:
            raise ValueError(f"Slot policy already registered for {name!r}")
        self._slot_policies[name] = policy

        if policy.persistent and policy.ttl_days is not None:
            self._start_ttl_sweeper()

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

    async def apply_migrations(self) -> None:
        """Run registered schema migrators up to current version for
        each configured namespace. No-op when on-disk version equals
        current.

        Migrators are pulled from :mod:`cascadeui.persistence.migrations`
        via :func:`get_schema_migrator`. The loop advances one version
        per iteration so multi-step upgrades (v1 -> v3) run v1->v2
        then v2->v3 sequentially.
        """
        for ns_cfg, table in (
            (self.registry, TABLE_PERSISTENT_VIEWS),
            (self.application, TABLE_APPLICATION_SLOTS),
        ):
            if ns_cfg.backend is None:
                continue
            current = CURRENT_SCHEMA_VERSIONS[table]
            on_disk = await ns_cfg.backend.get_schema_version(table)

            if on_disk == 0:
                # Fresh install. DDL already created the table at
                # current version, so record it and move on.
                await ns_cfg.backend.set_schema_version(table, current)
                continue

            while on_disk < current:
                migrator = get_schema_migrator(table, on_disk)
                if migrator is None:
                    raise PersistenceInitError(
                        f"No migrator registered for {table} "
                        f"v{on_disk} -> v{on_disk + 1}; cannot upgrade"
                    )
                await migrator(ns_cfg.backend)
                on_disk += 1
                await ns_cfg.backend.set_schema_version(table, on_disk)
                logger.info(f"Migrated {table} to v{on_disk}")

            if on_disk > current:
                # DB was written by a newer CascadeUI release. Refuse
                # to run rather than silently downgrade data.
                raise PersistenceInitError(
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

        Returns a summary dict with four lists of ``persistence_key`` values:

        - ``restored`` -- reattached successfully.
        - ``skipped`` -- view class not imported OR missing kwargs
          migrator. Row stays on disk so the next restart can pick it
          up once the import or migrator is fixed.
        - ``failed`` -- construction, migrator, or ``on_restore`` raised.
          Row stays on disk for manual recovery.
        - ``removed`` -- channel or message gone. Row deleted from disk
          via :meth:`prune_registry` and its bookkeeping action dispatched.

        Requires ``self._bot``. No-op when bot is absent (data-only
        persistence mode).
        """
        summary: dict[str, list[str]] = {
            "restored": [],
            "skipped": [],
            "failed": [],
            "removed": [],
        }
        if self._bot is None:
            return summary

        rows = self._registry_rows
        if not rows:
            return summary

        # Late import breaks the views <-> persistence cycle. The class
        # registry is populated by _PersistentMixin.__init_subclass__ at
        # import time, so user modules must be imported before this
        # method runs (standard setup_hook ordering).
        from ..views.persistent import _persistent_view_classes

        removed_keys: list[str] = []

        for row in rows:
            persistence_key = row["persistence_key"]
            class_name = row["view_class"]

            view_cls = _persistent_view_classes.get(class_name)
            if view_cls is None:
                logger.warning(
                    f"Persistent view class {class_name!r} not found for "
                    f"persistence_key {persistence_key!r}. Ensure the module is imported "
                    "before PersistenceMiddleware.initialize()."
                )
                summary["skipped"].append(persistence_key)
                continue

            migrated = await self._migrate_init_kwargs(row, view_cls, summary)
            if migrated is None:
                # Summary list already populated by _migrate_init_kwargs
                continue
            init_kwargs = migrated

            fetched = await self._fetch_restore_message(row, removed_keys)
            if fetched is None:
                # Already appended to removed_keys; row deletion happens
                # in one batch at end of loop.
                continue
            _channel, message = fetched

            outcome = await self._reattach_one(row, view_cls, init_kwargs, message, class_name)
            summary[outcome].append(persistence_key)

        # Delete rows whose channel or message disappeared while the bot
        # was offline. prune_registry dispatches REGISTRY_PRUNED so
        # subscribers observe the bookkeeping action.
        if removed_keys:
            await self.prune_registry(persistence_keys=removed_keys)
            summary["removed"].extend(removed_keys)

        logger.info(f"Persistent view reattach complete: {summary}")
        return summary

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
    ) -> Optional[tuple[Any, Any]]:
        """Fetch the target channel and message for a registry row.
        Appends to ``removed_keys`` and returns ``None`` when either is
        gone or the channel is not messageable."""
        import discord

        persistence_key = row["persistence_key"]
        channel_id = row["channel_id"]
        message_id = row["message_id"]

        try:
            channel = self._bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self._bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(f"Could not fetch channel {channel_id} for {persistence_key!r}: {exc}")
            removed_keys.append(persistence_key)
            return None

        if not isinstance(channel, discord.abc.Messageable):
            logger.warning(
                f"Channel {channel_id} for {persistence_key!r} is not messageable "
                f"({type(channel).__name__}); removing entry"
            )
            removed_keys.append(persistence_key)
            return None

        try:
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(f"Could not fetch message {message_id} for {persistence_key!r}: {exc}")
            removed_keys.append(persistence_key)
            return None

        return channel, message

    async def _reattach_one(
        self,
        row: dict[str, Any],
        view_cls: type,
        init_kwargs: dict[str, Any],
        message: Any,
        class_name: str,
    ) -> str:
        """Construct + register a single view. Returns the summary
        bucket name (``"restored"`` or ``"failed"``)."""
        persistence_key = row["persistence_key"]
        view = None
        # Tracks whether ``_register_state`` succeeded -- if a downstream
        # step (``_update_message_state``, ``register_view``, ``on_restore``)
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

            # __init__ ran with user_id=None so session auto-derivation
            # was skipped; re-derive now that identity is known.
            if view.user_id and not view.session_id:
                view.session_id = f"{type(view)._class_session_key()}:user_{view.user_id}"

            self._bot.add_view(view, message_id=message.id)

            # _register_view first so the instance index is complete before
            # _register_state's VIEW_CREATED notifies subscribers. Mirrors
            # the order used by _send_pipeline -- subscribers and instance
            # limit checks see a consistent state row at every step.
            self._store._register_view(view)

            # Batch the registration dispatches and ``on_restore`` so the
            # three startup actions (SESSION_CREATED + VIEW_CREATED + VIEW_UPDATED)
            # plus any actions the user dispatches inside ``on_restore`` collapse
            # into a single BATCH_COMPLETE notification per restored view.
            # Without the batch, a project with N persistent views fires 3N+
            # standalone notification cycles at startup. Batch source_id is
            # the view's id so the BATCH_COMPLETE rides the acting-view
            # inline-notification path, matching the _send_pipeline contract.
            async with self._store.batch(source_id=view.id):
                await view._register_state()
                state_registered = True
                await view._update_message_state(message)
                await view.on_restore(self._bot)

            logger.info(f"Restored persistent view {persistence_key!r} ({class_name})")
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

    # // ========================================( Middleware install )======================================== // #

    def install_middleware(self) -> None:
        """Install per-namespace write-through middleware on the store.

        Constructs one :class:`PersistenceMiddleware` instance and
        registers it with the store's middleware chain. The middleware
        is stashed on ``self._middleware`` so :meth:`close` can flush +
        close it cleanly during shutdown.

        Skipped when every namespace is opted out (all three backends
        are ``None``): without a backend, the middleware would do
        nothing but still incur per-dispatch overhead.
        """
        # Late import: persistence imports state, state imports
        # persistence in type-checking only. A top-level import would
        # fire the circular path at load time.
        from ..state.middleware.persistence import PersistenceMiddleware

        if not any(cfg.backend is not None for cfg in (self.registry, self.application)):
            return

        middleware = PersistenceMiddleware(self)
        self._middleware: PersistenceMiddleware = middleware
        self._store._add_middleware(middleware)

        # Start the daily TTL sweeper when any persistent slot declares
        # ttl_days. Nothing to sweep otherwise -- skip the task.
        self._start_ttl_sweeper()

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
                    deleted = await backend.row_delete_where_lt(
                        TABLE_APPLICATION_SLOTS,
                        "expires_at",
                        int(time.time()),
                    )
                    if deleted:
                        await self._store.dispatch(
                            "APPLICATION_SLOTS_PRUNED",
                            {"deleted": deleted, "reason": "ttl_sweep"},
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
        """Delete application_slots rows.

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
        elif older_than_days is not None:
            cutoff = int(time.time()) - (older_than_days * 86400)
            deleted = await backend.row_delete_where_lt(
                TABLE_APPLICATION_SLOTS, "expires_at", cutoff
            )
        else:
            return 0

        await self._store.dispatch(
            "APPLICATION_SLOTS_PRUNED",
            {"deleted": deleted, "cutoff": older_than_days},
        )
        return deleted

    async def prune_registry(
        self,
        *,
        persistence_keys: Optional[list[str]] = None,
    ) -> int:
        """Delete persistent_views rows. When ``persistence_keys`` is given,
        only those rows are removed; otherwise clears the whole
        registry (destructive, rarely wanted)."""
        backend = self.registry.backend
        if backend is None:
            return 0

        deleted = 0
        if persistence_keys is None:
            # Clear all: row_select + row_delete per row. Callers who
            # hit this path are rare (test cleanup, admin tooling).
            rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
            for r in rows:
                deleted += await backend.row_delete(
                    TABLE_PERSISTENT_VIEWS, {"persistence_key": r["persistence_key"]}
                )
        else:
            for sk in persistence_keys:
                deleted += await backend.row_delete(TABLE_PERSISTENT_VIEWS, {"persistence_key": sk})

        await self._store.dispatch(
            "REGISTRY_PRUNED",
            {"deleted": deleted, "reason": "explicit" if persistence_keys else "clear_all"},
        )
        return deleted

    # // ========================================( Shutdown )======================================== // #

    async def flush_all(self) -> None:
        """Drain every namespace's pending writes synchronously.

        Thin passthrough to :meth:`PersistenceMiddleware.flush_all`. Call
        from devtools or operator-facing commands that want an immediate
        disk write without tearing the manager down. No-op when no
        middleware is installed (every namespace opted out of persistence).
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
