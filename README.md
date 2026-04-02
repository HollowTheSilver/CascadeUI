<p align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/docs/assets/banner.png" alt="CascadeUI - A Redux-Inspired Framework for Discord.py" width="100%">
</p>

<p align="center">
  <a href="https://pypi.org/project/pycascadeui/"><img src="https://img.shields.io/pypi/v/pycascadeui?logo=pypi&logoColor=white" alt="PyPI"></a>
  <a href="https://pypi.org/project/pycascadeui/"><img src="https://img.shields.io/pypi/dm/pycascadeui?logo=pypi&logoColor=white&label=downloads" alt="Downloads"></a>
  <a href="https://github.com/HollowTheSilver/CascadeUI/stargazers"><img src="https://img.shields.io/github/stars/HollowTheSilver/CascadeUI?style=flat&logo=github&label=stars" alt="Stars"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12%20|%203.13%20|%203.14-blue.svg?logo=python&logoColor=white" alt="Python 3.10-3.14"></a>
  <a href="https://github.com/Rapptz/discord.py"><img src="https://img.shields.io/badge/discord.py-2.7+-738adb.svg?logo=discord&logoColor=white" alt="discord.py 2.7+"></a>
  <a href="https://discord.com/invite/9Xj68BpKRb"><img src="https://img.shields.io/discord/1405822635920855040?logo=discord&logoColor=white&label=Discord&color=5865F2" alt="Discord"></a>
  <a href="https://hollowthesilver.github.io/CascadeUI/"><img src="https://img.shields.io/badge/docs-GitHub%20Pages-8A2BE2?logo=readthedocs" alt="Docs"></a>
  <a href="https://github.com/psf/black"><img src="https://img.shields.io/badge/code%20style-black-000000.svg" alt="Code style: black"></a>
  <a href="https://github.com/HollowTheSilver/CascadeUI/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/HollowTheSilver/CascadeUI/ci.yml?logo=github&label=CI" alt="CI"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

<p align="center">
  Redux-inspired UI framework for <a href="https://github.com/Rapptz/discord.py">discord.py</a>.<br>
  Centralized state, dispatched actions, reducers, and subscriber notifications<br>
  for predictable state flow and composable UI patterns.
</p>

<p align="center">
  <a href="https://hollowthesilver.github.io/CascadeUI/"><strong>Read the full documentation</strong></a>
</p>

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-hero.gif" alt="CascadeUI V2 Dashboard" width="600">
</div>

---

## Features

#### State Management
- **Centralized State Store** -- Singleton store with dispatch/reducer cycle, action batching, computed values, event hooks, and subscriber filtering by action type + state selectors
- **Custom Reducers** -- Register your own action types with `@cascade_reducer`
- **Middleware** -- Intercept and transform actions; built-in debounced persistence, logging, and undo/redo

#### Views & Patterns
- **V2 Components** -- `StatefulLayoutView` with Container, Section, TextDisplay layouts; accent-colored cards, inline action buttons, and flexible component trees
- **V1 Components** -- `StatefulView` wrapping discord.py `View` with embeds and content
- **Pre-built Patterns** -- Tabs, Wizards, Forms, Pagination -- available in both V2 (`TabLayoutView`, `WizardLayoutView`, etc.) and V1 (`TabView`, `WizardView`, etc.)
- **Navigation Stack** -- Push/pop views to build multi-level menus on a single message
- **Session Limiting** -- Declarative per-view limits with automatic old-view cleanup
- **Undo/Redo** -- Snapshot-based state history per view session

#### Components & Theming
- **Stateful Components** -- `StatefulButton`, `StatefulSelect`, plus composites and behavioral wrappers (loading, confirmation, cooldown)
- **V2 Helpers** -- `card()`, `action_section()`, `toggle_section()`, `alert()`, `key_value()`, `gallery()` for concise V2 assembly
- **Form Validation** -- Built-in validators (`min_length`, `max_length`, `regex`, `choices`, `min_value`, `max_value`) with per-field error reporting
- **Theming** -- Global registry with per-view overrides; accent colors for V2 Containers, embed colors for V1

