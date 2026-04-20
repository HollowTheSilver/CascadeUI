# Persistence

CascadeUI state is ephemeral by default. Nothing reaches disk until you
opt a slot in. Two independent namespaces cover the two things that can
survive a restart, each served by a backend that declares its
capabilities up front:

| Namespace | Contents | Config class |
|-----------|----------|--------------|
| `registry` | `PersistentView` reattach rows (one row per `persistence_key`) | `RegistryPersistence` |
| `application` | Reducer slots opted in via `persistent_slots` or `access_slot(..., persistent=True)` | `ApplicationPersistence` |

`PersistenceMiddleware` owns the full startup pipeline: wire backends to
namespaces, apply migrations, block until rehydrate completes, install the
write-through dispatch hook, and reattach persistent views when a bot is
supplied. Install it through `setup_middleware`, which awaits each
middleware's `initialize(store)` method in order.

## Setup

Construct `PersistenceMiddleware` once in your bot's `setup_hook`,
**after loading your cogs**:

```python
from cascadeui import setup_middleware
from cascadeui.state.middleware import PersistenceMiddleware
from cascadeui.persistence import SQLiteBackend

class MyBot(commands.Bot):
    async def setup_hook(self):
        # Load cogs first so PersistentView subclasses register themselves
        # via __init_subclass__ before the middleware looks them up
        await self.load_extension("cogs.dashboard")
        await self.load_extension("cogs.counter")

        # One backend covers both namespaces (shorthand form)
        await setup_middleware(
            PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=self),
        )
```

!!! warning "Cog loading order matters"
    The persistence middleware must initialize **after** every cog that
    defines a `PersistentView` subclass is imported. Python's import
    machinery populates the class registry via `__init_subclass__`;
    initializing against an empty registry silently orphans every
    surviving persistent view.

!!! warning "Cogs do not install middleware"
    Middleware install belongs to the bot author, not a cog. A cog that
    called `setup_middleware(...)` inside its own `setup(bot)` would
    silently mutate the bot's store without the author's consent.
    Declare the dependency in the cog's docstring instead and let the
    bot's `setup_hook` satisfy it.

!!! tip "Zero-config construction is allowed"
    `PersistenceMiddleware()` with no arguments defaults to
    `SQLiteBackend("cascadeui.db")` for both namespaces. The optional
    `aiosqlite` dependency is required; without it `initialize` raises
    `PersistenceInitError` with an install hint. Zero-config is
    ephemeral-in-practice because no slots are opted in yet -- install
    it, then opt slots in with `persistent_slots = ("name",)` on your
    view class (or `access_slot(..., persistent=True)` from a reducer)
    as you need them.

### Per-namespace configuration

The shorthand `backend=` fills any namespace that was not given an explicit
config. Passing a namespace config overrides the shorthand for that
namespace:

```python
from cascadeui import setup_middleware
from cascadeui.state.middleware import PersistenceMiddleware
from cascadeui.persistence import (
    InMemoryBackend,
    SQLiteBackend,
    RegistryPersistence,
    ApplicationPersistence,
    SlotPolicy,
)

await setup_middleware(
    PersistenceMiddleware(
        # Shorthand: any namespace without an explicit config uses this
        backend=SQLiteBackend("cascadeui.db"),

        # Application slots use a separate SQLite file with per-slot policies.
        # Slots not listed here still default to ephemeral -- list only the
        # slots you want durable.
        application=ApplicationPersistence(
            backend=SQLiteBackend("application.db"),
            slots={
                "user_preferences": SlotPolicy(persistent=True),
                "search_cache": SlotPolicy(persistent=True, ttl_days=7),
            },
        ),
        bot=bot,
    ),
)
```

Pass `backend=None` inside a specific namespace config to opt that namespace
out entirely. The shorthand does *not* override an explicit config, so this
is the canonical way to turn one namespace off while leaving the other on:

```python
await setup_middleware(
    PersistenceMiddleware(
        backend=SQLiteBackend("cascadeui.db"),
        application=ApplicationPersistence(backend=None),  # registry only
        bot=bot,
    ),
)
```

