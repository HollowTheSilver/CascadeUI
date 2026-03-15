# CascadeUI

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![discord.py 2.1+](https://img.shields.io/badge/discord.py-2.1+-738adb.svg)](https://github.com/Rapptz/discord.py)

> **Note**: CascadeUI is currently in active development and has not yet been officially released. Installation is source-only until the package is published on PyPI.

Stateful UI components and state management for [discord.py](https://github.com/Rapptz/discord.py).

CascadeUI brings a Redux-inspired architecture to Discord bot interfaces. Views, buttons, selects, and forms are backed by a centralized state store with dispatched actions, reducers, and subscriber notifications. The result is predictable state flow and composable UI patterns that scale beyond simple one-off views.

---

## Key Features

- **Centralized State Store**
  - Singleton store with dispatch/reducer cycle
  - Action history for debugging
  - Subscriber filtering by action type and state selectors

- **Stateful Views**
  - Wraps discord.py `View` with automatic state integration
  - Lifecycle management: send, interact, timeout, cleanup
  - View-to-view transitions with navigation history
  - Pre-built patterns: `TabView`, `WizardView`, `FormView`, `PaginatedView`

- **Stateful Components**
  - `StatefulButton` and `StatefulSelect` with automatic action dispatching
  - Composite components: `ConfirmationButtons`, `PaginationControls`, `FormLayout`, `ToggleGroup`
  - Behavioral wrappers: loading states, confirmation prompts, per-user cooldowns
  - Utilities: `ProgressBar` for visual progress in embeds

- **DevTools**
  - Built-in state inspector with paginated embed output
  - View active views, sessions, action history, store configuration
  - Drop-in cog or use `StateInspector` directly

- **Theming**
  - Global theme registry with per-view overrides
  - Apply colors, emojis, and footer text to embeds in one call
  - Built-in themes: default, dark, light

- **Persistence**
  - Decorator-based state persistence to JSON
  - Automatic save on state change, restore on startup

- **Middleware**
  - Intercept and transform actions in the dispatch pipeline
  - Built-in: debounced persistence, action logging
  - Write custom middleware for rate limiting, validation, analytics

- **Custom Reducers**
  - Register your own action types with `@cascade_reducer`
  - Immutable state updates with `copy.deepcopy`

---

## Quick Start

### Installation

```bash
# Install from source (PyPI coming soon)
git clone https://github.com/HollowTheSilver/CascadeUI.git
cd CascadeUI
pip install -e .
```

**Requirements**: Python 3.10+ | discord.py 2.1+

### A Counter in 30 Lines

```python
import discord
from discord.ext import commands
from cascadeui import StatefulView, StatefulButton, cascade_reducer
import copy

@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    new_state = copy.deepcopy(state)
    new_state.setdefault("application", {}).setdefault("counters", {})
    view_id = action["payload"]["view_id"]
    new_state["application"]["counters"][view_id] = action["payload"]["counter"]
    return new_state

class CounterView(StatefulView):
    def __init__(self, context):
        super().__init__(context=context)
        self.counter = 0
        self.add_item(StatefulButton(label="+1", style=discord.ButtonStyle.primary, callback=self.increment))
        self.add_item(StatefulButton(label="-1", style=discord.ButtonStyle.danger, callback=self.decrement))
        self.add_exit_button()

    async def increment(self, interaction):
        await interaction.response.defer()
        self.counter += 1
        await self.dispatch("COUNTER_UPDATED", {"view_id": self.id, "counter": self.counter})
        if self.message:
            await self.message.edit(embed=discord.Embed(title="Counter", description=f"Value: {self.counter}"), view=self)

    async def decrement(self, interaction):
        await interaction.response.defer()
        self.counter -= 1
        await self.dispatch("COUNTER_UPDATED", {"view_id": self.id, "counter": self.counter})
        if self.message:
            await self.message.edit(embed=discord.Embed(title="Counter", description=f"Value: {self.counter}"), view=self)

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.hybrid_command()
async def counter(ctx):
    view = CounterView(context=ctx)
    await view.send(embed=discord.Embed(title="Counter", description="Value: 0"))
```

---

## Core Concepts

### State Store

A singleton `StateStore` holds all application state and coordinates updates through a dispatch/reducer cycle. Actions are plain dicts with a `type` and `payload`. Reducers transform state immutably. Subscribers receive filtered notifications when relevant state changes.

```python
from cascadeui import get_store

store = get_store()
store.register_reducer("MY_ACTION", my_reducer)
await store.dispatch("MY_ACTION", {"key": "value"})
```

### State Selectors

Selectors let views subscribe to specific slices of state. Without a selector, a view's `update_from_state()` fires on every matching action. With a selector, it only fires when the selected value actually changes.

```python
class CounterView(StatefulView):
    subscribed_actions = {"COUNTER_UPDATED"}

    def state_selector(self, state):
        # Only re-render when MY counter changes, not anyone else's
        return state.get("application", {}).get("counters", {}).get(self.state_key)

    async def update_from_state(self, state):
        counter = self.state_selector(state)
        if self.message and counter is not None:
            await self.message.edit(embed=discord.Embed(title=f"Count: {counter}"))
```

You can also use selectors at the store level for non-view subscribers:

```python
store.subscribe("my-listener", callback, selector=lambda s: s["application"]["score"])
```

### Views

`StatefulView` wraps discord.py's `View` with state integration, lifecycle management, and task tracking. Use `view.send()` to display a view, which handles state registration and message tracking automatically.

```python
class MyView(StatefulView):
    def __init__(self, context):
        super().__init__(context=context)
        # add buttons, selects, etc.

    async def update_from_state(self, state):
        # react to state changes (optional override)
        pass

view = MyView(context=ctx)
await view.send(content="Hello")
```

Views automatically clean up on timeout (disabling components and notifying the state store) and support transitions between views via `view.transition_to(OtherView)`.

### View Patterns

CascadeUI includes pre-built view patterns for common UI layouts:

**TabView** - button-based tab switching where each tab renders its own content:

```python
from cascadeui import TabView

class SettingsView(TabView):
    def __init__(self, context):
        tabs = {
            "General": self.general_tab,
            "Audio": self.audio_tab,
            "Display": self.display_tab,
        }
        super().__init__(context=context, tabs=tabs)

    async def general_tab(self, embed):
        embed.description = "General settings here"
        return embed
```

**WizardView** - multi-step form with Back/Next/Finish navigation and per-step validation:

```python
from cascadeui import WizardView

class SetupWizard(WizardView):
    def __init__(self, context):
        steps = [
            {"name": "Welcome", "builder": self.welcome_step},
            {"name": "Config", "builder": self.config_step, "validator": self.validate_config},
            {"name": "Done", "builder": self.done_step},
        ]
        super().__init__(context=context, steps=steps, on_finish=self.finish)
```

### Components

`StatefulButton` and `StatefulSelect` extend discord.py's built-in components with automatic state dispatching. Every interaction triggers a `COMPONENT_INTERACTION` action in the store.

Composite components group related items together:

```python
from cascadeui import ConfirmationButtons, PaginationControls

confirmation = ConfirmationButtons(on_confirm=my_handler, on_cancel=my_cancel)
confirmation.add_to_view(my_view)

pagination = PaginationControls(page_count=5, on_page_change=handle_page)
pagination.add_to_view(my_view)
```

**ToggleGroup** - radio-button-like selection where only one option is active at a time:

```python
from cascadeui import ToggleGroup

group = ToggleGroup(
    options=["Easy", "Medium", "Hard"],
    on_select=difficulty_handler,
    default="Medium",
)
group.add_to_view(my_view)
```

**ProgressBar** - text-based progress indicator for embed fields (not a discord component):

```python
from cascadeui import ProgressBar

bar = ProgressBar(total=100, width=20)
embed.add_field(name="Progress", value=bar.render(65))
# Output: █████████████░░░░░░░ 65%
```

### Component Wrappers

Wrappers modify component behavior without changing the component itself:

| Wrapper | What it does |
|---------|-------------|
| `with_loading_state(button)` | Shows a loading indicator while the callback runs |
| `with_confirmation(button, message="...")` | Adds a yes/no prompt before execution |
| `with_cooldown(button, seconds=5)` | Enforces a per-user cooldown between clicks |

### Theming

Register themes globally and apply them per-view or as a default:

```python
from cascadeui import Theme, register_theme, set_default_theme

my_theme = Theme("custom", {
    "primary_color": discord.Color.purple(),
    "header_emoji": ">>",
    "footer_text": "My Bot"
})
register_theme(my_theme)
set_default_theme("custom")

# Or apply to a specific view
view = MyView(context=ctx, theme=my_theme)
theme = view.get_theme()
theme.apply_to_embed(embed)
```

### Persistence

Decorate a view class with `@cascade_persistent` to automatically save and restore state to disk:

```python
from cascadeui import cascade_persistent

@cascade_persistent(file_path="bot_state.json")
class PersistentView(StatefulView):
    ...
```

### Custom Reducers

Use the `@cascade_reducer` decorator to register reducers for custom action types:

```python
from cascadeui import cascade_reducer
import copy

@cascade_reducer("PROFILE_UPDATED")
async def profile_reducer(action, state):
    new_state = copy.deepcopy(state)
    user_id = action["payload"]["user_id"]
    new_state.setdefault("profiles", {})[user_id] = action["payload"]["data"]
    return new_state
```

Reducers must return a new state dict. Use `copy.deepcopy(state)` to avoid mutating the current state.

### Middleware

Middleware sits between dispatch and reducers, letting you intercept, transform, or react to actions without modifying core logic:

```python
from cascadeui import get_store, DebouncedPersistence, logging_middleware

store = get_store()

# Log every action
store.add_middleware(logging_middleware())

# Debounce persistence writes (flush at most every 2 seconds)
persistence = DebouncedPersistence(store, interval=2.0)
store.add_middleware(persistence)

# Write your own middleware
async def rate_limit(action, state, next_fn):
    # Inspect, modify, or block actions before they hit the reducer
    return await next_fn(action, state)

store.add_middleware(rate_limit)
```

Built-in middleware:

| Middleware | What it does |
|-----------|-------------|
| `DebouncedPersistence(store, interval)` | Batches disk writes, flushes on lifecycle events |
| `logging_middleware()` | Logs every dispatched action at INFO level |

### DevTools

CascadeUI includes a built-in state inspector for debugging. It renders paginated embeds showing active views, sessions, action history, and store configuration.

```python
from cascadeui import DevToolsCog

# As a cog (adds /inspect command, owner-only)
await bot.add_cog(DevToolsCog(bot))

# Or use StateInspector directly
from cascadeui import StateInspector

inspector = StateInspector()
pages = inspector.build_pages()  # list of embeds
```

---

## Data Flow

```
User clicks button
       |
  Interaction dispatched
       |
  Original callback runs (responds to Discord)
       |
  COMPONENT_INTERACTION action dispatched
       |
  Middleware pipeline (logging, persistence, custom)
       |
  Reducer transforms state immutably
       |
  Subscribers notified (filtered by action type)
       |
  Views update their UI from new state
```

---

## Examples

Working examples are in the [`examples/`](examples/) directory:

| Example | What it covers |
|---------|---------------|
| **counter.py** | Basic stateful counter with increment, decrement, reset |
| **themed_form.py** | Theme switching, component wrappers, pagination, form views |
| **persistent_counter.py** | State persistence to disk with `@cascade_persistent` |

Each example is a discord.py cog that can be loaded into any bot.

---

## Development

### Setting Up

```bash
git clone https://github.com/HollowTheSilver/CascadeUI.git
cd CascadeUI
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest tests/ -v
```

### Code Style

```bash
black --line-length 100 cascadeui/
isort --profile black --line-length 100 cascadeui/
```

---

## License

MIT License - see [LICENSE](LICENSE) for details.

---

