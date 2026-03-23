# Persistence

CascadeUI provides two persistence patterns through a single entry point: `setup_persistence()`.

## Setup

Call `setup_persistence()` once in your bot's `setup_hook`, **after loading your cogs**:

```python
from cascadeui import setup_persistence
from cascadeui.persistence import SQLiteBackend

class MyBot(commands.Bot):
    async def setup_hook(self):
        # Load cogs first -- imports register PersistentView subclasses
        await self.load_extension("cogs.dashboard")
        await self.load_extension("cogs.counter")

        # Then enable persistence
        await setup_persistence(self, backend=SQLiteBackend("cascadeui.db"))
```

!!! warning "Cog loading order matters"
    `setup_persistence` must be called **after** all cogs are loaded. When Python imports a module containing a `PersistentView` subclass, `__init_subclass__` registers it in the class registry. If `setup_persistence` runs first, the registry is empty and no views get restored.

### With or Without `bot`

```python
# Data-only persistence (no bot needed)
await setup_persistence(backend=SQLiteBackend("cascadeui.db"))

# Full persistence: data + view re-attachment
await setup_persistence(bot, backend=SQLiteBackend("cascadeui.db"))
```

- **Without `bot`**: Enables the storage backend and restores state from disk. Views with `state_key` can look up their saved data when re-invoked.
- **With `bot`**: Does everything above, plus re-attaches `PersistentView` instances to their original Discord messages so they stay interactive after a restart.

## Storage Backends

### JSON File (built-in)

No extra dependencies. Good for development and small bots:

```python
await setup_persistence(bot, file_path="bot_state.json")
```

Before every save, a `.bak` backup is created for recovery.

### SQLite (recommended)

Requires `aiosqlite`. Uses WAL mode for concurrent reads and avoids file locking issues on Windows:

```bash
pip install pycascadeui[sqlite]
```

```python
from cascadeui.persistence import SQLiteBackend

await setup_persistence(bot, backend=SQLiteBackend("cascadeui.db"))
```

### Redis

Requires `redis` (with async support). Useful for bots running across multiple processes or machines:

```bash
pip install pycascadeui[redis]
```

```python
from cascadeui.persistence import RedisBackend

await setup_persistence(bot, backend=RedisBackend(url="redis://localhost"))
```

### Custom Backend

Implement the `StorageBackend` interface:

```python
class MyBackend:
    async def save_state(self, state: dict) -> bool:
        # Serialize and store. Return True on success.
        ...

    async def load_state(self) -> dict:
        # Load and deserialize. Return empty dict if no saved state.
        ...
```

### Migrating Between Backends

Move state from one backend to another:

```python
from cascadeui.persistence import migrate_storage, FileStorageBackend, SQLiteBackend

await migrate_storage(
    source=FileStorageBackend("old_state.json"),
    target=SQLiteBackend("cascadeui.db"),
)
```

## Pattern 1: Data Persistence (re-invoke to restore)

Use a regular `StatefulView` with a `state_key` to persist data across view lifetimes:

```python
class CounterView(StatefulView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Restore saved counter value from state
        store = get_store()
        counters = store.state.get("application", {}).get("counters", {})
        self.counter = counters.get(self.state_key, 0)
```

The view will timeout normally, but its data stays on disk. When the user runs the command again, the new view instance reads the saved data and picks up where they left off.

**Key concepts:**

- `state_key` provides a stable identity for data lookup (unlike `self.id` which is a new UUID each time)
- Scope per-user with `state_key=f"counter:{user_id}"`, per-guild with `state_key=f"counter:{guild_id}"`, etc.
- The view itself is recreated each time, only the data persists

## Pattern 2: View Persistence (survive bot restarts)

Use `PersistentView` for views that should stay interactive across bot restarts without user re-invocation:

```python
from cascadeui import PersistentView, StatefulButton

class RoleSelectorView(PersistentView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(StatefulButton(
            label="Get Role",
            custom_id="roles:get",  # Required for persistent views
            callback=self.give_role,
        ))

    async def give_role(self, interaction):
        # Handle role assignment
        ...

    async def on_restore(self, bot):
        # Optional: runs after the view is restored on restart
        ...
```

Send it once (typically from an admin command):

```python
@bot.hybrid_command()
async def setup_roles(ctx):
    view = RoleSelectorView(context=ctx, state_key="role_selector:main")
    await view.send(embed=discord.Embed(title="Role Selector"))
```

After a bot restart, `setup_persistence(bot)` automatically:

1. Reads the persistent view registry from saved state
2. Looks up the `RoleSelectorView` class by name
3. Fetches the original channel and message (skipping non-messageable channels)
4. Creates a new view instance and attaches it via `bot.add_view(view, message_id=...)`
5. Restores identity fields (`user_id`, `guild_id`) from the saved entry so session limiting works correctly after restart
6. Calls `on_restore(bot)` for any post-restore setup

**Requirements for PersistentView:**

- `state_key` is required (raises `ValueError` if not provided)
- All components must have explicit `custom_id` values (auto-generated IDs won't survive restarts)
- `timeout` is forced to `None` (persistent views never timeout)

!!! info "owner_only defaults to False"
    `StatefulView` defaults to `owner_only = True`, meaning only the user who created the view can interact with it. `PersistentView` flips this to `False` because persistent views are typically shared panels (role selectors, ticket systems, dashboards) that any user should be able to use.

    If your persistent view should be restricted to its creator, set it explicitly:

    ```python
    class PrivateDashboard(PersistentView):
        owner_only = True  # Override the PersistentView default
    ```

!!! danger "PersistentView cannot be ephemeral"
    `PersistentView.send(ephemeral=True)` raises `ValueError`. Ephemeral messages have no permanent message ID and cannot be re-attached after a bot restart. This is a hard constraint from Discord's API.

### Stale Entry Handling

If things change while the bot is offline:

| Scenario | What happens |
|----------|-------------|
| Message deleted | Entry removed from state, won't try again |
| Channel deleted | Entry removed from state, won't try again |
| Channel is non-messageable (e.g. category, forum) | Entry removed from state, won't try again |
| View class renamed/removed | Entry skipped but kept (in case the import is just missing temporarily) |

## How It Works Under the Hood

When a `PersistentView` is sent, it dispatches a `PERSISTENT_VIEW_REGISTERED` action that stores:

- `state_key` (lookup key)
- `class_name` (for reconstructing the view)
- `message_id`, `channel_id`, `guild_id`, `user_id` (for re-attaching and session indexing)

This gets persisted to disk along with all other state. On restart, `setup_persistence` reads this registry, rebuilds the views, and restores their identity fields so session limiting works correctly across restarts.

!!! warning "One message per state_key"
    The persistent view registry tracks one message per `state_key`. If you send a second view with the same `state_key`, the framework automatically exits the previous view instance (unsubscribing, unregistering, and disabling its components) and overwrites the registry entry. If the previous instance is no longer alive (e.g., from a prior bot session that wasn't restored), the old message's components are removed directly. Design your `state_key` values to be unique per intended instance (e.g., `"roles:main"` for a single panel, or `f"profile:{user_id}"` for per-user views).

The `__init_subclass__` hook on `PersistentView` automatically registers every subclass in a class name -> class mapping. This is why cog loading order matters: the subclass must be imported (triggering `__init_subclass__`) before `setup_persistence` tries to look it up.
