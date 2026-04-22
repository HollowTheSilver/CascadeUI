"""
V2 Leaderboard -- CascadeUI Ranked Display Pattern
==================================================

A server leaderboard that ranks guild members by a simulated MMR value.
Works in any guild without configuration: when the Members privileged
intent is enabled and the cache is populated, real members fill the
top of the board; any remaining slots are padded with deterministic
synthetic Demo Player rows so the display is always exactly 25
entries across 5 pages.

Demonstrates:

    - ``LeaderboardLayoutView`` for paginated ranked displays
    - ``entry_layout = "sections"`` for rich per-entry rendering with
      avatar thumbnails
    - Section-mode split hooks: ``format_primary`` + ``format_secondary``
      for the two-line entry body, plus async ``get_avatar_url`` for
      the thumbnail accessory
    - Visual parity across rows: real members resolve an avatar via
      ``bot.fetch_user``; synthetic rows carry a ``synthetic`` flag
      and route to a Discord default avatar URL so every Section
      renders with a thumbnail accessory. Synthetic user IDs are
      fake-but-valid-shape snowflakes so the client renders
      ``<@ID>`` as an "@Unknown User" mention pill -- same blue
      highlight as a real member mention, no Members intent required.
      The library's TextDisplay-collapse fallback remains the
      last-resort path (Section requires a non-``None`` accessory)
      -- this example bypasses it by always returning a URL.
    - ``build_summary`` union-return hook: returning a ``dict[str, str]``
      renders the aggregate stats inline on page 1 (library default);
      returning a ``Container`` (via ``card(...)``) promotes the summary
      to a standalone card rendered above the rankings card on every
      page. This example returns a ``Container`` so the aggregate
      header stays visible while the user flips through all five
      pages of rankings.
    - Symmetric ``title=`` + ``subtitle=`` constructor kwargs: both are
      three-tier (class default -> subclass override -> explicit arg).
      This example passes both at init -- ``title`` to splice in the
      guild name, and ``subtitle=None`` to suppress the H3 since the
      standalone Overview card returned from ``build_summary`` already
      acts as the top-level heading slot.
    - ``leaderboard_top_n`` + ``leaderboard_per_page`` for multi-page nav
    - The ``(user_id, stats_dict)`` tuple contract the pattern consumes
    - ``progress_bar`` used inline for a live win-rate cell
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

from cascadeui import LeaderboardLayoutView, card, divider, key_value, progress_bar


# // ========================================( Config )======================================== // #


_TARGET_SIZE = 25  # Exactly 5 pages of 5 entries

# Base for the fake-but-valid-shape snowflakes assigned to synthetic demo
# rows. A 64-bit integer in the 10^17 range is a plausible Discord
# snowflake shape; the client tries to resolve it, fails, and renders
# ``<@ID>`` as an "@Unknown User" pill. That's what gives the demo rows
# the blue mention-pill look without requiring real guild members.
_FAKE_SNOWFLAKE_BASE = 100_000_000_000_000_000

# Discord's six default avatar PNGs. Returning one of these for a synthetic
# entry restores visual parity with real-member rows: every Section renders
# with an accessory instead of the TextDisplay-collapse fallback.
_DEFAULT_AVATAR_URL = "https://cdn.discordapp.com/embed/avatars/{n}.png"


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
    real guild members without requiring the Members intent. The
    ``synthetic`` flag tells ``get_avatar_url`` to skip the
    ``fetch_user`` round-trip -- 25 failed lookups per rebuild would
    otherwise burn rate-limit budget for nothing.
    """
    synthetic_id = _FAKE_SNOWFLAKE_BASE + index
    stats = _mock_stats_for(synthetic_id)
    stats["synthetic"] = True
    return (synthetic_id, stats)


def _build_entries(real_members) -> tuple:
    """Produce exactly ``_TARGET_SIZE`` entries plus a mode label.

    Real members fill the top by MMR and anchor the rankings; any
    remaining slots take synthetic rows. When ``real_members`` is
    empty (intent disabled or cache not populated), the full board
    is synthetic so the example never errors out.
    """
    real = [
        (member.id, _mock_stats_for(member.id))
        for member in real_members
        if not member.bot
    ]
    real.sort(key=lambda row: row[1]["mmr"], reverse=True)

    if not real:
        entries = [_synthetic_entry(i) for i in range(_TARGET_SIZE)]
        return entries, "Demo (Members intent disabled)"

    real = real[:_TARGET_SIZE]
    if len(real) < _TARGET_SIZE:
        pad = [_synthetic_entry(i) for i in range(_TARGET_SIZE - len(real))]
        return real + pad, f"Live server ({len(real)} real + {len(pad)} demo)"

    return real, f"Live server ({len(real)} real)"


# // ========================================( Leaderboard View )======================================== // #


