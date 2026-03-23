# Examples

Working examples are in the [`examples/`](https://github.com/HollowTheSilver/CascadeUI/tree/main/examples) directory. Each is a discord.py cog that can be loaded into any bot.

## counter.py

Basic stateful counter with increment, decrement, and reset buttons. Demonstrates `StatefulView`, `StatefulButton`, and custom reducers.

**Command:** `/counter`

## themed_form.py

Theme switching, component wrappers (loading state, confirmation), pagination, form views with validation, and modal text input with per-field validation. Shows how to use the theming system, behavioral wrappers, and the validation module together.

**Commands:** `/profile`, `/themetest`, `/componenttest`, `/paginationtest`, `/formtest`, `/validatetest`

## persistence.py

SQLite-backed data persistence and `PersistentView` that survives bot restarts. Includes a persistent counter scoped per user and a role selector panel that stays interactive across restarts. Running `/setup_roles` again automatically cleans up the previous panel.

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

## settings_menu.py

Advanced example showcasing multiple CascadeUI features working together in a realistic settings menu. Demonstrates:

- **Session limiting** (`session_limit=1`, `session_scope="user_guild"`) to prevent duplicate settings panels
- **Scoped state** (`scope="user"`) for per-user settings that persist across views
- **Navigation stack** (push/pop) for a hub-and-spoke menu layout with sub-pages
- **Theming** with a live theme switcher that applies changes to the embed in real time
- **Undo/redo** for reverting notification preference toggles
- **State selectors** for efficient re-rendering (only updates when the user's settings change)
- **Batched dispatch** for atomic multi-field updates
- **Custom reducer** (`SETTINGS_UPDATED`) for domain-specific state management

The hub view pushes to Appearance, Notifications, and Locale sub-views, each with a back button to return. Session limiting ensures that running `/settings` a second time exits the first panel before creating the new one.

**Command:** `/settings`

## ticket_system.py

A production-style support ticket system demonstrating most of CascadeUI's framework features working together in a single cog:

- **PersistentView** for a ticket panel that survives bot restarts
- **Modal + Validation** for ticket creation with field-level error checking (subject, description, priority)
- **PaginatedView** for paginated ticket lists via `from_data` factory, with `refresh_data()` for live updates and `_build_extra_items()` for a select menu below navigation
- **Ticket detail view** with live status updates via state selectors and inline close button
- **Custom reducers** (`TICKET_CREATED`, `TICKET_CLOSED`) for domain-specific state management
- **Live-updating list** -- the ticket list subscribes to `TICKET_CREATED`/`TICKET_CLOSED` and rebuilds its pages automatically when tickets change
- **State selectors** so the panel only re-renders when the open ticket count changes
- **Session limiting** on the ticket list view (one per user per guild)
- **Theming** with a custom "support" theme applied to all embeds

The panel is posted once per channel with `/ticket_setup`. Any user can create tickets via a modal, view their open tickets in a paginated list, select a ticket to see its full details, or close tickets through a select-and-confirm flow. Running `/ticket_setup` again in the same channel replaces the old panel automatically.

**Commands:** `/ticket_setup`, `/my_tickets`

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
        await self.load_extension("examples.settings_menu")
        await self.load_extension("examples.ticket_system")

        # Enable persistence (after loading cogs)
        await setup_persistence(self, backend=SQLiteBackend("cascadeui.db"))
```

4. Run your bot and use the slash commands
