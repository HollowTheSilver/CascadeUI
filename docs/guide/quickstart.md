# Quick Start

This guide walks through building a simple stateful counter to introduce the core concepts.

## The Counter Example

A counter with increment and decrement buttons, backed by the state store:

```python
import discord
from discord.ext import commands
from cascadeui import StatefulView, StatefulButton, cascade_reducer
import copy

# 1. Define a reducer for your custom action
@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    new_state = copy.deepcopy(state)
    new_state.setdefault("application", {}).setdefault("counters", {})
    view_id = action["payload"]["view_id"]
    new_state["application"]["counters"][view_id] = action["payload"]["counter"]
    return new_state

# 2. Create a stateful view
class CounterView(StatefulView):
    def __init__(self, context):
        super().__init__(context=context)
        self.counter = 0
        self.add_item(StatefulButton(
            label="+1",
            style=discord.ButtonStyle.primary,
            callback=self.increment,
        ))
        self.add_item(StatefulButton(
            label="-1",
            style=discord.ButtonStyle.danger,
            callback=self.decrement,
        ))
        self.add_exit_button()

    async def increment(self, interaction):
        await interaction.response.defer()
        self.counter += 1
        await self.dispatch("COUNTER_UPDATED", {
            "view_id": self.id,
            "counter": self.counter,
        })
        if self.message:
            await self.message.edit(
                embed=discord.Embed(title="Counter", description=f"Value: {self.counter}"),
                view=self,
            )

    async def decrement(self, interaction):
        await interaction.response.defer()
        self.counter -= 1
        await self.dispatch("COUNTER_UPDATED", {
            "view_id": self.id,
            "counter": self.counter,
        })
        if self.message:
            await self.message.edit(
                embed=discord.Embed(title="Counter", description=f"Value: {self.counter}"),
                view=self,
            )

# 3. Wire it up to a command
bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.hybrid_command()
async def counter(ctx):
    view = CounterView(context=ctx)
    await view.send(embed=discord.Embed(title="Counter", description="Value: 0"))
```

## What Just Happened?

1. **`@cascade_reducer`** registered a function that handles `COUNTER_UPDATED` actions. Reducers receive the action and current state, and return a new state dict.

2. **`StatefulView`** wraps discord.py's `View` with state integration. It auto-subscribes to the state store and handles cleanup on timeout.

3. **`StatefulButton`** extends discord.py's `Button` with automatic `COMPONENT_INTERACTION` dispatching. Every click is tracked in the state store.

4. **`view.dispatch()`** sends an action through the middleware pipeline into the reducer, which updates state. Subscribers (including the view itself) are notified.

5. **`view.send()`** handles message creation, state registration, and message tracking in one call.

## Next Steps

- [State Management](state.md) - understand the dispatch/reducer cycle
- [Views](views.md) - lifecycle, transitions, and pre-built patterns
- [Components](components.md) - buttons, selects, wrappers, and composition
- [Persistence](persistence.md) - save and restore state across restarts