class ServerLeaderboard(LeaderboardLayoutView):
    """Server leaderboard with real-member ranking and demo padding.

    Always renders exactly 25 entries across 5 pages of 5. Real members
    occupy the top slots when available; synthetic Demo Player rows
    pad the rest so the paginated layout stays consistent regardless
    of guild size or intent configuration.

    Runs in Section render mode: each entry is a two-line
    ``Section`` with an avatar thumbnail accessory. Real members
    resolve their Discord avatar; synthetic rows skip the resolve
    and route to a Discord default avatar so every Section renders
    with a thumbnail.
    """

    leaderboard_top_n = _TARGET_SIZE
    leaderboard_per_page = 5
    entry_layout = "sections"
    exit_policy = "delete"

    def __init__(self, *args, mode: str = "", bot=None, **kwargs):
        # ``bot`` is captured here (not pulled from ``context`` later) so
        # ``get_avatar_url`` can reach it without touching the interaction.
        # Section-mode leaderboards typically need a user-fetch entry point,
        # and passing the bot at construction makes the dependency explicit.
        self._mode = mode
        self._bot = bot
        super().__init__(*args, **kwargs)

    def format_primary(self, rank: int, user_id: int, stats: dict) -> str:
        """Top line of the section: rank + name.

        Mirrors the library default (``format_rank`` + ``format_name``);
        overridden here so every Section-mode hook sits alongside its siblings.
        """
        return f"{self.format_rank(rank)} {self.format_name(user_id, stats)}"

    def format_secondary(self, rank: int, user_id: int, stats: dict) -> str:
        """Bottom line of the section: MMR, W/G, and a live win-rate bar.

        ``progress_bar`` is a first-class V2 builder that returns a
        ``TextDisplay``; the ``.content`` attribute holds the rendered
        bar string, which this override embeds inline in the secondary
        row. Bar width stays small (6 cells) so it fits cleanly alongside
        the numeric stats without wrapping.
        """
        games = stats["games"]
        wins = stats["wins"]
        bar = progress_bar(wins, games or 1, width=6, show_percent=True).content
        return (
            f"`{stats['mmr']}` MMR \u2022 "
            f"{wins}W / {games}G \u2022 "
            f"{bar}"
        )

    async def get_avatar_url(self, user_id: int, stats: dict):
        """Resolve a thumbnail URL for the Section accessory.

        Real members route through ``bot.fetch_user`` so the avatar
        reflects their current Discord profile. Synthetic rows and
        ``fetch_user`` failures both route to a Discord default avatar
        URL so every Section still renders with an accessory --
        otherwise the library's TextDisplay-collapse fallback would
        kick in and produce an uneven display where only some rows
        have thumbnails.

        The try/except swallows any ``HTTPException`` from ``fetch_user``
        (rate limit, user not found) and degrades to the same default.
        """
        # Synthetic rows carry a ``synthetic`` flag so this hook can
        # skip the ``fetch_user`` round-trip entirely -- the fake
        # snowflake would fail resolution anyway, and 25 failed lookups
        # per page rebuild would burn rate-limit budget for nothing.
        # The default avatar slot (0-5) is picked via ``user_id % 6``
        # so the same demo entry always draws the same face.
        if stats.get("synthetic") or self._bot is None:
            return _DEFAULT_AVATAR_URL.format(n=user_id % 6)
        try:
            user = await self._bot.fetch_user(user_id)
        except Exception:
            return _DEFAULT_AVATAR_URL.format(n=user_id % 6)
        # A 128px CDN variant renders noticeably faster client-side than the
        # default 1024px asset -- Section thumbnails are small on-screen, so
        # the larger original is wasted bytes through the Discord client's
        # image pipeline.
        return user.display_avatar.with_size(128).url

    def build_summary(self, entries):
        """Return a standalone Container so the summary persists across pages.

        The ``build_summary`` hook is union-return: a ``dict[str, str]``
        renders inline on page 1 only, a ``Container`` renders as a
        standalone top-level card on every page, and ``None`` (or an
        empty dict) skips the summary entirely. This override returns
        a ``Container`` so the Mode row and aggregate stats stay
        visible while the user flips through all five pages of rankings.

        The ``Mode`` row identifies the data source (real, demo, or a
        mix). Remaining rows aggregate the full 25-entry slice.
        """
        total_games = sum(e[1]["games"] for e in entries)
        total_wins = sum(e[1]["wins"] for e in entries)
        avg_mmr = (sum(e[1]["mmr"] for e in entries) // len(entries)) if entries else 0
        summary = {
            "Ranked players": str(len(entries)),
            "Games played": str(total_games),
            "Total wins": str(total_wins),
            "Average MMR": str(avg_mmr),
        }
        if self._mode:
            summary["Mode"] = self._mode
        return card(
            TextDisplay("## Overview"),
            divider(),
            key_value(summary),
        )


# // ========================================( Cog )======================================== // #


class LeaderboardCog(commands.Cog, name="v2_leaderboard_example"):
    """Server leaderboard command demonstrating ``LeaderboardLayoutView``."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="leaderboard",
        description="Show the server leaderboard",
    )
    async def leaderboard(self, context: Context):
        """Build the leaderboard with real members when available.

        The cog inspects ``bot.intents.members`` and the guild member
        cache. When both are available, real members fill the top by
        MMR and synthetic rows pad up to 25 entries. Otherwise the
        board is fully synthetic so the example still produces a
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
        )
        await view.send(ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
