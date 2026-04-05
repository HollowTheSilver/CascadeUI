"""
V2 TicTacToe -- CascadeUI Multi-User Game
==========================================

A two-player TicTacToe game demonstrating multi-user interaction
patterns with V2 components:

    - Challenge acceptance flow (opponent must accept before the game starts)
    - allowed_users for restricting interaction to specific players
    - register_participant for session-aware multi-user views
    - Dynamic board size (3x3 to 5x5 via the size parameter)
    - Configurable win length (e.g. 3-in-a-row on a 5x5 board)
    - Turn enforcement inside move callbacks (not interaction_check)
    - Mutual rematch agreement (both players must confirm)
    - ActionRows of StatefulButtons forming the NxN game grid
    - Dynamic accent colors reflecting game state (turn, win, draw)
    - card() and alert() for structured visual feedback
    - Custom reducer tracking game statistics across sessions

This is the first CascadeUI example where multiple specific users
interact with the same view. The allowed_users attribute restricts
who can click, and per-callback logic enforces turn order.

Commands:
    /tictactoe @user [size] [win]   Challenge someone to a game (size 3-5, win 2-size)

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    SessionLimitError,
    StatefulButton,
    StatefulLayoutView,
    alert,
    card,
    cascade_reducer,
    divider,
    gap,
)

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


# // ========================================( Reducer )======================================== // #


@cascade_reducer("GAME_FINISHED")
async def game_reducer(action, state):
    """Track game outcomes in application state."""
    new_state = state
    app = new_state.setdefault("application", {})
    stats = app.setdefault("tictactoe", {"games": 0, "x_wins": 0, "o_wins": 0, "draws": 0})

    result = action["payload"].get("result")
    stats["games"] += 1
    if result == "X":
        stats["x_wins"] += 1
    elif result == "O":
        stats["o_wins"] += 1
    else:
        stats["draws"] += 1

    return new_state


# // ========================================( Challenge View )======================================== // #


class ChallengeView(StatefulLayoutView):
    """Pre-game challenge prompt that the opponent must accept or decline.

    Only the opponent can interact (via allowed_users). No session_limit
    is set, so challenges don't block the challenger's session slot before
    the game starts.
    """

    owner_only_message = "Only the challenged player can respond."

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
        self._build_ui()

    def _build_ui(self):
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
        await interaction.response.defer()
        await self.exit(delete_message=True)

        # Create the game view (challenger's context owns it for session limiting)
        view = TicTacToeView(
            interaction=interaction,
            user_id=self.challenger_id,
            guild_id=self.guild_id,
            opponent_id=self.opponent.id,
            size=self.size,
            win_length=self.win_length,
        )

        try:
            await view.send()
        except Exception:
            await interaction.followup.send(
                "Failed to start the game. Try again.",
                ephemeral=True,
            )
            return

        try:
            await view.register_participant(self.opponent.id)
        except SessionLimitError:
            await view.exit(delete_message=True)
            await interaction.followup.send(
                f"<@{self.opponent.id}> is already in a game.",
                ephemeral=True,
            )

    async def _decline(self, interaction: discord.Interaction):
        await interaction.response.defer()
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
        await self.exit()

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
        await super().on_timeout()


# // ========================================( Game View )======================================== // #


class TicTacToeView(StatefulLayoutView):
    """Two-player TicTacToe with V2 components and dynamic board size.

    Both players interact with the same message. ``allowed_users``
    restricts interaction to the two players, and turn enforcement
    in ``_make_move`` handles ordering. The opponent is registered
    as a participant so session limiting applies to both players.
    """

    owner_only_message = "You're not part of this game."
    session_limit = 1
    session_scope = "user_guild"

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
        self._build_ui()

    # // ==================( UI Building )================== // #

    def _build_ui(self):
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

        # Build the NxN button grid
        for row_idx in range(self.size):
            buttons = []
            for col_idx in range(self.size):
                cell = row_idx * self.size + col_idx
                buttons.append(self._make_cell_button(cell))
            card_items.append(ActionRow(*buttons))

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

    def _make_cell_button(self, cell: int):
        """Create a button for a board cell."""
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
                await interaction.response.send_message(
                    f"It's <@{current_player}>'s turn!", ephemeral=True
                )
                return

            await interaction.response.defer()

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

            self._build_ui()
            if self.message:
                await self.message.edit(view=self)

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
        """Dispatch game result to state."""
        await self.dispatch("GAME_FINISHED", {"result": self.winner})

    # // ==================( Actions )================== // #

    async def _forfeit(self, interaction: discord.Interaction):
        """The clicking player forfeits, the other player wins."""
        await interaction.response.defer()
        self._forfeited_by = interaction.user.id
        # Determine winner based on who clicked, not whose turn it is
        self.winner = "O" if interaction.user.id == self.player_x else "X"
        await self._finish_game()
        self._build_ui()
        if self.message:
            await self.message.edit(view=self)

    async def _rematch(self, interaction: discord.Interaction):
        """Vote for a rematch. Resets the board when both players agree."""
        await interaction.response.defer()

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

        self._build_ui()
        if self.message:
            await self.message.edit(view=self)

    async def _close(self, interaction: discord.Interaction):
        """Close the game and delete the message."""
        await interaction.response.defer()
        await self.exit(delete_message=True)


# // ========================================( Cog )======================================== // #


class TicTacToeExample(commands.Cog, name="v2_tictactoe_example"):
    """Two-player TicTacToe with V2 components."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="tictactoe",
        description="Challenge someone to a game of TicTacToe.",
    )
    @app_commands.describe(
        opponent="The player to challenge",
        size="Board size from 3 to 5 (default 3)",
        win="How many in a row to win (2 to size, default = size)",
    )
    async def tictactoe(
        self,
        context: Context,
        opponent: discord.Member,
        size: app_commands.Range[int, 3, 5] = 3,
        win: app_commands.Range[int, 2, 5] = None,
    ) -> None:
        """Challenge another member to TicTacToe.

        The opponent must accept the challenge before the game starts.
        Both players interact with the same message. The challenger
        plays as X, the opponent as O. Players swap sides on rematch.

        Board sizes from 3x3 up to 5x5 are supported. The ``win``
        parameter sets how many marks in a row are needed to win
        (defaults to the board size). Lower values on larger boards
        make games faster and more chaotic.
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

        win_length = win if win is not None else size
        if win_length < 2 or win_length > size:
            await context.send(
                f"Win length must be between 2 and {size}.",
                ephemeral=True,
            )
            return

        view = ChallengeView(
            context=context,
            challenger_id=context.author.id,
            opponent=opponent,
            size=size,
            win_length=win_length,
        )
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(TicTacToeExample(bot=bot))
