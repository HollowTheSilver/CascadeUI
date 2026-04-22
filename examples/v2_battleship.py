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
* ``check_instance_available()`` at the command level to reject challenges
  before the opponent is involved.
* Instance limiting (``instance_limit=1``, ``instance_scope="user_guild"``)
  with ``auto_register_participants`` to claim both players atomically.
* Ephemeral fleet panels with ``auto_refresh_ephemeral`` for the 15-min
  token handoff, ``parent=`` kwarg for automatic cleanup attachment, and
  ``instance_policy="replace"`` for dedup.
* Phase-aware ``exit()`` override (delete during setup, freeze on
  completion) and ``task_manager`` for the auto-start timer.
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
import random

import discord
from discord import SelectOption, app_commands
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    DisplayLayoutView,
    EmojiGrid,
    InstanceLimitError,
    LeaderboardLayoutView,
    StateStore,
    StatefulButton,
    StatefulLayoutView,
    StatefulSelect,
    alert,
    card,
    cascade_reducer,
    computed,
    divider,
    emoji_grid,
    gap,
    read_slot,
    get_store,
    key_value,
    access_slot,
    stats_card,
)

import logging

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
COL_EMOJI = [f"{i}\ufe0f\u20e3" if i < 10 else "\U0001f51f" for i in range(1, BOARD_SIZE + 1)]

# Per-ship colored squares for visual distinction on the private board
SHIP_COLORS = {
    "Carrier": "\U0001f7e5",  # 🟥 Red
    "Battleship": "\U0001f7e7",  # 🟧 Orange
    "Cruiser": "\U0001f7e8",  # 🟨 Yellow
    "Submarine": "\U0001f7e9",  # 🟩 Green
    "Destroyer": "\U0001f7ea",  # 🟪 Purple
}

# Emoji for the attack board (shots fired at opponent)
WATER = "\u2b1b"  # ⬛ Black square -- empty grid cell
MISS = "\U0001f7e6"  # 🟦 Blue square -- miss (ocean splash)
HIT = "\U0001f525"  # 🔥 Fire -- hit (ship not yet sunk)
SUNK = "\U0001f480"  # 💀 Skull -- sunk ship cell

# Emoji for the private board (your ships + incoming damage)
SHIP_HIT = "\U0001f525"  # 🔥 Fire -- your ship was hit
SHIP_SUNK = "\U0001f480"  # 💀 Skull -- your ship was sunk
WATER_MISS = "\U0001f7e6"  # 🟦 Blue square -- opponent missed here
WATER_EMPTY = "\u2b1b"  # ⬛ Black square -- empty water

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


def _make_defense_grid(ships: dict[str, list[int]]) -> EmojiGrid:
    """Create a defense grid with ships painted in their fleet colors."""
    grid = emoji_grid(
        BOARD_SIZE, BOARD_SIZE, fill=WATER_EMPTY, row_labels=ROW_EMOJI, col_labels=COL_EMOJI
    )
    for name, cells in ships.items():
        grid[cells] = SHIP_COLORS.get(name, "\u2b1c")
    return grid


def _ship_status_line(ships: dict[str, list[int]], sunk_ships: set[str], emoji: bool = True) -> str:
    """One-line summary of fleet status: ship names with color and strikethrough for sunk."""
    parts = []
    for name, _ in SHIPS:
        if name in ships:
            color = SHIP_COLORS.get(name, "")
            label = f"~~{name}~~" if name in sunk_ships else f"**{name}**"
            parts.append(f"{color} {label}" if emoji else label)
    return " \u2022 ".join(parts)


# // ========================================( Reducers )======================================== // #
#
# Battleship demonstrates a deliberate mixed-scope state design.
#
# Shared per-game state lives under ``state["application"]["battleship"]``
# and is written by the four lifecycle reducers below. Both players read
# this slot (via ``MyShipsView.state_selector``), so it cannot live behind
# a per-user scope key.
#
#     state["application"]["battleship"] = {
#         "fleets":      {player_id: {ship_name: [cell_indices]}},
#         "phase":       "setup" | "active" | "finished",
#         "shots_fired": int,   # per-match counter, reset on REMATCH
#     }
#
# Per-player lifetime totals (games, wins, forfeits) live under
# ``user_guild``-scoped state instead, written by ``SCOPED_UPDATE``
# actions dispatched from ``BattleshipView._record_player_stats`` at game
# end. Lifetime stats belong to one player and persist across matches
# against different opponents -- exactly what scoped state is for.
#
# ``access_slot(state, "battleship")`` keeps the slot key in one
# string instead of repeating ``state.setdefault("application", {})
# .setdefault("battleship", {})`` in every reducer.