### Data-only vs reattach

`bot=` is optional. Without it, the middleware initializes backends and
rehydrates state, but skips persistent-view reattach:

```python
# Data-only: state survives restart, but PersistentView panels do not
# re-attach to their original messages
await setup_middleware(
    PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db")),
)
```

Data-only mode still restores any slots marked `persistent=True`.
Full mode logs a reattach summary with four buckets, and callers that
need the structured result can await `reattach_persistent_views()`
directly on the manager:

```python
summary = await store.persistence_manager.reattach_persistent_views()
# {"restored": [...], "skipped": [...], "failed": [...], "removed": [...]}
```

- `restored`: view reattached successfully.
- `skipped`: view class not imported, or kwargs migrator missing. Row stays
  on disk so the next restart retries.
- `failed`: construction, kwargs migrator, or `on_restore` raised. Row
  stays on disk for manual recovery.
- `removed`: channel or message deleted while the bot was offline. Row
  removed via `prune_registry` (which dispatches `REGISTRY_PRUNED`).

## Backends

### Built-in backends

The library ships two backends:

| Backend | Import | Capabilities |
|---------|--------|--------------|
| `InMemoryBackend` | `cascadeui.persistence` | KV, RELATIONAL, TTL_INDEX, SCHEMA_META |
| `SQLiteBackend` | `cascadeui.persistence` (requires `aiosqlite`) | KV, RELATIONAL, TTL_INDEX, SCHEMA_META |

`InMemoryBackend` is the reference implementation. It matches the Protocol
exactly and is useful for tests. `SQLiteBackend` is the recommended
production default: WAL mode for concurrent reads, `ON CONFLICT` upsert,
NULL-safe TTL prune, and LIKE-ESCAPE safe scan.

```bash
pip install pycascadeui[sqlite]
```

```python
from cascadeui import setup_middleware
from cascadeui.state.middleware import PersistenceMiddleware
from cascadeui.persistence import SQLiteBackend

await setup_middleware(
    PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=bot),
)
```

### Writing a custom backend

A backend is any class that satisfies the `PersistenceBackend` Protocol and
declares its capabilities. No inheritance is required; the Protocol is
`@runtime_checkable`:

```python
from cascadeui.persistence import Capability, PersistenceBackend

class MyBackend:
    capabilities = Capability.KV | Capability.RELATIONAL | Capability.SCHEMA_META

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...

    # Key-value surface (Capability.KV)
    async def kv_read(self, namespace, key): ...
    async def kv_write(self, namespace, key, value): ...
    async def kv_delete(self, namespace, key): ...
    async def kv_scan(self, namespace, prefix=""): ...

    # Relational surface (Capability.RELATIONAL)
    async def row_upsert(self, namespace, row, key_columns): ...
    async def row_select(self, namespace, where=None): ...
    async def row_delete(self, namespace, where): ...
    async def row_delete_where_lt(self, namespace, column, value): ...

    # Schema metadata (Capability.SCHEMA_META)
    async def get_schema_version(self, table): ...
    async def set_schema_version(self, table, version): ...
```

### Capability flags

Each namespace config declares which capabilities it needs; the manager
validates those against the backend's declared set when the middleware
initializes. A mismatch raises `PersistenceConfigError` before any
backend method runs:

| Namespace | Required capabilities |
|-----------|----------------------|
| `RegistryPersistence` | `RELATIONAL \| SCHEMA_META` |
| `ApplicationPersistence` (no TTL slots) | `RELATIONAL \| SCHEMA_META` |
| `ApplicationPersistence` (any `ttl_days` slot) | `RELATIONAL \| SCHEMA_META \| TTL_INDEX` |

Declare capabilities on the class, not the instance:

```python
class MinimalKVBackend:
    capabilities = Capability.KV | Capability.SCHEMA_META
```

