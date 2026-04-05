# // ========================================( Modules )======================================== // #


import discord
from datetime import datetime
from discord.ext import commands
from discord.ext.commands import Context

# Import CascadeUI components
from cascadeui import StatefulView, StatefulButton, get_store, cascade_reducer

import logging

logger = logging.getLogger(__name__)


# // ========================================( Views )======================================== // #


class CounterView(StatefulView):
    session_limit = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Initialize counter value
        self.counter = 0

        # Add buttons
        self.add_item(
            StatefulButton(
                label="Increment", style=discord.ButtonStyle.primary, callback=self.increment
            )
        )

        self.add_item(
            StatefulButton(
                label="Decrement", style=discord.ButtonStyle.danger, callback=self.decrement
            )
        )

        self.add_item(
            StatefulButton(label="Reset", style=discord.ButtonStyle.secondary, callback=self.reset)
        )

        self.add_exit_button()

    async def increment(self, interaction):
        """Increment the counter with immediate UI update."""
        await interaction.response.defer()
        self.counter += 1
        await self.dispatch("COUNTER_UPDATED", {"view_id": self.id, "counter": self.counter})
        await self.update_ui()

    async def decrement(self, interaction):
        """Decrement the counter with immediate UI update."""
        await interaction.response.defer()
        self.counter -= 1
        await self.dispatch("COUNTER_UPDATED", {"view_id": self.id, "counter": self.counter})
        await self.update_ui()

    async def reset(self, interaction):
        """Reset the counter to zero."""
        await interaction.response.defer()
        self.counter = 0
        await self.dispatch("COUNTER_UPDATED", {"view_id": self.id, "counter": self.counter})
        await self.update_ui()

    async def update_ui(self):
        """Update the UI with current counter value."""
        embed = discord.Embed(
            title="Fast Counter",
            description=f"Current value: {self.counter}",
            color=discord.Color.blue() if self.counter >= 0 else discord.Color.red(),
        )
        embed.set_footer(text=f"Last updated: {datetime.now().strftime('%H:%M:%S')}")

        if self.message:
            await self.message.edit(embed=embed, view=self)


# Create a custom reducer for the counter
@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    """Handle counter updates in the state."""
    new_state = state

    if "application" not in new_state:
        new_state["application"] = {}

    if "counters" not in new_state["application"]:
        new_state["application"]["counters"] = {}

    key = action["payload"].get("state_key") or action["payload"].get("view_id")
    counter_value = action["payload"].get("counter")

    if key:
        new_state["application"]["counters"][key] = counter_value
        logger.debug(f"State updated: counters[{key}] = {counter_value}")

    return new_state


# // ========================================( Cog )======================================== // #


class CounterExample(commands.Cog, name="counter_example"):
    """Example discord extension class."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="counter", description="Display an interactive counter example user interface."
    )
    async def counter(self, context: Context) -> None:
        """Display an interactive counter view."""
        view = CounterView(context=context)

        embed = discord.Embed(
            title="Counter Example", description="Current value: 0", color=discord.Color.blue()
        )

        # Use the new send() API for proper state registration
        await view.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(CounterExample(bot=bot))