#### Infrastructure
- **Persistence** -- `setup_persistence()` for data + view survival across restarts; pluggable backends (JSON, SQLite, Redis)
- **DevTools** -- Built-in V2 tabbed state inspector with self-filtering and live auto-refresh

---

## Architecture

```
User clicks button  ->  Interaction callback  ->  dispatch(action)
     -> Middleware pipeline  ->  Reducer (immutable state update)
     -> Subscribers notified (filtered)  ->  Views re-render
```

All state lives in a single `StateStore` singleton. **Actions** are plain dicts describing what happened. **Reducers** receive the action and return new state immutably. **Subscribers** (views) are notified when state changes, filtered by action type and state selectors so views only re-render when their relevant slice actually changes.

This is the same unidirectional data flow pattern used by Redux and similar state management libraries, adapted for Discord's interaction-driven UI model.

---

## Quick Start

> [!TIP]
> Install from PyPI, then `import cascadeui` -- the package name on PyPI is `pycascadeui` but the import is `cascadeui`.

```bash
pip install pycascadeui

# Optional backends
pip install pycascadeui[sqlite]   # SQLite persistence via aiosqlite
pip install pycascadeui[redis]    # Redis persistence
```

**Requirements**: Python 3.10+ | discord.py 2.7+

<br>

### A counter in 30 seconds

Buttons live inside the container alongside the counter text. No separate embed required.

```python
import discord
from discord.ext import commands
from discord.ui import ActionRow, Container, TextDisplay
from cascadeui import StatefulLayoutView, StatefulButton

class CounterView(StatefulLayoutView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.count = 0
        self._build_ui()

    def _build_ui(self):
        self.clear_items()
        color = discord.Color.green() if self.count > 0 else discord.Color.red() if self.count < 0 else discord.Color.light_grey()
        self.add_item(Container(
            TextDisplay(f"# Counter\nValue: **{self.count}**"),
            ActionRow(
                StatefulButton(label="-1", style=discord.ButtonStyle.danger, callback=self.decrement),
                StatefulButton(label="+1", style=discord.ButtonStyle.success, callback=self.increment),
            ),
            accent_colour=color,
        ))
        self.add_exit_button()

    async def increment(self, interaction):
        await interaction.response.defer()
        self.count += 1
        self._build_ui()
        await self.message.edit(view=self)

    async def decrement(self, interaction):
        await interaction.response.defer()
        self.count -= 1
        self._build_ui()
        await self.message.edit(view=self)

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.hybrid_command()
async def counter(ctx):
    view = CounterView(context=ctx)
    await view.send()
```

---

## Feature Showcase

### Tabbed Dashboard

> Multiple containers with different accent colors, sections with inline action buttons, and tab-based navigation -- all in one message.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-dashboard.gif" alt="Tabbed Dashboard" width="600">
</div>

```python
from cascadeui import TabLayoutView, card, action_section, toggle_section, key_value

class DashboardView(TabLayoutView):
    session_limit = 1
    session_scope = "user_guild"
    session_policy = "replace"

    def __init__(self, *args, **kwargs):
        tabs = {
            "Overview": self.build_overview,
            "Modules": self.build_modules,
        }
        super().__init__(*args, tabs=tabs, **kwargs)

    async def build_overview(self):
        return [
            card("## Server Stats", key_value({"Members": 142, "Modules": "3/5"}), color=discord.Color.green()),
            card("## Quick Actions", action_section("Manage modules", label="Modules", callback=self._go_modules)),
        ]

    async def build_modules(self):
        return [Container(
            toggle_section("Moderation", active=True, callback=self._toggle_mod),
            toggle_section("Logging", active=False, callback=self._toggle_log),
        )]
```

