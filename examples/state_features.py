
# // ========================================( Modules )======================================== // #


import discord
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import (
    StatefulView, StatefulButton, get_store,
    cascade_reducer, computed,
)

import logging

logger = logging.getLogger(__name__)


# // ========================================( Computed + Reducers )======================================== // #


# Computed value: derives total from the per-user votes dict.
# Cached automatically — only recomputes when the votes dict changes.
@computed(selector=lambda s: s.get("application", {}).get("votes", {}))
def total_votes(votes):
    return sum(votes.values())


@cascade_reducer("VOTE_CAST")
async def vote_reducer(action, state):
    """Increment or decrement a user's vote count."""
    new_state = state
    new_state.setdefault("application", {})
    new_state["application"].setdefault("votes", {})

    user_id = str(action["payload"]["user_id"])
    delta = action["payload"]["delta"]
    current = new_state["application"]["votes"].get(user_id, 0)
    new_state["application"]["votes"][user_id] = max(0, current + delta)

    return new_state


@cascade_reducer("VOTE_LOG")
async def vote_log_reducer(action, state):
    """Append an entry to the vote activity log (kept to last 20)."""
    new_state = state
    new_state.setdefault("application", {})
    new_state["application"].setdefault("vote_log", [])
    new_state["application"]["vote_log"].append(action["payload"]["entry"])
    new_state["application"]["vote_log"] = new_state["application"]["vote_log"][-20:]
    return new_state


# // ========================================( Views )======================================== // #


class ScopedCounterView(StatefulView):
    """Per-user counter using state scoping.

    Each user gets an independent counter stored under their own
    scoped namespace. Two users clicking the same view see different counts.
    """

    session_limit = 1
    scope = "user"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulButton(
            label="Click +1", style=discord.ButtonStyle.primary,
            callback=self.click,
        ))
        self.add_item(StatefulButton(
            label="My Count", style=discord.ButtonStyle.secondary,
            callback=self.view_count,
        ))
        self.add_exit_button()

    async def click(self, interaction):
        await interaction.response.defer()
        current = self.scoped_state.get("clicks", 0)
        await self.dispatch_scoped({"clicks": current + 1})

        embed = discord.Embed(
            title="Scoped Counter",
            description=(
                f"Your clicks: **{current + 1}**\n\n"
                "Each user has an independent counter via state scoping."
            ),
            color=discord.Color.blue(),
        )
        if self.message:
            await self.message.edit(embed=embed, view=self)

    async def view_count(self, interaction):
        count = self.scoped_state.get("clicks", 0)
        await interaction.response.send_message(
            f"Your personal click count: **{count}**", ephemeral=True,
        )

    async def update_from_state(self, state):
        pass


class VotingView(StatefulView):
    """Voting demo using action batching, computed state, and event hooks.

    Each vote dispatches two actions atomically via batch():
    one to update the vote count, one to log the activity.
    The total is derived from a @computed value (cached, lazy).
    A hook logs every component interaction to the console.
    """

    session_limit = 1
    owner_only = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulButton(
            label="Vote +1", style=discord.ButtonStyle.primary,
            callback=self.upvote,
        ))
        self.add_item(StatefulButton(
            label="Vote -1", style=discord.ButtonStyle.danger,
            callback=self.downvote,
        ))
        self.add_item(StatefulButton(
            label="Refresh", style=discord.ButtonStyle.secondary,
            callback=self.refresh,
        ))
        self.add_exit_button()

    async def _cast_vote(self, interaction, delta):
        await interaction.response.defer()

        # Batch: vote count + activity log dispatched atomically.
        # Subscribers and persistence fire once after both complete.
        async with self.batch() as b:
            await b.dispatch("VOTE_CAST", {
                "user_id": interaction.user.id,
                "delta": delta,
            })
            await b.dispatch("VOTE_LOG", {
                "entry": f"{interaction.user.display_name} voted {'+1' if delta > 0 else '-1'}",
            })

        await self._refresh_embed()

    async def upvote(self, interaction):
        await self._cast_vote(interaction, 1)

    async def downvote(self, interaction):
        await self._cast_vote(interaction, -1)

    async def refresh(self, interaction):
        await interaction.response.defer()
        await self._refresh_embed()

    async def _refresh_embed(self):
        store = get_store()
        votes = store.state.get("application", {}).get("votes", {})
        log = store.state.get("application", {}).get("vote_log", [])

        # Computed value — cached, only recomputes when votes dict changes
        total = store.computed["total_votes"]

        embed = discord.Embed(
            title="Voting Demo",
            description=f"**Total votes:** {total}",
            color=discord.Color.gold(),
        )

        if votes:
            breakdown = "\n".join(f"<@{uid}>: {v}" for uid, v in votes.items())
            embed.add_field(name="Per-User Breakdown", value=breakdown, inline=True)

        if log:
            recent = "\n".join(log[-5:])
            embed.add_field(name="Recent Activity", value=recent, inline=True)

        embed.set_footer(text="Batched dispatch + computed state + hooks")

        if self.message:
            await self.message.edit(embed=embed, view=self)

    async def update_from_state(self, state):
        pass


# // ========================================( Cog )======================================== // #


class StateFeaturesExample(commands.Cog, name="state_features_example"):
    """Demonstrates state scoping, action batching, computed values, and event hooks."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._hook_registered = False

    async def cog_load(self):
        # Register an event hook to log component interactions
        store = get_store()
        store.on("component_interaction", self._log_interaction)
        self._hook_registered = True

    async def cog_unload(self):
        if self._hook_registered:
            store = get_store()
            store.off("component_interaction", self._log_interaction)

    async def _log_interaction(self, action, state):
        view_id = action["payload"].get("view_id", "?")
        component = action["payload"].get("component_id", "?")
        logger.info(f"[Hook] Component '{component}' in view {view_id}")

    @commands.hybrid_command(
        name="scopetest",
        description="Per-user scoped state counter."
    )
    async def scopetest(self, context: Context) -> None:
        view = ScopedCounterView(context=context)

        embed = discord.Embed(
            title="Scoped Counter",
            description=(
                "Your clicks: **0**\n\n"
                "Each user has an independent counter via state scoping."
            ),
            color=discord.Color.blue(),
        )
        await view.send(embed=embed)

    @commands.hybrid_command(
        name="advancedtest",
        description="Batching, computed state, and hooks demo."
    )
    async def advancedtest(self, context: Context) -> None:
        view = VotingView(context=context)

        embed = discord.Embed(
            title="Voting Demo",
            description=(
                "**Total votes:** 0\n\n"
                "Cast votes to see batching, computed state, and hooks in action."
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Batched dispatch + computed state + hooks")
        await view.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(StateFeaturesExample(bot=bot))