@cascade_reducer("BATTLESHIP_REROLL")
async def battleship_reroll_reducer(action, state):
    """Record the rerolled fleet so selectors can detect per-player changes."""
    fleets = access_slot(state, "battleship", "fleets")
    fleets[action["payload"]["player_id"]] = action["payload"]["ships"]
    return state


@cascade_reducer("BATTLESHIP_STARTED")
async def battleship_started_reducer(action, state):
    """Transition phase to active so setup-only UI elements drop out."""
    bs = access_slot(state, "battleship")
    bs["phase"] = "active"
    return state


@cascade_reducer("BATTLESHIP_SHOT")
async def battleship_shot_reducer(action, state):
    """Increment the shot counter so every shot produces a selector delta."""
    bs = access_slot(state, "battleship")
    bs["shots_fired"] = bs.get("shots_fired", 0) + 1
    return state


@cascade_reducer("BATTLESHIP_REMATCH")
async def battleship_rematch_reducer(action, state):
    """Reset per-match fields for a fresh game; lifetime stats are scoped."""
    bs = access_slot(state, "battleship")
    bs["phase"] = "setup"
    bs["shots_fired"] = 0
    return state


@cascade_reducer("BATTLESHIP_FINISHED")
async def battleship_finished_reducer(action, state):
    """Mark the match finished in the shared game slot.

    Per-player lifetime stats (games, wins, forfeits) are dispatched
    separately as ``SCOPED_UPDATE`` actions targeting ``user_guild``
    scope -- see ``BattleshipView._record_player_stats`` for the
    rationale.
    """
    bs = access_slot(state, "battleship")
    bs["phase"] = "finished"
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
@computed(selector=lambda s: s.get("application", {}).get("battleship_stats", {}))
def battleship_leaderboards(bucket: dict) -> dict:
    """Return ``{guild_id: [(user_id, stats), ...]}`` sorted by wins desc."""
    # Wrap the cached bucket in a minimal envelope so StateStore.iter_scoped
    # can parse the scope keys. The library handles the "user_guild:uid:gid"
    # format and silently skips malformed entries.
    envelope = {"application": {"battleship_stats": bucket}}
    by_guild: dict = {}
    for ids, stats in StateStore.iter_scoped(
        envelope, "user_guild", slot_name="battleship_stats"
    ):
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
    exit_policy = "delete"  # disposable prompt -- never freeze
    # No scoped state -- the challenge prompt is a one-shot gate that
    # reads nothing from the store and writes nothing.
    state_scope = None

    def __init__(self, *args, challenger_id: int, opponent: discord.Member, **kwargs):
        kwargs.setdefault("timeout", CHALLENGE_TIMEOUT)
        super().__init__(*args, **kwargs)
        self.challenger_id = challenger_id
        self.opponent = opponent
        self.allowed_users = {opponent.id}
        self.build_ui()

    def build_ui(self):
        self.clear_items()
        self.add_item(
            card(
                "## \u2693 Battleship Challenge",
                TextDisplay(
                    f"<@{self.challenger_id}> challenges <@{self.opponent.id}> "
                    f"to a **{BOARD_SIZE}\u00d7{BOARD_SIZE}** game of Battleship!"
                ),
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

        view = BattleshipView(
            interaction=interaction,
            user_id=self.challenger_id,
            guild_id=self.guild_id,
            opponent_id=self.opponent.id,
        )

        # send() returns None when on_instance_limit handles the block
        # (the override on BattleshipView sends a custom <@user> message).
        # auto_register_participants = True on BattleshipView claims a slot
        # for both players from allowed_users during send(); rollback is
        # all-or-nothing, so a None return means zero side effects.
        if await view.send() is None:
            return

        # Auto-send the opponent's fleet view as an ephemeral followup so they
        # see their ships immediately and discover the live re-roll showcase
        # on their first interaction. Only the opponent's fleet view is
        # auto-sent here, because ephemeral followups must attach to the
        # interaction being handled -- this callback runs on the opponent's
        # Accept click. The challenger spawns their own fleet view by clicking
        # "View Fleet" on the public card, which fires their own interaction.
        # Per StatefulLayoutView.send() contract: only Discord HTTP errors
        # propagate from the send pipeline. Session/participant rejections
        # return None (handled by the library), and RuntimeError would mean
        # a programmer error, not something to swallow at runtime.
        fleet_view = MyShipsView(
            interaction=interaction,
            user_id=self.opponent.id,
            guild_id=self.guild_id,
            parent_view=view,
            parent=view,
        )
        try:
            await fleet_view.send(ephemeral=True)
        except discord.HTTPException as e:
            logger.warning(f"Failed to auto-send opponent's fleet view: {e}")

    async def _decline(self, interaction: discord.Interaction):
        self.clear_items()
        self.add_item(
            card(
                "## \u2693 Battleship Challenge",
                TextDisplay(
                    f"<@{self.opponent.id}> declined the challenge from "
                    f"<@{self.challenger_id}>."
                ),
                color=discord.Color.dark_grey(),
            )
        )
        # ``delete_message=False`` overrides the class ``exit_policy = "delete"``
        # for this one call so the decline card remains visible in the channel
        # as a record of the refusal, matching the TicTacToe decline behavior.
        await self.exit(delete_message=False)

    async def on_timeout(self):
        self.clear_items()
        self.add_item(
            card(
                "## \u2693 Battleship Challenge",
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
    attack grids, two defense grids). Grids are mutated incrementally
    on each shot -- no rebuild-from-scratch render functions. The same
    grid objects are dropped into ``card()`` on every ``build_ui()``
    call; their ``content`` is always current at add time.
    """

    unauthorized_message = "You're not part of this game."
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "reject"
    # protect_attached is a no-op under instance_policy="reject" (rejection
    # means replacement never fires).
    protect_attached = True
    # ``state_scope = None`` because game state lives under a custom reducer
    # (BATTLESHIP_REROLL) written to the global state tree, not under any of
    # the four built-in scope keys.
    state_scope = None
    # Lifetime stats land in a named scoped bucket so Battleship data
    # stays isolated from every other subsystem's scoped data. The
    # bucket name also flows through ``persistent_slots`` so only this
    # bucket is write-through to disk -- TicTacToe, Settings, and other
    # views' scoped writes are unaffected. The live game state lives
    # under the custom ``battleship`` slot (seeded fresh per match) so
    # nothing in-match is persisted.
    scoped_slot = "battleship_stats"
    persistent_slots = ("battleship_stats",)
    auto_defer = True
    # participant_limit = 2 is technically redundant with allowed_users
    # = {player_1, player_2} -- two specific IDs already cap occupancy
    # at two. Both are set explicitly to show the auth domain
    # (allowed_users / on_unauthorized) and the capacity domain
    # (participant_limit / on_participant_limit) side by side in one
    # class body. See "Combining allowed_users and participant_limit"
    # in docs/guide/views.md.
    participant_limit = 2
    auto_register_participants = True
    # exit_policy is not set here -- the choice is phase-dependent, so
    # exit() is overridden below. Subscriptions are narrowed to
    # BATTLESHIP_REROLL only: the default VIEW_DESTROYED set would
    # trigger redundant rebuilds every time a child fleet panel exits
    # during _cleanup_attached_children.
    subscribed_actions = {"BATTLESHIP_REROLL"}

    # Ship placements live in ``state["application"]["battleship"]["fleets"]``
    # via the BATTLESHIP_REROLL reducer. These properties are the canonical
    # read path; writes happen exclusively through dispatch() or the
    # _seed_fleets helper below. Making them read-only @property enforces
    # that -- any stray ``self.ships_1 = ...`` raises AttributeError at the
    # call site instead of silently desynchronising state from local data.
    @property
    def ships_1(self) -> dict[str, list[int]]:
        return self._fleets().get(self.player_1, {})

    @property
    def ships_2(self) -> dict[str, list[int]]:
        return self._fleets().get(self.player_2, {})

    def _fleets(self) -> dict[int, dict[str, list[int]]]:
        return read_slot(self.state_store.state, "battleship", "fleets", default={})

    def _place_fresh_fleets(self, state) -> None:
        """Write a fresh random fleet for each player into the application slot.

        Called from ``seed_initial_state`` (initial game, runs inside
        the send-pipeline batch) and from ``_rematch`` (after the
        BATTLESHIP_REMATCH dispatch resets the per-match fields).
        Bypasses the BATTLESHIP_REROLL dispatch path because seeding is
        initialization, not user-driven change -- no fan-out is wanted.
        The reducer owns every subsequent write.
        """
        fleets = access_slot(state, "battleship", "fleets")
        fleets[self.player_1] = _place_ships(BOARD_SIZE, SHIPS)
        fleets[self.player_2] = _place_ships(BOARD_SIZE, SHIPS)

    def _repaint_defense_grids(self) -> None:
        """Repaint both defense grids from the current state-stored fleets.

        Defense grids visually encode where ships sit; whenever fleets
        change (initial seed or rematch swap) the grids must be cleared
        and repainted so the visual matches the data.
        """
        self._defense_1.clear()
        self._defense_2.clear()
        for name, cells in self.ships_1.items():
            self._defense_1[cells] = SHIP_COLORS.get(name, "\u2b1c")
        for name, cells in self.ships_2.items():
            self._defense_2[cells] = SHIP_COLORS.get(name, "\u2b1c")

    def __init__(self, *args, opponent_id: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.player_1 = self.user_id  # Challenger goes first
        self.player_2 = opponent_id
        self.allowed_users = {self.player_1, self.player_2}

        # Live grids -- created empty here, painted in
        # seed_initial_state once fleets exist in state. Same grid
        # instances stay live for the view's lifetime; mutations from
        # _fire() are visible on every rebuild. Defense slots use the
        # attack-grid factory at init because no ships exist yet; the
        # ship painting happens once fleets are placed, via _make_defense_grid.
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

        # Selected coordinates (from selects)
        self._selected_row: int | None = None
        self._selected_col: int | None = None

        # No state seeding or build_ui() here -- both deferred to
        # seed_initial_state. The hook fires inside _send_pipeline
        # AFTER register_view (so the slot is reachable from other
        # views' selectors) but BEFORE the Discord HTTP send (so the
        # painted grids and built component tree ship in the first
        # render). __init__ stays pure: instance attributes only, no
        # state writes, no UI construction.

    async def seed_initial_state(self, state):
        """Seed fleets, paint defense grids, and build the initial component tree.

        The three steps share a data dependency -- defense grids paint
        the seeded positions and ``build_ui`` reads ``self.ships_1`` /
        ``self.ships_2`` (which read state) -- so collapsing them into
        one hook keeps the order obvious. Runs once per send: the
        rematch path reseeds via ``_place_fresh_fleets`` directly
        because the view is already alive at that point.
        """
        self._place_fresh_fleets(state)
        self._repaint_defense_grids()
        self.build_ui()

    async def send(self, *, ephemeral: bool = False):
        result = await super().send(ephemeral=ephemeral)
        # send() returns None when session limiting blocks the view.
        # Starting background tasks on a rejected view would leave
        # orphaned timers dispatching actions from a dead instance.
        if result is not None:
            self._start_setup_timer()
        return result

    # // ==================( UI Building )================== // #

    def _current_player_id(self) -> int:
        return self.player_1 if self.turn == 1 else self.player_2

    def build_ui(self):
        """Rebuild the full component tree from current game state.

        Grid objects are long-lived -- they're already up-to-date from
        incremental mutations in ``_fire()``. This method just drops
        them into the component tree at the right position.
        """
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
        p1_mark = "\u2705" if self.player_1 in self._ready else "\u23f3"
        p2_mark = "\u2705" if self.player_2 in self._ready else "\u23f3"

        self.add_item(
            card(
                "## \u2693 Fleet Setup",
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
                    emoji="\U0001f6a2",
                    callback=self._show_my_ships,
                ),
                StatefulButton(
                    label="Ready",
                    style=discord.ButtonStyle.success,
                    emoji="\u2714",
                    callback=self._ready_up,
                ),
                StatefulButton(
                    label="Cancel",
                    style=discord.ButtonStyle.danger,
                    emoji="\u274c",
                    callback=self._cancel_setup,
                ),
            )
        )

    def _build_active(self):
        """Build the UI for active play: attack board + targeting controls.

        The attack grid is a long-lived ``EmojiGrid`` that was already
        mutated by ``_fire()`` -- it's dropped straight into the card
        with no rendering step.
        """
        current_id = self._current_player_id()
        color = COLOR_P1_TURN if self.turn == 1 else COLOR_P2_TURN

        attack = self._attack_1 if self.turn == 1 else self._attack_2
        opponent_ships = self.ships_2 if self.turn == 1 else self.ships_1
        sunk = self.sunk_by_1 if self.turn == 1 else self.sunk_by_2

        self.add_item(
            card(
                TextDisplay("## \u2693 Battleship"),
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

        # Last shot result
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

        # Action buttons
        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Fire!",
                    style=discord.ButtonStyle.danger,
                    emoji="\U0001f4a5",
                    callback=self._fire,
                ),
                StatefulButton(
                    label="My Ships",
                    style=discord.ButtonStyle.secondary,
                    emoji="\U0001f6a2",
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
                TextDisplay("## \u2693 Battleship"),
                divider(),
                attack,
                divider(),
                color=COLOR_FORFEIT if self._forfeited_by else COLOR_WIN,
            )
        )

        # Result alert
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
                    emoji="\U0001f504",
                    callback=self._rematch,
                ),
                StatefulButton(
                    label="My Ships",
                    style=discord.ButtonStyle.secondary,
                    emoji="\U0001f6a2",
                    callback=self._show_my_ships,
                ),
                StatefulButton(
                    label="Close",
                    style=discord.ButtonStyle.secondary,
                    emoji="\u274c",
                    callback=self._close,
                ),
            )
        )

    # // ==================( Callbacks )================== // #

    async def _cancel_setup(self, interaction: discord.Interaction):
        """Either player can cancel the game during setup."""
        # phase == "setup" here, so exit() resolves to delete via the override.
        await self.exit()

    async def _ready_up(self, interaction: discord.Interaction):
        """Lock in the clicking player's fleet."""
        self._ready.add(interaction.user.id)

        started = len(self._ready) >= 2
        if started:
            self.phase = "active"

        self.build_ui()
        await self.refresh()

        if started:
            # Notify open MyShipsView instances so their re-roll button hides.
            await self.dispatch("BATTLESHIP_STARTED", {})

    async def _on_row_select(self, interaction: discord.Interaction, values: list[str]):
        """Store the selected row. No UI rebuild needed."""
        self._selected_row = int(values[0])

    async def _on_col_select(self, interaction: discord.Interaction, values: list[str]):
        """Store the selected column. No UI rebuild needed."""
        self._selected_col = int(values[0])

    async def _fire(self, interaction: discord.Interaction):
        """Fire at the selected cell.

        Mutates the current player's attack grid and the opponent's
        defense grid incrementally -- no rebuild-from-scratch needed.
        The grid content auto-updates on each cell assignment.
        """
        current_id = self._current_player_id()

        # Turn enforcement
        if interaction.user.id != current_id:
            await self.respond(interaction, f"It's <@{current_id}>'s turn!", ephemeral=True)
            return

        # Validate selection
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
                interaction, f"You already fired at **{coord}**! Pick a different cell.",
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
                self._last_result = f"\U0001f4a5 Sunk <@{victim_id}>'s **{hit_ship.lower()}**!"

                # Check win condition
                if len(sunk_set) == len(opponent_ships):
                    self.winner = self.turn
                    await self._finish_game(forfeit=False)
                    self.build_ui()
                    await self.refresh()
                    return
            else:
                self._last_result = f"\U0001f525 Hit <@{victim_id}>'s ship at **{coord}**!"
        else:
            attack[target] = MISS
            defense[target] = WATER_MISS
            self._last_result = f"\u26aa Missed <@{victim_id}> at **{coord}**."

        # Switch turn
        self.turn = 2 if self.turn == 1 else 1
        self._selected_row = None
        self._selected_col = None

        self.build_ui()
        await self.refresh()

        # Notify ephemeral MyShipsView subscribers
        await self.dispatch("BATTLESHIP_SHOT", {"cell": target})

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
            parent_view=self,
            parent=self,
        )
        # See _accept for the rationale on the narrowed except clause.
        try:
            await view.send(ephemeral=True)
        except discord.HTTPException as e:
            logger.warning(f"Failed to open fleet view: {e}")

    async def _forfeit(self, interaction: discord.Interaction):
        """The clicking player forfeits."""
        self._forfeited_by = interaction.user.id
        self.winner = 2 if interaction.user.id == self.player_1 else 1
        self._last_result = None
        await self._finish_game(forfeit=True)
        self.build_ui()
        await self.refresh()

    async def _rematch(self, interaction: discord.Interaction):
        """Vote for a rematch. Resets to fleet setup when both players agree."""
        self._rematch_votes.add(interaction.user.id)

        if len(self._rematch_votes) >= 2:
            # Swap who goes first, re-randomize ships. Reseed through
            # the same helper used by seed_initial_state so the
            # ships_1 / ships_2 properties (which read from state) see
            # the new placements immediately.
            self.player_1, self.player_2 = self.player_2, self.player_1
            self._place_fresh_fleets(self.state_store.state)
            self.sunk_by_1 = set()
            self.sunk_by_2 = set()

            # Attack grids clear back to all-water; defense grids get
            # repainted from the freshly seeded fleets.
            self._attack_1.clear()
            self._attack_2.clear()
            self._repaint_defense_grids()

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
            await self._cleanup_attached_children()

            # Reset phase + shot counter in application state so fresh
            # MyShipsView instances opened post-rematch see clean values
            # and their state_selector fires correctly on the first
            # STARTED/SHOT of the new match.
            await self.dispatch("BATTLESHIP_REMATCH", {})

            self._start_setup_timer()

        self.build_ui()
        await self.refresh()

    async def _close(self, interaction: discord.Interaction):
        """Close the game. Phase-aware behavior lives in ``exit()`` below."""
        await self.exit()

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
        """Start the auto-lock countdown for fleet setup.

        Routed through ``task_manager`` so the timer is cancelled when the
        view exits and so successive rematches don't accumulate stale
        background tasks.  Cancels any prior timer first.
        """
        self.task_manager.cancel_tasks(self.id)
        self.task_manager.create_task(self.id, self._auto_start())

    async def _auto_start(self):
        """Auto-start the game after SETUP_TIMEOUT if still in setup phase."""
        await asyncio.sleep(SETUP_TIMEOUT)
        if not self.is_finished() and self.phase == "setup":
            self.phase = "active"
            self.build_ui()
            await self.refresh()
            await self.dispatch("BATTLESHIP_STARTED", {})

    async def _finish_game(self, forfeit: bool):
        """Dispatch game result and close any private fleet panels.

        The main game view stays alive so players can click Rematch or Close,
        but the ephemeral fleet panels have no purpose once the game is over --
        cleaning them up here keeps the registry honest and prevents stale
        ephemerals from lingering until their interaction-token timeout.

        Lifetime totals for both players are recorded as ``user_guild``
        scoped writes so each player's stats follow them across matches
        regardless of which seat they sat in. The original "p1_wins" /
        "p2_wins" keys tied to seat position would silently misattribute
        wins after a rematch swap.
        """
        winner_id = self.player_1 if self.winner == 1 else self.player_2
        loser_id = self.player_2 if self.winner == 1 else self.player_1
        async with self.state_store.batch():
            await self.dispatch(
                "BATTLESHIP_FINISHED",
                {"winner": self.winner, "forfeit": forfeit},
            )
            await self._record_player_stats(winner_id, won=True, forfeit=False)
            await self._record_player_stats(loser_id, won=False, forfeit=forfeit)
        await self._cleanup_attached_children()

    async def _record_player_stats(
        self, player_id: int, *, won: bool, forfeit: bool
    ) -> None:
        """Bump a player's lifetime totals via ``user_guild``-scoped state.

        Demonstrates the mixed-scope half of the design: shared per-game
        state (phase, fleets, shots_fired) lives under the BATTLESHIP_*
        custom reducers because both players read it; per-player lifetime
        totals live under ``user_guild`` scope because they belong to one
        player and persist across opponents.

        ``dispatch_scoped`` is called with explicit ``scope=`` and
        ``user_id=`` kwargs so this view (``state_scope = None``) can
        write into another player's scope key. Without overrides the
        call would target ``self.user_id``, which is wrong when
        recording the loser's stats from the winner's view.
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
        # SCOPED_UPDATE shallow-merges ``data`` into the existing slice.
        # The view's ``scoped_slot`` attribute routes the write into
        # ``state["application"]["battleship_stats"]``; other subsystems
        # with their own ``scoped_slot`` values are untouched.
        await self.dispatch_scoped(
            new_stats,
            scope="user_guild",
            user_id=player_id,
            guild_id=self.guild_id,
        )


# // ========================================( Ephemeral Fleet View )======================================== // #


class MyShipsView(StatefulLayoutView):
    """Ephemeral private board view with live updates and live re-roll.

    Holds a back-reference to the parent ``BattleshipView`` so the re-roll
    callback can mutate ship placements and defense grids directly. The
    re-roll button only appears while the parent is in the setup phase;
    once the game starts it disappears and the view becomes a pure
    spectator of incoming damage.

    The defense grid is a long-lived ``EmojiGrid`` owned by the parent.
    This view references it directly -- mutations from ``_fire()`` on
    the parent are already visible when this view rebuilds.

    Cross-view reactivity:
        * Subscribes to ``BATTLESHIP_REROLL`` so the view rebuilds in place
          when its own re-roll button is clicked (no manual refresh).
        * Subscribes to ``BATTLESHIP_SHOT`` so the board auto-refreshes when
          the opponent fires.
        * Subscribes to ``BATTLESHIP_STARTED`` so the re-roll button hides
          when both players ready up.
        * Subscribes to ``BATTLESHIP_FINISHED`` to redraw the final state.

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
    |                                | fresh ephemeral via "Continue Session"   |
    +--------------------------------+------------------------------------------+
    | Parent game cancelled / ends   | attach_child cleanup on parent.exit()    |
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
        "BATTLESHIP_FINISHED",
    }
    owner_only = True
    instance_limit = 1
    instance_scope = "user_guild"
    # Fleet ephemerals should never linger frozen. instance_policy="replace"
    # is the library default, set explicitly here because the whole dedup
    # story below depends on it: clicking "View Fleet" again evicts the
    # previous fleet view via the replace path, which then runs
    # replace_policy="delete" (also the default) to delete its message.
    # Every bare exit() path -- close button, timeout, _cleanup_attached_children
    # on game end -- also deletes via exit_policy="delete".
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "delete"

    # Library default; restated for visibility. The library installs a
    # "Continue Session" button shortly before the interaction token
    # expires, spawning a fresh ephemeral with no visible cliff.
    auto_refresh_ephemeral = True
    refresh_button_label = "Refresh"

    # Spam-clicking Regenerate during setup can ship 5+ message edits per
    # second to the same channel, which Discord rate-limits at the channel
    # level. A 250 ms proactive cooldown caps the edit rate at ~4/sec; the
    # deferred refresh re-enters on_state_changed at fire time so the user
    # always sees the LATEST fleet, never an interim flicker.
    refresh_cooldown_ms = 250

    def state_selector(self, state):
        """Return the state tuple this view depends on.

        The store short-circuits ``on_state_changed`` when the tuple
        compares equal to the previous dispatch's tuple. Three slices
        cover the four subscribed actions without false negatives:

        * ``own_fleet`` -- changes only when THIS player rerolls, so the
          opponent's BATTLESHIP_REROLL short-circuits here (the primary
          performance win: no rebuild on data this view doesn't show).
        * ``phase`` -- flips on STARTED ("active") and FINISHED
          ("finished"), so setup-only UI elements drop correctly.
        * ``shots_fired`` -- monotonically increases on every SHOT, so
          incoming damage always produces a rebuild regardless of whose
          turn it was.
        """
        return (
            read_slot(state, "battleship", "fleets", self.user_id),
            read_slot(state, "battleship", "phase"),
            read_slot(state, "battleship", "shots_fired", default=0),
        )

    def __init__(self, *, parent_view: "BattleshipView", **kwargs):
        super().__init__(**kwargs)
        self.parent_view = parent_view
        self.build_ui()

    def _own_ships(self) -> dict[str, list[int]]:
        return (
            self.parent_view.ships_1
            if self.user_id == self.parent_view.player_1
            else self.parent_view.ships_2
        )

    def _own_sunk(self) -> set[str]:
        return (
            self.parent_view.sunk_by_2
            if self.user_id == self.parent_view.player_1
            else self.parent_view.sunk_by_1
        )

    def _own_defense_grid(self) -> EmojiGrid:
        """Return the defense grid for this player."""
        return (
            self.parent_view._defense_1
            if self.user_id == self.parent_view.player_1
            else self.parent_view._defense_2
        )

    def build_ui(self):
        self.clear_items()
        ships = self._own_ships()
        sunk = self._own_sunk()
        defense = self._own_defense_grid()
        fleet = _ship_status_line(ships=ships, sunk_ships=sunk)

        in_setup = self.parent_view.phase == "setup"
        title = "## \U0001f6a2 My Fleet (Setup)" if in_setup else "## \U0001f6a2 My Fleet"
        prompt = (
            "Generate a new board layout, or close and hit **Ready**."
            if in_setup
            else f"{SHIP_HIT} Hit \u2022 {SHIP_SUNK} Sunk \u2022 "
            f"{WATER_MISS} Miss \u2022 {WATER_EMPTY} Empty"
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
            buttons.append(
                StatefulButton(
                    label="Regenerate",
                    style=discord.ButtonStyle.primary,
                    emoji="\U0001f3b2",
                    callback=self._reroll,
                )
            )
        buttons.append(
            StatefulButton(
                label="Close",
                style=discord.ButtonStyle.secondary,
                emoji="\u274c",
                callback=self._close,
            )
        )
        self.add_item(ActionRow(*buttons))

    async def _reroll(self, interaction: discord.Interaction):
        """Re-randomize this player's fleet and broadcast to the public card.

        The dispatch carries the new placement as payload; the REROLL
        reducer writes it into ``state["application"]["battleship"]
        ["fleets"][player_id]``. The parent's ``ships_1`` / ``ships_2``
        properties then read the new placement directly from state, and
        the opponent's MyShipsView short-circuits via its
        ``state_selector`` because its own fleet slice didn't change.
        """
        # Phase guard: re-roll is meaningless once the game has started or
        # the parent has been torn down. No awaits between this guard and
        # the defense-grid refresh below, so the window is closed.
        if self.parent_view.is_finished() or self.parent_view.phase != "setup":
            return

        new_ships = _place_ships(BOARD_SIZE, SHIPS)
        defense = self._own_defense_grid()
        defense.clear()
        for name, cells in new_ships.items():
            defense[cells] = SHIP_COLORS.get(name, "\u2b1c")

        self.parent_view._ready.discard(self.user_id)

        await self.dispatch(
            "BATTLESHIP_REROLL",
            {"player_id": self.user_id, "ships": new_ships},
        )

    async def _close(self, interaction: discord.Interaction):
        await self.exit()


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

        # Pre-check: reject the command if the challenger already has an
        # active game, before the opponent ever sees a challenge prompt.
        if not BattleshipView.check_instance_available(
            user_id=context.author.id,
            guild_id=context.guild.id,
        ):
            await context.send(
                "You're already in a game. Finish or exit it first.",
                ephemeral=True,
            )
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
        """Display server-wide totals and the top 3 players by wins."""
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
            leaderboard_top_n = 10

            def format_stats(self, user_id, stats):
                wins = stats.get("wins", 0)
                games = stats.get("games", 0)
                forfeits = stats.get("forfeits", 0)
                win_rate = (wins / games * 100) if games else 0.0
                return f"{wins}W / {games}G \u2022 {win_rate:.0f}% \u2022 {forfeits}F"

            def build_summary(self, entries):
                # Each game contributes to two player rows
                unique_games = sum(e[1].get("games", 0) for e in entries) // 2
                total_forfeits = sum(e[1].get("forfeits", 0) for e in entries)
                return {
                    "Games played": str(unique_games),
                    "Forfeits": str(total_forfeits),
                    "Players": str(len(entries)),
                }

        view = _BattleshipLeaderboard(
            context=context,
            entries=entries,
            title=f"Battleship Leaderboard -- {context.guild.name}",
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
    await bot.add_cog(BattleshipExample(bot=bot))
