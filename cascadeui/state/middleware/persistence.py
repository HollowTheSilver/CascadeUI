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
- **Opt-in filter at the dispatch seam.** Only slots declared persistent
  reach the backend, by whichever route
  :func:`~cascadeui.state.slots.is_persistent_slot` documents. Everything
  else is skipped at routing time and the middleware never schedules a
  task for it. This mirrors the opt-in model used by ``PersistentView``.
- **Direct task ownership.** Flush tasks are created via
  ``asyncio.create_task`` and tracked on the middleware itself. Cancel
  unwinds through asyncio's own coroutine driver so shutdown leaves no
  orphaned coroutines.
- **Exponential backoff retry.** Failed flushes re-enqueue dirty rows
  and schedule a retry with backoff capped at 60s. A write still running
  after 30 seconds fails the same way. After ``MAX_RETRIES`` consecutive
  failures the namespace logs CRITICAL and resets its counter; the rows
  remain dirty so the next action retries.
- **Observability hooks.** The middleware fires ``on_flush`` and
  ``on_error`` on the manager so devtools or operator tooling can
  observe write cadence without scraping logs.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import json
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from ...persistence.manager import (
    _NON_PERSISTABLE_KWARGS,
    _validate_prune_unreachable_after_days,
)
from ...persistence.protocols import PersistenceBackend
from ...persistence.schema import (
    CURRENT_SCHEMA_VERSIONS,
    TABLE_APPLICATION_SLOTS,
    TABLE_PERSISTENT_VIEWS,
)
from ...utils.hooks import await_maybe
from ...utils.tasks import _bounded_wait
from ..store import _ON_COMMIT
from ..types import Action, StateData

if TYPE_CHECKING:
    from ...persistence.config import ApplicationPersistence, RegistryPersistence
    from ...persistence.manager import PersistenceManager

logger = logging.getLogger(__name__)


# // ========================================( Constants )======================================== // #


# Built-in actions with nothing to save: views and sessions rebuild from registry
# rows, navigation is ephemeral, INSPECTOR_PURGED_STALE clears transient logs, and
# the *_PRUNED actions report deletes already made. Left out: UNDO, REDO and
# SCOPED_UPDATE change slots, and the persistent-view actions write the registry.
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
        "BATCH_COMPLETE",
        "INSPECTOR_PURGED_STALE",
        "APPLICATION_SLOTS_PRUNED",
        "REGISTRY_PRUNED",
    }
)


# Flush tasks live on ``PersistenceMiddleware._tasks`` via ``_spawn``, not
# ``TaskManager``: their debounce, retry, and cancel-on-close are local here.


_CONTAINERS = (dict, list, tuple)


def _entries(container: Any) -> Any:
    """An iterator of a dict's ``(key, item)`` pairs, or a list's ``(index, item)`` pairs."""
    return iter(container.items()) if isinstance(container, dict) else iter(enumerate(container))


def _non_string_key(value: Any) -> Optional[tuple]:
    """The first dict key in ``value`` that is not a ``str``, with its path and dict, or ``None``.

    Walked with a stack: from Python 3.12 ``json.dumps`` writes values nested
    past the recursion limit, and a recursive walk would raise on them.
    """
    if not isinstance(value, _CONTAINERS):
        return None
    # A frame per container: its entry iterator, the container, and the trail
    # of keys and indexes that reached it, as (parent trail, step) pairs.
    stack = [(_entries(value), value, None)]
    while stack:
        entries, container, trail = stack[-1]
        keyed = isinstance(container, dict)
        for step, item in entries:
            if keyed and not isinstance(step, str):
                steps = []
                while trail is not None:
                    trail, part = trail
                    steps.append(f"[{part!r}]")
                return step, "".join(reversed(steps)), container
            if isinstance(item, _CONTAINERS):
                stack.append((_entries(item), item, (trail, step)))
                break
        else:
            stack.pop()
    return None


def _key_coercion_note(value: Any, root: str) -> Optional[str]:
    """Describe the first non-``str`` dict key in ``value``, or ``None`` when there is none.

    Written, the value still round-trips, but JSON stores every key as a
    string, so that key comes back changed after a restart.
    """
    found = _non_string_key(value)
    if found is None:
        return None
    key, path, container = found
    stored = next(iter(json.loads(json.dumps({key: None}))))
    if stored in container:
        return (
            f"has the keys {key!r} and {stored!r} at {root}{path}. JSON stores keys as "
            f"strings, so both are saved as {stored!r} and a restart keeps only one of "
            f"their values. Fix: use string keys, e.g. str(user_id)."
        )
    return (
        f"has the key {key!r} at {root}{path}. JSON stores keys as strings, so after a "
        f"restart it reads back as {stored!r} and a lookup by {key!r} misses. Fix: use "
        f"string keys, e.g. str(user_id)."
    )