[View full example](examples/v2_dashboard.py)

---

### Settings with Navigation Stack

> Push/pop between settings sub-pages on the same message. Accent colors change with the selected theme. Session limiting keeps one panel per user.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-settings.gif" alt="Settings Menu" width="600">
</div>

```python
from cascadeui import StatefulLayoutView, SessionLimitError

class SettingsHub(StatefulLayoutView):
    session_limit = 1
    session_scope = "user_guild"
    session_policy = "replace"
    scope = "user"

    async def go_appearance(self, interaction):
        await self.push(AppearanceView, interaction, rebuild=lambda v: v._build_ui())

class NotificationsView(StatefulLayoutView):
    enable_undo = True
    undo_limit = 10

    async def toggle_dm(self, interaction):
        await interaction.response.defer()
        await self.dispatch("SETTINGS_UPDATED", {"scope_key": f"user:{self.user_id}", ...})

    async def do_undo(self, interaction):
        await interaction.response.defer()
        await self.undo(interaction)
```

[View full example](examples/v2_settings.py)

---

### Live Cross-View Updates

> Dispatch a named action from any view and every subscriber reacts instantly. Here, changing settings in the V2 panel updates the V1 panel's hub live, and vice versa.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-cross-view-reactivity.gif" alt="Cross-View Reactivity" width="600">
</div>

---

### Session Limiting

> Declare `session_limit` on any view class to cap active instances per user, guild, or globally. The old panel is automatically exited when a new one opens.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-session-limiting.gif" alt="V2 Session Limiting" width="600">
</div>

```python
class DashboardView(TabLayoutView):
    session_limit = 1               # Only one open at a time
    session_scope = "user_guild"    # Per user per guild
    session_policy = "replace"      # Exit the old one, open the new one
```

Works with both V2 and V1 views. Use `session_policy = "reject"` to block the second attempt instead of replacing.

---

### Undo/Redo

> Snapshot-based undo/redo per view session. Two class attributes and you get full state history.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-undo-redo.gif" alt="Undo/Redo" width="600">
</div>

```python
class NotificationsView(StatefulLayoutView):
    enable_undo = True
    undo_limit = 10

    async def toggle_setting(self, interaction):
        await interaction.response.defer()
        await self.dispatch("SETTINGS_UPDATED", {"dm_notifications": not self.dm_enabled})

    async def undo_change(self, interaction):
        await interaction.response.defer()
        await self.undo(interaction)

    async def redo_change(self, interaction):
        await interaction.response.defer()
        await self.redo(interaction)
```

The `UndoMiddleware` captures application state snapshots before each reducer runs, scoped to the view's session. Works with any dispatched action.

---

### Pagination

> Build paginated views from raw data with `from_data()`. Pages are V2 containers with accent colors. Navigation includes jump buttons and a go-to-page modal when the page count exceeds the threshold.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-pagination.gif" alt="Pagination" width="600">
</div>

```python
from cascadeui import PaginatedLayoutView

def format_page(items):
    lines = [f"**{item['name']}** | {item['rarity']} | {item['value']}g" for item in items]
    return [Container(
        TextDisplay("## Inventory"),
        Separator(),
        TextDisplay("\n".join(lines)),
        accent_colour=discord.Color.blue(),
    )]

view = await PaginatedLayoutView.from_data(
    items=all_items,
    per_page=4,
    formatter=format_page,
    context=ctx,
)
await view.send()
```

[View full example](examples/v2_pagination.py)

---

### Persistence

CascadeUI supports two persistence patterns. Both use pluggable backends: **JSON** (default), **SQLite** (`aiosqlite`), and **Redis** (`redis.asyncio`).

```python
# Enable persistence once in your bot's setup_hook:
async def setup_hook(self):
    await setup_persistence(bot=self, backend=SQLiteBackend("cascadeui.db"))
```

#### View persistence: survives restarts

