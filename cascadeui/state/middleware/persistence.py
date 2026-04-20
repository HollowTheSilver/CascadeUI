"""Fan-out persistence middleware for the two-namespace architecture.

One middleware instance routes every dispatched action to its owning
namespace (registry, application), accumulates dirty rows per key, and
schedules per-namespace debounced flushes.

Design contracts:

- **Per-key accumulation.** The middleware tracks dirty rows by their
  primary key (``persistence_key`` / ``slot_name``) so a concurrent burst on
  the same row collapses into one upsert. Deletes are tracked alongside
  and applied after upserts.
- **Per-namespace windows with max-age ceiling.** Registry writes fire
  immediately; application writes debounce at 2s with a 10s ceiling.
  Steady traffic that never hits the idle window still flushes when
  ``max_age`` expires.
- **Opt-in filter at the dispatch seam.** Only slots registered
  persistent via :func:`~cascadeui.state.slots.access_slot` with
  ``persistent=True`` reach the backend. Everything else is skipped at
  routing time and the middleware never schedules a task for it. This
  mirrors the opt-in model used by ``PersistentView``.
- **Direct task ownership.** Flush tasks are created via
  ``asyncio.create_task`` and tracked on the middleware itself. Cancel
  unwinds through asyncio's own coroutine driver so shutdown leaves no
  orphaned coroutines.
- **Exponential backoff retry.** Failed flushes re-enqueue dirty rows
  and schedule a retry with backoff capped at 60s. After
  ``MAX_RETRIES`` consecutive failures the namespace logs CRITICAL and
  resets its counter; the rows remain dirty so the next action retries.
- **Observability hooks.** The middleware fires ``on_flush`` and
  ``on_error`` on the manager so devtools or operator tooling can
  observe write cadence without scraping logs.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from ...persistence.manager import _NON_PERSISTABLE_KWARGS
from ...persistence.protocols import PersistenceBackend
from ...persistence.schema import (
    TABLE_APPLICATION_SLOTS,
    TABLE_PERSISTENT_VIEWS,
)
from ..types import Action, StateData

if TYPE_CHECKING:
    from ...persistence.config import ApplicationPersistence, RegistryPersistence
    from ...persistence.manager import PersistenceManager

logger = logging.getLogger(__name__)


# // ========================================( Constants )======================================== // #


# Built-in actions that carry no persistence obligation. Views and
# sessions are rebuilt from the registry rows on restart, navigation
# state is ephemeral, and the *_PRUNED actions fire *after* the manager
# has already deleted rows on disk.
_BOOKKEEPING_ACTIONS = frozenset(
    {
        "SESSION_CREATED",
        "SESSION_UPDATED",
        "VIEW_CREATED",
        "VIEW_UPDATED",
        "VIEW_DESTROYED",
        "COMPONENT_INTERACTION",
        "MODAL_SUBMITTED",
        "NAVIGATION_PUSH",
        "NAVIGATION_POP",
        "NAVIGATION_REPLACE",
        "UNDO",
        "REDO",
        "BATCH_COMPLETE",
        "APPLICATION_SLOTS_PRUNED",
        "REGISTRY_PRUNED",
    }
)


# All scheduled flush tasks are tracked on ``PersistenceMiddleware._tasks``
# rather than through ``TaskManager``. ``TaskManager._wrap_task`` double-
# wraps the coroutine, which orphans the inner ``_run_flush`` coroutine
# when the outer task is cancelled before its first step (Python never
# enters a never-stepped coroutine frame on cancel, so no except block
# can close the inner coro). Owning the tasks directly lets
# ``asyncio.Task`` drive ``_run_flush`` itself so cancel unwinds cleanly.


# // ========================================( Per-namespace state )======================================== // #


@dataclass
class _NamespaceState:
    """Mutable state carried per namespace (registry / application).

    Each namespace has its own debounce window, dirty-row buffer, and
    flush task. Keeping them in one object simplifies the routing
    methods -- they all look like ``self._route_*`` setting fields on
    ``self._ns_*`` and calling ``_schedule(ns)``.
    """

    name: str
    backend: Optional[PersistenceBackend]
    interval: float
    max_age: float
    dirty_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    deleted_keys: set[str] = field(default_factory=set)
    first_dirty_at: Optional[float] = None
    last_action_at: float = 0.0
    task: Optional[asyncio.Task] = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retry_count: int = 0


# // ========================================( Middleware )======================================== // #


class PersistenceMiddleware:
    """Fan-out persistence middleware for the two-namespace architecture.

    The canonical construction path is direct -- the middleware owns
    its configuration and runs its own async startup pipeline
    (initialize_backends -> apply_migrations -> rehydrate -> install
    message cleanup -> reattach persistent views) inside
    :meth:`initialize`. Pass the middleware to
    :func:`~cascadeui.setup.setup_middleware` to install it::

        await setup_middleware(
            PersistenceMiddleware(backend=SQLiteBackend("data.db"), bot=self),
            UndoMiddleware(),
        )

    A pre-built :class:`PersistenceManager` may also be supplied via
    ``manager=`` for callers that need to customize manager internals
    before install. When ``manager=`` is passed, the pipeline kwargs
    (``backend``, ``registry``, ``application``, ``bot``) are ignored.
    """

    # Backoff curve for failed flushes: 1s, 2s, 4s, 8s, 16s, capped at
    # 60s. MAX_RETRIES bounds the consecutive-failure count before the
    # namespace logs CRITICAL and resets.
    _BACKOFF_BASE: float = 1.0
    _BACKOFF_CAP: float = 60.0
    MAX_RETRIES: int = 5

    def __init__(
        self,
        manager: Optional["PersistenceManager"] = None,
        *,
        backend: Optional[PersistenceBackend] = None,
        registry: Optional["RegistryPersistence"] = None,
        application: Optional["ApplicationPersistence"] = None,
        bot: Any = None,
        migrators: Optional[Any] = None,
    ) -> None:
        # Fail fast on a wrong-type bot. The reattach pipeline calls
        # ``bot.add_listener`` / ``bot.get_channel``; passing a bare
        # string or a non-Client object would crash deep inside
        # :meth:`initialize` long after the construction site is gone
        # from the stack. Lazy import so the state package does not
        # hard-require discord.py at module load.
        if bot is not None:
            import discord

            if not isinstance(bot, discord.Client):
                raise TypeError(
                    "PersistenceMiddleware bot= must be a discord.py Bot "
                    f"(discord.Client subclass) or None, got {type(bot).__name__!r}."
                )

        # Two construction paths. The legacy path (``manager=``) is
        # kept so internal call sites that hand-build a manager before
        # install keep working. The direct path stashes the raw config
        # and defers manager construction to :meth:`initialize`, where
        # the store reference is finally available.
        self._pending_config: Optional[dict[str, Any]]
        if manager is not None:
            self._manager = manager
            self._store = manager._store
            self._pending_config = None
            # Legacy path presumes the caller already ran the pipeline
            # (initialize_backends + migrations + rehydrate) before
            # constructing the middleware. Flag init as done so
            # :meth:`initialize` short-circuits and does not re-run
            # rehydrate on top of a hot store.
            self._initialized: bool = True
            self._build_namespaces(manager)
        else:
            self._manager = None  # type: ignore[assignment]
            self._store = None  # type: ignore[assignment]
            self._pending_config = {
                "backend": backend,
                "registry": registry,
                "application": application,
                "bot": bot,
                "migrators": migrators,
            }
            self._initialized = False
            # Namespace state is built after the manager resolves in
            # :meth:`initialize`; leave as None until then.
            self._ns_registry = None  # type: ignore[assignment]
            self._ns_application = None  # type: ignore[assignment]

        self._closed: bool = False
        # Tasks are owned here so cancel-before-start unwinds through
        # asyncio.Task's own coroutine driver (which closes the coro
        # cleanly) rather than orphaning a never-awaited coroutine.
        self._tasks: set[asyncio.Task] = set()

    def _build_namespaces(self, manager: "PersistenceManager") -> None:
        """Construct the per-namespace routing state from a resolved manager.

        Registry defaults to immediate flush: PersistentView lifecycle
        is low-frequency and losing a row strands the view across
        restart. Application debounces at 2s with a 10s ceiling because
        reducer writes come in bursts during user interaction.
        """
        self._ns_registry = _NamespaceState(
            name="registry",
            backend=manager.registry.backend,
            interval=0.0,
            max_age=0.0,
        )
        self._ns_application = _NamespaceState(
            name="application",
            backend=manager.application.backend,
            interval=2.0,
            max_age=10.0,
        )

    # // ========================================( Initialize )======================================== // #

    async def initialize(self, store: Any) -> None:
        """Run the async startup pipeline for this middleware.

        Invoked by :func:`~cascadeui.setup.setup_middleware` after the
        middleware is installed into the dispatch chain. The pipeline
        is idempotent; re-invocation is a no-op once the middleware
        has initialized.

        The pipeline runs in seven phases:

        1. Build the :class:`PersistenceManager` from the stashed
           config if one was not supplied at construction.
        2. Initialize unique backends.
        3. Apply schema migrations.
        4. Blocking rehydrate: read both namespaces into the store.
        5. Install the gateway message-cleanup listener (when ``bot``
           is available) so externally-deleted messages trigger the
           view's ``on_message_delete`` hook.
        6. Stash the manager on the store for later prune, slot-policy
           registration, and shutdown.
        7. Reattach persistent views (when ``bot`` is available).
        """
        if self._initialized:
            return

        cfg = self._pending_config or {}
        manager = self._resolve_manager(store, cfg)
        self._manager = manager
        self._store = store
        self._build_namespaces(manager)

        await manager.initialize_backends()
        await manager.apply_migrations()
        await manager.rehydrate()

        bot = cfg.get("bot")
        # Restored views skip send() so they never trigger the lazy
        # listener installation path in _StatefulMixin. Install it
        # eagerly here so on_message_delete fires for externally-
        # deleted persistent messages.
        if bot is not None and not getattr(store, "_cleanup_listener_installed", False):
            store._install_message_cleanup(bot)

        # Stash on the store so later code (prune, slot-policy
        # registration, shutdown) can look the manager up without
        # threading it through every call site.
        store.persistence_manager = manager

        # Start the daily TTL sweeper when any persistent slot declares
        # ttl_days. Nothing to sweep otherwise -- skip the task. The
        # legacy install_middleware path calls this itself; the direct
        # path routes it here so the state is reachable from either
        # entry.
        manager._start_ttl_sweeper()

        if bot is not None:
            await manager.reattach_persistent_views()
        else:
            # Warn loudly when the bot is absent but PersistentView
            # subclasses are registered. Without a bot, those views
            # will not be reattached on restart. Lazy import avoids
            # a cycle with views/persistent.py.
            from ...views.persistent import _persistent_view_classes

            if _persistent_view_classes:
                logger.warning(
                    f"PersistenceMiddleware initialized without bot=, "
                    f"but {len(_persistent_view_classes)} PersistentView "
                    f"subclass(es) are registered "
                    f"({', '.join(sorted(_persistent_view_classes))}). "
                    "Without a bot, these views will NOT be reattached "
                    "on restart. Pass the bot instance to enable "
                    "reattachment."
                )

        self._initialized = True

    def _resolve_manager(self, store: Any, cfg: dict[str, Any]) -> "PersistenceManager":
        """Build a :class:`PersistenceManager` from the stashed config.

        Zero-config construction defaults to aiosqlite-backed SQLite, and
        the ``backend=`` shorthand fills any namespace that was not given
        an explicit config.
        """
        from ...exceptions import PersistenceInitError
        from ...persistence.config import ApplicationPersistence, RegistryPersistence
        from ...persistence.manager import PersistenceManager

        backend = cfg.get("backend")
        registry = cfg.get("registry")
        application = cfg.get("application")
        bot = cfg.get("bot")

        # Zero-arg default: aiosqlite-backed SQLite at cascadeui.db.
        # Lazy import so the optional dependency only kicks in when
        # callers reach for the default.
        if backend is None and registry is None and application is None:
            try:
                from ...persistence.backends import SQLiteBackend
            except ImportError as exc:
                raise PersistenceInitError(
                    "PersistenceMiddleware with no backend configured "
                    "defaults to SQLiteBackend('cascadeui.db'), which "
                    "requires the optional 'aiosqlite' dependency. "
                    "Install it with: pip install 'cascadeui[sqlite]' "
                    "or pass an explicit backend= argument."
                ) from exc
            backend = SQLiteBackend("cascadeui.db")

        resolved_registry = (
            registry if registry is not None else RegistryPersistence(backend=backend)
        )
        resolved_application = (
            application if application is not None else ApplicationPersistence(backend=backend)
        )

        return PersistenceManager(
            store=store,
            registry=resolved_registry,
            application=resolved_application,
            bot=bot,
        )

    # // ========================================( Middleware entry )======================================== // #

    async def __call__(
        self,
        action: Action,
        state: StateData,
        next_fn: Callable,
    ) -> StateData:
        """Run the reducer chain, then route effects to the namespaces."""
        # Safety net: a dispatch that fires between install and
        # initialize cannot route because the namespaces have not been
        # built yet. Pass through the chain so the reducer runs, but
        # skip persistence side effects.
        if not self._initialized or self._ns_registry is None:
            return await next_fn(action, state)

        state_before = self._store.state
        result = await next_fn(action, state)
        state_after = self._store.state

        if self._closed:
            return result

        action_type = action["type"]
        if action_type in _BOOKKEEPING_ACTIONS:
            return result
        if state_after is state_before:
            return result

        if action_type in ("PERSISTENT_VIEW_REGISTERED", "PERSISTENT_VIEW_UNREGISTERED"):
            self._route_registry(action)
        else:
            self._route_application(state_before, state_after)

        return result

    # // ========================================( Routing )======================================== // #

    def _route_registry(self, action: Action) -> None:
        ns = self._ns_registry
        if ns.backend is None:
            return
        payload = action["payload"]
        persistence_key = payload.get("persistence_key")
        if not persistence_key:
            return

        if action["type"] == "PERSISTENT_VIEW_UNREGISTERED":
            ns.deleted_keys.add(persistence_key)
            ns.dirty_rows.pop(persistence_key, None)
        else:
            # Look up the registering view in _active_views to read
            # _init_kwargs + kwargs_schema_version. The dispatch site
            # (_register_persistent) runs after register_view(), so the
            # view is guaranteed present when the middleware observes
            # this action.
            view = self._find_view_by_persistence_key(persistence_key)
            row = self._build_registry_row(payload, view)
            if row is None:
                return
            ns.dirty_rows[persistence_key] = row
            ns.deleted_keys.discard(persistence_key)

        self._schedule(ns)

    def _route_application(self, state_before: StateData, state_after: StateData) -> None:
        ns = self._ns_application
        if ns.backend is None:
            return

        app_before = state_before.get("application") or {}
        app_after = state_after.get("application") or {}

        # Positive filter: scan only slots registered persistent via
        # access_slot(..., persistent=True). Everything else is in-memory
        # by default and produces zero persistence pressure. No full-tree
        # walk -- the set iteration is bounded by how many slots the
        # user explicitly opted in.
        from ..slots import _PERSISTENT_SLOTS

        if not _PERSISTENT_SLOTS:
            return

        changed: set[str] = set()
        for slot_name in _PERSISTENT_SLOTS:
            if app_before.get(slot_name) is not app_after.get(slot_name):
                changed.add(slot_name)

        if not changed:
            return

        now = int(time.time())
        for slot_name in changed:
            value = app_after.get(slot_name)
            if value is None:
                ns.deleted_keys.add(slot_name)
                ns.dirty_rows.pop(slot_name, None)
                continue

            try:
                # No ``default=`` fallback by design: a non-JSON payload
                # surfaces as TypeError so the slot write is declined and
                # logged, not silently coerced into a string the rehydrate
                # path cannot consume. Matches the _capture_registry_row
                # contract below.
                serialized = json.dumps(value)
            except (TypeError, ValueError) as exc:
                logger.error(f"Application slot {slot_name!r} is not JSON-serializable: {exc}")
                continue

            policy = self._manager.get_slot_policy(slot_name)
            expires_at: Optional[int] = None
            if policy.ttl_days is not None:
                expires_at = now + (policy.ttl_days * 86400)

            ns.dirty_rows[slot_name] = {
                "slot_name": slot_name,
                "payload": serialized,
                "schema_version": 1,
                "updated_at": now,
                "expires_at": expires_at,
            }
            ns.deleted_keys.discard(slot_name)

        self._schedule(ns)

    # // ========================================( Scheduler )======================================== // #

    def _schedule(self, ns: _NamespaceState) -> None:
        """Schedule or reschedule a debounced flush for ``ns``."""
        now = time.monotonic()
        if ns.first_dirty_at is None:
            ns.first_dirty_at = now
        ns.last_action_at = now

        # Immediate flush: registry lifecycle events. Fire one task per
        # call without cancelling prior tasks so a burst of register +
        # unregister does not coalesce into a lost write.
        if ns.interval <= 0.0:
            self._spawn(self._run_flush(ns, wait=0.0))
            return

        # Debounced flush: cancel and reschedule. The dirty-row buffer
        # carries state across cancels, so accumulated writes flush
        # together when the timer finally fires.
        if ns.task is not None and not ns.task.done():
            ns.task.cancel()

        # Window selection: smaller of (idle interval) and
        # (max_age ceiling minus already-elapsed age). The ceiling keeps
        # steady traffic from starving writes indefinitely.
        age = now - ns.first_dirty_at
        wait_idle = ns.interval
        wait_ceiling = max(0.0, ns.max_age - age)
        wait = min(wait_idle, wait_ceiling)

        ns.task = self._spawn(self._run_flush(ns, wait=wait))

    def _spawn(self, coro: "asyncio.coroutines.Coroutine") -> asyncio.Task:
        """Create an asyncio.Task directly and track it on ``self._tasks``.

        Bypasses ``TaskManager`` so cancellation before the first step
        still reaches the inner coroutine -- see the module header for
        the reasoning.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run_flush(self, ns: _NamespaceState, wait: float) -> None:
        """Sleep then flush. Cancellation during sleep is a no-op."""
        try:
            if wait > 0:
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return

        async with ns.write_lock:
            await self._flush(ns)

    async def _flush(self, ns: _NamespaceState) -> None:
        """Drain ``ns.dirty_rows`` and ``ns.deleted_keys`` to the backend."""
        if ns.backend is None:
            return
        if not ns.dirty_rows and not ns.deleted_keys:
            return

        # Snapshot under the write lock so retries or subsequent
        # scheduled flushes do not see half-drained state.
        rows = list(ns.dirty_rows.values())
        deletes = list(ns.deleted_keys)
        ns.dirty_rows.clear()
        ns.deleted_keys.clear()
        ns.first_dirty_at = None

        table, key_columns, delete_column = self._namespace_tables(ns.name)

        try:
            for row in rows:
                await ns.backend.row_upsert(table, row, key_columns)
            for key in deletes:
                await ns.backend.row_delete(table, {delete_column: key})
        except Exception as exc:
            ns.retry_count += 1
            logger.error(
                f"Persistence flush failed for {ns.name!r} "
                f"(retry {ns.retry_count}/{self.MAX_RETRIES}): {exc}"
            )
            await self._fire_hook("on_error", ns.name, exc)

            # Re-enqueue so the next scheduled flush retries. setdefault
            # preserves any newer row that arrived between snapshot and
            # exception (unlikely under the write lock, but cheap to
            # guard).
            for row in rows:
                key = row[key_columns[0]]
                ns.dirty_rows.setdefault(key, row)
            ns.deleted_keys.update(deletes)

            if ns.retry_count >= self.MAX_RETRIES:
                logger.critical(
                    f"Persistence namespace {ns.name!r} failed "
                    f"{self.MAX_RETRIES} consecutive flushes; pausing "
                    "retries. Dirty rows remain in memory and will flush "
                    "on the next dispatch."
                )
                ns.retry_count = 0
                return

            backoff = min(
                self._BACKOFF_CAP,
                self._BACKOFF_BASE * (2 ** (ns.retry_count - 1)),
            )
            ns.task = self._spawn(self._run_flush(ns, wait=backoff))
            return

        ns.retry_count = 0
        await self._fire_hook("on_flush", ns.name, len(rows), len(deletes))

    # // ========================================( Helpers )======================================== // #

    def _namespace_tables(self, name: str) -> tuple[str, list[str], str]:
        """Return ``(table, key_columns, delete_column)`` for ``name``."""
        if name == "registry":
            return TABLE_PERSISTENT_VIEWS, ["persistence_key"], "persistence_key"
        if name == "application":
            return TABLE_APPLICATION_SLOTS, ["slot_name"], "slot_name"
        raise ValueError(f"unknown namespace: {name!r}")

    def _find_view_by_persistence_key(self, persistence_key: str) -> Optional[Any]:
        """Return the first active view whose ``_persistence_key`` matches."""
        for view in self._store._active_views.values():
            if getattr(view, "_persistence_key", None) == persistence_key:
                return view
        return None

    def _build_registry_row(self, payload: dict, view: Optional[Any]) -> Optional[dict[str, Any]]:
        """Assemble a registry row from the action payload and live view.

        Missing live view is logged and returns ``None`` -- the view
        exited between dispatch and middleware, so there is no init
        kwargs snapshot to persist. Rare enough in practice that
        declining the write is safer than inventing a stub row.
        """
        persistence_key = payload.get("persistence_key")
        if view is None:
            logger.warning(
                f"No live view found for persistence_key {persistence_key!r}; "
                "skipping registry persist"
            )
            return None

        # Drop kwargs that the registry surfaces via dedicated columns
        # (``persistence_key``) or that are never safely round-tripped
        # through JSON (``theme``). Without this filter, a live ``Theme``
        # would silently stringify to ``"<Theme object at 0x...>"`` (the
        # old ``default=str`` fallback) and corrupt the row; reattach
        # would then either raise or build a view with the wrong theme.
        init_kwargs = {
            k: v
            for k, v in getattr(view, "_init_kwargs", {}).items()
            if k not in _NON_PERSISTABLE_KWARGS
        }
        try:
            # No ``default=`` fallback by design: any non-JSON kwarg
            # surfaces as a TypeError here so the row is declined and
            # logged, not silently coerced into a string the reattach
            # path cannot consume.
            init_kwargs_json = json.dumps(init_kwargs)
        except (TypeError, ValueError) as exc:
            logger.error(f"init_kwargs for {persistence_key!r} not JSON-serializable: {exc}")
            return None

        kwargs_version = int(getattr(view, "kwargs_schema_version", 1))
        now = int(time.time())

        return {
            "persistence_key": persistence_key,
            "view_class": payload.get("class_name"),
            "custom_id": None,
            "message_id": int(payload["message_id"]),
            "channel_id": int(payload["channel_id"]),
            "guild_id": int(payload["guild_id"]) if payload.get("guild_id") else None,
            "user_id": int(payload["user_id"]) if payload.get("user_id") else None,
            "session_id": getattr(view, "session_id", None),
            "init_kwargs": init_kwargs_json,
            "kwargs_schema_version": kwargs_version,
            "schema_version": 1,
            "created_at": now,
            "updated_at": now,
        }

    async def _fire_hook(self, hook_name: str, *args) -> None:
        """Dispatch a persistence observability hook registered on the manager.

        Hooks are stored as ``manager._hooks[hook_name] = [callbacks]``.
        Errors inside a hook are logged and swallowed so one misbehaving
        observer cannot break the flush pipeline.
        """
        hooks: Optional[dict] = getattr(self._manager, "_hooks", None)
        if not hooks:
            return
        for callback in hooks.get(hook_name, ()):
            try:
                result = callback(*args)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error(f"Persistence hook {hook_name!r} raised: {exc}")

    # // ========================================( Shutdown )======================================== // #

    async def flush_all(self) -> None:
        """Cancel pending tasks and flush every namespace synchronously.

        Called by :meth:`~cascadeui.persistence.manager.PersistenceManager.close`
        during bot shutdown. Each namespace flushes under its write
        lock so no partial writes slip past ``close``.

        Cancelled tasks are gathered with exceptions suppressed so
        cancellation unwinds fully before backend close. Because the
        tasks own their coroutines directly (see ``_spawn``), asyncio's
        own driver closes the coroutine on cancel -- no orphaned
        "coroutine was never awaited" warnings at shutdown.
        """
        to_cancel = [t for t in self._tasks if not t.done()]
        for task in to_cancel:
            task.cancel()
        if to_cancel:
            await asyncio.gather(*to_cancel, return_exceptions=True)

        # Skip namespaces that were never built -- the direct
        # construction path leaves both None until :meth:`initialize`
        # runs, so a shutdown path that fires before startup completes
        # (failed boot, test teardown before install) must not crash
        # on ``None.write_lock``.
        for ns in (self._ns_registry, self._ns_application):
            if ns is None:
                continue
            async with ns.write_lock:
                await self._flush(ns)

    async def close(self) -> None:
        """Stop accepting new writes and drain outstanding flushes."""
        self._closed = True
        await self.flush_all()
