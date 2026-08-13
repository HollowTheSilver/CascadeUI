"""
V2 Computed Values -- CascadeUI Memoized Derived State
======================================================

A quick poll demonstrating ``@computed`` for global memoized values
that multiple views can share without recalculating:

    - ``@computed`` decorator registers a derived value on the store
    - Selector picks a state slice; compute function transforms it
    - Result is cached until the selector output changes
    - Any view reads the same cache via ``store.computed["name"]``
    - Compared to ``state_selector()`` (per-view change detection),
      computed values are global and shared across all views
    - ``subscribed_actions`` + default ``on_state_changed()`` for
      automatic rebuild on cross-view state changes

The poll stores raw votes in application state via a custom reducer,
keyed per guild so one bot serving several servers keeps each
server's poll separate. Two ``@computed`` values derive per-guild
totals and the current leader in one shared cache; the view reads its
own guild's slice in ``build_ui()`` and displays it alongside the raw
vote buttons.

Commands:
    /poll   Open a quick poll

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    StatefulButton,
    StatefulLayoutView,
    access_slot,
    card,
    cascade_reducer,
    computed,
    gap,
    get_store,
    key_value,
    read_slot,
    render_progress,
    stats_card,
)

# // ========================================( Config )======================================== // #


# Poll choices with their display emoji
CHOICES = {
    "python": "\N{SNAKE}",
    "rust": "\N{CRAB}",
    "go": "\N{HAMSTER FACE}",
    "typescript": "\N{BLUE BOOK}",
}


# // ========================================( Reducer )======================================== // #


@cascade_reducer("POLL_VOTE")
async def poll_vote_reducer(action, state):
    """Record a vote in the voting guild's own bucket of application state.

    Each user gets one vote per guild. Changing your vote removes the
    old one. The ``@cascade_reducer`` decorator auto-deepcopies state,
    so mutations here are safe. ``access_slot``'s ``key=`` partitions
    the bucket by guild: without it, every guild running this cog
    would read and write the same votes dict, and a poll opened in one
    server would show votes cast in another.
    """
    poll = access_slot(state, "poll", action["payload"]["guild_id"])
    poll.setdefault("votes", {})
    poll.setdefault("total_voters", 0)

    user_id = str(action["payload"]["user_id"])
    choice = action["payload"]["choice"]
    votes = poll["votes"]

    # Track previous choice for vote switching
    previous = None
    for lang, voters in votes.items():
        if user_id in voters:
            previous = lang
            break

    if previous == choice:
        return state

    # Remove old vote
    if previous:
        votes[previous].remove(user_id)

    # Add new vote
    votes.setdefault(choice, []).append(user_id)

    # Recount unique voters
    all_voters = set()
    for voters in votes.values():
        all_voters.update(voters)
    poll["total_voters"] = len(all_voters)

    return state


# // ========================================( Computed Values )======================================== // #


# The selector picks the slice of state to watch.
# The decorated function transforms that slice into the derived value.
# Results are cached until the selector output changes -- ten views
# reading the same computed value share one cached result.


@computed(selector=lambda s: s.get("application", {}).get("poll", {}))
def vote_totals(poll_by_guild):
    """Derive per-choice vote counts for every guild's poll.

    Returns ``{guild_id: {choice: count}}`` -- one cached computation
    serves every guild's ``PollView``, and each view reads its own
    ``guild_id`` key. Mirrors the per-guild ``@computed`` leaderboards
    in the TicTacToe and Battleship examples. Without ``@computed``,
    every view would recalculate this on every render; with it, the
    totals are computed once per guild and cached until that guild's
    votes dict changes.
    """
    return {
        guild_id: {lang: len(voters) for lang, voters in data.get("votes", {}).items()}
        for guild_id, data in poll_by_guild.items()
    }


@computed(selector=lambda s: s.get("application", {}).get("poll", {}))
def poll_leader(poll_by_guild):
    """Derive the current leader (or None for a tie/empty) per guild.

    Returns ``{guild_id: leader_or_None}``. Demonstrates a computed
    value that returns a derived scalar per key rather than a
    transformed collection.
    """
    leaders = {}
    for guild_id, data in poll_by_guild.items():
        votes = data.get("votes", {})
        counts = {lang: len(voters) for lang, voters in votes.items()}
        max_count = max(counts.values(), default=0)

        if max_count == 0:
            leaders[guild_id] = None
            continue

        top = [lang for lang, count in counts.items() if count == max_count]
        leaders[guild_id] = top[0] if len(top) == 1 else None  # None on a tie
    return leaders


# // ========================================( Views )======================================== // #


class PollView(StatefulLayoutView):
    """Quick poll with computed-value-driven summary display.

    Demonstrates:
        - Reading ``store.computed["name"]`` in ``build_ui()``
        - ``subscribed_actions`` for cross-view rebuild on vote changes
        - ``stats_card()`` and ``card()`` auto-reading the theme context
        - ``owner_only = False`` so any guild member can vote
    """

    # // ----( Policy surface )---- // #

    # Anyone in the guild can vote, not just the command invoker
    owner_only = False
    instance_limit = 1
    instance_scope = "guild"
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "delete"
    auto_defer = True
    serialize_interactions = True

    # Subscribe to POLL_VOTE so the view rebuilds when anyone votes.
    # The default ``on_state_changed()`` calls ``build_ui()`` then
    # ``refresh()`` -- no override needed for this pattern.
    subscribed_actions = {"POLL_VOTE"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.build_ui()

    def build_ui(self):
        self.clear_items()

        store = get_store()

        # Read computed values from the store's global cache.
        # ``store.computed["name"]`` calls ``ComputedValue.get(state)``
        # internally -- the selector checks if the input changed, and
        # only then runs the compute function. Both values are keyed by
        # guild, so this view reads only its own guild's slice.
        totals = store.computed["vote_totals"].get(self.guild_id, {})
        leader = store.computed["poll_leader"].get(self.guild_id)
        total_voters = read_slot(store.state, "poll", self.guild_id, "total_voters", default=0)

        # Header card -- theme accent applied automatically via
        # the contextvars-based theme context set by build_ui wrapping
        self.add_item(card("## \N{BAR CHART} Quick Poll", "Vote for your favorite language!"))
        self.add_item(gap())

        # Results card using stats_card -- also theme-aware.
        # stats_card() reads get_current_theme() when no explicit
        # color= is passed, same as card().
        result_stats = {}
        # Scaled against the leading choice rather than a fixed vote count, so
        # the bar stays five cells wide however many votes arrive. render_progress
        # returns the bar as a string for inlining; progress_bar() is the same
        # thing wrapped in a TextDisplay, for when it is its own component.
        busiest = max([*totals.values(), 1])
        for lang, emoji in CHOICES.items():
            count = totals.get(lang, 0)
            bar = render_progress(count, busiest, width=5, show_percent=False)
            label = f"{emoji} {lang.capitalize()}"
            result_stats[label] = f"`{bar}` {count}"

        footer = None
        if leader:
            footer = f"{CHOICES[leader]} {leader.capitalize()} is leading!"
        elif total_voters > 0:
            footer = "It's a tie!"

        self.add_item(stats_card("Results", result_stats, footer=footer))
        self.add_item(gap())

        # Vote buttons in an ActionRow
        buttons = []
        for lang, emoji in CHOICES.items():
            buttons.append(
                StatefulButton(
                    label=lang.capitalize(),
                    emoji=emoji,
                    style=discord.ButtonStyle.primary,
                    callback=self._make_vote_callback(lang),
                )
            )
        self.add_item(ActionRow(*buttons))

        # Voter count as subtext
        self.add_item(TextDisplay(f"-# {total_voters} vote{'s' if total_voters != 1 else ''} cast"))

    def _make_vote_callback(self, choice):
        """Build a vote callback for a specific choice.

        ``dispatch("POLL_VOTE")`` fires the custom reducer, which
        updates application state. All PollView instances subscribed
        to ``POLL_VOTE`` rebuild automatically via the default
        ``on_state_changed()`` -> ``build_ui()`` -> ``refresh()``
        chain.
        """

        async def callback(interaction):
            await self.dispatch(
                "POLL_VOTE",
                {"user_id": interaction.user.id, "choice": choice, "guild_id": self.guild_id},
            )

        return callback


# // ========================================( Cog )======================================== // #


class ComputedCog(commands.Cog, name="v2_computed_example"):
    """Quick poll demonstrating @computed for memoized derived state."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(name="poll", description="Open a quick poll")
    async def poll(self, ctx: Context):
        """Open a quick poll for the server.

        Anyone in the guild can vote. Changing your vote removes the
        old one. The poll uses ``@computed`` to derive totals and
        leader from raw vote data.
        """
        if not ctx.guild:
            await ctx.send("This command can only be used in a server.", ephemeral=True)
            return

        view = PollView(context=ctx)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(ComputedCog(bot=bot))
