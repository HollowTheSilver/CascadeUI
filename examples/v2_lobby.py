"""
V2 Lobby -- CascadeUI Open-Join Multi-User Pattern
===================================================

A Werewolf/trivia-style lobby demonstrating the open-join multi-user
pattern with V2 components:

    - ``participant_limit`` for view-capacity enforcement (max 8 players)
    - ``auto_register_participants = False`` -- joining is an explicit
      user action, not implied by membership in ``allowed_users``
    - ``register_participant`` bool-return contract: a single check
      handles both per-user session collisions and full-lobby rejection
    - ``on_participant_limit`` override that mentions the joiner so they
      see a personalized "lobby is full" message
    - Host vs participant authority distinction: anyone can Join/Leave,
      only the host (``user_id``) can Start Game or Disband
    - Live participant card refresh on every join/leave via ``refresh()``
    - ``unregister_participant`` for the Leave path -- the library owns
      both the join and leave directions, so users never touch
      ``_participants`` or the session index directly
    - ``protect_attached = False`` -- a lobby is a staging area, not
      a committed game. When the host opens a new lobby, the old one is
      replaced and ``on_replaced`` pings each waiting participant
    - ``card()`` + ``key_value()`` for the structured lobby display
    - Custom reducer tracking lobby completions in application state

Commands:
    /lobby [max_players]    Open a new lobby (host = invoker, max 2-8)

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow

from cascadeui import (
    StatefulButton,
    StatefulLayoutView,
    alert,
    card,
    cascade_reducer,
    divider,
    key_value,
    access_slot,
)

import logging

logger = logging.getLogger(__name__)


# // ========================================( Constants )======================================== // #


LOBBY_TIMEOUT = 600  # 10 minutes
DEFAULT_MAX_PLAYERS = 8
COLOR_OPEN = discord.Color.green()
COLOR_FULL = discord.Color.gold()
COLOR_CLOSED = discord.Color.dark_grey()


# // ========================================( Reducer )======================================== // #


def _lobby_stats_default():
    return {"opened": 0, "started": 0, "disbanded": 0}


@cascade_reducer("LOBBY_STARTED")
async def lobby_started_reducer(action, state):
    """Track lobby completions in application state."""
    stats = access_slot(state, "lobby", "stats", default_factory=_lobby_stats_default)
    stats["started"] += 1
    return state


@cascade_reducer("LOBBY_DISBANDED")
async def lobby_disbanded_reducer(action, state):
    """Track lobby disbands in application state."""
    stats = access_slot(state, "lobby", "stats", default_factory=_lobby_stats_default)
    stats["disbanded"] += 1
    return state


# // ========================================( Lobby View )======================================== // #


class LobbyView(StatefulLayoutView):
    """Open-join lobby for up to ``participant_limit`` players.

    The host (``user_id``) is automatically counted toward the cap and
    can never be ejected. Anyone in the channel may click Join until the
    lobby is full; only the host can Start Game or Disband.

    ``instance_limit = 1`` restricts each host to one active lobby.
    Opening a second lobby replaces the first. ``protect_attached``
    is ``False`` so replacement proceeds even with players waiting, and
    ``on_replaced`` notifies participants in the channel.

    ``allowed_users`` is intentionally left empty -- this is the
    open-join pattern. Authorization happens at the per-callback level
    (Start/Disband check ``interaction.user.id == self.user_id``), not
    at the view level via ``allowed_users``.
    """

    # No allowed_users -- open join. interaction_check passes everyone.
    owner_only = False
    participant_limit = DEFAULT_MAX_PLAYERS
    auto_register_participants = False  # Manual Join button instead
    participant_limit_message = "This lobby is full."
    timeout = LOBBY_TIMEOUT
    exit_policy = "delete"
    # One lobby per host. Opening a new lobby replaces the old one.
    instance_limit = 1
    instance_scope = "user"  # one lobby per host across all guilds
    instance_policy = "replace"
    replace_policy = "delete"  # library default; declared for policy-surface visibility
    # A lobby is a staging area, not a committed game - replacement is
    # expected when the host opens a fresh lobby. protect_attached
    # defaults to True, which would block replacement when participants
    # are present. False allows replacement to proceed, and the
    # on_replaced hook below notifies waiting players.
    protect_attached = False
    # ``state_scope = None`` because lobby stats live under custom reducers
    # written to the global state tree, not under any built-in scope key.
    state_scope = None
    auto_defer = True  # library default; declared for policy-surface visibility

    def __init__(self, *args, max_players: int = DEFAULT_MAX_PLAYERS, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-invocation policy override -- runs the same validator
        # pipeline as __init_subclass__, so a slash-command argument
        # outside the valid range fails immediately at view construction.
        self.set_class_attribute("participant_limit", max_players)
        self._started = False
        self.build_ui()

    # // ==================( UI )================== // #

    def build_ui(self) -> None:
        self.clear_items()

        host_mention = f"<@{self.user_id}>"
        slots_filled = len(self.participants) + 1  # +1 for host
        capacity = self.participant_limit

        if self._started:
            status = "Started"
            color = COLOR_CLOSED
        elif slots_filled >= capacity:
            status = "Full"
            color = COLOR_FULL
        else:
            status = "Open"
            color = COLOR_OPEN

        # roster_lines is always non-empty -- the host is line 1.
        roster_lines = [f"1. {host_mention} *(host)*"]
        for idx, uid in enumerate(sorted(self.participants), start=2):
            roster_lines.append(f"{idx}. <@{uid}>")
        roster_text = "\n".join(roster_lines)

        self.add_item(
            card(
                "## Game Lobby",
                key_value(
                    {
                        "Host": host_mention,
                        "Status": status,
                        "Slots": f"{slots_filled} / {capacity}",
                    }
                ),
                divider(),
                "### Players",
                roster_text,
                color=color,
            )
        )

        if not self._started:
            self.add_item(
                ActionRow(
                    StatefulButton(
                        label="Join",
                        style=discord.ButtonStyle.success,
                        emoji="\u2795",  # heavy plus
                        callback=self._join,
                    ),
                    StatefulButton(
                        label="Leave",
                        style=discord.ButtonStyle.secondary,
                        emoji="\u2796",  # heavy minus
                        callback=self._leave,
                    ),
                    StatefulButton(
                        label="Start Game",
                        style=discord.ButtonStyle.primary,
                        emoji="\u25b6",  # play
                        callback=self._start,
                    ),
                    StatefulButton(
                        label="Disband",
                        style=discord.ButtonStyle.danger,
                        emoji="\u274c",
                        callback=self._disband,
                    ),
                )
            )

    # // ==================( Hooks )================== // #

    async def on_participant_limit(self, user_id, interaction=None):
        """Custom rejection mentioning the joiner."""
        if interaction is not None:
            await self.respond(
                interaction,
                f"<@{user_id}> the lobby is full ({self.participant_limit} players).",
                ephemeral=True,
            )

    async def on_replaced(self):
        """Notify waiting players when the host opens a new lobby.

        The default on_replaced sends ``replaced_message`` to the
        channel. This override pings each participant by mention so
        they know the lobby they joined is gone.
        """
        if self.participants and self._message:
            mentions = " ".join(f"<@{uid}>" for uid in self.participants)
            await self._message.channel.send(
                f"{mentions} - the host opened a new lobby. This one has been closed."
            )

    # // ==================( Actions )================== // #

    async def _join(self, interaction: discord.Interaction):
        if interaction.user.id == self.user_id:
            await self.respond(interaction, "You're already in the lobby as the host.", ephemeral=True)
            return
        if interaction.user.id in self.participants:
            await self.respond(interaction, "You're already in the lobby.", ephemeral=True)
            return

        # Single bool-check covers both per-user session collisions
        # (on_instance_limit) and lobby capacity (on_participant_limit).
        if not await self.register_participant(interaction.user.id, interaction=interaction):
            return

        self.build_ui()
        await self.refresh()

    async def _leave(self, interaction: discord.Interaction):
        if interaction.user.id == self.user_id:
            await self.respond(
                interaction, "The host can't leave - use Disband to close the lobby.",
                ephemeral=True,
            )
            return
        if interaction.user.id not in self.participants:
            await self.respond(interaction, "You're not in the lobby.", ephemeral=True)
            return

        self.unregister_participant(interaction.user.id)
        self.build_ui()
        await self.refresh()

    async def _start(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await self.respond(
                interaction, "Only the host can start the game.", ephemeral=True
            )
            return

        self._started = True
        await self.dispatch("LOBBY_STARTED", {"players": len(self.participants) + 1})

        self.clear_items()
        self.add_item(
            alert(
                f"## Game Started\n{len(self.participants) + 1} players are in.",
                level="success",
            )
        )
        await self.refresh()

    async def _disband(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await self.respond(
                interaction, "Only the host can disband the lobby.", ephemeral=True
            )
            return

        await self.dispatch("LOBBY_DISBANDED", {})
        # exit_policy = "delete" handles the deletion -- no inline arg needed.
        await self.exit()


# // ========================================( Cog )======================================== // #


class LobbyExample(commands.Cog, name="v2_lobby_example"):
    """Open-join lobby with capacity enforcement."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="lobby",
        description="Open a new game lobby that anyone can join.",
    )
    @app_commands.describe(max_players="Maximum number of players (2-8, default 8)")
    async def lobby(
        self,
        context: Context,
        max_players: app_commands.Range[int, 2, 8] = DEFAULT_MAX_PLAYERS,
    ) -> None:
        view = LobbyView(
            context=context,
            user_id=context.author.id,
            guild_id=context.guild.id if context.guild else None,
            max_players=max_players,
        )
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(LobbyExample(bot))