!!! info "Backend contracts beyond method signatures"
    Three correctness properties are required of every backend:

    1. **Copy on store**: `row_upsert` must not retain a reference to the
       caller's dict. A later mutation on the caller side must not bleed
       into storage.
    2. **NULL-safe TTL prune**: `row_delete_where_lt` must not sweep rows
       whose target column is `NULL`. SQL's `NULL < value` evaluates to
       `NULL` (never true); the Protocol requires that same semantic
       from every implementation.
    3. **Scan snapshot safety**: `kv_scan` must not raise
       `RuntimeError` when the caller writes to the namespace
       mid-iteration. Snapshot keys up front.

    `InMemoryBackend` is the reference for all three. Tests in
    `tests/test_backends.py` parametrize the full Protocol surface across
    every shipped backend. Drop a custom class into that fixture to see
    the same coverage applied to yours.

## Slot policies

`SlotPolicy` carries per-slot policy for application slots: opt-in
persistence and an optional TTL. Slots default to ephemeral -- the
policy's `persistent=True` flag is the opt-in that the persistence
middleware watches for.

```python
from cascadeui.persistence import SlotPolicy

SlotPolicy()                                    # ephemeral (default)
SlotPolicy(persistent=True)                     # durable, no TTL
SlotPolicy(persistent=True, ttl_days=7)         # durable, prune after 7 days
SlotPolicy(ttl_days=7)                          # ValueError -- TTL needs persistent=True
```

Declare slot policies in two places:

```python
# (1) Static: inside ApplicationPersistence.slots
application=ApplicationPersistence(
    backend=SQLiteBackend("app.db"),
    slots={
        "preferences": SlotPolicy(persistent=True),
        "cache:search": SlotPolicy(persistent=True, ttl_days=7),
    },
)

# (2) Runtime: after PersistenceMiddleware has initialized, via the manager
manager = store.persistence_manager
manager.register_slot_policy(
    "cache:autocomplete",
    SlotPolicy(persistent=True, ttl_days=1),
)
```

Unregistered slots fall back to `SlotPolicy()` (ephemeral, no TTL) with
a DEBUG log: audit-friendly without emitting warnings on every dispatch.

!!! tip "Three ways to opt a slot in"
    All three routes mark the slot persistent; pick the one that lives
    closest to where the slot is defined:

    1. `persistent_slots = ("name",)` on the view class -- declarative
       and the recommended default. Registered at class-definition time.
    2. `SlotPolicy(persistent=True)` in `ApplicationPersistence.slots` --
       use when persistence is pure config (TTL tuning, no owning view).
    3. `access_slot(..., persistent=True)` from code -- use when the
       declaration lives next to the slot's seed logic inside a reducer
       or a `seed_initial_state` hook.

    All three register the slot name in a sticky module-level set, so
    every later write with the same name inherits the contract.

## Choosing a persistence pattern

Two axes decide the pattern: whether the *data* must survive restart,
and whether the *view* must re-attach to its original message.

| Data survives restart? | View re-attaches? | Pattern | Stable `persistence_key` required? |
|------------------------|-------------------|---------|------------------------------|
| No | No | Plain `StatefulView` (no persistence) | No |
| Yes | No | Pattern 1 (named slot) | **Yes** -- pass `persistence_key=` explicitly |
| No | Yes | `PersistentView` subclass (registry only) | Yes (registry identity) |
| Yes | Yes | `PersistentView` plus Pattern 1 slot | Yes (shared across both roles) |

`persistence_key` is opt-in identity. The property falls back to `self.id`
(a fresh UUID per instance) when no `persistence_key=` is passed at
construction. That fallback is safe for the top row and for views
that never key a persistent slot off `self.persistence_key`. The three rows
that involve persistence need a domain-stable value -- guild id,
composite user-guild key, or an explicit `persistence_key=f"counter:{uid}"`
-- to avoid writing to a fresh bucket on every restart.

## Pattern 1: Data persistence via a named slot

Persistence in this pattern comes from the `persistent_slots` class
attribute (or `access_slot(..., persistent=True)`) -- that flag is the
opt-in that tells the library to write the slot to disk. Setting
`persistence_key` alone persists nothing; it only names the lookup bucket
inside the slot.

The view instance is recreated on each invocation; only the data
survives. The canonical opt-in is declarative: list the slot name in
the view's `persistent_slots` class attribute, write to the slot from
a reducer, and read back through `slot_property`.

