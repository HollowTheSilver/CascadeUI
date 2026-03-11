# CascadeUI

Stateful UI components and state management for [discord.py](https://github.com/Rapptz/discord.py).

CascadeUI brings a Redux-inspired architecture to Discord bot interfaces. Views, buttons, selects, and forms are backed by a centralized state store with dispatched actions, reducers, and subscriber notifications. The result is predictable state flow and composable UI patterns that scale beyond simple one-off views.

## Installation

```bash
pip install cascadeui
```

Requires Python 3.10+ and discord.py 2.1+.

## Quick Start

```python
import discord
from discord.ext import commands
from cascadeui import StatefulView, StatefulButton, cascade_reducer

# Define a custom reducer for your state
@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    import copy
    new_state = copy.deepcopy(state)
    new_state.setdefault("application", {}).setdefault("counters", {})
    view_id = action["payload"]["view_id"]
    new_state["application"]["counters"][view_id] = action["payload"]["counter"]
    return new_state

class CounterView(StatefulView):
    def __init__(self, context):
        super().__init__(context=context)
        self.counter = 0

        self.add_item(StatefulButton(
            label="+1",
            style=discord.ButtonStyle.primary,
            callback=self.increment
        ))
        self.add_item(StatefulButton(
            label="-1",
            style=discord.ButtonStyle.danger,
            callback=self.decrement
        ))
        self.add_exit_button()

    async def increment(self, interaction):
        await interaction.response.defer()
        self.counter += 1
        await self.dispatch("COUNTER_UPDATED", {"view_id": self.id, "counter": self.counter})
        await self.update_ui()

    async def decrement(self, interaction):
        await interaction.response.defer()
        self.counter -= 1
        await self.dispatch("COUNTER_UPDATED", {"view_id": self.id, "counter": self.counter})
        await self.update_ui()

    async def update_ui(self):
        embed = discord.Embed(title="Counter", description=f"Value: {self.counter}")
        if self.message:
            await self.message.edit(embed=embed, view=self)

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.hybrid_command()
async def counter(ctx):
    view = CounterView(context=ctx)
    embed = discord.Embed(title="Counter", description="Value: 0")
    await view.send(embed=embed)
```

## Core Concepts

### State Store

A singleton `StateStore` holds all application state and coordinates updates through a dispatch/reducer cycle. Actions are plain dicts with a `type` and `payload`. Reducers transform state immutably. Subscribers (usually views) receive filtered notifications when relevant state changes.

```python
from cascadeui import get_store

store = get_store()
store.register_reducer("MY_ACTION", my_reducer)
await store.dispatch("MY_ACTION", {"key": "value"})
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

### Component Wrappers

Wrappers modify component behavior without changing the component itself:

- `with_loading_state(button)` shows a loading indicator while the callback runs
- `with_confirmation(button, message="Are you sure?")` adds a yes/no prompt before execution
- `with_cooldown(button, seconds=5)` enforces a per-user cooldown between clicks

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

## Examples

Working examples are in the [`examples/`](examples/) directory:

- **counter.py** - Basic stateful counter with increment/decrement/reset
- **themed_form.py** - Theme switching, component wrappers, pagination, and form views

Each example is a discord.py cog that can be loaded into any bot.

## License

MIT. See [LICENSE](LICENSE) for the full text.
