# Examples

Working examples are in the [`examples/`](https://github.com/HollowTheSilver/CascadeUI/tree/main/examples) directory. Each is a discord.py cog that can be loaded into any bot.

---

## V2 Examples

These use the V2 component system (`StatefulLayoutView`, Containers, Sections, TextDisplay).

### v2_counter.py

Basic stateful counter with increment, decrement, and reset buttons. Demonstrates `StatefulLayoutView`, `StatefulButton`, custom reducers, and V2 helpers (`card`, `key_value`, `divider`).

**Command:** `/v2counter`

### v2_dashboard.py

Multi-tab server dashboard using `TabLayoutView`. Shows server info, member stats, and module toggles across three tabs. Demonstrates `action_section`, `toggle_section`, `key_value`, `alert`, tab switching, and session limiting.

**Command:** `/v2dashboard`

### v2_settings.py

Full settings menu showcasing most CascadeUI features with V2 components:

- **Session limiting** (`session_limit=1`, `session_scope="user_guild"`) to prevent duplicate panels
- **Scoped state** (`scope="user"`) for per-user settings
- **Navigation stack** (push/pop with `rebuild=` callback) for hub-and-spoke layout
- **Theming** with a live theme switcher
- **Undo/redo** for reverting notification preference toggles
- **Custom reducer** (`SETTINGS_UPDATED`) for domain-specific state management
- **Cross-view reactivity** between V2 settings and V1 settings via shared state keys

**Command:** `/v2settings`

### v2_form.py

Registration form using `FormLayoutView` with modal-based text input, dropdown selects, and per-field validation (regex, min/max length). Custom `_rebuild_display` for a styled form summary using `card`, `key_value`, and `divider`.

**Command:** `/v2form`

### v2_pagination.py

Paginated inventory viewer using `PaginatedLayoutView` with `from_data()`. Container-based page content with jump controls (first/last, go-to-page modal). Demonstrates `_build_extra_items()` for an exit button below navigation.

**Command:** `/v2pages`

### v2_wizard.py

Multi-step setup wizard using `WizardLayoutView`. Three steps with per-step validation, back/next navigation, and a finish handler. Uses `card`, `action_section`, `toggle_section`, and `alert` for step content.

**Command:** `/v2wizard`

### v2_persistence.py

SQLite-backed data persistence and `PersistentLayoutView` that survives bot restarts. Includes a persistent counter scoped per user and a role selector panel with exclusive-mode categories (selecting one role in a category auto-removes others). Running `/v2roles` again automatically cleans up the previous panel.

**Commands:** `/v2pcounter`, `/v2roles`

### v2_tictactoe.py

Two-player TicTacToe demonstrating multi-user interaction patterns. Features a challenge acceptance flow (opponent must accept before the game starts), dynamic board size (3x3 to 5x5), configurable win length (e.g. 3-in-a-row on a 5x5 board), Discord mentions, mutual rematch agreement (both players must confirm), forfeit tracking, and participant-aware session limiting via `register_participant()`. Uses `allowed_users` for restricting interaction to the two players and a custom reducer for tracking game statistics.

**Command:** `/tictactoe @user [size] [win]`

---

## V1 Examples (Classic)

These use the V1 component system (`StatefulView`, embeds, row-based layout).

### counter.py

Basic stateful counter with increment, decrement, and reset buttons. Demonstrates `StatefulView`, `StatefulButton`, and custom reducers.

**Command:** `/counter`

### settings_menu.py

Advanced settings menu with session limiting, scoped state, push/pop navigation, theming, undo/redo, state selectors, batched dispatch, and a custom reducer. The V1 counterpart to `v2_settings.py` — both share the same state keys for cross-view reactivity.

**Command:** `/settings`

### themed_form.py

Theme switching, component wrappers (loading state, confirmation), pagination, form views with validation, and modal text input with per-field validation.

**Commands:** `/profile`, `/themetest`, `/componenttest`, `/paginationtest`, `/formtest`, `/validatetest`

### persistence.py

SQLite-backed data persistence and `PersistentView` that survives bot restarts. Includes a persistent counter scoped per user and a role selector panel that stays interactive across restarts.

**Commands:** `/pcounter`, `/setup_roles`

### navigation.py

Navigation stack with push/pop between multi-level views. Demonstrates a main menu that pushes to settings and about pages, with a nested sub-page two levels deep.

**Command:** `/navtest`

### state_features.py

Per-user state scoping (independent counters per user), action batching (multiple dispatches with single notification), computed/derived values (cached totals), and event hooks (logging component interactions).

**Commands:** `/scopetest`, `/advancedtest`

### undo_redo.py

Undo/redo counter using `UndoMiddleware`. Shows stack depth in the embed so you can see exactly how the undo/redo stacks change with each action.

**Command:** `/undotest`

### ticket_system.py

A production-style support ticket system demonstrating most of CascadeUI's framework features:

- **PersistentView** for a ticket panel that survives bot restarts
- **Modal + Validation** for ticket creation with field-level error checking
- **PaginatedView** with `from_data`, `refresh_data()`, and `_build_extra_items()`
- **Custom reducers** (`TICKET_CREATED`, `TICKET_CLOSED`)
- **Live-updating list** via state subscriptions
- **Session limiting** on the ticket list view
- **Theming** with a custom "support" theme

**Commands:** `/ticket_setup`, `/my_tickets`

---

## Running the Examples

1. Create a bot on the [Discord Developer Portal](https://discord.com/developers/applications)
2. Install CascadeUI: `pip install pycascadeui[sqlite]`
3. Load the example cogs in your bot:

```python
from cascadeui import setup_persistence
from cascadeui.persistence import SQLiteBackend

class MyBot(commands.Bot):
    async def setup_hook(self):
        # V2 examples
        await self.load_extension("examples.v2_counter")
        await self.load_extension("examples.v2_dashboard")
        await self.load_extension("examples.v2_settings")
        await self.load_extension("examples.v2_form")
        await self.load_extension("examples.v2_pagination")
        await self.load_extension("examples.v2_wizard")
        await self.load_extension("examples.v2_persistence")
        await self.load_extension("examples.v2_tictactoe")

        # V1 examples
        await self.load_extension("examples.counter")
        await self.load_extension("examples.settings_menu")
        await self.load_extension("examples.themed_form")
        await self.load_extension("examples.persistence")
        await self.load_extension("examples.navigation")
        await self.load_extension("examples.state_features")
        await self.load_extension("examples.undo_redo")
        await self.load_extension("examples.ticket_system")

        # Enable persistence (after loading cogs)
        await setup_persistence(self, backend=SQLiteBackend("cascadeui.db"))
```

4. Run your bot and use the slash commands