=== "V2"

    ```python
    from cascadeui import (
        StatefulLayoutView, cascade_reducer, slot_property, access_slot,
    )

    @cascade_reducer("COUNTER_INCREMENT")
    async def increment(action, state):
        slot = access_slot(state, "counters", action["payload"]["key"])
        slot["value"] = slot.get("value", 0) + 1
        return state

    class CounterView(StatefulLayoutView):
        instance_limit = 1
        persistent_slots = ("counters",)

        value = slot_property(
            "value", slot="counters", key=lambda self: self.persistence_key, default=0,
        )
    ```

=== "V1"

    ```python
    from cascadeui import (
        StatefulView, cascade_reducer, slot_property, access_slot,
    )

    @cascade_reducer("COUNTER_INCREMENT")
    async def increment(action, state):
        slot = access_slot(state, "counters", action["payload"]["key"])
        slot["value"] = slot.get("value", 0) + 1
        return state

    class CounterView(StatefulView):
        instance_limit = 1
        persistent_slots = ("counters",)

        value = slot_property(
            "value", slot="counters", key=lambda self: self.persistence_key, default=0,
        )
    ```

!!! warning "Pattern 1 needs a stable `persistence_key`"
    `slot_property(..., key=lambda self: self.persistence_key)` reads the
    slot bucket whose name is `self.persistence_key`. Pass `persistence_key=...`
    explicitly at construction (e.g. `persistence_key=f"counter:{user_id}"`)
    or point the `key=` lambda at a different stable identifier --
    guild id, composite user-guild key, or any domain value that
    survives reconstruction.

    The UUID fallback on `persistence_key` is fresh per instance. Combining
    it with `persistent=True` writes to a new bucket every restart,
    leaving the prior data orphaned on disk.

!!! info "`persistent_slots` vs manual opt-in"
    `persistent_slots` is shorthand for `access_slot(name, persistent=True)`
    without the seed hook. Every route registers the name in the same
    sticky module-level set, so you do not need to re-pass the kwarg
    from reducers or other call sites. Use the class attribute as the
    default; reach for `access_slot(..., persistent=True)` only when the
    opt-in genuinely belongs next to seed logic (dynamic slot names,
    per-invocation decisions).

## Pattern 2: View persistence via `PersistentView`

`PersistentView` (V1) and `PersistentLayoutView` (V2) stay interactive
across bot restarts:

=== "V2"

    ```python
    from cascadeui import PersistentLayoutView, StatefulButton, card
    from discord.ui import ActionRow

    class RoleSelectorPanel(PersistentLayoutView):
        instance_limit = 1
        instance_scope = "guild"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.add_item(
                card(
                    "## Role Selector",
                    ActionRow(
                        StatefulButton(
                            label="Get Role",
                            custom_id="roles:get",
                            callback=self.give_role,
                        ),
                    ),
                    color=discord.Color.blurple(),
                )
            )

        async def give_role(self, interaction): ...

        async def on_restore(self, bot): ...
    ```

=== "V1"

    ```python
    from cascadeui import PersistentView, StatefulButton

    class RoleSelectorView(PersistentView):
        instance_limit = 1
        instance_scope = "guild"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.add_item(StatefulButton(
                label="Get Role",
                custom_id="roles:get",
                callback=self.give_role,
            ))

        async def give_role(self, interaction): ...

        async def on_restore(self, bot): ...
    ```

Send once from an admin command:

```python
@bot.hybrid_command()
async def setup_roles(ctx):
    view = RoleSelectorPanel(context=ctx, persistence_key=f"roles:panel:{ctx.guild.id}")
    await view.send()
```

After a restart, `setup_middleware(PersistenceMiddleware(bot=self, ...))`
drives the reattach pipeline during startup:

1. Reads the registry via `RegistryPersistence.backend.row_select()`.
2. Looks up each row's `view_class` in the class registry.
3. Walks the kwargs migrator chain from the stored `kwargs_schema_version`
   to the class's current version.
