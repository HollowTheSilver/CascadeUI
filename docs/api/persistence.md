# API: Persistence

Persistence in CascadeUI spans two isolated namespaces (registry, application), each routed to a backend through a capability-flag Protocol. Scoped state rides under the application namespace: views opt a scoped slot in via `persistent_slots = ("scoped",)` on the class. The guide at [docs/guide/persistence.md](../guide/persistence.md) walks through setup patterns. This page is a flat symbol reference.

---

## `PersistenceMiddleware(manager=None, *, backend=None, registry=None, application=None, bot=None, migrators=None, restore_concurrency=8, prune_unreachable_after_days=None)`

Write-through middleware that owns the persistence pipeline. Construct once in `setup_hook`, after every cog that defines a `PersistentView` subclass has loaded, and pass it through `setup_middleware` to install it into the dispatch chain.

```python
from cascadeui import PersistenceMiddleware, setup_middleware
from cascadeui.persistence import SQLiteBackend

# Shorthand: one backend fills every unconfigured namespace
await setup_middleware(
    PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=bot),
)

# Data-only (no view re-attachment)
await setup_middleware(
    PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db")),
)
```

**Parameters**

- `manager` -- optional pre-built `PersistenceManager`. When supplied, the pipeline kwargs (`backend`, `registry`, `application`, `bot`, `migrators`, `restore_concurrency`, `prune_unreachable_after_days`) are ignored and the middleware presumes the caller already ran `initialize_backends`, `apply_migrations`, and `rehydrate`. Reserved for advanced call sites that customize manager internals before install.
- `backend` -- shorthand: fills any namespace not configured via `registry=`/`application=`.
- `registry`, `application` -- per-namespace overrides. Each accepts the matching config class from `cascadeui.persistence`. Explicit config wins over shorthand; passing the config with `backend=None` opts the namespace out entirely.
- `bot` -- when supplied, enables the reattach pipeline for `PersistentView` subclasses, installs the message-deletion cleanup listener, and closes persistence when the bot closes, a SIGTERM included (see [`close()`](#async-close-and-async-flush_all)). When omitted, only state data is restored, and closing persistence at shutdown is the caller's job.
- `migrators` -- optional dict with `"schema"` and/or `"kwargs"` keys, each mapping a `(name, from_version)` tuple to an async migrator callable. When omitted, no migrators are registered through this kwarg; the `@register_migrator` / `@register_kwargs_migrator` decorators are the canonical registration path, and this dict is the programmatic bulk alternative.
- `restore_concurrency` -- positive int bounding concurrency in both restore phases: the channel and message fetches during startup reattach, and the post-ready `on_restore` repaint that follows once the gateway is ready (default `8`). The repaint additionally serializes panels that share a channel (message edits rate-bucket per channel), so same-channel repaints run one at a time regardless of this value, while panels in distinct channels fan out under it.
- `prune_unreachable_after_days` -- `None` (the default) or a positive int. When set, the manager runs `prune_unreachable(older_than_days=...)` once the gateway is ready and daily after, until it closes. Requires `bot=`. A zero, negative, or non-int value, or a value without `bot=`, raises `ValueError` here. See [Rows that stay unreachable](../guide/persistence.md#rows-that-stay-unreachable).

### `async initialize(store)`

Runs the async startup pipeline: build the manager from the stashed config, initialize unique backends, apply schema migrations, blocking rehydrate both namespaces, install the gateway message-cleanup listener (when `bot` is available), stash the manager on the store as `store.persistence_manager`, start the TTL sweeper if any slot declares `ttl_days` and the unreachable sweep if `prune_unreachable_after_days` is set, and reattach persistent views (when `bot` is available). Idempotent: a later call skips the pipeline, but reopens persistence if it has closed and restores the persistent panels a closed bot left, which is what a restart in the same process needs from the new bot's `setup_hook`. With a pre-built `manager=`, the pipeline itself is skipped, but the manager's configured sweeps are still started.

Invoked automatically by `setup_middleware`. Direct invocation is supported for test fixtures that bypass the install helper.

**Raises** -- `ValueError` for a `restore_concurrency` below 1 or an unrecognized key in `migrators`; `TypeError` for a `bot` that is not a `discord.Client` or a `migrators` value of the wrong shape; `PersistenceInitError` when the optional `aiosqlite` dependency is required but missing. Opting every namespace out with `backend=None` is supported and does not raise.

---

## Per-namespace configuration

### `RegistryPersistence(backend=...)`

Governs the `PersistentView` registry namespace. Rows hold one entry per `persistence_key`; registry rows have no TTL and live until the view unregisters or you prune them explicitly. Pass `backend=None` to opt the registry out of persistence (persistent views still work in memory, but do not survive a restart).

### `ApplicationPersistence(backend=..., slots={})`

Governs the `state["application"]` namespace. `slots` maps slot name to a `SlotPolicy` for per-slot retention; slots without an explicit policy use `SlotPolicy()` defaults (in-memory, no TTL). When at least one slot declares `ttl_days`, the manager starts a daily TTL sweeper that deletes expired rows. Pass `backend=None` to opt application slots out of persistence entirely.

### `SlotPolicy(ttl_days=None, persistent=False)`

Per-slot policy declared inside `ApplicationPersistence.slots={"slot_name": SlotPolicy(...)}`. `persistent=True` writes the slot through to the backend; `persistent=False` (the default) keeps it in-memory. `ttl_days=N` lets the daily sweep delete the slot N days after its last write, from storage and from the running bot; `ttl_days=None` disables TTL. `persistent=False` paired with `ttl_days=N` raises `ValueError` -- in-memory slots never reach storage, so a TTL has nothing to prune.

Slot opt-in is additive with the class-level `persistent_slots` tuple on `_StatefulMixin` subclasses and with `access_slot(..., persistent=True)`. All three register the slot in the library's sticky `_PERSISTENT_SLOTS` set and combine cleanly when used together; `SlotPolicy` is the only one of them that also carries a TTL.

---

## `PersistenceBackend` (Protocol)

A backend is any class declaring `capabilities: Capability` and the methods required by the flags it advertises. The namespace configs check that flag against what each namespace needs, at config construction, and raise `PersistenceConfigError` when one is missing. A flag advertised without its methods is not caught there; it surfaces during setup with the missing method named.

```python
from typing import Any, AsyncIterator, ClassVar

from cascadeui.persistence import Capability, PersistenceBackend


class MyBackend:
    capabilities: ClassVar[Capability] = (
        Capability.KV | Capability.RELATIONAL | Capability.SCHEMA_META
    )

    # Lifecycle
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...

    # Capability.KV
    async def kv_read(self, namespace: str, key: str) -> bytes | None: ...
    async def kv_write(self, namespace: str, key: str, value: bytes) -> None: ...
    async def kv_delete(self, namespace: str, key: str) -> None: ...
    async def kv_scan(
        self, namespace: str, prefix: str = ""
    ) -> AsyncIterator[tuple[str, bytes]]: ...

    # Capability.RELATIONAL
    async def row_upsert(
        self, namespace: str, row: dict[str, Any], key_columns: list[str]
    ) -> None: ...
    async def row_upsert_many(
        self, namespace: str, rows: list[dict[str, Any]], key_columns: list[str]
    ) -> None: ...
    async def row_select(
        self, namespace: str, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...
    async def row_delete(self, namespace: str, where: dict[str, Any]) -> int: ...
    async def row_delete_where_lt(
        self, namespace: str, column: str, value: Any
    ) -> int: ...

    # Capability.SCHEMA_META
    async def get_schema_version(self, table: str) -> int: ...
    async def set_schema_version(self, table: str, version: int) -> None: ...
```

Three correctness guarantees beyond the method signatures:

1. **Copy-on-store / copy-on-return** -- backends must defensively copy dict/list inputs in `row_upsert` and `kv_write`, and copy outputs in `row_select` and `kv_scan`. Callers mutating the returned dict must not see that mutation on the next read.
2. **NULL-safe TTL prune** -- `row_delete_where_lt` must not sweep rows whose column value is missing or `None`.
3. **Scan-snapshot safety** -- `kv_scan` must not raise `RuntimeError` when the caller writes to the same namespace mid-iteration.

`row_upsert_many` batches the writes a flush would otherwise issue one at a time -- the SQL backends collapse it into a single transaction (`executemany` plus one commit). It shares `row_upsert`'s copy-on-store and conflict semantics. The persistence middleware falls back to per-row `row_upsert` when a backend does not implement it, so a custom backend may omit it.

A closed backend must open again when `initialize()` is called: a restart in the same process reopens persistence on the same backend objects. Its `close()` runs inside the bot's close, so persistence waits up to ten seconds for it, then logs a warning and closes without it.

`InMemoryBackend` is the reference implementation.

---

## `Capability`

Flag enum advertising which method sets a backend implements. Any combination via bitwise OR.

- `Capability.KV` -- `kv_read`, `kv_write`, `kv_delete`, `kv_scan`
- `Capability.RELATIONAL` -- `row_upsert`, `row_upsert_many`, `row_select`, `row_delete`, `row_delete_where_lt`
- `Capability.SCHEMA_META` -- `get_schema_version`, `set_schema_version`
- `Capability.TTL_INDEX` -- declares the backend has an indexed TTL column. Required when any `SlotPolicy` declares `ttl_days`.
- `Capability.RAW_SQL` -- `execute`, `fetch`, `fetch_one`, `executemany`, and the `transaction()` context manager (which yields a `Transaction`, the typed protocol importable from the package root that a custom backend's `transaction()` returns). Declared by the SQL backends; `InMemoryBackend` omits it.
- `Capability.OPEN_ROWS` -- rows are open mappings: `row_upsert` accepts unknown columns, `row_select` round-trips them, and a missing column reads as `None`. Schema migrators skip their DDL on such a backend, since a column-add alters nothing on disk. Declared by `InMemoryBackend`; the SQL backends have fixed columns and declare `RAW_SQL` instead.

The namespace configs check the backend's *declared* capability flags against what the namespace requires, raising `PersistenceConfigError` when a flag is absent. That check runs while the config object is constructed, before any backend method is called. Declaring a flag whose methods are not implemented is not caught here; it surfaces as an `AttributeError` at the first call.

One check runs later and conditionally: when `apply_migrations` finds a pending schema migration for a namespace, that namespace's backend must declare `OPEN_ROWS` or `RAW_SQL`. A backend declaring neither raises `PersistenceConfigError` during `PersistenceMiddleware.initialize`, rather than rejecting registry writes after the new version is recorded. A backend with nothing to migrate is never asked for either flag.

---

## Built-in backends

### `InMemoryBackend`

Always available. Declares `KV | RELATIONAL | TTL_INDEX | SCHEMA_META | OPEN_ROWS` (every capability except `RAW_SQL`, which an in-memory store has no engine for; `OPEN_ROWS` is what schema migrations key on instead). Process-local; state is lost on restart. Useful for tests and single-run bots.

```python
from cascadeui.persistence import InMemoryBackend

backend = InMemoryBackend()
```

### `SQLiteBackend(db_path="cascadeui.db", *, table_prefix="")`

Requires `pip install pycascadeui[sqlite]`. Declares `KV`, `RELATIONAL`, `TTL_INDEX`, `SCHEMA_META`, and `RAW_SQL`; its rows are fixed columns, so it does not declare `OPEN_ROWS` and a schema change needs a migrator. Uses WAL mode and prepared statements, with `synchronous=NORMAL` and a 5-second busy timeout applied as connection PRAGMAs.

```python
from cascadeui.persistence import SQLiteBackend

backend = SQLiteBackend("cascadeui.db")
```

Importable from `cascadeui.persistence` only when `aiosqlite` is installed; the import is optional and silent otherwise. `table_prefix` moves every table and index the backend creates and queries under the prefix, which keeps two CascadeUI deployments sharing one database apart -- see [the table set](../guide/persistence.md#the-tables-a-sql-backend-creates). A non-empty prefix outside lower-case letters, digits, and underscores raises `ValueError` at construction; a non-`str` prefix raises `TypeError`.

### `PostgresBackend(dsn, *, pool_kwargs=None, table_prefix="")`

Requires `pip install pycascadeui[postgres]`. Declares the same five as `SQLiteBackend`, `OPEN_ROWS` excluded for the same reason. Backed by an `asyncpg` connection pool with JSONB storage, and adds `LISTEN`/`NOTIFY` for cross-process scoped-state invalidation: the right choice for a multi-process deployment.

```python
from cascadeui.persistence import PostgresBackend

backend = PostgresBackend("postgresql://user:pass@host/db?sslmode=verify-full")
```

Importable from `cascadeui.persistence` only when `asyncpg` is installed; the import is optional and silent otherwise. `dsn` takes the standard libpq URL; `pool_kwargs` forwards extra arguments to the `asyncpg` pool. `table_prefix` behaves as on `SQLiteBackend`.

The `LISTEN` connection is watched by two class attributes a subclass may override. `listener_poll_seconds` (default `10.0`) is the time between health checks of that connection; a check is local and cheap, and a dropped connection is noticed at the next one, so a lower value reconnects sooner. `listener_retry_seconds` (default `5.0`) is the wait before reconnecting after an error.

### `physical_table(backend, table) -> str`

Resolves a library table's name to the one `backend` created, applying its `table_prefix` the way the backend's own SQL does. A migrator writing raw SQL against a library table passes the name through this rather than interpolating it, so a prefixed deployment does not write to the unprefixed table.

---

## `PersistenceManager`

The reattach/rehydrate/prune coordinator. Normally created and wired automatically by `PersistenceMiddleware.initialize`; access the live instance via `store.persistence_manager` when you need to drive pruning manually.

```python
# Drop one slot entirely (any age).
await mgr.prune_application(slot="settings")

# Drop every slot whose TTL ran out more than 90 days ago.
await mgr.prune_application(older_than_days=90)

# Drop specific registry rows; omit persistence_keys to clear the
# whole registry (destructive, rarely wanted).
await mgr.prune_registry(persistence_keys=["roles:main", "tickets:panel"])
```

`slot=` and `older_than_days=` are mutually exclusive on `prune_application`.
A pruned slot leaves the running bot as well as the database: its value in
`state["application"]` and any write still waiting for it go too.

`prune_registry` matches by key alone, so it deletes whatever holds a key when
it runs. It is for a key no live panel holds: a live one is retired through its
own `exit()`, which removes the registration only while that view owns it, and
`StateStore.get_active_view(persistence_key=...)` says whether there is one. A
prune that does take a live panel's row logs a warning.

`prune_registry` also takes `reason=`, which labels the `REGISTRY_PRUNED`
dispatch so a subscriber can tell why a row went. Left unset it is
`"explicit"` for a targeted prune and `"clear_all"` for a full wipe. The
library's own prunes pass `"gone"` when a reattach pass or the unreachable
sweep deleted rows whose channel or message returned a 404, and
`"unreachable"` when the sweep deleted rows that stayed unreachable past its
cutoff. The dispatch's `source` names the call that pruned: `"prune_registry"`,
`"reattach"`, or `"prune_unreachable"`.

### `async close()` and `async flush_all()`

`close()` stops the sweepers, writes every change still batched, then closes
each backend. Safe to call twice, even from two tasks at once. With `bot=` it
runs when the bot closes, after the bot's own `close()` and even when that
raises, and on Linux and macOS a SIGTERM closes the bot unless the application
handles SIGTERM itself; without a bot, await it at shutdown. Persistence
reopens when `setup_middleware()` runs again in the `setup_hook` of the bot
that replaces a closed one in the same process, and the persistent panels the
closed bot left are restored through the new one. After a close a SIGTERM
started, that ends the process instead, once any change made since is
written. Changes made while persistence is closed are held in memory and
written when it reopens, and the first one logs a warning, since they are
lost if the process exits first. See
[Shutdown](../guide/persistence.md#shutdown).

Each backend's `close()` gets up to ten seconds; one that takes longer is
logged and persistence closes without it, so the bot's `close()` still
returns. Closing a SQL backend waits up to five seconds for a transaction
already running on it, then closes anyway. On `SQLiteBackend` a transaction
cut off that way raises `RuntimeError` when it exits and commits nothing; on
`PostgresBackend` its connection is terminated.

`flush_all()` writes what is batched now and leaves persistence running.

### `last_reattach_summary` and `total_reattach_summary`

`last_reattach_summary` is the summary the most recent reattach pass returned
(`None` before the first), and `total_reattach_summary` is a read-only
`dict[str, list[str]]` covering every pass, with each `persistence_key` under
the outcome its most recent pass gave it. Both carry the same five buckets:
`restored`, `skipped`, `failed`, `removed`, `unreachable`.

Reconcile records kept outside the registry from `total_reattach_summary`.
`removed` is why: only the pass that deletes a row can report it, so after a
[re-drive](../guide/persistence.md#re-driving-reattach-after-a-runtime-cog-load)
the latest summary reports nothing removed and a reconcile keyed on it clears
nothing. A key moves between buckets as
later passes re-verdict it, and appears in exactly one.

Both cover reattach passes only. A pass's own deletions dispatch
`REGISTRY_PRUNED` too, with `reason="gone"`, so a subscriber registered
before a pass ran hears of its removals both ways: act on reattach removals
from one of the two, for instance by skipping a `REGISTRY_PRUNED` whose
`source` is `"reattach"`. A row deleted afterwards,
by the unreachable sweep or by `prune_registry`, keeps the outcome its last
pass gave it; those deletions arrive as `REGISTRY_PRUNED`, which a subscriber
registered after startup receives. The two together cover every registration
that goes away.

### `unreachable_since`

Read-only `dict[str, int]` of `persistence_key` to the epoch second at which
a registry row was first found unreachable. A row absent from the mapping is
either reachable or has never been observed otherwise. Reflects this process's
view; `prune_unreachable` reads disk.

### `prune_unreachable(*, older_than_days)`

Deletes registry rows that have stayed unreachable past a cutoff, after
re-checking each one against Discord.

```python
result = await mgr.prune_unreachable(older_than_days=30)
# {"pruned": [...], "gone": [...], "recovered": [...], "kept": [...]}
```

A candidate that fetches successfully is kept and its stamp cleared
(`recovered`), whatever its age. A definitive `discord.NotFound` is deleted
regardless of age. Everything still unreachable is deleted only when its
stamp predates the cutoff, and reported as `kept` otherwise. A row rewritten
mid-pass, or one whose pre-delete re-read failed at the backend, is `kept`
too: neither verdict describes the row as it stands. A row whose key a live
panel in this process holds is `kept` without a fetch. Deletions route
through `prune_registry`: rows whose channel or message returned a 404 (also
listed in `gone`) with `reason="gone"`, as a reattach pass reports them, and
the rows that aged past the cutoff with `reason="unreachable"`. The call never runs
alongside a reattach pass: whichever starts second waits. Calling either one
from a hook that runs inside the other, such as a `REGISTRY_PRUNED` hook
registered with `store.on()`, raises `RuntimeError` instead of waiting on
itself forever; schedule the call with `asyncio.create_task(...)` from there.

`PersistenceMiddleware(prune_unreachable_after_days=N)` runs this call
automatically, first once the gateway is ready and then daily.

Raises `ValueError` when `older_than_days` is negative or not an `int`
(`bool` included), and `RuntimeError` when the middleware was constructed
without `bot=`. Returns the empty summary when the registry namespace has no
backend. `older_than_days=0` is allowed: re-verification, not the age, is
what makes a deletion safe.

---

## Exceptions

All five exception types are importable from the package root
(`from cascadeui import PersistenceError, ...`). They form a simple
hierarchy so callers can catch the whole family with `PersistenceError`
or handle specific phases individually.

| Class | Parent | Fires when |
|-------|--------|------------|
| `PersistenceError` | `Exception` | Base class for every persistence failure. Catch this to handle any persistence error. |
| `PersistenceConfigError` | `PersistenceError` | Raised when a namespace config is built against a backend whose declared capabilities do not cover what that namespace requires. Fires at config construction, before any backend method runs. |
| `PersistenceInitError` | `PersistenceError` | Raised from `backend.initialize()` on connection failures, table-creation errors, or permission problems. Prevents the bot from starting against an unhealthy persistence layer. |
| `PersistenceSchemaError` | `PersistenceError` | Raised when the on-disk schema version is higher than the library supports, or when no migrator is registered for the next schema step. |
| `PersistenceRehydrateError` | `PersistenceError` | Raised during `PersistenceMiddleware.initialize` when a persisted JSON blob is corrupted, a required row is malformed, or the backend returns unexpected shape. Per-view re-attachment failures do NOT raise this -- they are logged and skipped. |

```python
from cascadeui import (
    PersistenceError,
    PersistenceMiddleware,
    PersistenceSchemaError,
    setup_middleware,
)

try:
    await setup_middleware(PersistenceMiddleware(backend=backend))
except PersistenceSchemaError as exc:
    # Schema is ahead of the library -- refuse to boot rather than
    # corrupt newer on-disk state by downgrading silently.
    log.critical("Persistence schema too new: %s", exc)
    raise
except PersistenceError as exc:
    log.error("Persistence layer failed to initialize: %s", exc)
    raise
```
