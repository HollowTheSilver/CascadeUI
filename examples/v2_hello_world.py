"""
V2 Hello World -- CascadeUI Minimal Example
============================================

The smallest working CascadeUI view: a single button that counts clicks.
Demonstrates the building blocks every CascadeUI view uses:

    - Class-level policy attributes for access and instance control
    - Per-user scoped state via ``state_scope = "user"``
    - Reactivity via ``subscribed_actions`` + ``state_selector``
    - ``build_ui()`` for rebuilding the component tree from state
    - ``dispatch_scoped()`` for writing into the view's scope slice

Class attributes like ``owner_only``, ``instance_limit``, and
``instance_policy`` handle the boilerplate that every interactive
view needs -- ownership checks, duplicate prevention, and graceful
replacement. The store drives rebuilds: ``_increment`` dispatches
a scoped write, the selector picks up the new value, and the default
``on_state_changed()`` calls ``build_ui()`` + ``refresh()`` with no
callback glue in user code.

Commands:
    /hello   Open the counter

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import StatefulButton, StatefulLayoutView, StateStore, card

# // ========================================( Counter View )======================================== // #


class CounterView(StatefulLayoutView):
    """Single-button counter. Click the button, the number goes up."""

    # -- Access control --
    # Only the user who opened this view can click its buttons.
    # Rejected users see an ephemeral "This view belongs to someone else."
    owner_only = True

    # -- Instance control --
    # One counter per user. If they open a second, the old one is
    # replaced (edited to frozen state) and the new one takes over.
    instance_limit = 1
    instance_scope = "user"
    instance_policy = "replace"

    # -- Lifecycle --
    # ``"disable"`` freezes components on exit/timeout; ``"delete"`` would
    # remove the message instead.
    exit_policy = "disable"

    # -- State scope --
    # Count is stored per user, independent of guild. The same user
    # sees the same counter across every server that shares this bot.
    # At the state-tree level, writes from ``dispatch_scoped({"count": N})``
    # land at ``state["application"]["scoped"]["user:<id>"]["count"]``.
    # Other shapes: "guild" -> "guild:<id>", "user_guild" -> "user_guild:<uid>:<gid>",
    # "global" -> "global". See docs/guide/state.md for the full scope model.
    state_scope = "user"

    # -- Reactivity --
    # Subscribe to SCOPED_UPDATE so the view notices its own writes,
    # and return the count from state_selector so the store only
    # rebuilds when the number actually changes. The default
    # on_state_changed() calls build_ui() then refresh(), so no
    # callback override is needed.
    subscribed_actions = {"SCOPED_UPDATE"}

    def state_selector(self, state):
        # Selectors must read from the ``state`` argument so the comparison
        # sees the post-reduce snapshot. ``self.scoped_state`` reads from
        # the live store and would race the dispatcher.
        return StateStore.get_scoped_from(state, "user", user_id=self.user_id).get("count", 0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # First render happens in __init__ so send() has components to ship.
        # Subsequent renders are driven by on_state_changed() reacting to
        # the SCOPED_UPDATE dispatched from _increment().
        self.build_ui()

    def build_ui(self):
        self.clear_items()
        count = self.scoped_state.get("count", 0)

        self.add_item(card(TextDisplay(f"Count: **{count}**")))
        self.add_item(
            ActionRow(
                StatefulButton(
                    label="+1",
                    style=discord.ButtonStyle.primary,
                    callback=self._increment,
                )
            )
        )

    async def _increment(self, interaction: discord.Interaction):
        count = self.scoped_state.get("count", 0)
        await self.dispatch_scoped({"count": count + 1})


# // ========================================( Cog )======================================== // #


class HelloCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="hello", description="Open the counter")
    async def hello(self, ctx: Context):
        # Optional: pre-check instance availability before constructing
        # the view. Useful when __init__ does expensive work (DB queries,
        # API calls) and you want to fail fast. The view's send() handles
        # this automatically, but the pre-check avoids wasted setup.
        #
        # if not CounterView.check_instance_available(user_id=ctx.author.id):
        #     await ctx.send("You already have a counter open!", ephemeral=True)
        #     return

        view = CounterView(interaction=ctx.interaction)
        await view.send()


async def setup(bot: commands.Bot):
    await bot.add_cog(HelloCog(bot))
