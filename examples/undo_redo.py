
# // ========================================( Modules )======================================== // #


import copy
import discord
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import (
    StatefulView, StatefulButton, get_store,
    cascade_reducer, UndoMiddleware,
)

import logging

logger = logging.getLogger(__name__)


# // ========================================( Reducer )======================================== // #


@cascade_reducer("COUNTER_SET")
async def counter_set_reducer(action, state):
    """Set the undo counter value in application state."""
    new_state = copy.deepcopy(state)
    new_state.setdefault("application", {})
    new_state["application"]["undo_counter"] = action["payload"]["value"]
    return new_state


# // ========================================( Views )======================================== // #


class UndoCounterView(StatefulView):
    """Counter with undo/redo support.

    Each increment/decrement creates a state snapshot. Undo reverts
    to the previous value; redo re-applies it. The embed shows
    current stack depths so you can see exactly what's happening.
    """

    enable_undo = True
    undo_limit = 10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        store = get_store()
        self.counter = store.state.get("application", {}).get("undo_counter", 0)

        self.add_item(StatefulButton(
            label="+1", style=discord.ButtonStyle.primary,
            callback=self.increment,
        ))
        self.add_item(StatefulButton(
            label="-1", style=discord.ButtonStyle.danger,
            callback=self.decrement,
        ))
        self.add_item(StatefulButton(
            label="Undo", style=discord.ButtonStyle.secondary,
            emoji="↩", callback=self.do_undo,
        ))
        self.add_item(StatefulButton(
            label="Redo", style=discord.ButtonStyle.secondary,
            emoji="↪", callback=self.do_redo,
        ))
        self.add_exit_button()

    async def increment(self, interaction):
        await interaction.response.defer()
        self.counter += 1
        await self.dispatch("COUNTER_SET", {"value": self.counter})
        await self._refresh()

    async def decrement(self, interaction):
        await interaction.response.defer()
        self.counter -= 1
        await self.dispatch("COUNTER_SET", {"value": self.counter})
        await self._refresh()

    async def do_undo(self, interaction):
        await interaction.response.defer()
        await self.undo(interaction)
        # Re-sync local counter from the restored state
        self.counter = get_store().state.get("application", {}).get("undo_counter", 0)
        await self._refresh()

    async def do_redo(self, interaction):
        await interaction.response.defer()
        await self.redo(interaction)
        self.counter = get_store().state.get("application", {}).get("undo_counter", 0)
        await self._refresh()

    async def _refresh(self):
        store = get_store()
        session = store.state.get("sessions", {}).get(self.session_id, {})
        undo_depth = len(session.get("undo_stack", []))
        redo_depth = len(session.get("redo_stack", []))

        embed = discord.Embed(
            title="Undo/Redo Counter",
            description=f"**Value:** {self.counter}",
            color=discord.Color.blue() if self.counter >= 0 else discord.Color.red(),
        )
        embed.add_field(name="Undo Stack", value=str(undo_depth), inline=True)
        embed.add_field(name="Redo Stack", value=str(redo_depth), inline=True)
        embed.add_field(name="Limit", value=str(self.undo_limit), inline=True)

        if self.message:
            await self.message.edit(embed=embed, view=self)

    async def update_from_state(self, state):
        pass


# // ========================================( Cog )======================================== // #


class UndoRedoExample(commands.Cog, name="undo_redo_example"):
    """Counter with undo/redo support via UndoMiddleware."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="undotest",
        description="Counter with undo/redo support."
    )
    async def undotest(self, context: Context) -> None:
        view = UndoCounterView(context=context)

        embed = discord.Embed(
            title="Undo/Redo Counter",
            description="**Value:** 0",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Undo Stack", value="0", inline=True)
        embed.add_field(name="Redo Stack", value="0", inline=True)
        embed.add_field(name="Limit", value=str(view.undo_limit), inline=True)

        await view.send(embed=embed)


async def setup(bot) -> None:
    # Add UndoMiddleware if not already present
    store = get_store()
    if not any(isinstance(mw, UndoMiddleware) for mw in store._middleware):
        store.add_middleware(UndoMiddleware(store))

    await bot.add_cog(UndoRedoExample(bot=bot))
