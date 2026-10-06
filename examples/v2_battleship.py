"""Two-player Battleship with V2 components, designed around a live EmojiGrid.

Gameplay
--------
Both players interact with the same public message. Ship positions are
private -- players view their own board via the ephemeral "My Ships" button.
A challenge/accept flow starts the game. Each player gets a random fleet
on a 10x10 grid. During setup, players can preview and re-generate their
fleet via the ephemeral view. Once both players are ready (or the timer
expires), the game starts. Players take turns firing shots by selecting a
row and column, then clicking Fire. The first player to sink all five
opponent ships wins.

CascadeUI patterns demonstrated
--------------------------------
* Long-lived ``EmojiGrid`` as the live board representation -- grids are
  created once, mutated incrementally on each shot/sink, and dropped
  straight into ``card()`` every rebuild. No render functions, no
  intermediate data structures for display.
* Cross-view reactivity via named-action subscriptions -- a single
  dispatch (``BATTLESHIP_SHOT``, ``BATTLESHIP_REROLL``) triggers
  ``on_state_changed()`` on both the public board and private fleet
  panels, so two views stay in sync without manual refresh plumbing.
* ``check_instance_available()`` at the command level to reject a
  challenge before the opponent sees it, for either player.
* Instance limiting (``instance_limit=1``, ``instance_scope="user_guild"``)
  with ``auto_register_participants`` to claim both players atomically.
* Ephemeral fleet panels with ``auto_refresh_ephemeral`` for the 15-min
  token handoff, ``parent=`` kwarg for automatic cleanup attachment, and
  ``instance_policy="replace"`` for dedup. The panel reads the game view
  back through ``self.parent`` rather than keeping a second reference of
  its own, which is what every board and grid read below resolves through.
* Phase-aware ``exit()`` override (delete during setup, freeze on
  completion) and ``create_task()`` for the auto-start timer, so the view
  owns it and cancels it on exit.
* ``exit_children()`` closes both players' private fleet panels on a
  rematch or at game end, while the board itself stays open.
* ``seed_initial_state`` hook -- fleet randomization, defense-grid
  paint, and the initial component build all happen here instead of
  in ``__init__``. The hook fires inside the send-pipeline batch
  after ``register_view`` (so other views' selectors see the slot)
  but before the Discord HTTP send (so the painted grids ship in the
  first render). ``__init__`` stays pure: instance attributes only.
* Mixed-scope state design -- shared per-game data (phase, fleets,
  shots_fired) lives under a custom ``access_slot`` because both
  players read it; per-player lifetime stats (games, wins, forfeits) live
  under ``user_guild``-scoped state because they belong to one player and
  persist across opponents. See ``_record_player_stats``.
* Stats subcommands -- ``/battleship stats`` and ``/battleship
  leaderboard`` read the per-player scoped slices back out. The
  leaderboard scans the ``battleship_stats`` bucket directly, since it
  discovers users rather than looking them up.

Commands:
    /battleship play @user          Challenge someone to a game
    /battleship stats [user]        Show a player's lifetime record
    /battleship leaderboard         Show server-wide rankings

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #

import asyncio
import logging
import random
from datetime import timedelta

import discord
from discord import SelectOption, app_commands
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    DISCORD_CALL_ERRORS,
    DisplayLayoutView,
    EmojiGrid,
    InstanceLimitError,
    LeaderboardLayoutView,
    StatefulButton,
    StatefulLayoutView,
    StatefulSelect,
    StateStore,
    access_slot,
    alert,
    card,
    cascade_reducer,
    computed,
    divider,
    emoji_grid,
    gap,
    get_store,
    image_section,
    key_value,
    read_slot,
    render_progress,
    stats_card,
    with_cooldown,
)

logger = logging.getLogger(__name__)


# // ========================================( Constants )======================================== // #


BOARD_SIZE = 10

SHIPS = [
    ("Carrier", 5),
    ("Battleship", 4),
    ("Cruiser", 3),
    ("Submarine", 3),
    ("Destroyer", 2),
]

ROW_LABELS = "ABCDEFGHIJ"
COL_LABELS = [str(i) for i in range(1, 11)]

# Regional indicator emoji for axis labels (A-J)
ROW_EMOJI = [chr(0x1F1E6 + i) for i in range(BOARD_SIZE)]
# Keycap emoji for column labels (1-10)
COL_EMOJI = [
    f"{i}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}" if i < 10 else "\N{KEYCAP TEN}"
    for i in range(1, BOARD_SIZE + 1)
]

# Per-ship colored squares for visual distinction on the private board
SHIP_COLORS = {
    "Carrier": "\N{LARGE RED SQUARE}",  # 🟥 Red
    "Battleship": "\N{LARGE ORANGE SQUARE}",  # 🟧 Orange
    "Cruiser": "\N{LARGE YELLOW SQUARE}",  # 🟨 Yellow
    "Submarine": "\N{LARGE GREEN SQUARE}",  # 🟩 Green
    "Destroyer": "\N{LARGE PURPLE SQUARE}",  # 🟪 Purple
}

# Emoji for the attack board (shots fired at opponent)
WATER = "\N{BLACK LARGE SQUARE}"  # ⬛ Black square -- empty grid cell
MISS = "\N{LARGE BLUE SQUARE}"  # 🟦 Blue square -- miss (ocean splash)
HIT = "\N{FIRE}"  # 🔥 Fire -- hit (ship not yet sunk)
SUNK = "\N{SKULL}"  # 💀 Skull -- sunk ship cell

# Emoji for the private board (your ships + incoming damage)
SHIP_HIT = "\N{FIRE}"  # 🔥 Fire -- your ship was hit
SHIP_SUNK = "\N{SKULL}"  # 💀 Skull -- your ship was sunk
WATER_MISS = "\N{LARGE BLUE SQUARE}"  # 🟦 Blue square -- opponent missed here
WATER_EMPTY = "\N{BLACK LARGE SQUARE}"  # ⬛ Black square -- empty water

# Colors
COLOR_P1_TURN = discord.Color.blurple()
COLOR_P2_TURN = discord.Color.orange()
COLOR_WIN = discord.Color.green()
COLOR_FORFEIT = discord.Color.red()

CHALLENGE_TIMEOUT = 60  # seconds
SETUP_TIMEOUT = 60  # seconds for fleet setup auto-lock


# // ========================================( Helpers )======================================== // #


def _place_ships(size: int, ships: list[tuple[str, int]]) -> dict[str, list[int]]:
    """Randomly place ships on a board, returning a dict of ship name -> cell indices.

    Each cell index is row * size + col. Ships cannot overlap or go out of bounds.
    """
    occupied: set[int] = set()
    placements: dict[str, list[int]] = {}

    for name, length in ships:
        for _ in range(1000):  # safety limit
            horizontal = random.choice([True, False])
            if horizontal:
                row = random.randint(0, size - 1)
                col = random.randint(0, size - length)
                cells = [row * size + col + i for i in range(length)]
            else:
                row = random.randint(0, size - length)
                col = random.randint(0, size - 1)
                cells = [(row + i) * size + col for i in range(length)]

            if not occupied.intersection(cells):
                occupied.update(cells)
                placements[name] = cells
                break

    return placements


def _make_attack_grid() -> EmojiGrid:
    """Create a blank attack grid (all water)."""
    return emoji_grid(
        BOARD_SIZE, BOARD_SIZE, fill=WATER, row_labels=ROW_EMOJI, col_labels=COL_EMOJI
    )


def _paint_fleet(grid: EmojiGrid, ships: dict[str, list[int]]) -> None:
    """Clear a defense grid and paint each ship in its fleet color."""
    grid.clear()
    for name, cells in ships.items():
        grid[cells] = SHIP_COLORS.get(name, "\N{WHITE LARGE SQUARE}")


def _ship_status_line(ships: dict[str, list[int]], sunk_ships: set[str], emoji: bool = True) -> str:
    """One-line summary of fleet status: ship names with color and strikethrough for sunk."""
    parts = []
    for name, _ in SHIPS:
        if name in ships:
            color = SHIP_COLORS.get(name, "")
            label = f"~~{name}~~" if name in sunk_ships else f"**{name}**"
            parts.append(f"{color} {label}" if emoji else label)
    return " \N{BULLET} ".join(parts)


# // ========================================( Reducers )======================================== // #
#
# Shared per-game state lives under ``state["application"]["battleship"]``,
# keyed by ``match_id`` (``BattleshipView.id``, stable across a rematch),
# and is written by the five lifecycle reducers below. Both players read
# it, so it cannot sit behind a per-user scope key, and it is keyed by
# match because one player can be seated in two matches in different
# guilds at once.
#
#     state["application"]["battleship"] = {
#         match_id: {
#             "fleets":      {player_id: {ship_name: [cell_indices]}},
#             "phase":       "setup" | "active" | "finished",
#             "shots_fired": int,   # per-match counter, reset on REMATCH
#         },
#         ...
#     }
#
# Per-player lifetime totals live under ``user_guild`` scope instead
# (see ``BattleshipView._record_player_stats``).


@cascade_reducer("BATTLESHIP_REROLL")
async def battleship_reroll_reducer(action, state):
    """Record the rerolled fleet so selectors can detect per-player changes."""
    match = access_slot(state, "battleship", action["payload"]["match_id"])
    fleets = match.setdefault("fleets", {})
    fleets[action["payload"]["player_id"]] = action["payload"]["ships"]
    return state


@cascade_reducer("BATTLESHIP_STARTED")
async def battleship_started_reducer(action, state):
    """Transition phase to active so setup-only UI elements drop out."""
    match = access_slot(state, "battleship", action["payload"]["match_id"])
    match["phase"] = "active"
    return state


@cascade_reducer("BATTLESHIP_SHOT")
async def battleship_shot_reducer(action, state):
    """Increment the shot counter so every shot produces a selector delta."""
    match = access_slot(state, "battleship", action["payload"]["match_id"])
    match["shots_fired"] = match.get("shots_fired", 0) + 1
    return state


@cascade_reducer("BATTLESHIP_REMATCH")
async def battleship_rematch_reducer(action, state):
    """Reset per-match fields and place fresh ship layouts for both players.

    Lifetime stats live under user_guild scope and survive the
    rematch untouched. Player IDs ride the payload because the
    seat swap happens in the view before dispatch.
    """
    match = access_slot(state, "battleship", action["payload"]["match_id"])
    match["phase"] = "setup"
    match["shots_fired"] = 0

    fleets = match.setdefault("fleets", {})
    fleets[action["payload"]["player_1"]] = _place_ships(BOARD_SIZE, SHIPS)
    fleets[action["payload"]["player_2"]] = _place_ships(BOARD_SIZE, SHIPS)
    return state


@cascade_reducer("BATTLESHIP_FINISHED")
async def battleship_finished_reducer(action, state):
    """Mark the match finished in the shared game slot."""
    match = access_slot(state, "battleship", action["payload"]["match_id"])
    match["phase"] = "finished"
    return state


# Derived leaderboard -- grouped by guild, sorted within each guild.
#
# The selector reads the raw ``battleship_stats`` bucket (keys shaped
# like ``user_guild:{uid}:{gid}``). The compute_fn parses keys, groups
# entries by guild_id, filters zero-game rows, and sorts each guild's
# list by wins desc then games desc. Callsites do
# ``store.computed["battleship_leaderboards"].get(guild_id, [])``.
#
# Any finished game writes a fresh scoped entry, which changes the
# bucket dict and invalidates the cache. For two-player demos this is
# cheap; production-scale rebuilds would want a per-guild shape.
@computed(selector=lambda s: read_slot(s, "battleship_stats", default={}))
def battleship_leaderboards(bucket: dict) -> dict:
    """Return ``{guild_id: [(user_id, stats), ...]}`` sorted by wins desc."""
    # Wrap the cached bucket in a minimal envelope so StateStore.iter_scoped
    # can parse the scope keys. The library handles the "user_guild:uid:gid"
    # format and silently skips malformed entries.
    envelope = {"application": {"battleship_stats": bucket}}
    by_guild: dict = {}
    for ids, stats in StateStore.iter_scoped(envelope, "user_guild", slot_name="battleship_stats"):
        if stats.get("games", 0) == 0:
            continue
        by_guild.setdefault(ids["guild_id"], []).append((ids["user_id"], stats))
    for entries in by_guild.values():
        entries.sort(key=lambda e: (-e[1].get("wins", 0), -e[1].get("games", 0)))
    return by_guild


# // ========================================( Challenge View )======================================== // #


class BattleshipChallengeView(StatefulLayoutView):
    """Pre-game challenge prompt that the opponent must accept or decline.

    Only the opponent can interact (via allowed_users). No instance_limit
    so challenges don't consume a session slot before the game starts.
    """

    unauthorized_message = "Only the challenged player can respond."
    # Accept removes the prompt; Decline and expiry leave a record card.
    exit_policy = "delete"
    # No scoped state -- the challenge prompt is a one-shot gate that
    # reads nothing from the store and writes nothing.
    state_scope = None

    def __init__(self, *args, challenger_id: int, opponent: discord.Member, **kwargs):
        kwargs.setdefault("timeout", CHALLENGE_TIMEOUT)
        super().__init__(*args, **kwargs)
        self.challenger_id = challenger_id
        self.opponent = opponent
        self.allowed_users = {opponent.id}
        self._expires_at = discord.utils.utcnow() + timedelta(seconds=self.timeout)
        self.build_ui()

    def build_ui(self):
        self.clear_items()
        self.add_item(
            card(
                image_section(
                    "## \N{ANCHOR} Battleship Challenge",
                    f"-# <@{self.challenger_id}> challenges <@{self.opponent.id}>",
                    url=self.opponent.display_avatar.with_size(128).url,
                ),
                divider(),
                key_value(
                    {
                        "Grid": f"{BOARD_SIZE} \N{MULTIPLICATION SIGN} {BOARD_SIZE}",
                        "Fleet": f"{len(SHIPS)} ships \N{MIDDLE DOT} "
                        f"{sum(size for _, size in SHIPS)} cells",
                    }
                ),
                divider(),
                ActionRow(
                    StatefulButton(
                        label="Accept",
                        style=discord.ButtonStyle.success,
                        emoji="\N{HEAVY CHECK MARK}",
                        callback=self._accept,
                    ),
                    StatefulButton(
                        label="Decline",
                        style=discord.ButtonStyle.danger,
                        emoji="\N{HEAVY MULTIPLICATION X}",
                        callback=self._decline,
                    ),
                ),
                f"-# Only <@{self.opponent.id}> can respond \N{MIDDLE DOT} "
                f"expires {discord.utils.format_dt(self._expires_at, 'R')}",
                color=discord.Color.blurple(),
            )
        )

    async def _accept(self, interaction: discord.Interaction):
        view = BattleshipView(
            interaction=interaction,
            user_id=self.challenger_id,
            guild_id=self.guild_id,
            opponent_id=self.opponent.id,
        )

        # auto_register_participants = True on BattleshipView claims a slot
        # for both players from allowed_users during send(), all or nothing.
        # A None return means no game was posted, so the challenge stays up
        # and on_instance_limit has told the opponent who is busy.
        if await view.send() is None:
            return
        await self.exit()

        # Only the opponent's fleet panel can be sent here: an ephemeral
        # followup attaches to the interaction being handled, which is the
        # opponent's Accept click. The challenger opens theirs from View Fleet.
        fleet_view = MyShipsView(
            interaction=interaction,
            user_id=self.opponent.id,
            guild_id=self.guild_id,
            parent=view,
        )
        # A failed Discord call costs only this convenience panel, so it is
        # logged; a programming error still raises.
        try:
            await fleet_view.send(ephemeral=True)
        except DISCORD_CALL_ERRORS as e:
            logger.warning(f"Failed to auto-send opponent's fleet view: {e}")

    async def _decline(self, interaction: discord.Interaction):
        self.clear_items()
        self.add_item(
            card(
                "## \N{ANCHOR} Battleship Challenge",
                TextDisplay(
                    f"<@{self.opponent.id}> declined the challenge from "
                    f"<@{self.challenger_id}>."
                ),
                color=discord.Color.dark_grey(),
            )
        )
        # delete_message=False keeps the decline card as a record.
        await self.exit(delete_message=False)

    async def on_timeout(self):
        self.clear_items()
        self.add_item(
            card(
                "## \N{ANCHOR} Battleship Challenge",
                TextDisplay(
                    f"Challenge from <@{self.challenger_id}> to " f"<@{self.opponent.id}> expired."
                ),
                color=discord.Color.dark_grey(),
            )
        )
        await self.exit(delete_message=False)


# // ========================================( Game View )======================================== // #


class BattleshipView(StatefulLayoutView):
    """Two-player Battleship with V2 components and live EmojiGrid boards.

    Both players interact with the same message. ``allowed_users``
    restricts interaction to the two players, and per-callback logic
    enforces turn order. Ship positions are private -- players view
    their own board via the ephemeral "My Ships" button.

    Board state lives on four long-lived ``EmojiGrid`` instances (two
    attack grids, two defense grids), mutated in place on each shot and
    dropped into ``card()`` on every ``build_ui()`` call.
    """

    unauthorized_message = "You're not part of this game."
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "reject"
    # Either player's click resets it. A match left idle this long freezes
    # as it stands, with no result recorded.
    timeout = 600.0
    # Game state lives in the custom ``battleship`` slot (see the reducers),
    # not under a scope key.
    state_scope = None
    # Lifetime stats go to their own scoped bucket, and persistent_slots
    # saves that bucket alone, so W/L totals survive a restart and nothing
    # from a match in progress is written.
    scoped_slot = "battleship_stats"
    persistent_slots = ("battleship_stats",)
    auto_defer = True
    # allowed_users already fixes the roster at two, so this cap never fires;
    # it is set to show the capacity check beside the access check. See
    # "Combining allowed_users and participant_limit" in docs/guide/views.md.
    participant_limit = 2
    auto_register_participants = True
    # exit_policy is not set here -- the choice is phase-dependent, so
    # exit() is overridden below. subscribed_actions defaults to an empty
    # set (no notifications), so this is an opt-in rather than a narrowing:
    # a re-roll clicked inside a player's own fleet panel has to repaint the
    # public card, and every other rebuild here runs from this view's own
    # callbacks.
    subscribed_actions = {"BATTLESHIP_REROLL"}
    # The setup card, turn indicator, and shot results all name the
    # players, and the tree rebuilds on every Ready toggle, re-roll, and
    # shot, so without this a match re-notifies both players once per
    # move.
    allowed_mentions = discord.AllowedMentions.none()

    # Ship placements live in
    # ``state["application"]["battleship"][match_id]["fleets"]`` via the
    # BATTLESHIP_REROLL reducer. These properties are the canonical read
    # path; writes happen exclusively through dispatch() or the
    # _place_fresh_fleets helper below. Making them read-only @property enforces
    # that -- any stray ``self.ships_1 = ...`` raises AttributeError at the
    # call site instead of silently desynchronising state from local data.
    def state_selector(self, state):
        # A re-roll in another match leaves this board alone.
        return read_slot(state, "battleship", self._match_key, "fleets")

    @property
    def ships_1(self) -> dict[str, list[int]]:
        return self._fleets().get(self.player_1, {})

    @property
    def ships_2(self) -> dict[str, list[int]]:
        return self._fleets().get(self.player_2, {})

    @property
    def _match_key(self) -> str:
        """Stable key partitioning this match inside the shared 'battleship' slot.

        ``self.id`` is a UUID stamped once at construction and unchanged
        across a rematch (same view instance, seats just swap), so it
        isolates this match's fleets/phase/shots_fired from every other
        concurrent match a multi-guild bot may be running at once --
        including a match involving one of the same two players in a
        different guild.
        """
        return self.id

    def _fleets(self) -> dict[int, dict[str, list[int]]]:
        return read_slot(
            self.state_store.state, "battleship", self._match_key, "fleets", default={}
        )

    def _place_fresh_fleets(self, state) -> None:
        """Write a randomly-generated ship layout for each player into the slot.

        Called from ``seed_initial_state`` only, which is handed the live
        store state to seed before the first render. A rematch places
        ships through the BATTLESHIP_REMATCH reducer instead, since the
        view is already live then.
        """
        match = access_slot(state, "battleship", self._match_key)
        fleets = match.setdefault("fleets", {})
        fleets[self.player_1] = _place_ships(BOARD_SIZE, SHIPS)
        fleets[self.player_2] = _place_ships(BOARD_SIZE, SHIPS)

    def _repaint_defense_grids(self) -> None:
        """Repaint both defense grids from the fleets in state."""
        _paint_fleet(self._defense_1, self.ships_1)
        _paint_fleet(self._defense_2, self.ships_2)

    def __init__(self, *args, opponent_id: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.player_1 = self.user_id  # Challenger goes first
        self.player_2 = opponent_id
        self.allowed_users = {self.player_1, self.player_2}

        # Defense grids start as blank water too, and are painted in
        # seed_initial_state once fleets exist in state.
        self._attack_1 = _make_attack_grid()
        self._attack_2 = _make_attack_grid()
        self._defense_1 = _make_attack_grid()
        self._defense_2 = _make_attack_grid()

        # Sunk tracking -- needed for win condition + fleet status line.
        # The grid encodes the visual state; these sets track which named
        # ships are fully sunk so the status line can strikethrough them.
        self.sunk_by_1: set[str] = set()  # Ship names P1 has sunk (on P2's board)
        self.sunk_by_2: set[str] = set()  # Ship names P2 has sunk (on P1's board)

        # Game state
        self.phase = "setup"  # "setup", "active", or game over (winner set)
        self.turn = 1  # Player 1 or 2
        self.winner: int | None = None
        self._forfeited_by: int | None = None
        self._last_result: str | None = None  # "Hit!", "Miss!", "Sunk the Carrier!"
        self._rematch_votes: set[int] = set()
        self._ready: set[int] = set()
        self._setup_task: asyncio.Task | None = None

        # Selected coordinates (from selects)
        self._selected_row: int | None = None
        self._selected_col: int | None = None

    async def seed_initial_state(self, state):
        """Place fresh ships, paint defense grids, and build the component tree.

        Each step reads the one before it: the grids paint the placed
        ships, and ``build_ui`` reads the fleets from state.
        """
        self._place_fresh_fleets(state)
        self._repaint_defense_grids()
        self.build_ui()

    async def send(self, *, ephemeral: bool = False):
        result = await super().send(ephemeral=ephemeral)
        # send() returns None when instance limiting blocks the view.
        # Starting background tasks on a rejected view would leave
        # orphaned timers dispatching actions from a dead instance.
        if result is not None:
            self._start_setup_timer()
        return result

    # // ==================( UI Building )================== // #

    def _current_player_id(self) -> int:
        return self.player_1 if self.turn == 1 else self.player_2

    def build_ui(self):
        """Rebuild the full component tree from current game state."""
        self.clear_items()

        if self.phase == "setup":
            self._build_setup()
        elif self.winner:
            self._build_game_over()
        else:
            self._build_active()

    def _build_setup(self):
        """Build the UI for fleet setup: ready up + cancel.

        Re-roll lives on the ephemeral ``MyShipsView``, not here. This
        is deliberate: it keeps the live cross-view update colocated
        with the private fleet preview, so a player who opens their
        fleet view sees the re-roll button right next to the board it
        affects.
        """
        p1_mark = (
            "\N{WHITE HEAVY CHECK MARK}"
            if self.player_1 in self._ready
            else "\N{HOURGLASS WITH FLOWING SAND}"
        )
        p2_mark = (
            "\N{WHITE HEAVY CHECK MARK}"
            if self.player_2 in self._ready
            else "\N{HOURGLASS WITH FLOWING SAND}"
        )

        self.add_item(
            card(
                "## \N{ANCHOR} Fleet Setup",
                TextDisplay(
                    "Open your fleet to preview or re-generate your board.\n"
                    f"Hit **Ready** to lock in, or auto-starts in **{SETUP_TIMEOUT}s**."
                ),
                divider(),
                TextDisplay(f"{p1_mark} <@{self.player_1}>"),
                TextDisplay(f"{p2_mark} <@{self.player_2}>"),
                color=discord.Color.blurple(),
            )
        )

        self.add_item(
            ActionRow(
                StatefulButton(
                    label="View Fleet",
                    style=discord.ButtonStyle.primary,
                    emoji="\N{SHIP}",
                    callback=self._show_my_ships,
                ),
                StatefulButton(
                    label="Ready",
                    style=discord.ButtonStyle.success,
                    emoji="\N{HEAVY CHECK MARK}",
                    callback=self._ready_up,
                ),
                StatefulButton(
                    label="Cancel",
                    style=discord.ButtonStyle.danger,
                    emoji="\N{CROSS MARK}",
                    callback=self._cancel_setup,
                ),
            )
        )

    def _build_active(self):
        """Build the UI for active play: attack board + targeting controls."""
        current_id = self._current_player_id()
        color = COLOR_P1_TURN if self.turn == 1 else COLOR_P2_TURN

        attack = self._attack_1 if self.turn == 1 else self._attack_2
        opponent_ships = self.ships_2 if self.turn == 1 else self.ships_1
        sunk = self.sunk_by_1 if self.turn == 1 else self.sunk_by_2

        self.add_item(
            card(
                TextDisplay("## \N{ANCHOR} Battleship"),
                divider(),
                gap(),
                TextDisplay(f"**Turn:** <@{current_id}>"),
                gap(),
                divider(),
                attack,
                divider(),
                TextDisplay(
                    f"{_ship_status_line(ships=opponent_ships, sunk_ships=sunk, emoji=False)}\n"
                ),
                color=color,
            )
        )

        if self._last_result:
            if "Sunk" in self._last_result:
                level = "error"
            elif "Hit" in self._last_result:
                level = "warning"
            else:
                level = "info"
            self.add_item(alert(self._last_result, level=level))

        # Row select -- ``default=True`` on the matching option is the only
        # way to preserve visual selection across V2 immediate-mode rebuilds.
        row_options = [
            SelectOption(
                label=f"Row {ROW_LABELS[i]}",
                value=str(i),
                default=(self._selected_row == i),
            )
            for i in range(BOARD_SIZE)
        ]
        self.add_item(
            ActionRow(
                StatefulSelect(
                    options=row_options,
                    placeholder="Select row",
                    callback=self._on_row_select,
                )
            )
        )

        # Column select -- same rebuild-preservation pattern as the row select.
        col_options = [
            SelectOption(
                label=f"Column {COL_LABELS[i]}",
                value=str(i),
                default=(self._selected_col == i),
            )
            for i in range(BOARD_SIZE)
        ]
        self.add_item(
            ActionRow(
                StatefulSelect(
                    options=col_options,
                    placeholder="Select column",
                    callback=self._on_col_select,
                )
            )
        )

        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Fire!",
                    style=discord.ButtonStyle.danger,
                    emoji="\N{COLLISION SYMBOL}",
                    callback=self._fire,
                ),
                StatefulButton(
                    label="My Ships",
                    style=discord.ButtonStyle.secondary,
                    emoji="\N{SHIP}",
                    callback=self._show_my_ships,
                ),
                StatefulButton(
                    label="Forfeit",
                    style=discord.ButtonStyle.secondary,
                    callback=self._forfeit,
                ),
            )
        )

    def _build_game_over(self):
        """Build the UI for game-over state."""
        winner_id = self.player_1 if self.winner == 1 else self.player_2
        attack = self._attack_1 if self.winner == 1 else self._attack_2

        self.add_item(
            card(
                TextDisplay("## \N{ANCHOR} Battleship"),
                divider(),
                attack,
                divider(),
                color=COLOR_FORFEIT if self._forfeited_by else COLOR_WIN,
            )
        )

        if self._forfeited_by:
            self.add_item(
                alert(
                    f"<@{self._forfeited_by}> forfeited. **<@{winner_id}> wins!**",
                    level="success",
                )
            )
        else:
            self.add_item(
                alert(f"**<@{winner_id}> sank the entire fleet and wins!**", level="success")
            )

        # Rematch / close
        vote_count = len(self._rematch_votes)
        rematch_label = f"Rematch ({vote_count}/2)" if vote_count > 0 else "Rematch"

        self.add_item(
            ActionRow(
                StatefulButton(
                    label=rematch_label,
                    style=discord.ButtonStyle.primary,
                    emoji="\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}",
                    callback=self._rematch,
                    # Fixed, since a generated id would change with the vote count.
                    custom_id="rematch",
                ),
                StatefulButton(
                    label="My Ships",
                    style=discord.ButtonStyle.secondary,
                    emoji="\N{SHIP}",
                    callback=self._show_my_ships,
                ),
                # delete_message defaults to None, which reaches the
                # phase-aware exit() override below: delete during setup,
                # freeze once the game has started.
                self.make_exit_button(label="Close"),
            )
        )

    # // ==================( Callbacks )================== // #

    async def _cancel_setup(self, interaction: discord.Interaction):
        """Either player can cancel the game during setup."""
        # Clicks queue behind one another, so a click can come from a board
        # the game has moved past; each callback checks the phase it was
        # drawn for, and a dropped click is acknowledged by the library.
        if self.phase != "setup":
            return
        # The exit() override deletes the message during setup.
        await self.exit()

    async def _ready_up(self, interaction: discord.Interaction):
        """Lock in the clicking player's fleet."""
        if self.phase != "setup":
            return
        self._ready.add(interaction.user.id)

        started = len(self._ready) >= 2
        if started:
            self.phase = "active"

        self.build_ui()
        await self.refresh()

        if started:
            # Notify open MyShipsView instances so their re-roll button hides.
            await self.dispatch("BATTLESHIP_STARTED", {"match_id": self._match_key})

    async def _on_row_select(self, interaction: discord.Interaction, values: list[str]):
        """Store the selected row. No UI rebuild needed."""
        self._selected_row = int(values[0])

    async def _on_col_select(self, interaction: discord.Interaction, values: list[str]):
        """Store the selected column. No UI rebuild needed."""
        self._selected_col = int(values[0])

    async def _fire(self, interaction: discord.Interaction):
        """Fire at the selected cell, marking both players' grids in place."""
        if self.phase != "active" or self.winner is not None:
            return
        current_id = self._current_player_id()

        if interaction.user.id != current_id:
            await self.respond(interaction, f"It's <@{current_id}>'s turn!", ephemeral=True)
            return

        if self._selected_row is None or self._selected_col is None:
            await self.respond(
                interaction, "Select a **row** and **column** first!", ephemeral=True
            )
            return

        target = self._selected_row * BOARD_SIZE + self._selected_col
        coord = f"{ROW_LABELS[self._selected_row]}{COL_LABELS[self._selected_col]}"

        # Duplicate shot check -- the grid itself is the source of truth.
        # Any non-WATER cell has already been fired at.
        attack = self._attack_1 if self.turn == 1 else self._attack_2
        if attack[target] != WATER:
            await self.respond(
                interaction,
                f"You already fired at **{coord}**! Pick a different cell.",
                ephemeral=True,
            )
            return

        # Resolve the shot against the opponent's ships
        opponent_ships = self.ships_2 if self.turn == 1 else self.ships_1
        sunk_set = self.sunk_by_1 if self.turn == 1 else self.sunk_by_2
        defense = self._defense_2 if self.turn == 1 else self._defense_1
        # Must be resolved BEFORE the turn flip below; otherwise victim_id
        # would point at the attacker.
        victim_id = self.player_2 if self.turn == 1 else self.player_1

        # Check if the cell belongs to any ship
        hit_ship: str | None = None
        for name, cells in opponent_ships.items():
            if target in cells:
                hit_ship = name
                break

        if hit_ship:
            attack[target] = HIT
            defense[target] = SHIP_HIT

            # Check if this ship is now fully sunk -- every cell of the
            # ship is non-WATER on the attack grid (only HIT is possible
            # for ship cells that haven't been marked SUNK yet).
            ship_cells = opponent_ships[hit_ship]
            if all(attack[c] != WATER for c in ship_cells):
                sunk_set.add(hit_ship)
                attack[ship_cells] = SUNK
                defense[ship_cells] = SHIP_SUNK
                self._last_result = (
                    f"\N{COLLISION SYMBOL} Sunk <@{victim_id}>'s **{hit_ship.lower()}**!"
                )

                # Check win condition
                if len(sunk_set) == len(opponent_ships):
                    self.winner = self.turn
                    await self._finish_game(forfeit=False)
                    self.build_ui()
                    await self.refresh()
                    return
            else:
                self._last_result = f"\N{FIRE} Hit <@{victim_id}>'s ship at **{coord}**!"
        else:
            attack[target] = MISS
            defense[target] = WATER_MISS
            self._last_result = f"\N{MEDIUM WHITE CIRCLE} Missed <@{victim_id}> at **{coord}**."

        self.turn = 2 if self.turn == 1 else 1
        self._selected_row = None
        self._selected_col = None

        self.build_ui()
        await self.refresh()

        # Notify ephemeral MyShipsView subscribers
        await self.dispatch("BATTLESHIP_SHOT", {"cell": target, "match_id": self._match_key})

    async def _show_my_ships(self, interaction: discord.Interaction):
        """Open an ephemeral live-updating fleet view for the clicking player.

        Dedup is handled at the library level via ``MyShipsView.instance_limit``:
        if this player already has a fleet view alive (visible or dismissed),
        the standard replace path evicts it before the new one sends.

        ``allowed_users = {p1, p2}`` on the parent view means
        ``interaction_check`` has already rejected anyone else before
        control reaches this callback -- no manual auth check needed here.
        """
        view = MyShipsView(
            interaction=interaction,
            user_id=interaction.user.id,
            guild_id=self.guild_id,
            parent=self,
        )
        try:
            await view.send(ephemeral=True)
        except DISCORD_CALL_ERRORS as e:
            logger.warning(f"Failed to open fleet view: {e}")

    async def _forfeit(self, interaction: discord.Interaction):
        """The clicking player forfeits."""
        if self.phase != "active" or self.winner is not None:
            return
        self._forfeited_by = interaction.user.id
        self.winner = 2 if interaction.user.id == self.player_1 else 1
        self._last_result = None
        await self._finish_game(forfeit=True)
        self.build_ui()
        await self.refresh()

    async def _rematch(self, interaction: discord.Interaction):
        """Vote for a rematch. Resets to fleet setup when both players agree."""
        if self.winner is None:
            return
        self._rematch_votes.add(interaction.user.id)

        if len(self._rematch_votes) >= 2:
            # Swap who goes first. Fresh ships are placed by the
            # BATTLESHIP_REMATCH reducer below.
            self.player_1, self.player_2 = self.player_2, self.player_1
            self.sunk_by_1 = set()
            self.sunk_by_2 = set()

            # Attack grids clear back to all-water.  Defense grids are
            # repainted AFTER the dispatch once the reducer has written
            # the fresh fleets into application state.
            self._attack_1.clear()
            self._attack_2.clear()

            self.turn = 1
            self.winner = None
            self._forfeited_by = None
            self._last_result = None
            self._rematch_votes = set()
            self._selected_row = None
            self._selected_col = None
            self.phase = "setup"
            self._ready = set()

            # Close stale ephemeral views -- they reference the old grid
            # state snapshot. Players re-open from the new setup card.
            await self.exit_children()

            # Reset phase + shot counter and place fresh ships in one
            # reducer pass.  Player IDs ride the payload because the
            # reducer is module-level and the swap above has already
            # happened on the view.
            await self.dispatch(
                "BATTLESHIP_REMATCH",
                {
                    "player_1": self.player_1,
                    "player_2": self.player_2,
                    "match_id": self._match_key,
                },
            )

            # Now that state holds the fresh fleets, repaint defense
            # grids from ``self.ships_1`` / ``self.ships_2`` (which read
            # state).  Repainting before the dispatch would paint the
            # old fleets.
            self._repaint_defense_grids()

            self._start_setup_timer()

        self.build_ui()
        await self.refresh()

    async def exit(self, delete_message: bool | None = None):
        # Phase-aware default: setup-phase exits delete the message (nothing
        # worth preserving); active or game-over exits freeze it so the
        # forfeit / completion record stays visible.  Explicit caller args
        # always win.
        if delete_message is None:
            delete_message = self.phase == "setup" and self.winner is None
        await super().exit(delete_message=delete_message)

    async def on_instance_limit(self, error: InstanceLimitError) -> None:
        # blocked_user_id identifies who actually collided -- either the
        # owner (instance_policy reject) or a participant who is already
        # in another game (auto_register_participants rollback).  Address
        # the acting user in second person when they are the blocker
        # themselves; mention the participant in third person otherwise.
        if self.interaction is not None:
            blocked = error.blocked_user_id or self.user_id
            if blocked == self.interaction.user.id:
                message = "You're already in another game."
            else:
                message = f"<@{blocked}> is already in another game."
            await self.respond(self.interaction, message, ephemeral=True)

    # // ==================( Game Logic )================== // #

    def _start_setup_timer(self):
        """Start the auto-lock countdown for fleet setup, replacing any earlier one.

        ``create_task()`` makes the view own the timer, so it is cancelled
        when the view exits. Cancelling through the kept handle leaves the
        view's other tasks alone.
        """
        if self._setup_task is not None:
            self._setup_task.cancel()
        self._setup_task = self.create_task(self._auto_start())

    async def _auto_start(self):
        """Auto-start the game after SETUP_TIMEOUT if still in setup phase."""
        await asyncio.sleep(SETUP_TIMEOUT)
        if not self.is_finished() and self.phase == "setup":
            self.phase = "active"
            self.build_ui()
            await self.refresh()
            await self.dispatch("BATTLESHIP_STARTED", {"match_id": self._match_key})

    async def _finish_game(self, forfeit: bool):
        """Dispatch game result and close any private fleet panels.

        The game view stays up for Rematch or Close; the fleet panels
        have nothing left to show, so they close here. Stats are keyed
        by player rather than seat, so they follow a player across the
        seat swap a rematch makes.
        """
        winner_id = self.player_1 if self.winner == 1 else self.player_2
        loser_id = self.player_2 if self.winner == 1 else self.player_1
        async with self.batch():
            await self.dispatch(
                "BATTLESHIP_FINISHED",
                {"winner": self.winner, "forfeit": forfeit, "match_id": self._match_key},
            )
            await self._record_player_stats(winner_id, won=True, forfeit=False)
            await self._record_player_stats(loser_id, won=False, forfeit=forfeit)
        await self.exit_children()

    async def _record_player_stats(self, player_id: int, *, won: bool, forfeit: bool) -> None:
        """Bump a player's lifetime totals via ``user_guild``-scoped state.

        ``dispatch_scoped`` takes ``scope=`` because this view sets no
        ``state_scope``, and ``user_id=`` because the default is the
        view's owner, which is only one of the two players.
        """
        if self.guild_id is None:
            return
        existing = self.state_store.get_scoped(
            "user_guild",
            slot_name="battleship_stats",
            user_id=player_id,
            guild_id=self.guild_id,
        )
        new_stats = {
            "games": existing.get("games", 0) + 1,
            "wins": existing.get("wins", 0) + (1 if won else 0),
            "forfeits": existing.get("forfeits", 0) + (1 if forfeit else 0),
        }
        # SCOPED_UPDATE shallow-merges ``data`` into this player's entry in
        # the view's ``scoped_slot`` bucket.
        await self.dispatch_scoped(
            new_stats,
            scope="user_guild",
            user_id=player_id,
            guild_id=self.guild_id,
        )


