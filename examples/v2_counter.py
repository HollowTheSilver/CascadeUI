"""
V2 Counter — CascadeUI V2 Components Introduction
==================================================

The V2 equivalent of counter.py, demonstrating how V2 components
integrate content and controls into a single visual unit.

V2 advantages shown here:

    - card() helper with dynamic accent color (like embed color, but stackable)
    - TextDisplay for rich markdown without an embed wrapper
    - divider() for visual structure inside containers
    - ActionRow inside containers (buttons live with their content)

In V1, embeds and buttons are always visually separated: the embed
sits on top, the buttons float below. In V2, buttons can live inside
the same card as the text they control.

Commands:
    /v2counter   Display the counter

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import logging
from datetime import datetime

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import StatefulButton, StatefulLayoutView, card, cascade_reducer, divider

logger = logging.getLogger(__name__)


# // ========================================( Reducer )======================================== // #


@cascade_reducer("COUNTER_UPDATED")
async def counter_reducer(action, state):
    """Track counter values in application state."""
    new_state = state
    app = new_state.setdefault("application", {})
    counters = app.setdefault("counters", {})

    key = action["payload"].get("view_id")
    if key:
        counters[key] = action["payload"].get("counter", 0)

    return new_state


# // ========================================( View )======================================== // #


class V2CounterView(StatefulLayoutView):
    """A counter with buttons inside the display container.

    Shows the core V2 advantage: content and controls as one unit.
    The colored container, counter text, and control buttons all
    live together in a single card.
    """

    session_limit = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.counter = 0
        self._presses = 0
        self._build_ui()

    def _build_ui(self):
        """Rebuild the entire component tree with current counter state."""
        self.clear_items()

        # Accent color shifts based on counter value
        if self.counter > 0:
            color = discord.Color.green()
        elif self.counter < 0:
            color = discord.Color.red()
        else:
            color = discord.Color.light_grey()

        now = datetime.now().strftime("%H:%M:%S")

        # Control buttons
        minus5 = StatefulButton(
            label="-5", style=discord.ButtonStyle.danger, callback=self._make_change(-5)
        )
        minus1 = StatefulButton(
            label="-1", style=discord.ButtonStyle.danger, callback=self._make_change(-1)
        )
        reset = StatefulButton(label="0", style=discord.ButtonStyle.secondary, callback=self._reset)
        plus1 = StatefulButton(
            label="+1", style=discord.ButtonStyle.success, callback=self._make_change(1)
        )
        plus5 = StatefulButton(
            label="+5", style=discord.ButtonStyle.success, callback=self._make_change(5)
        )

        # Everything in one container: content and controls together.
        # In V1, the embed and buttons are always separate visual blocks.
        # In V2, the ActionRow sits inside the Container alongside the text.
        self.add_item(
            card(
                f"# Counter\nCurrent value: **{self.counter}**",
                divider(),
                TextDisplay(f"-# Presses: {self._presses} \u2022 Updated: {now}"),
                ActionRow(minus5, minus1, reset, plus1, plus5),
                color=color,
            )
        )

        self.add_exit_button()

    def _make_change(self, delta):
        """Create a callback that changes the counter by delta."""

        async def callback(interaction):
            await interaction.response.defer()
            self.counter += delta
            self._presses += 1
            await self._update_counter()

        return callback

    async def _reset(self, interaction):
        """Reset the counter to zero."""
        await interaction.response.defer()
        self.counter = 0
        self._presses += 1
        await self._update_counter()

    async def _update_counter(self):
        """Rebuild UI and push state."""
        self._build_ui()
        await self.dispatch(
            "COUNTER_UPDATED",
            {
                "view_id": self.id,
                "counter": self.counter,
            },
        )
        if self.message:
            await self.message.edit(view=self)

    async def update_from_state(self, state):
        pass


# // ========================================( Cog )======================================== // #


class V2CounterExample(commands.Cog, name="v2_counter_example"):
    """V2 counter demonstrating integrated content and controls."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2counter",
        description="Display an interactive V2 counter with integrated controls.",
    )
    async def v2counter(self, context: Context) -> None:
        """Display a V2 counter view.

        Unlike the V1 counter, the buttons live inside the container
        alongside the counter text. No separate embed required.
        """
        view = V2CounterView(context=context)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2CounterExample(bot=bot))
