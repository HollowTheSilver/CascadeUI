# Persistence

CascadeUI provides two persistence patterns through a single entry point: `setup_persistence()`.

## Setup

Call `setup_persistence()` once in your bot's `setup_hook`, **after loading your cogs**:

```python
from cascadeui import setup_persistence

class MyBot(commands.Bot):
    async def setup_hook(self):
        # Load cogs first -- imports register PersistentView subclasses
        await self.load_extension("cogs.dashboard")
        await self.load_extension("cogs.counter")

        # Then enable persistence
        await setup_persistence(self, file_path="bot_state.json")
```

!!! warning "Cog loading order matters"
    `setup_persistence` must be called **after** all cogs are loaded. When Python imports a module containing a `PersistentView` subclass, `__init_subclass__` registers it in the class registry. If `setup_persistence` runs first, the registry is empty and no views get restored.

### With or Without `bot`

```python
# Data-only persistence (no bot needed)
await setup_persistence(file_path="bot_state.json")

# Full persistence: data + view re-attachment
await setup_persistence(bot, file_path="bot_state.json")
```

- **Without `bot`**: Enables the `FileStorageBackend` and restores state from disk. Views with `state_key` can look up their saved data when re-invoked.
- **With `bot`**: Does everything above, plus re-attaches `PersistentView` instances to their original Discord messages so they stay interactive after a restart.

## Pattern 1: Data Persistence (re-invoke to restore)

Use a regular `StatefulView` with a `state_key` to persist data across view lifetimes:

```python
class CounterView(StatefulView):
    def __init__(self, context, user_id):
        super().__init__(
            context=context,
            state_key=f"counter:{user_id}",  # Stable identity for data
        )
        self.counter = 0

    async def send(self, **kwargs):
        # Restore saved counter value from state
        state = self.state_store.get_state()
        counters = state.get("application", {}).get("counters", {})
        saved = counters.get(self.state_key)
        if saved is not None:
            self.counter = saved
        return await super().send(
            embed=discord.Embed(title="Counter", description=f"Value: {self.counter}"),
            **kwargs,
        )
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
3. Fetches the original channel and message
4. Creates a new view instance and attaches it via `bot.add_view(view, message_id=...)`
5. Calls `on_restore(bot)` for any post-restore setup

**Requirements for PersistentView:**

- `state_key` is required (raises `ValueError` if not provided)
- All components must have explicit `custom_id` values (auto-generated IDs won't survive restarts)
- `timeout` is forced to `None` (persistent views never timeout)

### Stale Entry Handling

If things change while the bot is offline:

| Scenario | What happens |
|----------|-------------|
| Message deleted | Entry removed from state, won't try again |
| Channel deleted | Entry removed from state, won't try again |
| View class renamed/removed | Entry skipped but kept (in case the import is just missing temporarily) |

## How It Works Under the Hood

When a `PersistentView` is sent, it dispatches a `PERSISTENT_VIEW_REGISTERED` action that stores:

- `state_key` (lookup key)
- `class_name` (for reconstructing the view)
- `message_id`, `channel_id`, `guild_id` (for re-attaching to the message)

This gets persisted to disk along with all other state. On restart, `setup_persistence` reads this registry and rebuilds the views.

The `__init_subclass__` hook on `PersistentView` automatically registers every subclass in a class name -> class mapping. This is why cog loading order matters: the subclass must be imported (triggering `__init_subclass__`) before `setup_persistence` tries to look it up.

## File Storage

State is saved to a JSON file (default: `cascadeui_state.json`). Before every save, a `.bak` backup is created for recovery. The `StateSerializer` handles `datetime` and `set` objects; other non-serializable types raise a `TypeError`.