# // ========================================( Ephemeral Fleet View )======================================== // #


class MyShipsView(StatefulLayoutView):
    """Ephemeral private board view with live updates and live re-roll.

    Reads the game view through ``self.parent``. The re-roll button only
    appears while the game is in setup: it dispatches BATTLESHIP_REROLL and
    repaints the parent's defense grid, which this panel shows directly.
    Once the game starts the panel only shows incoming damage.

    Cross-view reactivity:
        * Subscribes to ``BATTLESHIP_REROLL`` so the view rebuilds in place
          when its own re-roll button is clicked (no manual refresh).
        * Subscribes to ``BATTLESHIP_SHOT`` so the board auto-refreshes when
          the opponent fires.
        * Subscribes to ``BATTLESHIP_STARTED`` so the re-roll button hides
          when both players ready up.

    Lifecycle ownership map:

    +--------------------------------+------------------------------------------+
    | Event                          | Handler                                  |
    +================================+==========================================+
    | User clicks "Close"            | self.exit() -> deletes via exit_policy   |
    +--------------------------------+------------------------------------------+
    | User dismisses ephemeral in UI | Library replace path on next View Fleet  |
    |                                | (instance_limit=1 evicts the stale entry)|
    +--------------------------------+------------------------------------------+
    | Token nearing 15-min expiry    | auto_refresh_ephemeral hands off to a    |
    |                                | fresh ephemeral via the Refresh button   |
    +--------------------------------+------------------------------------------+
    | Game cancelled or timed out    | exit() or timeout closes attached views  |
    +--------------------------------+------------------------------------------+
    | Game ends                      | _finish_game closes the fleet panels     |
    +--------------------------------+------------------------------------------+
    | User clicks Re-Roll            | No exit -- self.refresh() in place via   |
    |                                | the BATTLESHIP_REROLL subscriber path    |
    +--------------------------------+------------------------------------------+
    | Bot restart                    | Ephemerals are not persistent -- gone    |
    +--------------------------------+------------------------------------------+

    Library-level dedup: ``instance_limit=1`` with ``instance_scope="user_guild"``
    means clicking "View Fleet" while a previous fleet view is still alive
    (visible or dismissed) automatically evicts the old one via the standard
    replace path. No manual tracking is required on the parent.
    """

    subscribed_actions = {
        "BATTLESHIP_REROLL",
        "BATTLESHIP_SHOT",
        "BATTLESHIP_STARTED",
    }
    owner_only = True
    instance_limit = 1
    instance_scope = "user_guild"
    # Opening View Fleet again deletes the previous panel, and Close
    # deletes this one.
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "delete"
    # The panel is watched rather than clicked once play starts, so no
    # click would extend a timeout. It lives as long as the game: the
    # game's own exit and timeout close it.
    timeout = None

    # ``auto_refresh_ephemeral`` installs a Refresh button shortly before
    # the 15-minute interaction token expires; clicking it reopens the
    # panel as a fresh ephemeral.
    auto_refresh_ephemeral = True
    refresh_button_label = "Refresh"

    def state_selector(self, state):
        """Return the state tuple this view depends on.

        The store short-circuits ``on_state_changed`` when the tuple
        compares equal to the previous dispatch's tuple. Three slices
        cover the three subscribed actions without false negatives:

        * ``own_fleet`` -- changes only when THIS player rerolls, so the
          opponent's BATTLESHIP_REROLL short-circuits here (the primary
          performance win: no rebuild on data this view doesn't show).
        * ``phase`` -- flips to "active" on STARTED, so setup-only UI
          elements drop correctly.
        * ``shots_fired`` -- monotonically increases on every SHOT, so
          incoming damage always produces a rebuild regardless of whose
          turn it was.

        All three read through ``self.parent._match_key`` so a
        concurrent, unrelated match never contributes a false-positive
        (or false-negative) change to this tuple.
        """
        match_key = self.parent._match_key
        return (
            read_slot(state, "battleship", match_key, "fleets", self.user_id),
            read_slot(state, "battleship", match_key, "phase"),
            read_slot(state, "battleship", match_key, "shots_fired", default=0),
        )

    def __init__(self, **kwargs):
        # parent= attaches this panel to the game view for cleanup, and
        # self.parent reads it back, so the panel keeps no reference of
        # its own. Resolved by the time build_ui runs below.
        super().__init__(**kwargs)
        self.build_ui()

    def _own_ships(self) -> dict[str, list[int]]:
        return self.parent.ships_1 if self.user_id == self.parent.player_1 else self.parent.ships_2

    def _own_sunk(self) -> set[str]:
        return (
            self.parent.sunk_by_2 if self.user_id == self.parent.player_1 else self.parent.sunk_by_1
        )

    def _own_defense_grid(self) -> EmojiGrid:
        """Return the defense grid for this player."""
        return (
            self.parent._defense_1
            if self.user_id == self.parent.player_1
            else self.parent._defense_2
        )

    def build_ui(self):
        self.clear_items()
        ships = self._own_ships()
        sunk = self._own_sunk()
        defense = self._own_defense_grid()
        fleet = _ship_status_line(ships=ships, sunk_ships=sunk)

        in_setup = self.parent.phase == "setup"
        title = "## \N{SHIP} My Fleet (Setup)" if in_setup else "## \N{SHIP} My Fleet"
        prompt = (
            "Generate a new board layout, or close and hit **Ready**."
            if in_setup
            else f"{SHIP_HIT} Hit \N{BULLET} {SHIP_SUNK} Sunk \N{BULLET} "
            f"{WATER_MISS} Miss \N{BULLET} {WATER_EMPTY} Empty"
        )

        self.add_item(
            card(
                title,
                divider(),
                defense,
                divider(),
                TextDisplay(fleet),
                TextDisplay(prompt),
                color=discord.Color.blue(),
            )
        )

        buttons = []
        if in_setup:
            regenerate = StatefulButton(
                label="Regenerate",
                style=discord.ButtonStyle.primary,
                emoji="\N{GAME DIE}",
                callback=self._reroll,
            )
            # Caps re-rolls at ~2/sec, per player. The deadline lives on the
            # view, so it survives this button being rebuilt by the refresh
            # the click triggers.
            with_cooldown(
                regenerate,
                seconds=0.5,
                scope="user",
                message="Re-rolling too fast. Try again in {remaining}s.",
            )
            buttons.append(regenerate)
        buttons.append(self.make_exit_button(label="Close"))
        self.add_item(ActionRow(*buttons))

    async def _reroll(self, interaction: discord.Interaction):
        """Re-randomize this player's fleet and broadcast to the public card.

        The dispatch carries the new placement as payload; the REROLL
        reducer writes it into ``state["application"]["battleship"]
        [match_id]["fleets"][player_id]``. The parent's ``ships_1`` /
        ``ships_2`` properties then read the new placement directly from
        state, and the opponent's MyShipsView short-circuits via its
        ``state_selector`` because its own fleet slice didn't change.
        """
        # Phase guard: re-roll is meaningless once the game has started or
        # the parent has been torn down. No awaits between this guard and
        # the defense-grid refresh below, so the window is closed.
        if self.parent.is_finished() or self.parent.phase != "setup":
            return

        new_ships = _place_ships(BOARD_SIZE, SHIPS)
        _paint_fleet(self._own_defense_grid(), new_ships)

        self.parent._ready.discard(self.user_id)

        await self.dispatch(
            "BATTLESHIP_REROLL",
            {
                "player_id": self.user_id,
                "ships": new_ships,
                "match_id": self.parent._match_key,
            },
        )


