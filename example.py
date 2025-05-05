
# // ========================================( Modules )======================================== // #


import inspect
import discord
import datetime
from discord.ext import commands
from discord.ext.commands import Context

# Import CascadeUI components
from cascadeui import StatefulView, StatefulButton, get_store, cascade_reducer

from utilities.logger import AsyncLogger
from typing import (
    List,
    Optional,
    TypeVar,
)


# \\ Logger \\

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")


# \\ Generics \\

TestCascadeCog = TypeVar('TestCascadeCog', bound='TestCascade')


# // ========================================( Views )======================================== // #


class CounterView(StatefulView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Initialize counter value
        self.counter = 0

        # Add buttons
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

        # Add reset button
        self.add_item(StatefulButton(
            label="Reset",
            style=discord.ButtonStyle.secondary,
            callback=self.reset
        ))

        # Add exit button
        self.add_exit_button()

    async def increment(self, interaction):
        """Increment the counter with immediate UI update."""
        # Acknowledge interaction with deferred update
        await interaction.response.defer()

        # Update counter
        self.counter += 1

        # Dispatch action to update state (for persistence/synchronization)
        await self.dispatch("COUNTER_UPDATED", {
            "view_id": self.id,
            "counter": self.counter
        })

        # Immediate UI update
        await self.update_ui()

    async def decrement(self, interaction):
        """Decrement the counter with immediate UI update."""
        # Acknowledge interaction with deferred update
        await interaction.response.defer()

        # Update counter
        self.counter -= 1

        # Dispatch action to update state (for persistence/synchronization)
        await self.dispatch("COUNTER_UPDATED", {
            "view_id": self.id,
            "counter": self.counter
        })

        # Immediate UI update
        await self.update_ui()

    async def reset(self, interaction):
        """Reset the counter to zero."""
        # Acknowledge interaction with deferred update
        await interaction.response.defer()

        # Reset counter
        self.counter = 0

        # Dispatch action to update state
        await self.dispatch("COUNTER_UPDATED", {
            "view_id": self.id,
            "counter": self.counter
        })

        # Immediate UI update
        await self.update_ui()

    async def update_ui(self):
        """Update the UI with current counter value."""
        # Create an embed with counter value and styling
        embed = discord.Embed(
            title="Fast Counter",
            description=f"Current value: {self.counter}",
            color=discord.Color.blue() if self.counter >= 0 else discord.Color.red()
        )

        # Add a footer with a timestamp
        embed.set_footer(text=f"Last updated: {datetime.now().strftime('%H:%M:%S')}")

        # Update message if it exists
        if self.message:
            await self.message.edit(embed=embed, view=self)

    # Override to do nothing since we update manually
    async def update_from_state(self, state):
        """Disabled automatic state-driven updates."""
        pass


# Create a custom reducer for the counter
@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    """Handle counter updates in the state."""
    # Create a copy of the state to modify
    new_state = state.copy()

    # Initialize application state if needed
    if "application" not in new_state:
        new_state["application"] = {}

    if "counters" not in new_state["application"]:
        new_state["application"]["counters"] = {}

    # Get data from action
    view_id = action["payload"].get("view_id")
    counter_value = action["payload"].get("counter")

    # Update counter in state
    if view_id:
        new_state["application"]["counters"][view_id] = counter_value
        logger.debug(f"State updated: counters[{view_id}] = {counter_value}")

    return new_state


# // ========================================( Cog )======================================== // #


class Example(commands.Cog, name="example"):
    """
        Example discord extension class.

        """

    def __init__(self, bot) -> None:
        """
        Initialize Example cog.

        ... VersionAdded:: 1.0

        Parameters
        -----------
        bot: commands.Bot[Client]
            Discord client instance.

        Raises
        -------
        ...

        Returns
        --------
        :class:`NoneType`
            None
        """
        self.bot = bot
        _listeners: List[callable] = list()
        for attr in dir(self):
            if awaitable := getattr(self, attr, None):
                if inspect.iscoroutinefunction(awaitable) and attr.startswith("on_"):
                    _listeners.append(awaitable)
        _failed: List[Optional[callable]] = list()
        for _listener in _listeners:
            try:
                self.bot.add_listener(_listener, _listener.__name__)
            except (TypeError, AttributeError, Exception):
                _failed.append(_listener.__name__)
                _listener.remove(_listener)
        if _listeners:
            logger.info(f"Registered <{len(_listeners)}> event listeners")
        for _listener in _failed:
            logger.error(f"Failed to register listener '{_listener}'")

    @commands.hybrid_command(
        name="counter",  # Note: must be lowercase
        description="Display an interactive counter example user interface."
    )
    async def counter(self, context: Context) -> None:
        """
        Display an interactive counter view.

        :param context: The command context.
        """
        # Create the counter view with the context
        view = CounterView(context=context)

        # Create initial embed
        embed = discord.Embed(
            title="Counter Example",
            description="Current value: 0",
            color=discord.Color.blue()
        )

        # Send the message with the view
        # Note: You don't need to set view.message manually - the StatefulView will handle this
        await context.send(embed=embed, view=view)


async def setup(bot) -> None:
    cog: TestCascadeCog = Example(bot=bot)
    await bot.add_cog(cog)
