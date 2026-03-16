
# // ========================================( Modules )======================================== // #


import copy
import discord
from datetime import datetime
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import (
    StatefulView, StatefulButton, get_store,
    cascade_reducer,
)

import logging

logger = logging.getLogger(__name__)


# // ========================================( Reducer )======================================== // #


@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    """Handle counter updates in the state."""
    new_state = copy.deepcopy(state)

    if "application" not in new_state:
        new_state["application"] = {}

    if "counters" not in new_state["application"]:
        new_state["application"]["counters"] = {}

    # Key by state_key (stable) instead of view_id (ephemeral UUID)
    key = action["payload"].get("state_key") or action["payload"].get("view_id")
    counter_value = action["payload"].get("counter")

    if key:
        new_state["application"]["counters"][key] = counter_value

    return new_state


# // ========================================( Views )======================================== // #


class PersistentCounterView(StatefulView):
    """A counter that saves its state to disk automatically.

    Uses state_key to scope data by user ID, so each user's counter
    persists independently across view timeouts and bot restarts.

    Requires setup_persistence() to be called in the bot's setup_hook.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Restore counter from persisted state if available
        store = get_store()
        counters = store.state.get("application", {}).get("counters", {})
        self.counter = counters.get(self.state_key, 0)

        self.add_item(StatefulButton(
            label="Increment",
            style=discord.ButtonStyle.primary,
            callback=self.increment
        ))

        self.add_item(StatefulButton(
            label="Decrement",
            style=discord.ButtonStyle.danger,
            callback=self.decrement
        ))

        self.add_item(StatefulButton(
            label="Reset",
            style=discord.ButtonStyle.secondary,
            callback=self.reset
        ))

        self.add_exit_button()

    async def increment(self, interaction):
        await interaction.response.defer()
        self.counter += 1
        await self._sync_state()
        await self.update_ui()

    async def decrement(self, interaction):
        await interaction.response.defer()
        self.counter -= 1
        await self._sync_state()
        await self.update_ui()

    async def reset(self, interaction):
        await interaction.response.defer()
        self.counter = 0
        await self._sync_state()
        await self.update_ui()

    async def _sync_state(self):
        """Push the current counter value into the state store."""
        await self.dispatch("COUNTER_UPDATED", {
            "state_key": self.state_key,
            "counter": self.counter,
        })

    async def update_ui(self):
        embed = discord.Embed(
            title="Persistent Counter",
            description=f"Current value: {self.counter}",
            color=discord.Color.blue() if self.counter >= 0 else discord.Color.red()
        )
        embed.set_footer(text=f"Last updated: {datetime.now().strftime('%H:%M:%S')}")

        if self.message:
            await self.message.edit(embed=embed, view=self)

    async def update_from_state(self, state):
        pass


# // ========================================( Cog )======================================== // #


class PersistentCounterExample(commands.Cog, name="persistent_counter_example"):
    """Demonstrates state persistence across bot restarts."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="pcounter",
        description="Display a persistent counter that saves state to disk."
    )
    async def pcounter(self, context: Context) -> None:
        # state_key scopes data by user -- each user gets their own counter
        view = PersistentCounterView(
            context=context,
            state_key=f"counter:{context.author.id}",
        )

        embed = discord.Embed(
            title="Persistent Counter",
            description=f"Current value: {view.counter}",
            color=discord.Color.blue() if view.counter >= 0 else discord.Color.red()
        )

        if view.counter != 0:
            embed.set_footer(text="Restored from saved state")

        await view.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(PersistentCounterExample(bot=bot))