# // ========================================( Cog )======================================== // #


class BattleshipExample(commands.Cog, name="v2_battleship_example"):
    """Two-player Battleship with V2 components and lifetime stats."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_group(
        name="battleship",
        description="Play Battleship and view per-player stats.",
    )
    async def battleship(self, context: Context) -> None:
        """Parent group for /battleship subcommands."""
        # Subcommands do the work; the group itself is a routing stub.

    @battleship.command(
        name="play",
        description="Challenge someone to a game of Battleship.",
    )
    @app_commands.describe(opponent="The player to challenge")
    async def battleship_play(self, context: Context, opponent: discord.Member) -> None:
        """Challenge another member to Battleship.

        The opponent must accept the challenge before the game starts.
        Both players interact with the same message. Ships are placed
        randomly. Use the "My Ships" button to view your private board.
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

        # Pre-check both players before the opponent sees a challenge.
        if not BattleshipView.check_instance_available(
            user_id=context.author.id,
            guild_id=context.guild.id,
        ):
            await context.send(
                "You're already in a game. Finish it and close the board first.",
                ephemeral=True,
            )
            return
        if not BattleshipView.check_instance_available(
            user_id=opponent.id,
            guild_id=context.guild.id,
        ):
            await context.send(f"{opponent.mention} is already in a game.", ephemeral=True)
            return

        view = BattleshipChallengeView(
            context=context,
            challenger_id=context.author.id,
            opponent=opponent,
            guild_id=context.guild.id,
        )
        await view.send()

    @battleship.command(
        name="stats",
        description="Show a player's lifetime Battleship record.",
    )
    @app_commands.describe(user="The player to look up (defaults to you)")
    async def battleship_stats(
        self,
        context: Context,
        user: discord.Member = None,
    ) -> None:
        """Display one player's lifetime record in this guild."""
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        target = user or context.author
        store = get_store()
        stats = store.get_scoped(
            "user_guild",
            slot_name="battleship_stats",
            user_id=target.id,
            guild_id=context.guild.id,
        )

        if not stats or stats.get("games", 0) == 0:
            await context.send(
                f"{target.mention} has no recorded Battleship games in this server.",
                ephemeral=True,
            )
            return

        games = stats.get("games", 0)
        wins = stats.get("wins", 0)
        losses = games - wins  # derived -- Battleship has no draws
        forfeits = stats.get("forfeits", 0)
        win_rate = (wins / games * 100) if games else 0.0

        body = stats_card(
            f"Battleship -- {target.display_name}",
            {
                "Games": str(games),
                "Wins": str(wins),
                "Losses": str(losses),
                "Forfeits": str(forfeits),
                "Win rate": f"{win_rate:.1f}%",
            },
        )
        await DisplayLayoutView(context=context, container=body).send(ephemeral=True)

    @battleship.command(
        name="leaderboard",
        description="Show this server's Battleship leaderboard.",
    )
    async def battleship_leaderboard(self, context: Context) -> None:
        """Display server-wide totals and the top 10 players by wins."""
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        store = get_store()
        entries = store.computed["battleship_leaderboards"].get(context.guild.id, [])

        if not entries:
            await context.send(
                "No Battleship games have been played in this server yet.",
                ephemeral=True,
            )
            return

        class _BattleshipLeaderboard(LeaderboardLayoutView):
            # Section mode renders each entry as a two-line card with an avatar
            # thumbnail: top 10 across two pages of 5.
            leaderboard_top_n = 10
            leaderboard_per_page = 5
            entry_layout = "sections"

            def format_secondary(self, rank, user_id, stats):
                wins = stats.get("wins", 0)
                games = stats.get("games", 0)
                forfeits = stats.get("forfeits", 0)
                bar = render_progress(wins, games or 1, width=6, show_percent=True)
                return f"{wins}W / {games}G \N{BULLET} {forfeits}F \N{BULLET} {bar}"

            def build_header(self, page):
                # Overview stats card above the rankings on every page.
                # get_entries() is every player in the server (the entries=
                # list); ranked_entries holds only the top 10 shown below.
                entries = self.get_entries()
                # Each game contributes to two player rows.
                unique_games = sum(e[1].get("games", 0) for e in entries) // 2
                total_forfeits = sum(e[1].get("forfeits", 0) for e in entries)
                return stats_card(
                    "Overview",
                    {
                        "Games played": str(unique_games),
                        "Forfeits": str(total_forfeits),
                        "Players": str(len(entries)),
                    },
                )

            def build_footer(self, page):
                # A raw component folds inside the rankings card, below the rows.
                return TextDisplay("-# Sorted by wins")

        view = _BattleshipLeaderboard(
            context=context,
            entries=entries,
            title=f"Battleship Leaderboard -- {context.guild.name}",
            # The base view stores bot= for the default get_avatar_url, which
            # resolves each entry's avatar from the user cache (no hook needed).
            bot=self.bot,
        )
        await view.send(ephemeral=True)


async def setup(bot) -> None:
    # persistent_slots = ("battleship_stats",) requires PersistenceMiddleware.
    # Without it, stats accumulate during a session and are lost on restart.
    # Install it from your bot's setup_hook before loading this cog::
    #
    #     from cascadeui import PersistenceMiddleware, SQLiteBackend, setup_middleware
    #     await setup_middleware(
    #         PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=self),
    #     )
    #
    # v2_persistence.py is the full reference for the persistence surface:
    # data persistence via persistent_slots, view persistence via
    # PersistentView, and the bot=self argument that re-attaches persistent
    # panels on restart.
    await bot.add_cog(BattleshipExample(bot=bot))
