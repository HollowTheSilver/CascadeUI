# Quick Start

Build a working stateful counter in 5 minutes. This tutorial introduces the
core concepts one at a time -- by the end, the full data flow pattern is clear.

## Prerequisites

- Python 3.10+ with discord.py 2.7+ installed
- CascadeUI [installed](installation.md)
- A Discord bot token and a test server

---

## Step 1: Define a Reducer

A **reducer** handles a specific action type. When the view dispatches an
action, the matching reducer receives a deep copy of the state, mutates it,
and returns the result:

```python
from cascadeui import cascade_reducer

@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    app = state.setdefault("application", {})
    counters = app.setdefault("counters", {})
    counters[action["payload"]["view_id"]] = action["payload"]["value"]
    return state
```

The `@cascade_reducer` decorator registers this function to handle
`COUNTER_UPDATED` actions. The state is already deep-copied -- mutate it
directly, no `copy.deepcopy()` needed.

---

## Step 2: Build the View

A **view** is a single UI screen backed by the state store. V2 views
(`StatefulLayoutView`) use Discord's container-based component system -- the
component tree IS the message content:

```python
import discord
from discord.ui import ActionRow, TextDisplay
from cascadeui import StatefulLayoutView, StatefulButton, card, key_value

class CounterView(StatefulLayoutView):
    instance_limit = 1                # One counter per user per guild

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.counter = 0
        self.build_ui()

    def build_ui(self):
        """Rebuild the component tree from current state."""
        self.clear_items()
        self.add_item(card(
            "## Counter",
            key_value({"Value": str(self.counter)}),
            color=discord.Color.blurple(),
        ))
        self.add_item(ActionRow(
            StatefulButton(
                label="+1", style=discord.ButtonStyle.primary,
                callback=self.increment,
            ),
            StatefulButton(
                label="-1", style=discord.ButtonStyle.danger,
                callback=self.decrement,
            ),
        ))
        self.add_exit_button()

    async def increment(self, interaction):
        self.counter += 1
        await self.dispatch("COUNTER_UPDATED", {
            "view_id": self.id, "value": self.counter,
        })

    async def decrement(self, interaction):
        self.counter -= 1
        await self.dispatch("COUNTER_UPDATED", {
            "view_id": self.id, "value": self.counter,
        })
```

Key points:

- **`build_ui()`** rebuilds the component tree from scratch. The default
  `on_state_changed()` calls `build_ui()` followed by `refresh()` whenever
  the view's state changes -- no manual wiring needed.
- **`self.dispatch()`** sends the action through the middleware pipeline into
  the reducer, which updates state, which notifies subscribers, which triggers
  `on_state_changed()`.
- **`StatefulButton`** wraps discord.py's `Button` with automatic
  `COMPONENT_INTERACTION` dispatching for debugging and history tracking.
- **`card()`** creates a `Container` from its children -- strings are
  auto-wrapped in `TextDisplay`.

---

## Step 3: Wire It Up

Register the view as a slash command:

```python
from discord.ext import commands

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.hybrid_command()
async def counter(ctx):
    view = CounterView(context=ctx)
    await view.send()
```

`view.send()` handles message creation, state registration, session tracking,
and message reference capture in one call.

---

## The Data Flow

Here is what happens each time a button is clicked:

```
Click "+1"  →  increment()  →  dispatch("COUNTER_UPDATED")
                                        │
                               counter_reducer() mutates state
                                        │
                               on_state_changed() fires
                                        │
                               build_ui() → refresh()
                                        │
                               Discord message edited ✓
```

This is the **unidirectional data flow** pattern -- every state change follows
the same path. See [Core Concepts](concepts.md#data-flow) for the full
diagram.

---

## V1 Alternative

The same counter using the V1 component system (`StatefulView` + embeds):

```python
import discord
from discord.ext import commands
from cascadeui import StatefulView, StatefulButton, cascade_reducer

@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    app = state.setdefault("application", {})
    counters = app.setdefault("counters", {})
    counters[action["payload"]["view_id"]] = action["payload"]["value"]
    return state

class CounterView(StatefulView):
    instance_limit = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.counter = 0
        self.add_item(StatefulButton(
            label="+1", style=discord.ButtonStyle.primary,
            callback=self.increment,
        ))
        self.add_item(StatefulButton(
            label="-1", style=discord.ButtonStyle.danger,
            callback=self.decrement,
        ))
        self.add_exit_button()

    def build_ui(self):
        return {"embed": discord.Embed(
            title="Counter",
            description=f"Value: {self.counter}",
            color=discord.Color.blurple(),
        )}

    async def increment(self, interaction):
        self.counter += 1
        await self.dispatch("COUNTER_UPDATED", {
            "view_id": self.id, "value": self.counter,
        })

    async def decrement(self, interaction):
        self.counter -= 1
        await self.dispatch("COUNTER_UPDATED", {
            "view_id": self.id, "value": self.counter,
        })

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.hybrid_command()
async def counter(ctx):
    view = CounterView(context=ctx)
    await view.send(**view.build_ui())
```

V1 views use embeds for content and buttons below. `build_ui()` returns a
dict that is splatted into `refresh()` (via the default `on_state_changed()`).
The initial send passes the same dict to `view.send()`.

---

## Next Steps

- **[Core Concepts](concepts.md)** -- the mental models that make everything click
- **[Views](views.md)** -- lifecycle, navigation, sessions, policies
- **[Components](components.md)** -- selects, modals, V2 builders, grid helpers
- **[State Management](state.md)** -- custom reducers, scoped state, undo/redo
- **[View Patterns](patterns.md)** -- pre-built forms, wizards, tabs, pagination
