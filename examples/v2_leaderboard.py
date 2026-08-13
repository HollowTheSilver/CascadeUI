"""
V2 Leaderboard -- CascadeUI Ranked Display Pattern
==================================================

A server leaderboard that ranks guild members by a simulated MMR value.
Works in any guild without configuration: when the Members privileged
intent is enabled and the cache is populated, real members take the
available slots and deterministic synthetic Demo Player rows fill the
rest, so the display is always exactly 25 entries across 5 pages. The
assembled board ranks by MMR, so the two kinds interleave.

Demonstrates:

    - ``LeaderboardLayoutView`` for paginated ranked displays
    - ``entry_layout = "sections"`` for rich per-entry rendering with
      avatar thumbnails
    - Section-mode entry body via ``format_secondary`` (the second line:
      MMR, W/G, win-rate bar). ``format_primary`` (rank + name) and avatar
      resolution both use the library defaults -- passing ``bot=`` lets the
      default ``get_avatar_url`` resolve thumbnails from the user cache
    - Visual parity across rows: real members resolve an avatar from the
      user cache; synthetic demo rows miss it and render a Discord default
      avatar, so every Section keeps its thumbnail. Synthetic user IDs are
      fake-but-valid-shape snowflakes so the client renders ``<@ID>`` as an
      "@Unknown User" mention pill: same blue highlight as a real member
      mention, no Members intent required.
      The library's TextDisplay-collapse fallback remains the
      last-resort path (Section requires a non-``None`` accessory).
      This example bypasses it by always returning a URL.
    - ``build_header`` builds the Overview stats card above the rankings on
      every page, so the aggregate stats stay visible while the user flips
      through. Stats are composed content, not a separate hook: the override reads
      ``self.ranked_entries`` (the loaded top-N) and returns a ``card()``
      that renders as its own card above the rankings. The guild icon rides
      as the card's heading accessory via ``image_section``.
    - ``build_footer`` renders a ``-#`` caption folded inside the rankings
      card on every page, so the caption reads as the card's footer rather
      than floating below it.
    - Symmetric ``title=`` + ``subtitle=`` constructor kwargs: both are
      three-tier (class default -> subclass override -> explicit arg).
      This example passes both at init -- ``title`` to splice in the
      guild name, and ``subtitle=None`` to suppress the H3 since the
      Overview card from ``build_header`` already carries its own heading.
    - ``leaderboard_top_n`` + ``leaderboard_per_page`` for multi-page nav
    - The ``(user_id, stats_dict)`` tuple contract the pattern consumes
    - ``render_progress`` used inline for a live win-rate cell
    - ``reload(force=True)``: a "Toggle bars" button flips a render-only
      display flag and reloads; ``force`` bypasses the entry-signature
      short-circuit so an unchanged-entries re-render still rebuilds the
      pages
    - Graceful degradation when privileged intents are unavailable

Contrast with ``v2_battleship.py``: that example reads live stats from
``store.computed["battleship_leaderboards"]``. This one synthesizes a
plausible-looking dataset. The rendering surface is identical -- both
feed the same tuple shape into the same view class.

Commands:
    /leaderboard   Open the server leaderboard

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import random

from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import TextDisplay

from cascadeui import (
    LeaderboardLayoutView,
    action_section,
    card,
    divider,
    image_section,
    key_value,
    render_progress,
)

# // ========================================( Config )======================================== // #


_TARGET_SIZE = 25  # Exactly 5 pages of 5 entries

# Base for the fake-but-valid-shape snowflakes assigned to synthetic demo
# rows. A 64-bit integer in the 10^17 range is a plausible Discord
# snowflake shape; the client tries to resolve it, fails, and renders
# ``<@ID>`` as an "@Unknown User" pill. That's what gives the demo rows
# the blue mention-pill look without requiring real guild members.
_FAKE_SNOWFLAKE_BASE = 100_000_000_000_000_000


# // ========================================( Data )======================================== // #


def _mock_stats_for(seed: int) -> dict:
    """Synthesize stable stats from a deterministic seed.

    Seeding ``random.Random`` with the same value always produces the
    same stats, so the leaderboard does not reshuffle between opens.
    In a real deployment the stats would come from ``store.computed[...]``
    or a persistent scoped slot -- the tuple shape fed into the view
    is identical.
    """
    rng = random.Random(seed)
    games = rng.randint(5, 80)
    win_rate = rng.uniform(0.35, 0.72)
    wins = int(games * win_rate)
    mmr = 1000 + rng.randint(-250, 900)
    streak = rng.randint(0, 6)
    return {"games": games, "wins": wins, "mmr": mmr, "streak": streak}


def _synthetic_entry(index: int) -> tuple:
    """Build one padding entry with a fake-but-valid-shape snowflake.

    The id is a plausible 10^17-range snowflake the Discord client
    tries to resolve and fails, rendering ``<@ID>`` as an "@Unknown
    User" mention pill. That preserves the blue mention-pill look of
    real guild members without requiring the Members intent. A fake
    snowflake is never in the user cache, so the library's default
    ``get_avatar_url`` falls back to a Discord default avatar for it.
    """
    synthetic_id = _FAKE_SNOWFLAKE_BASE + index
    return (synthetic_id, _mock_stats_for(synthetic_id))


def _build_entries(real_members) -> tuple:
    """Produce exactly ``_TARGET_SIZE`` entries plus a mode label.

    Real members take the available slots first; any remainder is
    filled with synthetic rows. When ``real_members`` is empty (intent
    disabled or cache not populated), the full board is synthetic so
    the example never errors out.
    """
    real = [(member.id, _mock_stats_for(member.id)) for member in real_members if not member.bot]
    real.sort(key=lambda row: row[1]["mmr"], reverse=True)

    if not real:
        entries = [_synthetic_entry(i) for i in range(_TARGET_SIZE)]
        label = "Demo (Members intent disabled)"
    else:
        real = real[:_TARGET_SIZE]
        pad = [_synthetic_entry(i) for i in range(_TARGET_SIZE - len(real))]
        entries = real + pad
        label = (
            f"Live server ({len(real)} real + {len(pad)} demo)"
            if pad
            else f"Live server ({len(real)} real)"
        )

    # Rank the assembled board, not just the real half. The footer states
    # "sorted by MMR", and synthetic padding interleaves with real members
    # rather than trailing them.
    entries.sort(key=lambda row: row[1]["mmr"], reverse=True)
    return entries, label


# // ========================================( Leaderboard View )======================================== // #


class ServerLeaderboard(LeaderboardLayoutView):
    """Server leaderboard with real-member ranking and demo padding.

    Always renders exactly 25 entries across 5 pages of 5. Real members
    take the available slots when present and synthetic Demo Player rows
    fill the rest, so the paginated layout stays consistent regardless of
    guild size or intent configuration. Ranking is by MMR across the whole
    board, so a synthetic row can outrank a real one.

    Runs in Section render mode: each entry is a two-line
    ``Section`` with an avatar thumbnail accessory. Passing ``bot=``
    lets the library's default ``get_avatar_url`` resolve real members
    from the user cache; synthetic rows miss the cache and fall back to
    a Discord default avatar, so every Section renders with a thumbnail.
    """

    leaderboard_top_n = _TARGET_SIZE
    leaderboard_per_page = 5
    # Jump buttons (first / last / go-to-page) appear at >= jump_threshold
    # pages. Raised to 6 so this 5-page board shows only prev / next, freeing
    # two nav slots for the in-card build_footer (each page nears the 40-node cap).
    jump_threshold = 6
    entry_layout = "sections"
    exit_policy = "delete"

    def __init__(self, *args, mode: str = "", icon_url=None, **kwargs):
        # ``bot=`` flows through to the base view, which stores it for the
        # default ``get_avatar_url``. This example passes no avatar hook of its
        # own -- the library resolves thumbnails from the bot's user cache.
        self._mode = mode
        # Guild icon URL for the Overview card heading; ``None`` when the guild
        # has no icon, which ``build_header`` degrades to a plain heading.
        self._icon_url = icon_url
        # ``_detailed`` toggles the win-rate bar in every row. The button on
        # the Overview card (built in ``build_header``) flips it and calls
        # ``reload(force=True)``: the entry data is unchanged, so the
        # entry-signature short-circuit would skip the rebuild without the flag.
        self._detailed = True
        super().__init__(*args, **kwargs)

    def format_secondary(self, rank: int, user_id: int, stats: dict) -> str:
        """Bottom line of the section: MMR, W/G, and a live win-rate bar.

        ``render_progress`` returns the bar as a string, which is what a
        row wants: this override embeds it inline rather than adding a
        component. ``progress_bar`` is the same bar wrapped in a
        ``TextDisplay``, for when it stands on its own. Bar width stays
        small (6 cells) so it fits alongside the numeric stats without
        wrapping.
        """
        games = stats["games"]
        wins = stats["wins"]
        line = f"`{stats['mmr']}` MMR \N{BULLET} {wins}W / {games}G"
        if self._detailed:
            bar = render_progress(wins, games or 1, width=6, show_percent=True)
            line = f"{line} \N{BULLET} {bar}"
        return line

    # Every page carries two cards (the Overview stats card + the rankings card
    # with its in-card footer) plus the nav row, under Discord's 40-component
    # cap. jump_threshold = 6 drops the first / last / go-to buttons on this
    # 5-page board to keep headroom.
    def build_header(self, page: int):
        """Build the Overview stats card above the rankings, on every page.

        Stats are content you compose, not a separate hook: this reads
        ``self.ranked_entries`` (the loaded top-N slice) and returns a
        ``card()``. A ``Container`` return renders as its own card above the
        rankings on every page, so the aggregate stats stay visible while the
        user flips through the rankings. The guild icon rides as the card's
        heading accessory via ``image_section`` when the guild has one, and
        degrades to a plain ``## Overview`` heading otherwise. (``page`` is
        available to gate a frame to page 1 only; this example keeps the
        Overview visible throughout.)
        """
        entries = self.ranked_entries
        total_games = sum(e[1]["games"] for e in entries)
        avg_mmr = (sum(e[1]["mmr"] for e in entries) // len(entries)) if entries else 0
        stats = {
            "Ranked players": str(len(entries)),
            "Games played": str(total_games),
            "Average MMR": str(avg_mmr),
        }
        if self._mode:
            stats["Mode"] = self._mode
        heading = (
            image_section("## Overview", url=self._icon_url)
            if self._icon_url
            else TextDisplay("## Overview")
        )
        return card(
            heading,
            divider(),
            key_value(stats),
            action_section(
                f"Win-rate bars: {'on' if self._detailed else 'off'}",
                label="Toggle bars",
                callback=self._toggle_detail,
                custom_id="lb_toggle_detail",
            ),
        )

    def build_footer(self, page: int):
        """Render an in-card caption below the entries on every page.

        build_footer content rides inside the rankings card, so a short ``-#``
        subtext line reads as the card's footer. It states the sort basis, which
        the Mode row does not cover, so it adds information rather than repeating
        the Overview stats.
        """
        return TextDisplay("-# Rankings sorted by MMR")

    async def _toggle_detail(self, interaction):
        # The ranking entries do not change, only how each row renders, so the
        # entry-signature short-circuit in rebuild_pages would skip the rebuild.
        # reload(force=True) rebuilds the pages anyway and ships the new render.
        self._detailed = not self._detailed
        await self.reload(force=True)


# // ========================================( Cog )======================================== // #


class LeaderboardCog(commands.Cog, name="v2_leaderboard_example"):
    """Server leaderboard command demonstrating ``LeaderboardLayoutView``."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="leaderboard",
        description="Show the server leaderboard",
    )
    async def leaderboard(self, context: Context):
        """Build the leaderboard with real members when available.

        The cog inspects ``bot.intents.members`` and the guild member
        cache. When both are available, real members take the available
        slots and synthetic rows fill up to 25. Otherwise the board is
        fully synthetic so the example still produces a
        complete five-page display.
        """
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        intent_enabled = context.bot.intents.members
        members = list(context.guild.members) if intent_enabled else []
        entries, mode = _build_entries(members)

        view = ServerLeaderboard(
            context=context,
            entries=entries,
            title=f"Leaderboard - {context.guild.name}",
            subtitle=None,
            mode=mode,
            bot=context.bot,
            icon_url=context.guild.icon.url if context.guild.icon else None,
        )
        await view.send(ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(LeaderboardCog(bot=bot))
