<p align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/docs/assets/banner.png" alt="CascadeUI — A Redux-Inspired Framework for Discord.py" width="100%">
</p>

<p align="center">
  <a href="https://pypi.org/project/pycascadeui/"><img src="https://img.shields.io/pypi/v/pycascadeui?logo=pypi&logoColor=white" alt="PyPI"></a>
  <a href="https://pypi.org/project/pycascadeui/"><img src="https://img.shields.io/pypi/dm/pycascadeui?logo=pypi&logoColor=white&label=downloads" alt="Downloads"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12%20|%203.13%20|%203.14-blue.svg?logo=python&logoColor=white" alt="Python 3.10-3.14"></a>
  <a href="https://github.com/Rapptz/discord.py"><img src="https://img.shields.io/badge/discord.py-2.7+-738adb.svg?logo=discord&logoColor=white" alt="discord.py 2.7+"></a>
  <a href="https://github.com/HollowTheSilver/CascadeUI/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/HollowTheSilver/CascadeUI/ci.yml?logo=github&label=CI" alt="CI"></a>
  <a href="https://github.com/psf/black"><img src="https://img.shields.io/badge/code%20style-black-000000.svg" alt="Code style: black"></a>
  <a href="https://hollowthesilver.github.io/CascadeUI/"><img src="https://img.shields.io/badge/docs-GitHub%20Pages-8A2BE2?logo=readthedocs" alt="Docs"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

<p align="center">
  Redux-inspired UI framework for <a href="https://github.com/Rapptz/discord.py">discord.py</a>.<br>
  Centralized state, dispatched actions, reducers, and subscriber notifications<br>
  for predictable state flow and composable UI patterns.
</p>

> [!NOTE]
> This initial release targets **Discord V1 Components** (Views, Buttons, Selects, Modals). Full V2 Component support (LayoutView, Container, Section, TextDisplay, etc.) is planned for the next major release.

<p align="center">
  <a href="https://hollowthesilver.github.io/CascadeUI/"><strong>Read the full documentation</strong></a>
</p>

![CascadeUI Demo](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/hero-demo.gif)

---

## Features

#### State Management
- **Centralized State Store** -- Singleton store with dispatch/reducer cycle, action batching, computed values, event hooks, and subscriber filtering by action type + state selectors
- **Custom Reducers** -- Register your own action types with `@cascade_reducer`
- **Middleware** -- Intercept and transform actions; built-in debounced persistence, logging, and undo/redo

#### Views & Patterns
- **Stateful Views** -- Wraps discord.py `View` with lifecycle management, auto-defer safety net, interaction ownership, navigation stack (push/pop), state scoping, and undo/redo
- **Session Limiting** -- Declarative per-view limits (`session_limit`, `session_scope`, `session_policy`) with automatic old-view cleanup
- **Pre-built Patterns** -- `FormView`, `PaginatedView` (jump buttons + go-to-page modal), `TabView`, `WizardView`

#### Components & Theming
- **Stateful Components** -- `StatefulButton`, `StatefulSelect`, plus composites (`ConfirmationButtons`, `ToggleGroup`, `ProgressBar`) and behavioral wrappers (loading, confirmation, cooldown)
- **Form Validation** -- Built-in validators (`min_length`, `max_length`, `regex`, `choices`, `min_value`, `max_value`) with per-field error reporting
- **Theming** -- Global registry with per-view overrides; built-in default, dark, and light themes

#### Infrastructure
- **Persistence** -- `setup_persistence()` for data + view survival across restarts; pluggable backends (JSON, SQLite, Redis)
- **DevTools** -- Built-in state inspector with paginated embed output

---

## Quick Start

> [!TIP]
> Install from PyPI, then `import cascadeui` -- the package name on PyPI is `pycascadeui` but the import stays `cascadeui`.

```bash
pip install pycascadeui

# Optional backends
pip install pycascadeui[sqlite]   # SQLite persistence via aiosqlite
pip install pycascadeui[redis]    # Redis persistence
```

**Requirements**: Python 3.10+ | discord.py 2.7+

<br>

### A Counter in 30 Lines

```python
import copy, discord
from discord.ext import commands
from cascadeui import StatefulView, StatefulButton, cascade_reducer

@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    new_state = copy.deepcopy(state)
    new_state.setdefault("application", {}).setdefault("counters", {})
    vid = action["payload"]["view_id"]
    new_state["application"]["counters"][vid] = action["payload"]["value"]
    return new_state

class CounterView(StatefulView):
    def __init__(self, context):
        super().__init__(context=context)
        self.count = 0
        self.add_item(StatefulButton(label="+1", style=discord.ButtonStyle.primary, callback=self.increment))
        self.add_item(StatefulButton(label="-1", style=discord.ButtonStyle.danger, callback=self.decrement))
        self.add_exit_button()

    async def _update(self, interaction, delta):
        await interaction.response.defer()
        self.count += delta
        await self.dispatch("COUNTER_UPDATED", {"view_id": self.id, "value": self.count})
        if self.message:
            await self.message.edit(embed=discord.Embed(title="Counter", description=f"Value: {self.count}"), view=self)

    async def increment(self, interaction): await self._update(interaction, 1)
    async def decrement(self, interaction): await self._update(interaction, -1)

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.hybrid_command()
async def counter(ctx):
    view = CounterView(context=ctx)
    await view.send(embed=discord.Embed(title="Counter", description="Value: 0"))
```

