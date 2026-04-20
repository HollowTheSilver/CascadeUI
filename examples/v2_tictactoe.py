"""
V2 TicTacToe -- CascadeUI Multi-User Game
==========================================

A two-player TicTacToe game demonstrating multi-user interaction
patterns with V2 components:

    - Challenge acceptance flow (opponent must accept before the game starts)
    - ``allowed_users`` + ``register_participant`` for session-aware
      multi-user views
    - ``unauthorized_message`` for custom rejection text when a
      non-participant tries to click
    - ``check_instance_available()`` at the command level rejects the
      challenger before a challenge prompt is shown to the opponent
    - ``on_instance_limit`` override as a fallback that mentions the
      challenger by ID so the opponent knows which player is busy
    - ``protect_attached = True`` prevents the challenger from
      silently abandoning an active game with another player
    - ``exit_policy = "delete"`` on the disposable challenge prompt
    - Dynamic board size (3x3 to 5x5 via the size parameter)
    - Configurable win length (e.g. 3-in-a-row on a 5x5 board)
    - Turn enforcement inside move callbacks (not interaction_check)
    - Mutual rematch agreement (both players must confirm)
    - ActionRows of StatefulButtons forming the NxN game grid
    - Dynamic accent colors reflecting game state (turn, win, draw)
    - card() and alert() for structured visual feedback
    - Per-player lifetime stats under ``user_guild`` scope, written
      via ``SCOPED_UPDATE`` at game end -- no custom reducer needed

Commands:
    /tictactoe play @user [size] [win]   Challenge someone (size 3-5, win 3-size)
    /tictactoe stats [user]              Show a player's lifetime record
    /tictactoe leaderboard               Show server-wide rankings

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    DisplayLayoutView,
    InstanceLimitError,
    LeaderboardLayoutView,
    StateStore,
    StatefulButton,
    StatefulLayoutView,
    alert,
    button_grid,
    card,
    computed,
    divider,
    gap,
    get_store,
    key_value,
    stats_card,
)

import logging

logger = logging.getLogger(__name__)


# // ========================================( Constants )======================================== // #


EMPTY = "\u2800"  # Braille blank -- renders as a wide empty space on buttons
X_MARK = "\u2716"
O_MARK = "\u25ef"

# Colors for game states
COLOR_X_TURN = discord.Color.blurple()
COLOR_O_TURN = discord.Color.orange()
COLOR_X_WIN = discord.Color.blue()
COLOR_O_WIN = discord.Color.red()
COLOR_DRAW = discord.Color.light_grey()

CHALLENGE_TIMEOUT = 60  # seconds


# // ========================================( Helpers )======================================== // #


def _compute_win_lines(size: int, win_length: int) -> list[tuple[int, ...]]:
    """Compute all win lines for an NxN board with a given win length.

    Generates sliding windows of ``win_length`` consecutive cells across
    every row, column, and diagonal. When ``win_length == size`` this
    produces the same lines as standard NxN TicTacToe.
    """
    lines = []
    span = size - win_length + 1  # number of windows per axis

    # Rows
    for r in range(size):
        for start in range(span):
            lines.append(tuple(r * size + start + i for i in range(win_length)))
    # Columns
    for c in range(size):
        for start in range(span):
            lines.append(tuple((start + i) * size + c for i in range(win_length)))
    # Diagonals (top-left to bottom-right)
    for r in range(span):
        for c in range(span):
            lines.append(tuple((r + i) * size + (c + i) for i in range(win_length)))
    # Anti-diagonals (top-right to bottom-left)
    for r in range(span):
        for c in range(win_length - 1, size):
            lines.append(tuple((r + i) * size + (c - i) for i in range(win_length)))
    return lines


# // ========================================( Stats Schema )======================================== // #

# Per-player lifetime stats live under ``user_guild``-scoped state,
# written by ``SCOPED_UPDATE`` actions dispatched from
# ``TicTacToeView._record_player_stats`` at game end. Stats belong to
# one player and persist across opponents and rematches, exactly what
# scoped state is for.
#
# Schema at ``state["application"]["tictactoe_stats"]["user_guild:{uid}:{gid}"]``:
#
#     {
#         "games":    int,  # total games played
#         "wins":     int,  # outright wins (including by opponent forfeit)
#         "losses":   int,  # outright losses (including forfeits by self)
#         "draws":    int,  # drawn games
#         "forfeits": int,  # subset of losses where this player forfeited
#     }


# // ========================================( Challenge View )======================================== // #


class TicTacToeChallengeView(StatefulLayoutView):
    """Pre-game challenge prompt that the opponent must accept or decline.

    Only the opponent can interact (via allowed_users). One pending
    challenge per challenger per guild: a second ``/tictactoe`` invocation
    evicts the stale prompt via ``instance_policy = "replace"`` before
    the opponent responds. The game view is a separate class with its
    own instance slot, so accepting a challenge does not collide.
    """

    unauthorized_message = "Only the challenged player can respond."
    # One pending challenge per challenger per guild. A second
    # ``/tictactoe`` call by the same challenger evicts the old prompt
    # under ``instance_policy = "replace"``. Both attributes declared
    # explicitly so the full policy surface is visible in the class body.
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "replace"
    exit_policy = "delete"  # disposable prompt -- never freeze

    def __init__(
        self,
        *args,
        challenger_id: int,
        opponent: discord.Member,
        size: int,
        win_length: int,
        **kwargs,
    ):
        kwargs.setdefault("timeout", CHALLENGE_TIMEOUT)
        super().__init__(*args, **kwargs)
        self.challenger_id = challenger_id
        self.opponent = opponent
        self.size = size
        self.win_length = win_length
        self.allowed_users = {opponent.id}
        self.build_ui()

    def build_ui(self):
        self.clear_items()
        desc = f"a **{self.size}x{self.size}** game of TicTacToe"
        if self.win_length != self.size:
            desc += f" (**{self.win_length}** in a row)"
        self.add_item(
            card(
                "## TicTacToe Challenge",
                TextDisplay(f"<@{self.challenger_id}> challenges <@{self.opponent.id}> to {desc}!"),
                divider(),
                ActionRow(
                    StatefulButton(
                        label="Accept",
                        style=discord.ButtonStyle.success,
                        emoji="\u2714",
                        callback=self._accept,
                    ),
                    StatefulButton(
                        label="Decline",
                        style=discord.ButtonStyle.danger,
                        emoji="\u2716",
                        callback=self._decline,
                    ),
                ),
                color=discord.Color.blurple(),
            )
        )

    async def _accept(self, interaction: discord.Interaction):
        await self.exit()

        # Create the game view (challenger's context owns it for session limiting)
        view = TicTacToeView(
            interaction=interaction,
            user_id=self.challenger_id,
            guild_id=self.guild_id,
            opponent_id=self.opponent.id,
            size=self.size,
            win_length=self.win_length,
        )

        # auto_register_participants = True on TicTacToeView claims a slot
        # for both players from allowed_users during send(); rollback is
        # all-or-nothing, so a None return means zero side effects.
        if await view.send() is None:
            return

    async def _decline(self, interaction: discord.Interaction):
        self.clear_items()
        self.add_item(
            card(
                "## TicTacToe Challenge",
                TextDisplay(
                    f"<@{self.opponent.id}> declined the challenge from "
                    f"<@{self.challenger_id}>."
                ),
                color=discord.Color.dark_grey(),
            )
        )
        # exit_policy = "delete" governs the Accept path (handoff to the
        # game view). Decline and timeout show a final card instead of
        # disappearing, so both paths override the policy explicitly.
        await self.exit(delete_message=False)

    async def on_timeout(self):
        self.clear_items()
        self.add_item(
            card(
                "## TicTacToe Challenge",
                TextDisplay(
                    f"Challenge from <@{self.challenger_id}> to <@{self.opponent.id}> expired."
                ),
                color=discord.Color.dark_grey(),
            )
        )
        await self.exit(delete_message=False)


# // ========================================( Game View )======================================== // #


class TicTacToeView(StatefulLayoutView):
    """Two-player TicTacToe with V2 components and dynamic board size.

    Both players interact with the same message. ``allowed_users``
    restricts interaction to the two players, and turn enforcement
    in ``_make_move`` handles ordering. The opponent is registered
    as a participant so session limiting applies to both players.
    """

    unauthorized_message = "You're not part of this game."
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "replace"  # library default; declared for policy-surface visibility
    # protect_attached = True is the library default. The challenger
    # cannot silently abandon an active game -- they must exit the current
    # board before starting a new one. Without this, Player B's game would
    # vanish with no warning when Player A re-challenges someone else.
    protect_attached = True
    replace_policy = "delete"  # library default; declared for policy-surface visibility
    # ``state_scope = None`` because the live game board is ephemeral
    # view-local state -- nothing about an in-progress game belongs to
    # one player's profile. Lifetime stats are dispatched separately to
    # ``user_guild`` scope at game end via ``_record_player_stats``.
    state_scope = None
    # Lifetime stats land under a dedicated ``tictactoe_stats`` slot via
    # ``dispatch_scoped`` (see ``_record_player_stats``). Naming the slot
    # on the class (``scoped_slot``) keeps TicTacToe's per-player totals
    # in their own bucket instead of sharing the default ``scoped`` space
    # with other subsystems. Opting the slot in through ``persistent_slots``
    # marks it write-through for ``PersistenceMiddleware`` so W/L/D totals
    # survive restarts. The live game board has no persistent state
    # (``state_scope`` is ``None``) so nothing else here depends on disk.
    scoped_slot = "tictactoe_stats"
    persistent_slots = ("tictactoe_stats",)
    auto_defer = True  # library default; declared for policy-surface visibility
    # No Redux reactivity -- the board rebuilds from instance state on
    # every move. Stats dispatches happen at game end and are not
    # observed by this view.
    subscribed_actions = set()
    # participant_limit = 2 is technically redundant with allowed_users
    # = {player_x, player_o} -- two IDs already cap occupancy at two.
    # Both are kept as a side-by-side demonstration of the auth domain
    # (allowed_users / on_unauthorized) versus the capacity domain
    # (participant_limit / on_participant_limit). See "Combining
    # allowed_users and participant_limit" in docs/guide/views.md.
    participant_limit = 2
    auto_register_participants = True
    # Bare exit() (close button, timeout) deletes the message; the
    # board state is reproducible from any rematch, so there's nothing
    # worth freezing on the public card after the game ends.
    exit_policy = "delete"

    def __init__(self, *args, opponent_id: int, size: int = 3, win_length: int = 3, **kwargs):
        super().__init__(*args, **kwargs)
        self.player_x = self.user_id  # Challenger is X
        self.player_o = opponent_id
        self.size = size
        self.win_length = win_length
        self.win_lines = _compute_win_lines(size, win_length)
        self.allowed_users = {self.player_x, self.player_o}
        self.board = [EMPTY] * (size * size)
        self.turn = "X"  # X goes first
        self.winner = None  # None, "X", "O", or "draw"
        self._winning_line = None
        self._forfeited_by: int | None = None
        self._rematch_votes: set[int] = set()
        self.build_ui()

    async def on_instance_limit(self, error: InstanceLimitError) -> None:
        # blocked_user_id identifies who actually collided -- either the
        # challenger (instance_policy reject) or the opponent (auto_register
        # rollback).  Address the acting user in second person when they
        # are the blocker; mention the other player in third person.
        if self.interaction is not None:
            blocked = error.blocked_user_id or self.user_id
            if blocked == self.interaction.user.id:
                message = "You're already in a game."
            else:
                message = f"<@{blocked}> is already in a game."
            await self.respond(self.interaction, message, ephemeral=True)

    # // ==================( UI Building )================== // #

    def build_ui(self):
        """Rebuild the full component tree from current game state."""
        self.clear_items()

        # Card contents: title, turn/legend, grid
        card_items = []

        if self.winner == "draw":
            color = COLOR_DRAW
        elif self.winner:
            color = COLOR_X_WIN if self.winner == "X" else COLOR_O_WIN
        else:
            color = COLOR_X_TURN if self.turn == "X" else COLOR_O_TURN

        card_items.append(TextDisplay("## TicTacToe"))

        # Turn indicator and spacing (only during active play)
        if not self.winner:
            current_id = self.player_x if self.turn == "X" else self.player_o
            mark = X_MARK if self.turn == "X" else O_MARK
            card_items.append(TextDisplay(f"{mark} <@{current_id}>'s turn"))
            card_items.append(gap())

        # Player legend
        card_items.append(
            TextDisplay(f"{X_MARK} <@{self.player_x}> vs {O_MARK} <@{self.player_o}>")
        )
        card_items.append(divider())

        # Build the NxN button grid via the library helper -- one call
        # packs N ActionRows of N buttons each, applying the Discord 5x5
        # component limit automatically.
        card_items.extend(button_grid(self.size, self.size, self._make_cell_button))

        self.add_item(card(*card_items, color=color))

        # Game-over alert and action buttons
        if self.winner:
            if self.winner == "draw":
                self.add_item(alert("No moves left, it's a draw!", level="warning"))
            elif self._forfeited_by is not None:
                winner_id = self.player_x if self.winner == "X" else self.player_o
                self.add_item(
                    alert(
                        f"<@{self._forfeited_by}> forfeited. **<@{winner_id}> wins!**",
                        level="success",
                    )
                )
            else:
                winner_id = self.player_x if self.winner == "X" else self.player_o
                self.add_item(alert(f"**<@{winner_id}> wins the game!**", level="success"))

            # Rematch button with vote count folded into the label
            vote_count = len(self._rematch_votes)
            rematch_label = f"Rematch ({vote_count}/2)" if vote_count > 0 else "Rematch"

            self.add_item(
                ActionRow(
                    StatefulButton(
                        label=rematch_label,
                        style=discord.ButtonStyle.primary,
                        emoji="\U0001f504",
                        callback=self._rematch,
                    ),
                    StatefulButton(
                        label="Close",
                        style=discord.ButtonStyle.secondary,
                        emoji="\u274c",
                        callback=self._close,
                    ),
                )
            )
        else:
            self.add_item(
                ActionRow(
                    StatefulButton(
                        label="Forfeit",
                        style=discord.ButtonStyle.danger,
                        callback=self._forfeit,
                    ),
                )
            )

    def _make_cell_button(self, row: int, col: int):
        """Create a button for a board cell at ``(row, col)``.

        Signature matches ``button_grid``'s ``(row, col) -> Button``
        factory contract. The flat cell index is derived here for the
        board lookup and the move callback.
        """
        cell = row * self.size + col
        value = self.board[cell]
        is_empty = value == EMPTY
        game_over = self.winner is not None

        # Style based on cell state
        if value == X_MARK:
            style = discord.ButtonStyle.primary
        elif value == O_MARK:
            style = discord.ButtonStyle.danger
        else:
            style = discord.ButtonStyle.secondary

        # Highlight winning cells
        if self._winning_line and cell in self._winning_line:
            style = discord.ButtonStyle.success

        return StatefulButton(
            label=value,
            style=style,
            disabled=not is_empty or game_over,
            callback=self._make_move(cell),
        )

    # // ==================( Game Logic )================== // #

    def _make_move(self, cell: int):
        """Create a callback for placing a mark on a cell."""

        async def callback(interaction: discord.Interaction):
            # Turn enforcement: the other player gets an ephemeral nudge
            current_player = self.player_x if self.turn == "X" else self.player_o
            if interaction.user.id != current_player:
                await self.respond(
                    interaction, f"It's <@{current_player}>'s turn!", ephemeral=True
                )
                return

            # Place the mark
            self.board[cell] = X_MARK if self.turn == "X" else O_MARK

            # Check for win or draw
            winner = self._check_winner()
            if winner:
                self.winner = winner
                await self._finish_game()
            elif EMPTY not in self.board:
                self.winner = "draw"
                await self._finish_game()
            else:
                self.turn = "O" if self.turn == "X" else "X"

            self.build_ui()
            await self.refresh()

        return callback

    def _check_winner(self):
        """Check all win lines and return 'X', 'O', or None."""
        for line in self.win_lines:
            values = [self.board[i] for i in line]
            if values[0] != EMPTY and all(v == values[0] for v in values):
                self._winning_line = line
                return "X" if values[0] == X_MARK else "O"
        return None

    async def _finish_game(self):
        """Record the outcome as per-player stats.

        The two players' stats live under ``user_guild`` scope because
        lifetime totals belong to a single player and persist across
        opponents. Each player's outcome is dispatched independently so
        a forfeit by one side doesn't silently double-count against the
        other. Both dispatches are wrapped in a single ``batch()`` so
        subscribers see the post-game state as one transition and the
        undo stack collapses to one entry (matches Battleship parity).
        """
        if self.guild_id is None:
            return
        async with self.state_store.batch():
            if self.winner == "draw":
                await self._record_player_stats(self.player_x, outcome="draw", forfeit=False)
                await self._record_player_stats(self.player_o, outcome="draw", forfeit=False)
            else:
                winner_id = self.player_x if self.winner == "X" else self.player_o
                loser_id = self.player_o if self.winner == "X" else self.player_x
                loser_forfeited = self._forfeited_by is not None
                await self._record_player_stats(winner_id, outcome="win", forfeit=False)
                await self._record_player_stats(loser_id, outcome="loss", forfeit=loser_forfeited)

    async def _record_player_stats(
        self, player_id: int, *, outcome: str, forfeit: bool
    ) -> None:
        """Bump a player's lifetime totals via ``user_guild``-scoped state.

        TicTacToe tracks draws as a distinct outcome, so ``outcome`` is
        one of ``"win"``, ``"loss"``, or ``"draw"`` rather than the
        binary won/lost pair Battleship uses. ``forfeit`` is only
        meaningful when ``outcome == "loss"``.

        ``dispatch_scoped`` is called with explicit ``scope=`` and
        ``user_id=`` kwargs so this view (``state_scope = None``) can
        write into the opponent's scope key. Without overrides the call
        would target ``self.user_id``, which is wrong here.
        """
        existing = self.state_store.get_scoped(
            "user_guild",
            slot_name="tictactoe_stats",
            user_id=player_id,
            guild_id=self.guild_id,
        )
        new_stats = {
            "games": existing.get("games", 0) + 1,
            "wins": existing.get("wins", 0) + (1 if outcome == "win" else 0),
            "losses": existing.get("losses", 0) + (1 if outcome == "loss" else 0),
            "draws": existing.get("draws", 0) + (1 if outcome == "draw" else 0),
            "forfeits": existing.get("forfeits", 0) + (1 if forfeit else 0),
        }
        # SCOPED_UPDATE shallow-merges ``data`` into the existing slice.
        # The view's ``scoped_slot = "tictactoe_stats"`` routes the write
        # to its own bucket, so the stats dict is passed directly (no
        # ``"tictactoe"`` sub-key wrapper needed).
        await self.dispatch_scoped(
            new_stats,
            scope="user_guild",
            user_id=player_id,
            guild_id=self.guild_id,
        )

    # // ==================( Actions )================== // #

    async def _forfeit(self, interaction: discord.Interaction):
        """The clicking player forfeits, the other player wins."""
        self._forfeited_by = interaction.user.id
        # Determine winner based on who clicked, not whose turn it is
        self.winner = "O" if interaction.user.id == self.player_x else "X"
        await self._finish_game()
        self.build_ui()
        await self.refresh()

    async def _rematch(self, interaction: discord.Interaction):
        """Vote for a rematch. Resets the board when both players agree."""
        self._rematch_votes.add(interaction.user.id)

        if len(self._rematch_votes) >= 2:
            # Both players agreed, start a new game with swapped sides
            self.player_x, self.player_o = self.player_o, self.player_x
            self.board = [EMPTY] * (self.size * self.size)
            self.turn = "X"
            self.winner = None
            self._winning_line = None
            self._forfeited_by = None
            self._rematch_votes = set()

        self.build_ui()
        await self.refresh()

    async def _close(self, interaction: discord.Interaction):
        """Close the game (deletion handled by exit_policy = "delete")."""
        await self.exit()


# // ========================================( Cog )======================================== // #


# Derived leaderboard -- same shape as Battleship's. The selector reads
# the raw ``tictactoe_stats`` bucket; the compute_fn groups by guild
# and sorts each guild's list by wins desc then games desc.
@computed(selector=lambda s: s.get("application", {}).get("tictactoe_stats", {}))
def tictactoe_leaderboards(bucket: dict) -> dict:
    """Return ``{guild_id: [(user_id, stats), ...]}`` sorted by wins desc."""
    # Wrap the cached bucket in a minimal envelope so StateStore.iter_scoped
    # can parse the scope keys. The library handles the "user_guild:uid:gid"
    # format and silently skips malformed entries.
    envelope = {"application": {"tictactoe_stats": bucket}}
    by_guild: dict = {}
    for ids, stats in StateStore.iter_scoped(
        envelope, "user_guild", slot_name="tictactoe_stats"
    ):
        if stats.get("games", 0) == 0:
            continue
        by_guild.setdefault(ids["guild_id"], []).append((ids["user_id"], stats))
    for entries in by_guild.values():
        entries.sort(key=lambda e: (-e[1].get("wins", 0), -e[1].get("games", 0)))
    return by_guild


class TicTacToeExample(commands.Cog, name="v2_tictactoe_example"):
    """Two-player TicTacToe with V2 components and lifetime stats."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_group(
        name="tictactoe",
        description="Play TicTacToe and view per-player stats.",
    )
    async def tictactoe(self, context: Context) -> None:
        """Parent group for /tictactoe subcommands."""
        # Subcommands do the work; the group itself is a routing stub.

    @tictactoe.command(
        name="play",
        description="Challenge someone to a game of TicTacToe.",
    )
    @app_commands.describe(
        opponent="The player to challenge",
        size="Board size from 3 to 5 (default 3)",
        win="How many in a row to win (3 to size, default = size)",
    )
    async def tictactoe_play(
        self,
        context: Context,
        opponent: discord.Member,
        size: app_commands.Range[int, 3, 5] = 3,
        win: app_commands.Range[int, 3, 5] = None,
    ) -> None:
        """Challenge another member to TicTacToe.

        The opponent must accept the challenge before the game starts.
        Both players interact with the same message. The challenger
        plays as X, the opponent as O. Players swap sides on rematch.
        """
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        if opponent.id == context.author.id:
            await context.send("You can't play against yourself!", ephemeral=True)
            return

        if opponent.bot:
            await context.send("You can't play against a bot!", ephemeral=True)
            return

        # Pre-check: reject the command if the challenger already has an
        # active game, before the opponent ever sees a challenge prompt.
        if not TicTacToeView.check_instance_available(
            user_id=context.author.id,
            guild_id=context.guild.id,
        ):
            await context.send(
                "You're already in a game. Finish or exit it first.",
                ephemeral=True,
            )
            return

        win_length = win if win is not None else size
        if win_length < 3 or win_length > size:
            await context.send(
                f"Win length must be between 3 and {size}.",
                ephemeral=True,
            )
            return

        view = TicTacToeChallengeView(
            context=context,
            challenger_id=context.author.id,
            opponent=opponent,
            size=size,
            win_length=win_length,
        )
        await view.send()

    @tictactoe.command(
        name="stats",
        description="Show a player's lifetime TicTacToe record.",
    )
    @app_commands.describe(user="The player to look up (defaults to you)")
    async def tictactoe_stats(
        self,
        context: Context,
        user: discord.Member = None,
    ) -> None:
        """Display one player's lifetime W/L/D record in this guild."""
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        target = user or context.author
        store = get_store()
        stats = store.get_scoped(
            "user_guild",
            slot_name="tictactoe_stats",
            user_id=target.id,
            guild_id=context.guild.id,
        )

        if not stats or stats.get("games", 0) == 0:
            await context.send(
                f"{target.mention} has no recorded TicTacToe games in this server.",
                ephemeral=True,
            )
            return

        games = stats.get("games", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        draws = stats.get("draws", 0)
        forfeits = stats.get("forfeits", 0)
        win_rate = (wins / games * 100) if games else 0.0

        body = stats_card(
            f"TicTacToe -- {target.display_name}",
            {
                "Games": str(games),
                "Wins": str(wins),
                "Losses": str(losses),
                "Draws": str(draws),
                "Forfeits": str(forfeits),
                "Win rate": f"{win_rate:.1f}%",
            },
        )
        await DisplayLayoutView(context=context, container=body).send(ephemeral=True)

    @tictactoe.command(
        name="leaderboard",
        description="Show this server's TicTacToe leaderboard.",
    )
    async def tictactoe_leaderboard(self, context: Context) -> None:
        """Display server-wide totals and the top 3 players by wins."""
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        store = get_store()
        entries = store.computed["tictactoe_leaderboards"].get(context.guild.id, [])

        if not entries:
            await context.send(
                "No TicTacToe games have been played in this server yet.",
                ephemeral=True,
            )
            return

        class _TicTacToeLeaderboard(LeaderboardLayoutView):
            leaderboard_top_n = 10

            def format_stats(self, user_id, stats):
                wins = stats.get("wins", 0)
                games = stats.get("games", 0)
                draws = stats.get("draws", 0)
                win_rate = (wins / games * 100) if games else 0.0
                return f"{wins}W / {games}G \u2022 {win_rate:.0f}% \u2022 {draws}D"

            def build_summary(self, entries):
                # Each game contributes to two player rows
                unique_games = sum(e[1].get("games", 0) for e in entries) // 2
                total_draws = sum(e[1].get("draws", 0) for e in entries) // 2
                return {
                    "Games played": str(unique_games),
                    "Draws": str(total_draws),
                    "Players": str(len(entries)),
                }

        view = _TicTacToeLeaderboard(
            context=context,
            entries=entries,
            title=f"TicTacToe Leaderboard -- {context.guild.name}",
        )
        await view.send(ephemeral=True)


async def setup(bot) -> None:
    # persistent_slots = ("tictactoe_stats",) requires PersistenceMiddleware.
    # Without it, stats accumulate during a session and are lost on restart.
    # Install it from your bot's setup_hook before loading this cog::
    #
    #     from cascadeui import PersistenceMiddleware, SQLiteBackend, setup_middleware
    #     await setup_middleware(
    #         PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=self),
    #     )
    await bot.add_cog(TicTacToeExample(bot=bot))