async def _close_to_the_end(manager: "PersistenceManager") -> None:
    """Close ``manager`` even when the task running this is cancelled.

    A close started by another task (a shutdown command) is still running when
    the task that owns ``async with bot`` returns, and ``asyncio.run`` then
    cancels it. That teardown waits for the cancelled task, so one more attempt
    writes the batch and closes the backends, whose SQLite worker thread would
    otherwise keep the process alive. The cancel is re-raised afterwards.
    """
    try:
        await manager.close()
    except asyncio.CancelledError:
        try:
            await manager.close()
        except Exception as exc:
            logger.error(f"Persistence did not close with the bot: {exc}", exc_info=True)
        raise
    except Exception as exc:
        logger.error(f"Persistence did not close with the bot: {exc}", exc_info=True)


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
    # The flush past its wait and not yet holding ``write_lock``: it has not
    # taken its snapshot, so it writes every change routed before it does.
    queued: Optional[asyncio.Task] = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retry_count: int = 0
    # When the pending retry of a failed write runs. A change before then
    # waits for it rather than writing to a backend that just failed.
    retry_at: Optional[float] = None
    # Slot values queued since the last write, checked for keys JSON stores
    # as strings when that write takes them.
    unchecked: dict[str, Any] = field(default_factory=dict)
    # The stored form of each slot as last loaded or queued: the manager's own
    # record, so the slots rehydrate loads count as already stored.
    saved: dict[str, str] = field(default_factory=dict)
    # The expires_at of each slot with a TTL, also the manager's record, which
    # the expiry sweep reads to drop expired slots from memory.
    expiry: dict[str, int] = field(default_factory=dict)


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
    # A write still running after this long is cancelled and retried. A new
    # change does not cancel a flush past its wait, so without a bound a
    # stalled write would hold every later one behind it.
    _WRITE_TIMEOUT: float = 30.0

    def __init__(
        self,
        manager: Optional["PersistenceManager"] = None,
        *,
        backend: Optional[PersistenceBackend] = None,
        registry: Optional["RegistryPersistence"] = None,
        application: Optional["ApplicationPersistence"] = None,
        bot: Any = None,
        migrators: Optional[dict] = None,
        restore_concurrency: int = 8,
        prune_unreachable_after_days: Optional[int] = None,
    ) -> None:
        # Validated here as well as in the manager, so the kwargs path fails at
        # construction rather than at initialize(). With manager= this, like
        # every pipeline kwarg, is ignored: set it on the manager instead.
        if manager is None:
            _validate_prune_unreachable_after_days(prune_unreachable_after_days, bot)

        # Validate the restore-concurrency knob at the construction site so
        # a bad value fails here, not deep inside the reattach fan-out.
        if (
            not isinstance(restore_concurrency, int)
            or isinstance(restore_concurrency, bool)
            or restore_concurrency < 1
        ):
            raise ValueError(
                "PersistenceMiddleware restore_concurrency= must be a positive int, "
                f"got {restore_concurrency!r}."
            )

        # Registered during initialize(), before the migrations and the restore
        # read it, so a malformed map is refused here, at the construction site.
        if migrators is not None:
            if not isinstance(migrators, dict):
                raise TypeError(
                    "PersistenceMiddleware migrators= must be None or a dict with "
                    f"optional 'schema' / 'kwargs' keys, got {type(migrators).__name__!r}."
                )
            unknown = set(migrators) - {"schema", "kwargs"}
            if unknown:
                raise ValueError(
                    f"PersistenceMiddleware migrators= has unknown keys {sorted(unknown)}; "
                    "expected 'schema' and/or 'kwargs'."
                )

        # A non-Client bot would otherwise fail deep inside initialize(), long
        # after this call.
        if bot is not None:
            import discord

            if not isinstance(bot, discord.Client):
                raise TypeError(
                    "PersistenceMiddleware bot= must be a discord.py Bot "
                    f"(discord.Client subclass) or None, got {type(bot).__name__!r}."
                )

        # ``manager=`` takes a pre-built manager; otherwise the config waits for
        # :meth:`initialize`, where the store is available.
        self._pending_config: Optional[dict[str, Any]]
        if manager is not None:
            self._manager = manager
            self._store = manager._store
            self._pending_config = None
            # The caller is expected to have run a pre-built manager's pipeline,
            # so :meth:`initialize` must not rehydrate over a live store.
            self._initialized: bool = True
            # initialize() short-circuits on this path, so the back-reference
            # it normally sets has to be wired here or flush_all() and close()
            # stay permanent no-ops for a pre-built manager.
            manager._middleware = self
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
                "restore_concurrency": restore_concurrency,
                "prune_unreachable_after_days": prune_unreachable_after_days,
            }
            self._initialized = False
            # Namespace state is built after the manager resolves in
            # :meth:`initialize`; leave as None until then.
            self._ns_registry = None  # type: ignore[assignment]
            self._ns_application = None  # type: ignore[assignment]

        self._closed: bool = False
        self._warned_while_closed: bool = False
        # Each key note is logged once: a slot written on every change would
        # otherwise log the same ERROR each time.
        self._logged_key_notes: set = set()
        # Tasks are owned here so cancel-before-start unwinds through
        # asyncio.Task's own coroutine driver (which closes the coro
        # cleanly) rather than orphaning a never-awaited coroutine.
        self._tasks: set[asyncio.Task] = set()
        # The SIGTERM disposition installed by _close_on_sigterm, and the bot
        # close a SIGTERM started. Not in _tasks: the close cancels those.
        self._sigterm_disposition = None
        self._sigterm_close: Optional[asyncio.Task] = None
        # One future per wrapped bot close still running, resolved once
        # persistence has closed after it (see _close_with_bot).
        self._bot_closes: set = set()

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
            saved=manager._slot_payloads,
            expiry=manager._slot_expiry,
        )

    # // ========================================( Initialize )======================================== // #

    async def initialize(self, store: Any) -> None:
        """Run the async startup pipeline for this middleware.

        Invoked by :func:`~cascadeui.setup.setup_middleware` after the
        middleware is installed into the dispatch chain. The pipeline runs
        once. A later call reopens persistence if it has closed (a bot
        started again in the same process), writes the changes held while
        it was closed, restarts the sweepers, and closes persistence with
        the bot again.

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
        7. Reattach persistent views and register dynamic items (when
           ``bot`` is available).
        """
        if self._initialized:
            # A manager= install ran its own pipeline, but the sweepers are
            # the middleware's to start.
            if self._manager is not None:
                # A bot closed and started again in the same process runs
                # setup_hook again, after its close shut persistence.
                await self._reopen_with_bot(store)
            return

        cfg = self._pending_config or {}
        manager = self._resolve_manager(store, cfg)
        self._manager = manager
        self._store = store
        # The back-reference is what makes manager.flush_all() and
        # manager.close() reach the debounce buffers. Without it both are
        # permanent no-ops, and the operator-facing flush surfaces that
        # call them report a write that never happened.
        manager._middleware = self
        self._build_namespaces(manager)
        # Before the backends open: a migration or rehydrate that raises leaves
        # them open, and the bot's close is what shuts them.
        self._close_with_bot()

        # Register any caller-supplied migrators into the module registries
        # before apply_migrations (schema) and rehydrate (kwargs) consume them.
        self._register_migrators(cfg.get("migrators"))

        await manager.initialize_backends()
        await manager.apply_migrations()
        await manager.rehydrate()

        bot = cfg.get("bot")
        # Restored views skip send() so they never trigger the lazy
        # listener installation path in _StatefulMixin. Install it
        # eagerly here so on_message_delete fires for externally-
        # deleted persistent messages.
        if bot is not None:
            store._install_message_cleanup(bot)

        # Stash on the store so later code (prune, slot-policy
        # registration, shutdown) can look the manager up without
        # threading it through every call site.
        store.persistence_manager = manager

        # Start the daily TTL sweeper when any persistent slot declares
        # ttl_days, and the unreachable-row sweep when it is configured. Each
        # is skipped when there is nothing for it to do.
        manager._start_sweepers()

        # Routing is live before the restore: a restored panel's on_bind() can
        # write a persistent slot, and that write would otherwise never be
        # saved.
        self._initialized = True

        if bot is not None:
            # The reattach pass also registers every DynamicPersistentButton
            # subclass with the bot, so dispatch-time click routing needs no
            # separate wiring here.
            await manager.reattach_persistent_views()
        else:
            # Warn loudly when the bot is absent but PersistentView
            # subclasses are registered. Without a bot, those views
            # will not be reattached on restart. Lazy import avoids
            # a cycle with views/persistent.py.
            from ...components.base import _dynamic_button_classes
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

            if _dynamic_button_classes:
                logger.warning(
                    f"PersistenceMiddleware initialized without bot=, "
                    f"but {len(_dynamic_button_classes)} "
                    f"DynamicPersistentButton subclass(es) are registered "
                    f"({', '.join(sorted(_dynamic_button_classes))}). "
                    "Without a bot, these buttons will NOT route clicks "
                    "on restart. Pass the bot instance to enable "
                    "dispatch-time click routing."
                )

        # Last: a close while the backends are still opening would find nothing
        # to close, and the backend would then open with nothing left to close
        # it.
        self._close_on_sigterm()

    async def _reopen_with_bot(self, store: Any) -> None:
        """Open persistence again after the bot's close shut it, and restore its panels.

        The persistent panels a closed bot's views held are reattached through
        the bot now running, as a process restart reattaches them at boot.
        Reached from a ``setup_middleware()`` run again (the ``setup_hook`` of
        the bot a restart in the same process builds), from the bot's
        ``connect()`` when it runs again after the bot's close, and from a
        close that finds a restart began while it ran.

        Nothing reopens while a close of the bot is still running, or once
        the bot is closed: a close that began first (SIGTERM while
        ``setup_hook`` runs, a shutdown during a reconnect's backoff) would
        leave it open with nothing to close it. A close still running from
        before a restart reopens persistence itself when it finishes and
        finds the bot running again (see :meth:`_close_with_bot`). Waiting
        for it here instead would hang a close that waits for the bot to be
        ready, which happens only after this returns. After a close a SIGTERM
        started, the process ends instead of reopening.
        """
        # Wired first: a new bot object adopted while an old close still runs
        # has to close persistence with it, and connect() through it.
        self._close_with_bot()
        if any(not f.done() for f in self._bot_closes):
            return
        bot = getattr(self._manager, "_bot", None)
        if bot is not None and bot.is_closed():
            return
        # A SIGTERM this handled asked the process to stop, and the default is
        # back (no handler of the application's since): a restart would run
        # until the process manager kills it.
        if self._sigterm_close is not None and signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            await self._end_after_sigterm(bot)
            return
        reopened = await self._open_again()
        store.persistence_manager = self._manager
        self._manager._start_sweepers()
        self._close_on_sigterm()
        # The panels a closed bot left come back here, in the new bot's
        # setup_hook, as a process restart restores them at boot. A bot
        # adopted while the old one closed leaves persistence open.
        if bot is not None and (reopened or store._restore_owed):
            store._restore_owed = False
            await self._manager.reattach()
            store._link_released()

    async def _open_again(self) -> bool:
        """Open persistence a close shut. Returns whether it had closed."""
        reopened = await self._manager._reopen()
        if reopened or self._closed:
            reopened = True
            self._closed = False
            self._warned_while_closed = False
            for ns in (self._ns_registry, self._ns_application):
                # Nothing holds it while closed, and a bot run twice
                # through asyncio.run reopens on another loop.
                ns.write_lock = asyncio.Lock()
                if ns.dirty_rows or ns.deleted_keys:
                    self._schedule(ns)
        return reopened

    async def _end_after_sigterm(self, bot: Any) -> None:
        """End the process when a bot a SIGTERM closed is started again.

        The close the SIGTERM ran wrote the batch. A change held since then is
        written next, and SIGTERM's default action then ends the process, as
        it would have without the close.
        """
        logger.info(f"{type(bot).__name__} started again after SIGTERM; ending the process")
        try:
            namespaces = (self._ns_registry, self._ns_application)
            if any(ns.dirty_rows or ns.deleted_keys for ns in namespaces):
                await self._open_again()
                await _close_to_the_end(self._manager)
        except Exception as exc:
            logger.error(f"Changes held since SIGTERM were not written: {exc}", exc_info=True)
        finally:
            signal.raise_signal(signal.SIGTERM)

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
                    "Install it with: pip install 'pycascadeui[sqlite]' "
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
            restore_concurrency=cfg.get("restore_concurrency", 8),
            prune_unreachable_after_days=cfg.get("prune_unreachable_after_days"),
        )

    @staticmethod
    def _register_migrators(migrators: Optional[dict]) -> None:
        """Register a caller-supplied migrator map into the module registries.

        ``migrators`` is the validated dict from construction: optional
        ``"schema"`` and ``"kwargs"`` keys, each mapping a
        ``(name, from_version)`` tuple to an async migrator callable. Schema
        entries land in ``_MIGRATORS`` (keyed by ``(table, from_version)``,
        consumed by ``apply_migrations``); kwargs entries land in
        ``_KWARGS_MIGRATORS`` (keyed by ``(view_class_qualname,
        from_version)``, consumed during rehydrate).

        Idempotent: a key already registered (via this path, the decorator,
        or a prior construction) is skipped, so re-constructing the
        middleware does not raise. New migrators still register.
        """
        if not migrators:
            return
        from ...persistence.migrations import (
            get_kwargs_migrator,
            get_schema_migrator,
            register_kwargs_migrator,
            register_migrator,
        )

        # Skip-if-present, so re-constructing the middleware never raises. A real
        # collision (the library ships its own migrator) keeps the registered one
        # and warns, where the decorator path raises.
        for (table, from_version), fn in (migrators.get("schema") or {}).items():
            if get_schema_migrator(table, from_version) is not None:
                logger.warning(
                    f"A schema migrator for {table} v{from_version} is already "
                    f"registered; keeping it and ignoring the one passed via "
                    f"migrators=. Rename the step or drop the duplicate."
                )
                continue
            register_migrator(table, from_version)(fn)
        for (qualname, from_version), fn in (migrators.get("kwargs") or {}).items():
            if get_kwargs_migrator(qualname, from_version) is not None:
                logger.warning(
                    f"A kwargs migrator for {qualname} v{from_version} is already "
                    f"registered; keeping it and ignoring the one passed via "
                    f"migrators=."
                )
                continue
            register_kwargs_migrator(qualname, from_version)(fn)

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

        on_commit = _ON_COMMIT.get()
        if on_commit is None:
            # Called outside a dispatch: the state before and after the call.
            state_before = self._store.state
            result = await next_fn(action, state)
            self._route_commit(action, state_before, self._store.state)
            return result

        # Queued at the commit, from exactly what the reducer changed: not another
        # dispatch's commit during the chain, not skipped by a later raise, and
        # nothing for an action no reducer ran.
        def route(prior: StateData, committed: StateData) -> None:
            self._route_commit(action, prior, committed)

        on_commit.append(route)
        try:
            return await next_fn(action, state)
        finally:
            on_commit.remove(route)

    def _route_commit(
        self, action: Action, state_before: StateData, state_after: StateData
    ) -> None:
        """Queue the writes the change from ``state_before`` to ``state_after`` calls for."""
        action_type = action["type"]
        if action_type in _BOOKKEEPING_ACTIONS:
            return
        if state_after is state_before:
            return

        # A closed middleware still routes: the rows are held, unscheduled,
        # until a reopen writes them.
        if action_type in ("PERSISTENT_VIEW_REGISTERED", "PERSISTENT_VIEW_UNREGISTERED"):
            queued = self._route_registry(action, state_before, state_after)
            if queued and self._closed:
                key = action["payload"].get("persistence_key")
                self._warn_held(f"the {action_type} for {key!r}")
        else:
            queued = self._route_application(state_before, state_after)
            if queued and self._closed:
                self._warn_held(f"a change to {', '.join(repr(n) for n in sorted(queued))}")

    # // ========================================( Routing )======================================== // #

    def _route_registry(self, action: Action, before: StateData, after: StateData) -> bool:
        """Queue the registry write an action calls for; ``True`` when one was queued."""
        ns = self._ns_registry
        if ns.backend is None:
            return False
        payload = action["payload"]
        persistence_key = payload.get("persistence_key")
        if not persistence_key:
            return False

        if action["type"] == "PERSISTENT_VIEW_UNREGISTERED":
            # The removal the reducer made, not state identity: a middleware that
            # rebuilds the mapping hides a refusal, and deleting on one would take
            # the live successor's row.
            removed = persistence_key in before.get(
                "persistent_views", {}
            ) and persistence_key not in after.get("persistent_views", {})
            if not removed:
                return False
            ns.deleted_keys.add(persistence_key)
            ns.dirty_rows.pop(persistence_key, None)
            if self._manager is not None:
                self._manager._forget_registry_row(persistence_key)
        else:
            # The registering view supplies the kwargs. It stamped the message id
            # just recorded, so the lookup picks it over a superseded panel.
            view = self._store.get_active_view(persistence_key=persistence_key)
            row = self._build_registry_row(payload, view)
            if row is None:
                return False
            ns.dirty_rows[persistence_key] = row
            ns.deleted_keys.discard(persistence_key)
            if self._manager is not None:
                self._manager._remember_registry_row(persistence_key, row)

        self._schedule(ns)
        return True

    @staticmethod
    def _changed_persistent_slots(state_before: StateData, state_after: StateData) -> set[str]:
        # Only slots registered persistent are scanned; the rest stay in memory,
        # so the walk is bounded by how many slots opted in.
        from ..slots import _PERSISTENT_SLOTS

        app_before = state_before.get("application") or {}
        app_after = state_after.get("application") or {}
        return {
            slot_name
            for slot_name in _PERSISTENT_SLOTS
            if app_before.get(slot_name) is not app_after.get(slot_name)
        }

    def _warn_held(self, what: str) -> None:
        """Say once per closure that a change reached a closed persistence."""
        if self._warned_while_closed:
            return
        self._warned_while_closed = True
        logger.warning(
            f"Persistence is closed, so {what} is held in memory until setup_middleware() "
            "reopens it, and is lost if the process exits first. Persistence closes when "
            "the bot closes or when persistence_manager.close() runs."
        )

    def _first_key_note(self, note: str) -> bool:
        """Whether ``note`` is being logged for the first time by this middleware."""
        if note in self._logged_key_notes:
            return False
        self._logged_key_notes.add(note)
        return True

    def _route_application(self, state_before: StateData, state_after: StateData) -> list:
        """Queue the slot writes a change calls for; the names of the slots queued."""
        ns = self._ns_application
        if ns.backend is None:
            return []

        changed = self._changed_persistent_slots(state_before, state_after)
        if not changed:
            return []

        app_after = state_after.get("application") or {}
        now = int(time.time())
        queued = []
        for slot_name in changed:
            value = app_after.get(slot_name)
            if value is None:
                ns.deleted_keys.add(slot_name)
                ns.dirty_rows.pop(slot_name, None)
                ns.saved.pop(slot_name, None)
                ns.expiry.pop(slot_name, None)
                queued.append(slot_name)
                continue

            try:
                # No ``default=`` fallback: a non-JSON value is declined and
                # logged, not coerced into a string rehydrate cannot read, as in
                # ``_build_registry_row``.
                serialized = json.dumps(value)
            except (TypeError, ValueError, RecursionError) as exc:
                logger.error(
                    f"Application slot {slot_name!r} was not saved: {exc}. Store plain "
                    f"values, e.g. a member's .id rather than the Member."
                )
                continue
            # A @cascade_reducer copies every slot, so most slots arrive here as
            # new objects holding what is already stored. Writing one again
            # would also push its ttl_days expiry back.
            if ns.saved.get(slot_name) == serialized:
                continue
            ns.saved[slot_name] = serialized
            ns.unchecked[slot_name] = value

            policy = self._manager.get_slot_policy(slot_name)
            expires_at: Optional[int] = None
            if policy.ttl_days is not None:
                expires_at = now + (policy.ttl_days * 86400)
                ns.expiry[slot_name] = expires_at
            else:
                ns.expiry.pop(slot_name, None)

            ns.dirty_rows[slot_name] = {
                "slot_name": slot_name,
                "payload": serialized,
                "schema_version": CURRENT_SCHEMA_VERSIONS[TABLE_APPLICATION_SLOTS],
                "updated_at": now,
                "expires_at": expires_at,
            }
            ns.deleted_keys.discard(slot_name)
            queued.append(slot_name)

        # Rows a failed write left behind go with this change's flush even when
        # it queued nothing: a retry that ran out schedules no flush of its own.
        if queued or ns.dirty_rows or ns.deleted_keys:
            self._schedule(ns)
        return queued

    # // ========================================( Scheduler )======================================== // #

    def _schedule(self, ns: _NamespaceState) -> None:
        """Schedule or reschedule a debounced flush for ``ns``."""
        if self._closed:
            # The backend is closed or closing; a reopen schedules these rows.
            return
        now = time.monotonic()
        if ns.first_dirty_at is None:
            ns.first_dirty_at = now
        ns.last_action_at = now

        # Immediate flush: registry lifecycle events. Fire one task per
        # call without cancelling prior tasks so a burst of register +
        # unregister does not coalesce into a lost write.
        retrying = ns.retry_at is not None and ns.task is not None and not ns.task.done()
        if ns.interval <= 0.0:
            # The pending retry writes this change with the rows it holds.
            if not retrying:
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
        # The flush replaces a pending retry, so it keeps the retry's backoff;
        # otherwise steady traffic would retry a failing backend every window.
        if retrying:
            wait = max(wait, ns.retry_at - now)

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

        # Past its wait the flush is no longer the one a new change replaces:
        # cancelling it mid-write under steady traffic would mean no write
        # ever finishes. The next change schedules its own behind the lock.
        current = asyncio.current_task()
        if ns.task is current:
            ns.task = None
        # One flush waits for the lock at a time. A second would find the
        # buffer already drained, and during a slow write every change past
        # the max-age ceiling would queue another.
        if ns.queued is not None and not ns.queued.done():
            return
        ns.queued = current
        async with ns.write_lock:
            if ns.queued is current:
                ns.queued = None
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

        # Once per write, not per commit: the walk covers the whole slot, and one
        # scoped slot holds every user's data. Before the first await, so a cancel
        # cannot skip it, and only while the value still serializes to the written
        # payload, since it can change in place into a cycle or an unencodable key.
        unchecked, ns.unchecked = ns.unchecked, {}
        payloads = {row["slot_name"]: row["payload"] for row in rows} if unchecked else {}
        for slot_name, value in unchecked.items():
            try:
                written = json.dumps(value) == payloads.get(slot_name)
            except (TypeError, ValueError, RecursionError):
                written = False
            if not written:
                continue
            note = _key_coercion_note(value, slot_name)
            if note is not None and self._first_key_note(f"{slot_name}: {note}"):
                # Still written: refusing would drop every other entry in the
                # slot too, other users' included in a shared one.
                logger.error(f"Application slot {slot_name!r} {note}")

        table, key_columns, delete_column = self._namespace_tables(ns.name)

        def requeue() -> None:
            """Return the snapshot to the buffers it was drained from.

            Restores the single-buffer-per-key invariant the routing
            helpers maintain (a key lives in ``dirty_rows`` OR
            ``deleted_keys``, never both). A re-register or unregister
            that arrived while the write was in flight may have claimed a
            key for the opposite buffer, so each side skips a key the
            other side now owns: the snapshot never resurrects a key in
            the buffer it left, and the newer write survives the next
            flush. That guard is what keeps a re-registered persistent
            view reattaching after restart.
            """
            for row in rows:
                key = row[key_columns[0]]
                if key not in ns.deleted_keys:
                    ns.dirty_rows.setdefault(key, row)
            for key in deletes:
                if key not in ns.dirty_rows:
                    ns.deleted_keys.add(key)

        async def write() -> None:
            if rows:
                # Prefer the batched path (one round-trip) when the backend
                # implements it; fall back to per-row upsert so a custom
                # backend without row_upsert_many keeps working.
                upsert_many = getattr(ns.backend, "row_upsert_many", None)
                if upsert_many is not None:
                    await upsert_many(table, rows, key_columns)
                else:
                    for row in rows:
                        await ns.backend.row_upsert(table, row, key_columns)
            for key in deletes:
                await ns.backend.row_delete(table, {delete_column: key})

        try:
            await _bounded_wait(write(), self._WRITE_TIMEOUT)
        except asyncio.CancelledError:
            # A cancel bypasses the retry path below, and ``flush_all`` cancels
            # in-flight flushes before its final drain, so the drained batch goes
            # back for the next flush instead of being lost at shutdown.
            requeue()
            raise
        except Exception as exc:
            ns.retry_count += 1
            detail = str(exc) or (
                "timed out" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__
            )
            logger.error(
                f"Persistence flush failed for {ns.name!r} "
                f"(retry {ns.retry_count}/{self.MAX_RETRIES}): {detail}"
            )
            # Re-enqueue before the hook, so a cancel landing in it cannot
            # drop the rows this flush took out of the buffer.
            requeue()
            await self._fire_hook("on_error", ns.name, exc)

            # The final flush of a close: a retry would run against the
            # backend the close is about to shut. The rows wait for a reopen.
            if self._closed:
                return

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
            # A change during the write scheduled a flush. The retry replaces
            # it: left running, it would run beside the retry out of reach.
            if ns.task is not None and not ns.task.done():
                ns.task.cancel()
            ns.retry_at = time.monotonic() + backoff
            ns.task = self._spawn(self._run_flush(ns, wait=backoff))
            return

        ns.retry_count = 0
        # A flush queued behind the failed one can land while the retry still
        # sleeps; changes after it no longer wait for that retry.
        ns.retry_at = None
        await self._fire_hook("on_flush", ns.name, len(rows), len(deletes))

    # // ========================================( Helpers )======================================== // #

    def _namespace_tables(self, name: str) -> tuple[str, list[str], str]:
        """Return ``(table, key_columns, delete_column)`` for ``name``."""
        if name == "registry":
            return TABLE_PERSISTENT_VIEWS, ["persistence_key"], "persistence_key"
        if name == "application":
            return TABLE_APPLICATION_SLOTS, ["slot_name"], "slot_name"
        raise ValueError(f"unknown namespace: {name!r}")

    def _build_registry_row(self, payload: dict, view: Optional[Any]) -> Optional[dict[str, Any]]:
        """Assemble a registry row from the action payload and live view.

        Missing live view is logged and returns ``None`` -- the view
        exited between dispatch and middleware, so there is no init
        kwargs snapshot to persist. Rare enough in practice that
        declining the write is safer than inventing a stub row.
        """
        persistence_key = payload.get("persistence_key")
        if view is None:
            # Benign race: the view exited between its
            # PERSISTENT_VIEW_REGISTERED dispatch and this middleware
            # observing it. Declining the write is the correct fallback,
            # not an operator-actionable condition, so it logs at DEBUG.
            logger.debug(
                f"No live view found for persistence_key {persistence_key!r}; "
                "skipping registry persist"
            )
            return None

        # Read off the class: a test double answers any attribute on the instance.
        if getattr(type(view), "_registration_source", None) is not None:
            source = view._registration_source(persistence_key)
        else:
            source = (
                getattr(view, "_init_kwargs", {}),
                int(getattr(type(view), "kwargs_schema_version", 1)),
            )
        if source is None:
            logger.debug(
                f"Live view for persistence_key {persistence_key!r} holds no record of "
                f"the panel's arguments; skipping registry persist"
            )
            return None
        source_kwargs, kwargs_version = source
        # ``persistence_key`` has its own column, and ``theme`` and ``bot`` are
        # live objects no row can carry.
        init_kwargs = {k: v for k, v in source_kwargs.items() if k not in _NON_PERSISTABLE_KWARGS}
        try:
            # No ``default=`` fallback by design: any non-JSON kwarg
            # surfaces as a TypeError here so the row is declined and
            # logged, not silently coerced into a string the reattach
            # path cannot consume.
            init_kwargs_json = json.dumps(init_kwargs)
        except (TypeError, ValueError, RecursionError) as exc:
            # Enumerate the non-serializable kwargs so the error names them.
            # Runtime dependencies belong in on_bind, not the constructor.
            bad = []
            for key, value in init_kwargs.items():
                try:
                    json.dumps(value)
                except (TypeError, ValueError, RecursionError):
                    bad.append(f"{key}={type(value).__name__}")
            offenders = ", ".join(bad) if bad else str(exc)
            logger.error(
                f"Persistent view {persistence_key!r} was not saved -- its constructor "
                f"kwargs are not JSON-serializable ({offenders}). Runtime dependencies "
                f"(database pools, the bot, service clients) cannot survive the persistence "
                f"round-trip; pass them through on_bind(bot) instead."
            )
            return None
        note = _key_coercion_note(init_kwargs, "kwargs")
        if note is not None and self._first_key_note(f"{persistence_key}: {note}"):
            # Still written: a declined row would leave the panel dead on the
            # next restart rather than restored with one mismatched key.
            logger.error(f"Persistent view {persistence_key!r} {note}")

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
            "schema_version": CURRENT_SCHEMA_VERSIONS[TABLE_PERSISTENT_VIEWS],
            "created_at": now,
            "updated_at": now,
            # Explicit None, not omission. A SQL upsert only writes the columns
            # it is given, so leaving this out preserves whatever stamp the row
            # already carried -- and a panel re-posted under the same key into a
            # reachable channel would inherit the dead one's age and be pruned.
            "first_unreachable_at": None,
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
        # A snapshot: a hook registered during this call first runs on the
        # next one, and one that registers itself each time cannot loop here.
        for callback in list(hooks.get(hook_name, ())):
            try:
                # isawaitable, not iscoroutine: a hook returning a Future
                # or any awaitable object is still work to wait on, and the
                # narrow check silently dropped it on the floor.
                await await_maybe(callback(*args))
            except Exception as exc:
                logger.error(f"Persistence hook {hook_name!r} raised: {exc}")

    # // ========================================( Shutdown )======================================== // #

    async def flush_all(self) -> None:
        """Cancel pending tasks and flush every namespace synchronously.

        Called by :meth:`~cascadeui.persistence.manager.PersistenceManager.close`,
        which runs when the bot closes or when the caller closes persistence.
        Each namespace flushes under its write lock so no partial writes slip
        past ``close``.

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

        # A namespace is None until :meth:`initialize` builds it, as on a
        # shutdown after a failed boot.
        for ns in (self._ns_registry, self._ns_application):
            if ns is None:
                continue
            async with ns.write_lock:
                await self._flush(ns)

    async def close(self) -> None:
        """Stop scheduling writes, hold new changes for a reopen, and write what is batched."""
        self._closed = True
        await self.flush_all()

    def _close_with_bot(self) -> None:
        """Close persistence when the bot closes, and open it when the bot connects.

        discord.py has no shutdown event, so without this a stopped bot loses
        the application writes still batched, and a SQLite backend's worker
        thread keeps the process alive. The bot's ``close()`` is wrapped on
        the instance, so an override on the bot's class still runs first, and
        persistence closes after it, even when it raises. Closing twice is
        harmless, so a bot that also closes persistence itself is unaffected.

        ``connect()`` is wrapped too. It reopens persistence a close shut
        when the same bot connects again, which runs no ``setup_hook``. And a
        close from another task (SIGTERM, a shutdown command) still has
        persistence to close when ``connect()`` returns and the program
        moves on to ending, so ``connect()`` waits for it. A bot replaced by
        another (see :meth:`_adopt`) no longer closes it.
        """
        manager = self._manager
        bot = getattr(manager, "_bot", None)
        if bot is None:
            return

        closes = bot.close
        if getattr(closes, "_cascadeui_closes", None) is not manager:

            async def close(*args, **kwargs):
                # A reopen skips while this is pending, and connect() waits for it.
                closing = asyncio.get_running_loop().create_future()
                self._bot_closes.add(closing)
                finished = False
                try:
                    result = await closes(*args, **kwargs)
                    finished = True
                    return result
                finally:
                    # Before a reopen below restores the panels it leaves.
                    self._store._release_views_of(bot)
                    try:
                        if manager._bot is bot:
                            await _close_to_the_end(manager)
                    finally:
                        self._bot_closes.discard(closing)
                        closing.set_result(None)
                    # A restart that began during this close skipped its reopen,
                    # so it happens here. Not after a close cut off part way:
                    # the program is ending, and SQLite would keep it alive.
                    running = manager._bot
                    if finished and running is not None and not running.is_closed():
                        try:
                            await self._reopen_with_bot(self._store)
                        except Exception as exc:
                            logger.error(
                                f"Could not reopen persistence after {type(running).__name__} "
                                f"restarted: {exc}. Changes are held until persistence "
                                "reopens, and lost if the process exits first.",
                                exc_info=True,
                            )

            close._cascadeui_closes = manager
            bot.close = close

        connects = bot.connect
        if getattr(connects, "_cascadeui_opens", None) is not manager:

            async def connect(*args, **kwargs):
                if manager._bot is bot and (
                    self._closed or manager._closed or manager._close_started
                ):
                    try:
                        await self._reopen_with_bot(self._store)
                    except Exception as exc:
                        # A database that is down must not stop the bot
                        # reconnecting; the changes it misses are held.
                        logger.error(
                            f"Could not reopen persistence before {type(bot).__name__} "
                            f"connects: {exc}. Connecting anyway; changes are held until "
                            "persistence reopens, and lost if the process exits first.",
                            exc_info=True,
                        )
                try:
                    return await connects(*args, **kwargs)
                finally:
                    closing = [f for f in self._bot_closes if not f.done()]
                    if closing:
                        await asyncio.shield(asyncio.gather(*closing))

            connect._cascadeui_opens = manager
            bot.connect = connect

    def _close_on_sigterm(self) -> None:
        """Close the bot, and persistence with it, when the process gets SIGTERM.

        systemd, ``docker stop``, and most process managers stop a program
        with SIGTERM, which discord.py leaves at its default: the process
        dies at once, and the writes still batched are lost. So when nothing
        else handles SIGTERM, the first one closes the bot, which ends a
        program that runs the bot as its main task, and puts the default
        back, so a second SIGTERM ends a program that is still running, and a
        bot started again after it ends the process (see
        :meth:`_end_after_sigterm`). A handler the application installs takes
        precedence, before or after this. Nothing is installed where the event
        loop cannot handle signals: Windows, or a loop not running on the main
        thread. A SIGTERM that arrives once the bot has closed ends the program
        with the default action, as it would without this.
        """
        bot = getattr(self._manager, "_bot", None)
        if bot is None:
            return
        if bot.is_closed():
            return
        if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
            return
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self._on_sigterm)
        except RuntimeError:
            # NotImplementedError on Windows, and the refusal off the main thread.
            return
        self._sigterm_disposition = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(self) -> None:
        # asyncio still calls this after the application replaces the handler
        # with signal.signal(), so a changed disposition means it is theirs.
        if signal.getsignal(signal.SIGTERM) is not self._sigterm_disposition:
            return
        bot = getattr(self._manager, "_bot", None)
        if bot is None:
            return
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
        # A close already running (a shutdown command's) writes the batch, and
        # is waited for rather than started again: a second bot.close() would
        # run the bot's own close() override twice. With nothing left to
        # close, the program still running ends, as it would without this.
        closing = [f for f in self._bot_closes if not f.done()]
        if closing:
            logger.info(f"SIGTERM received: {type(bot).__name__} is already closing")
            self._sigterm_close = asyncio.ensure_future(asyncio.gather(*closing))
            return
        if bot.is_closed():
            signal.raise_signal(signal.SIGTERM)
            return
        logger.info(f"SIGTERM received: closing {type(bot).__name__}")
        self._sigterm_close = asyncio.ensure_future(self._close_bot(bot))

    @staticmethod
    async def _close_bot(bot) -> None:
        try:
            await bot.close()
        except Exception as exc:
            logger.error(f"Closing the bot on SIGTERM failed: {exc}", exc_info=True)

    def _adopt(self, replacement: "PersistenceMiddleware") -> None:
        """Take over the bot a replacement instance was built for.

        :func:`~cascadeui.setup_middleware` keeps the installed instance when
        a restart builds a new one. A restart that builds a new bot object as
        well passes that bot here, so persistence closes with it, and its
        message deletions reach the views.
        """
        if self._manager is None:
            return
        if replacement._manager is not None:
            bot = replacement._manager._bot
        else:
            bot = (replacement._pending_config or {}).get("bot")
        if bot is None or bot is self._manager._bot:
            return
        self._manager._bot = bot
        self._store._install_message_cleanup(bot)