4. Fetches the target channel and message (skips non-messageable channels).
5. Constructs the view, sets `_message`, restores `user_id` / `guild_id`,
   re-derives `session_id`, and calls `bot.add_view(view, message_id=...)`.
6. Registers the view in state, installs the message-deletion listener
   (eagerly, since restored views skip `send()`), and calls
   `on_restore(bot)`.

### Kwargs migrations for PersistentView subclasses

When a `PersistentView` subclass changes its `__init__` signature, bump
`kwargs_schema_version` on the class and register a migrator:

```python
from cascadeui.persistence import register_kwargs_migrator

class TicketPanel(PersistentLayoutView):
    kwargs_schema_version = 2   # was 1 before the rename
    ...

@register_kwargs_migrator("mybot.views.TicketPanel", from_version=1)
async def migrate_ticket_panel_1_to_2(kwargs):
    kwargs["channel_id"] = kwargs.pop("target_channel_id")
    return kwargs
```

The qualified class name must match the stored `view_class` column
(typically `f"{module}.{cls.__qualname__}"`). Rows whose version is
ahead of what any migrator handles are skipped with a WARNING and left
on disk for later recovery.

**Stale entry handling:**

| Scenario | Outcome |
|----------|---------|
| Message deleted externally | Row removed, reattach summary logs as `removed` |
| Channel deleted or non-messageable | Row removed, reattach summary logs as `removed` |
| View class not imported | Row kept, reattach summary logs as `skipped` |
| Kwargs migrator raises or returns non-dict | Row kept, reattach summary logs as `failed` |
| Construction or `on_restore` raises | Row kept, reattach summary logs as `failed` |

**Requirements for `PersistentView`:**

- `persistence_key` is required (raises `ValueError` if not provided).
- All components must have explicit `custom_id` values (auto-generated IDs
  do not survive restarts).
- `timeout` is forced to `None` (persistent views never time out).
- `owner_only` defaults to `False`; override explicitly if your panel
  should be creator-only.
- Ephemeral sends are rejected (`PersistentView.send(ephemeral=True)`
  raises `ValueError`; ephemeral messages have no permanent ID).

!!! warning "One message per `persistence_key`"
    The registry tracks one message per `persistence_key`. Sending a second
    view with the same key exits the previous instance and overwrites
    the row. Design keys to be unique per intended panel instance (for
    example, `"roles:main"` for a single shared panel,
    `f"profile:{user_id}"` for a per-user panel).

## Migrations

Two migrator surfaces exist:

- **Schema migrators** (library-owned, `register_migrator`): rewrite a
  backend table from version N to N+1. The library ships zero migrators
  today; the registry exists so future schema changes have a clean
  landing spot without another breaking release.
- **Kwargs migrators** (user-owned, `register_kwargs_migrator`): rewrite
  a single `PersistentView`'s stored `init_kwargs` blob from version N
  to N+1. Pure function of the kwargs dict, no backend access.

```python
from cascadeui.persistence import register_migrator

@register_migrator("persistent_views", from_version=1)
async def _migrate_persistent_views_1_to_2(backend):
    rows = await backend.row_select("persistent_views")
    for row in rows:
        row["new_column"] = derive(row)
        await backend.row_upsert("persistent_views", row, ["persistence_key"])
```

Library-owned migrators run automatically during `apply_migrations` in the
setup pipeline. A missing migrator for a required version step raises
`PersistenceInitError`. Fresh installs skip this path entirely because
the DDL creates tables at the current version.

## Pruning

Two prune methods live on the manager. Callers typically reach them via
the `/cascadeui` DevTools command group or a scheduled task:

```python
manager = store.persistence_manager

# Application slots: delete one slot, OR delete rows by expires_at cutoff.
await manager.prune_application(slot="cache:search")
await manager.prune_application(older_than_days=7)

# Registry: delete specific persistent-view rows (or everything).
await manager.prune_registry(persistence_keys=["roles:main", "tickets:panel"])
```

Each prune dispatches a bookkeeping action (`APPLICATION_SLOTS_PRUNED`,
`REGISTRY_PRUNED`) so subscribers and hooks observe the deletion without
inferring it from row counts.