> Post a panel once and it stays interactive forever. The bot restarts, the buttons still work. No need to re-send.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-persistence-restart.gif" alt="View Persistence" width="600">
</div>

```python
from cascadeui import PersistentLayoutView, StatefulButton

class RoleSelectorPanel(PersistentLayoutView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(Container(
            TextDisplay("### Color Roles"),
            Separator(),
            ActionRow(
                StatefulButton(label="Red", custom_id="roles:color:red", callback=self.toggle_red),
                StatefulButton(label="Blue", custom_id="roles:color:blue", callback=self.toggle_blue),
            ),
            accent_colour=discord.Color.red(),
        ))

# Post once, works forever. Re-running replaces the old panel:
view = RoleSelectorPanel(context=ctx, state_key=f"roles:panel:{ctx.guild.id}")
await view.send()
```

#### Data persistence: restores state

> State is saved to disk automatically and restored when the command is invoked again. Close the bot, restart, run the command, and your previous data is still there.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-persistence-data.gif" alt="Data Persistence" width="600">
</div>

```python
from cascadeui import StatefulLayoutView, setup_persistence

# Any view with a state_key gets its state saved and restored:
class SettingsView(StatefulLayoutView):
    scope = "user"

    async def save_preference(self, interaction):
        await self.dispatch("PREF_UPDATED", {"theme": "dark"})
        # State is automatically persisted via DebouncedPersistence middleware
        # Next time the user opens this view, their preference is restored
```

Both `PersistentLayoutView` (V2) and `PersistentView` (V1) use the same persistence system.

