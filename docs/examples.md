# Examples

Working examples are in the [`examples/`](https://github.com/HollowTheSilver/CascadeUI/tree/main/examples) directory. Each is a discord.py cog that can be loaded into any bot.

## counter.py

Basic stateful counter with increment, decrement, and reset buttons. Demonstrates `StatefulView`, `StatefulButton`, and custom reducers.

**Command:** `/counter`

## themed_form.py

Theme switching, component wrappers (loading state, confirmation), pagination, form views with validation, and modal text input with per-field validation. Shows how to use the theming system, behavioral wrappers, and the validation module together.

**Commands:** `/profile`, `/themetest`, `/componenttest`, `/paginationtest`, `/formtest`, `/validatetest`

## persistence.py

SQLite-backed data persistence and `PersistentView` that survives bot restarts. Includes a persistent counter scoped per user and a role selector panel that stays interactive across restarts.

**Commands:** `/pcounter`, `/setup_roles`

## navigation.py

Navigation stack with push/pop between multi-level views. Demonstrates a main menu that pushes to settings and about pages, with a nested sub-page two levels deep.

**Command:** `/navtest`

## state_features.py

Per-user state scoping (independent counters per user), action batching (multiple dispatches with single notification), computed/derived values (cached totals), and event hooks (logging component interactions).

**Commands:** `/scopetest`, `/advancedtest`

## undo_redo.py

Undo/redo counter using `UndoMiddleware`. Shows stack depth in the embed so you can see exactly how the undo/redo stacks change with each action.

**Command:** `/undotest`

## Running the Examples

1. Create a bot on the [Discord Developer Portal](https://discord.com/developers/applications)
2. Install CascadeUI: `pip install -e ".[sqlite]"`
3. Load the example cogs in your bot:

```python
from cascadeui import setup_persistence
from cascadeui.persistence import SQLiteBackend

class MyBot(commands.Bot):
    async def setup_hook(self):
        # Load example cogs
        await self.load_extension("examples.counter")
        await self.load_extension("examples.themed_form")
        await self.load_extension("examples.persistence")
        await self.load_extension("examples.navigation")
        await self.load_extension("examples.state_features")
        await self.load_extension("examples.undo_redo")

        # Enable persistence (after loading cogs)
        await setup_persistence(self, backend=SQLiteBackend("cascadeui.db"))
```

4. Run your bot and use the slash commands