---

## Feature Showcase

### Navigation Stack

> Push and pop views to build multi-level menus. Each level edits the same message.

![Navigation Stack](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/navigation.gif)

```python
class MainMenu(StatefulView):
    async def go_settings(self, interaction):
        await interaction.response.defer()
        new_view = await self.push(SettingsView, interaction)
        await interaction.edit_original_response(embed=new_view.build_embed(), view=new_view)

class SettingsView(StatefulView):
    async def go_back(self, interaction):
        await interaction.response.defer()
        prev_view = await self.pop(interaction)
        await interaction.edit_original_response(embed=prev_view.build_embed(), view=prev_view)
```

[Full guide](https://hollowthesilver.github.io/CascadeUI/guide/views/#navigation-stack)

---

### Pagination

> Build paginated views from raw data. Pages above `jump_threshold` (default 5) get first/last buttons and a go-to-page modal.

![Pagination](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/pagination.gif)

```python
view = await PaginatedView.from_data(
    items=all_items,
    per_page=10,
    formatter=lambda chunk: discord.Embed(
        title="Items",
        description="\n".join(str(i) for i in chunk),
    ),
    context=ctx,
)
await view.send()
```

[Full guide](https://hollowthesilver.github.io/CascadeUI/guide/views/#paginatedview)

---

### Session Limiting

> Prevent duplicate views from piling up. Open `/settings` twice and the old panel closes automatically.

![Session Limiting](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/session-limiting.gif)

```python
class SettingsView(StatefulView):
    session_limit = 1
    session_scope = "user_guild"
    session_policy = "replace"  # or "reject"
```

[Full guide](https://hollowthesilver.github.io/CascadeUI/guide/views/#session-limiting)

---

### Persistence

> Persistent data keeps your settings and records intact between sessions.

![Persistent Data](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/persistence-data.gif)

> Persistent views keep their buttons working even after a full bot restart.

![Surviving Restarts](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/persistence-restart.gif)

```python
# In your bot's setup_hook:
async def setup_hook():
    await setup_persistence(bot=bot, backend=SQLiteBackend("cascadeui.db"))

# Views with custom_id components survive restarts:
class RoleSelector(PersistentView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(StatefulButton(
            label="Get Role",
            custom_id="roles:get",  # required for persistent views
            callback=self.toggle_role,
        ))
```

[Full guide](https://hollowthesilver.github.io/CascadeUI/guide/persistence/)

---

### Ticket System (Demo)

> A full support ticket system combining PersistentView, Modal with validation, PaginatedView, custom reducers, state selectors, and theming in a single cog.

![Ticket System](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/ticket-system.gif)

```python
class TicketPanelView(PersistentView):
    subscribed_actions = {"TICKET_CREATED", "TICKET_CLOSED"}

    def state_selector(self, state):
        # Only re-render when open ticket count changes
        tickets = state.get("application", {}).get("tickets", {}).get(guild_id, [])
        return sum(1 for t in tickets if t["status"] == "open")
```

[View full example](examples/ticket_system.py)

---

### Undo/Redo

> Snapshot-based undo/redo per view session. Two class attributes and you get full state history.

```python
class NotificationSettings(StatefulView):
    enable_undo = True
    undo_limit = 10

    async def do_undo(self, interaction):
        await interaction.response.defer()
        await self.undo(interaction)

    async def do_redo(self, interaction):
        await interaction.response.defer()
        await self.redo(interaction)
```

[Full guide](https://hollowthesilver.github.io/CascadeUI/guide/views/#undoredo)

---

### Theming

> Register themes globally, apply per-view or as a default. Embed colors update instantly.

```python
my_theme = Theme("custom", {
    "primary_color": discord.Color.purple(),
    "header_emoji": ">>",
    "footer_text": "My Bot",
})
register_theme(my_theme)
set_default_theme("custom")
```

[Full guide](https://hollowthesilver.github.io/CascadeUI/guide/theming/)

---

## Architecture

```
User clicks button  →  Interaction callback  →  dispatch(action)
     → Middleware pipeline  →  Reducer (immutable state update)
     → Subscribers notified (filtered)  →  Views re-render
```

All state lives in a single `StateStore` singleton. **Actions** are plain dicts describing what happened. **Reducers** receive the action and return new state immutably. **Subscribers** (views) are notified when state changes, filtered by action type and state selectors so views only re-render when their relevant slice actually changes.

This is the same unidirectional data flow pattern used by Redux and similar state management libraries, adapted for Discord's interaction-driven UI model.

---

## Examples

Working examples in the [`examples/`](examples/) directory, each a discord.py cog:

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