[View full example](examples/v2_persistence.py) | [Full guide](https://hollowthesilver.github.io/CascadeUI/guide/persistence/)

---

### Forms & Validation

> Field definitions produce select menus and boolean toggles automatically. Validation runs on submit with per-field error reporting.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-form.gif" alt="Forms" width="600">
</div>

```python
from cascadeui import FormLayoutView, choices

class FeedbackForm(FormLayoutView):
    def __init__(self, *args, **kwargs):
        fields = [
            {"id": "category", "label": "Category", "type": "select", "required": True,
             "options": [{"label": "Bug", "value": "bug"}, {"label": "Feature", "value": "feature"}],
             "validators": [choices(["bug", "feature"])]},
            {"id": "urgent", "label": "Urgent", "type": "boolean"},
        ]
        super().__init__(*args, title="Feedback", fields=fields, on_submit=self.handle, **kwargs)
```

[View full example](examples/v2_form.py)

---

### Setup Wizard

> Step-by-step flows with validation gates. Users can't proceed until required fields are filled. Each step builds its own V2 component tree.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-wizard.gif" alt="Wizard" width="600">
</div>

```python
from cascadeui import WizardLayoutView, card

class SetupWizard(WizardLayoutView):
    def __init__(self, *args, **kwargs):
        steps = [
            {"name": "Welcome", "builder": self.build_welcome},
            {"name": "Config", "builder": self.build_config, "validator": self.validate_config},
            {"name": "Confirm", "builder": self.build_confirm},
        ]
        super().__init__(*args, steps=steps, on_finish=self.finish, **kwargs)

    async def validate_config(self):
        return (self._mod_level is not None), "Please select a moderation level."

    async def finish(self, interaction):
        await interaction.response.send_message("Setup complete!", ephemeral=True)
```

[View full example](examples/v2_wizard.py)

---

### Theming

> Register themes globally, apply per-view or as a default. V2 containers get accent colors, V1 embeds get embed colors -- same theme definition.

```python
from cascadeui import Theme, register_theme, set_default_theme

my_theme = Theme("custom", {
    "primary_color": discord.Color.purple(),
    "accent_colour": discord.Color.purple(),  # V2 Container accent
    "header_emoji": ">>",
    "footer_text": "My Bot",
})
register_theme(my_theme)
set_default_theme("custom")
```

[Full guide](https://hollowthesilver.github.io/CascadeUI/guide/theming/)

---

### Ticket System (Demo)

> A full support ticket system combining PersistentView, Modal with validation, PaginatedView, custom reducers, state selectors, and theming in a single cog.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v1-ticket-system.gif" alt="Ticket System" width="600">
</div>

[View full example](examples/ticket_system.py)

---

## V1 Components (Classic)

CascadeUI fully supports V1 components (View + Embeds). V1 is not deprecated -- use it when you need embed formatting (inline fields, author/footer/timestamp) or prefer the traditional layout. Every feature above (navigation, session limiting, persistence, undo/redo, theming) works identically with V1 views.

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v1-navigation.gif" alt="V1 Navigation" width="600">
</div>

V1 equivalents: `StatefulView`, `FormView`, `PaginatedView`, `TabView`, `WizardView`, `PersistentView`.

See the [V1 examples](examples/) for complete working code: [counter](examples/counter.py), [navigation](examples/navigation.py), [settings](examples/settings_menu.py), [pagination](examples/themed_form.py), [persistence](examples/persistence.py), [tickets](examples/ticket_system.py).

---

## Examples

Working examples in the [`examples/`](examples/) directory, each a discord.py cog:

#### V2 Components
| Example | What it covers |
|---------|---------------|
| **[v2_counter.py](examples/v2_counter.py)** | Counter with accent colors and integrated controls |
| **[v2_dashboard.py](examples/v2_dashboard.py)** | Tabs, sections, toggles, session limiting, V2 helpers |
| **[v2_settings.py](examples/v2_settings.py)** | Navigation, undo/redo, theming, scoped state, session limiting |
| **[v2_pagination.py](examples/v2_pagination.py)** | PaginatedLayoutView with from_data and accent-colored pages |
| **[v2_form.py](examples/v2_form.py)** | FormLayoutView with select fields and validation |
| **[v2_wizard.py](examples/v2_wizard.py)** | WizardLayoutView with step validation and finish callback |
| **[v2_persistence.py](examples/v2_persistence.py)** | PersistentLayoutView role selector with multi-category panels |

#### V1 Components (Classic)
| Example | What it covers |
|---------|---------------|
| **[counter.py](examples/counter.py)** | Basic stateful counter with custom reducer |
| **[themed_form.py](examples/themed_form.py)** | Themes, wrappers, pagination, form/modal validation |
| **[persistence.py](examples/persistence.py)** | SQLite persistence and PersistentView |
| **[navigation.py](examples/navigation.py)** | Push/pop navigation stack |
| **[state_features.py](examples/state_features.py)** | Scoping, batching, computed values, hooks |
| **[undo_redo.py](examples/undo_redo.py)** | Undo/redo with stack depth display |
| **[settings_menu.py](examples/settings_menu.py)** | Session limiting, navigation, undo/redo, theming |
| **[ticket_system.py](examples/ticket_system.py)** | PersistentView, modals, pagination, custom reducers |

---

## Documentation

> [!IMPORTANT]
> The full documentation site covers state management, views, components, persistence, theming, middleware, devtools, and API reference.

**[hollowthesilver.github.io/CascadeUI](https://hollowthesilver.github.io/CascadeUI/)**

---

## Support

Questions, bug reports, or just want to see what's next?

- **Discord**: [Join the server](https://discord.com/invite/9Xj68BpKRb) for help, discussion, and updates
- **Issues**: [GitHub Issues](https://github.com/HollowTheSilver/CascadeUI/issues) for bug reports and feature requests

---

## Development

```bash
git clone https://github.com/HollowTheSilver/CascadeUI.git
cd CascadeUI
pip install -e ".[dev]"

pytest tests/ -v                                   # run tests
black --line-length 100 cascadeui/                 # format
isort --profile black --line-length 100 cascadeui/ # sort imports
```

---

<p align="center">
  MIT License -- see <a href="LICENSE">LICENSE</a> for details.
</p>