### Automatic TTL sweeping

When any slot declares `ttl_days`, the manager starts a daily background
sweeper at `install_middleware()` time. It calls `row_delete_where_lt` on
`application_slots.expires_at` once every 24 hours and drops rows whose
absolute wall-clock expiration has passed. No cadence configuration is
exposed -- TTLs are expressed in days, sub-day granularity is meaningless,
and asking the user to also schedule a prune task is friction the library
can absorb.

`expires_at` is an absolute timestamp written at write-time, not a
duration. It survives bot restarts: a row written with `ttl_days=7` two
days before a crash still has five days left after a restart. `rehydrate()`
runs one prune pass before reading so rows that expired while the bot was
offline are dropped rather than loaded into memory.

Manual `prune_application(older_than_days=...)` remains available for
devtools and one-off operations.

## Observability

Register hooks on the manager to observe flush cadence and errors without
parsing logs:

```python
manager = store.persistence_manager

def on_flush(namespace, upsert_count, delete_count):
    metrics.record(f"persistence.{namespace}.upserts", upsert_count)
    metrics.record(f"persistence.{namespace}.deletes", delete_count)

def on_error(namespace, exc):
    alerts.fire(f"persistence.{namespace}.error", repr(exc))

manager.register_hook("on_flush", on_flush)
manager.register_hook("on_error", on_error)
```

Hooks run under the middleware's write lock for the namespace they
describe. Keep them fast and non-blocking. The middleware enters
exponential backoff on flush failure (1s, 2s, 4s, 8s, 16s, capped at 60s)
and logs CRITICAL after `MAX_RETRIES` consecutive failures. Rows stay
dirty across retries so no writes are lost.

## What gets persisted

CascadeUI state is ephemeral by default. Nothing reaches disk until
either (a) a slot is opted in via `persistent_slots`,
`access_slot(..., persistent=True)`, or `SlotPolicy(persistent=True)`,
or (b) a `PersistentView` subclass is registered. See
[Core Concepts - State Topology](concepts.md#state-topology) for the
full tree.

!!! warning "Scoped state is not persisted by default"
    `dispatch_scoped()` is a Redux organization pattern, not a persistence
    mechanism. Scoped writes live under `state["application"]["scoped"]`
    (or a named `scoped_slot`) and are dropped on restart unless the
    slot is explicitly opted in. Opt in with `persistent_slots = ("scoped",)`
    on the view class for the default scoped bucket, or
    `persistent_slots = ("my_named_slot",)` paired with
    `scoped_slot = "my_named_slot"` for a named bucket. TTLs live on
    `SlotPolicy(ttl_days=N)` at setup time, never on class attributes.

| State section | Persisted? | Namespace |
|--------------|------------|-----------|
| `views`, `sessions`, `components`, `modals` | No (ephemeral runtime) | — |
| `application.<slot>` without opt-in | No | — |
| `application.<slot>` marked `persistent=True` | Yes | `application` |
| `application.scoped` without opt-in | No | — |
| `application.scoped` marked `persistent=True` | Yes | `application` |
| `persistent_views` registry | Yes | `registry` |

Scoped state (`dispatch_scoped`) lives under
`state["application"]["scoped"]`, which means it uses the same
persistence plumbing as every other application slot. Opt in once with
`persistent_slots = ("scoped",)` on a view class (or
`access_slot(state, "scoped", ..., persistent=True)` inside
`seed_initial_state`) and scoped writes flow through
`ApplicationPersistence` to the backend. No mirror-into-a-slot dance
required.

!!! tip "Store IDs, not discord.py objects"
    State is serialized as JSON. discord.py model objects (`Member`,
    `Role`, `Channel`, etc.) are not JSON-serializable and raise
    `TypeError` at flush time with a message suggesting the fix. Store
    the `.id` integer:

    ```python
    # Wrong: fails at persistence time
    await self.dispatch_scoped({"target": interaction.user})

    # Right: store the snowflake ID
    await self.dispatch_scoped({"target_id": interaction.user.id})
    ```
