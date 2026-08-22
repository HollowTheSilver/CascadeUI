# API: Persistence

Persistence in CascadeUI spans two isolated namespaces (registry, application), each routed to a backend through a capability-flag Protocol. Scoped state rides under the application namespace: views opt a scoped slot in via `persistent_slots = ("scoped",)` on the class. The guide at [docs/guide/persistence.md](../guide/persistence.md) walks through setup patterns. This page is a flat symbol reference.

---

## `PersistenceMiddleware(manager=None, *, backend=None, registry=None, application=None, bot=None, migrators=None, restore_concurrency=8)`

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

- `manager` -- optional pre-built `PersistenceManager`. When supplied, the pipeline kwargs (`backend`, `registry`, `application`, `bot`, `migrators`) are ignored and the middleware presumes the caller already ran `initialize_backends`, `apply_migrations`, and `rehydrate`. Reserved for advanced call sites that customize manager internals before install.
- `backend` -- shorthand: fills any namespace not configured via `registry=`/`application=`.
- `registry`, `application` -- per-namespace overrides. Each accepts the matching config class from `cascadeui.persistence`. Explicit config wins over shorthand; passing the config with `backend=None` opts the namespace out entirely.
- `bot` -- when supplied, enables the reattach pipeline for `PersistentView` subclasses and installs the message-deletion cleanup listener. When omitted, only state data is restored.
- `migrators` -- optional dict with `"schema"` and/or `"kwargs"` keys, each mapping a `(name, from_version)` tuple to an async migrator callable. When omitted, no migrators are registered through this kwarg; the `@register_migrator` / `@register_kwargs_migrator` decorators are the canonical registration path, and this dict is the programmatic bulk alternative.
- `restore_concurrency` -- positive int bounding concurrency in both restore phases: the channel and message fetches during startup reattach, and the post-ready `on_restore` repaint that follows once the gateway is ready (default `8`). The repaint additionally serializes panels that share a channel (message edits rate-bucket per channel), so same-channel repaints run one at a time regardless of this value, while panels in distinct channels fan out under it.

### `async initialize(store)`

Runs the async startup pipeline: build the manager from the stashed config, initialize unique backends, apply schema migrations, blocking rehydrate both namespaces, install the gateway message-cleanup listener (when `bot` is available), stash the manager on the store as `store.persistence_manager`, start the TTL sweeper if any slot declares `ttl_days`, and reattach persistent views (when `bot` is available). Idempotent: subsequent calls return immediately.

Invoked automatically by `setup_middleware`. Direct invocation is supported for test fixtures that bypass the install helper.

**Raises** -- `ValueError` for a `restore_concurrency` below 1 or an unrecognized key in `migrators`; `TypeError` for a `bot` that is not a `discord.Client` or a `migrators` value of the wrong shape; `PersistenceInitError` when the optional `aiosqlite` dependency is required but missing. Opting every namespace out with `backend=None` is supported and does not raise.

---

## Per-namespace configuration

### `RegistryPersistence(backend=...)`

Governs the `PersistentView` registry namespace. Rows hold one entry per `persistence_key`; registry rows have no TTL and live until the view unregisters or you prune them explicitly. Pass `backend=None` to opt the registry out of persistence (persistent views still work in memory, but do not survive a restart).

### `ApplicationPersistence(backend=..., slots={})`

Governs the `state["application"]` namespace. `slots` maps slot name to a `SlotPolicy` for per-slot retention; slots without an explicit policy use `SlotPolicy()` defaults (in-memory, no TTL). When at least one slot declares `ttl_days`, the manager starts a daily TTL sweeper that deletes expired rows. Pass `backend=None` to opt application slots out of persistence entirely.

### `SlotPolicy(ttl_days=None, persistent=False)`

Per-slot policy declared inside `ApplicationPersistence.slots={"slot_name": SlotPolicy(...)}`. `persistent=True` writes the slot through to the backend; `persistent=False` (the default) keeps it in-memory. `ttl_days=N` prunes rows older than the cutoff on auto-prune cycles; `ttl_days=None` disables TTL. `persistent=False` paired with `ttl_days=N` raises `ValueError` -- in-memory slots never reach storage, so a TTL has nothing to prune.

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

---

## `PersistenceManager`

The reattach/rehydrate/prune coordinator. Normally created and wired automatically by `PersistenceMiddleware.initialize`; access the live instance via `store.persistence_manager` when you need to drive pruning manually.

```python
# Drop one slot entirely (any age).
await mgr.prune_application(slot="settings")

# Whole-namespace TTL sweep across every persistent slot.
await mgr.prune_application(older_than_days=90)

# Drop specific registry rows; omit persistence_keys to clear the
# whole registry (destructive, rarely wanted).
await mgr.prune_registry(persistence_keys=["roles:main", "tickets:panel"])
```

`slot=` and `older_than_days=` are mutually exclusive on `prune_application`.

`prune_registry` also takes `reason=`, which labels the `REGISTRY_PRUNED`
dispatch so a subscriber can tell why a row went. Left unset it keeps the
value the method has always computed.

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
# {"pruned": [...], "recovered": [...], "kept": [...]}
```

A candidate that fetches successfully is kept and its stamp cleared
(`recovered`), whatever its age. A definitive `discord.NotFound` is deleted
regardless of age. Everything still unreachable is deleted only when its
stamp predates the cutoff, and reported as `kept` otherwise. A row rewritten
mid-pass, or one whose pre-delete re-read failed at the backend, is `kept`
too: neither verdict describes the row as it stands. Deletions route
through `prune_registry` with `reason="unreachable"`.

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
