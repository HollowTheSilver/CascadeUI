# Quick Start

This guide walks through building a simple stateful counter to introduce the core concepts.

## The Counter Example

A counter with increment and decrement buttons, backed by the state store:

=== "V2"

    ```python
    import discord
    from discord.ext import commands
    from discord.ui import ActionRow, TextDisplay
    from cascadeui import (
        StatefulLayoutView, StatefulButton, cascade_reducer,
        card, key_value, divider,
    )

    # 1. Define a reducer for your custom action
    @cascade_reducer("COUNTER_UPDATED")
    async def counter_reducer(action, state):
        # @cascade_reducer passes a deep copy — mutate and return directly
        state.setdefault("application", {}).setdefault("counters", {})
        view_id = action["payload"]["view_id"]
        state["application"]["counters"][view_id] = action["payload"]["counter"]
        return state

    # 2. Create a stateful view
    class CounterView(StatefulLayoutView):
        session_limit = 1

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.counter = 0
            self._build_ui()

        def _build_ui(self):
            self.clear_items()
            self.add_item(
                card(
                    "## Counter",
                    key_value({"Value": str(self.counter)}),
                    color=discord.Color.blurple(),
                )
            )
            self.add_item(ActionRow(
                StatefulButton(label="+1", style=discord.ButtonStyle.primary,
                               callback=self.increment),
                StatefulButton(label="-1", style=discord.ButtonStyle.danger,
                               callback=self.decrement),
            ))
            self.add_exit_button()

        async def _update(self, interaction):
            await interaction.response.defer()
            await self.dispatch("COUNTER_UPDATED", {
                "view_id": self.id,
                "counter": self.counter,
            })
            self._build_ui()
            if self.message:
                await self.message.edit(view=self)

        async def increment(self, interaction):
            self.counter += 1
            await self._update(interaction)

        async def decrement(self, interaction):
            self.counter -= 1
            await self._update(interaction)

    # 3. Wire it up to a command
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

    @bot.hybrid_command()
    async def counter(ctx):
        view = CounterView(context=ctx)
        await view.send()
    ```

=== "V1 (Classic)"

    ```python
    import discord
    from discord.ext import commands
    from cascadeui import StatefulView, StatefulButton, cascade_reducer

    # 1. Define a reducer for your custom action
    @cascade_reducer("COUNTER_UPDATED")
    async def counter_reducer(action, state):
        # @cascade_reducer passes a deep copy — mutate and return directly
        state.setdefault("application", {}).setdefault("counters", {})
        view_id = action["payload"]["view_id"]
        state["application"]["counters"][view_id] = action["payload"]["counter"]
        return state

    # 2. Create a stateful view
    class CounterView(StatefulView):
        session_limit = 1

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

1. **`@cascade_reducer`** registered a function that handles `COUNTER_UPDATED` actions. Reducers receive the action and a deep-copied state, mutate it, and return it directly — no `copy.deepcopy()` needed.

2. **`StatefulLayoutView`** (V2) or **`StatefulView`** (V1) wraps discord.py's view classes with state integration. They auto-subscribe to the state store and handle cleanup on timeout.

3. **`StatefulButton`** extends discord.py's `Button` with automatic `COMPONENT_INTERACTION` dispatching. Every click is tracked in the state store.

4. **`view.dispatch()`** sends an action through the middleware pipeline into the reducer, which updates state. Subscribers (including the view itself) are notified.

5. **`view.send()`** handles message creation, state registration, and message tracking in one call. V2 views send the component tree as the message content. V1 views accept `embed` and `content` parameters.

## Next Steps

- [State Management](state.md) — understand the dispatch/reducer cycle
- [Views](views.md) — lifecycle, transitions, and pre-built patterns
- [Components](components.md) — buttons, selects, V2 helpers, wrappers, and composition
- [Persistence](persistence.md) — save and restore state across restarts
- [Known Limitations](known-limitations.md) — architectural constraints to be aware of
