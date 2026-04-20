"""
V2 Grids -- CascadeUI Dynamic Grid Display Showcase
===================================================

A display-only showcase for the grid helpers:

    - emoji_grid()  -- live TextDisplay subclass with mutable cells,
                      optional axis labels, and bulk assignment
    - button_grid() -- (row, col) -> Button factory packed into ActionRows,
                      enforcing Discord's 5x5 component cap

This cog demonstrates that grids are a standalone primitive: they can
be assembled from slash-command arguments and dropped into any V2 card
without surrounding game logic. The buttons produced by /button_grid
are disabled on purpose -- the showcase only exercises the rendering
path, not interaction handling. For interactive grid usage, see
`v2_battleship.py` and `v2_tictactoe.py`.

Commands:
    /emoji_grid    Render an EmojiGrid from slash arguments
    /button_grid   Render a disabled button grid from slash arguments
    /grid_gallery  Fire eight preset variations across three messages

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import TextDisplay

from cascadeui import (
    StatefulLayoutView,
    button_grid,
    card,
    divider,
    emoji_grid,
    gap,
)

import logging

logger = logging.getLogger(__name__)


# // ========================================( Showcase View )======================================== // #


class GridShowcaseView(StatefulLayoutView):
    """Display-only host for a pre-built card of grid components.

    The view owns no state and subscribes to no actions -- its only job
    is to ferry a ready-made component list into a V2 message. Every
    policy attribute is declared explicitly so the full policy surface
    is visible in the class body.
    """

    owner_only = True
    # ``instance_limit = None`` leaves the showcase unthrottled so a
    # recording can stack multiple grids in one channel. ``instance_policy``
    # and ``replace_policy`` are irrelevant while ``instance_limit`` is None
    # -- the replacement path never fires -- but they are declared so the
    # policy surface reads completely at a glance.
    instance_limit = None
    instance_scope = "user_guild"
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "disable"
    timeout = 180.0

    def __init__(self, *args, items=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Components are populated at construction time and never mutate
        # afterwards, so no ``build_ui`` is needed -- the default
        # ``on_state_changed`` no-op is the correct rebuild contract for
        # a static display view.
        for item in items or ():
            self.add_item(item)


# // ========================================( Helpers )======================================== // #


AxisPreset = Literal["alpha", "numeric", "none"]


def _resolve_preset(value: AxisPreset) -> Optional[str]:
    """Map the slash-command enum to the emoji_grid() argument."""
    return None if value == "none" else value


async def _send_showcase(context: Context, items) -> None:
    """Build a GridShowcaseView around the given items and send it."""
    view = GridShowcaseView(context=context, items=items)
    await view.send()


# // ========================================( Cog )======================================== // #


class V2GridsExample(commands.Cog, name="v2_grids_example"):
    """Dynamic grid display showcase."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # // ==================( /emoji_grid )================== // #

    @commands.hybrid_command(
        name="emoji_grid",
        description="Render a dynamic EmojiGrid from slash arguments.",
    )
    @app_commands.describe(
        rows="Number of rows (1-20 for readability).",
        cols="Number of columns (1-20 for readability).",
        fill="Fill emoji for empty cells.",
        row_labels="Row label preset, or 'none' to omit.",
        col_labels="Column label preset, or 'none' to omit.",
    )
    async def emoji_grid_cmd(
        self,
        context: Context,
        rows: int,
        cols: int,
        fill: str = "\u2b1b",
        row_labels: AxisPreset = "none",
        col_labels: AxisPreset = "none",
    ) -> None:
        """Render a standalone EmojiGrid from user-supplied dimensions.

        The grid is built with a couple of demo overlays (a star in the
        top-left and a filled rectangle in the interior) so the recording
        shows off mutable cells and fill_rect() without needing a game.
        """
        try:
            grid = emoji_grid(
                rows,
                cols,
                fill=fill,
                row_labels=_resolve_preset(row_labels),
                col_labels=_resolve_preset(col_labels),
            )
        except (ValueError, TypeError) as exc:
            # The library validates at construction -- surface the error
            # message verbatim so the recording can showcase the guardrails.
            await context.send(f"\u26a0\ufe0f `{exc}`", ephemeral=True)
            return

        # Demo overlays: a star in the top-left and a 2x2 accent block
        # anchored near the center. Both are safe for any grid >= 3x3.
        grid[(0, 0)] = "\u2b50"
        if rows >= 3 and cols >= 3:
            mid_r, mid_c = rows // 2, cols // 2
            grid.fill_rect(
                (max(0, mid_r - 1), max(0, mid_c - 1)),
                (min(rows - 1, mid_r), min(cols - 1, mid_c)),
                "\U0001f7e6",
            )

        showcase = card(
            f"## EmojiGrid `{rows}x{cols}`",
            TextDisplay(
                f"-# fill=`{fill}` \u2022 row_labels=`{row_labels}` "
                f"\u2022 col_labels=`{col_labels}`"
            ),
            divider(),
            grid,
            color=discord.Color.blurple(),
        )
        await _send_showcase(context, [showcase])

    # // ==================( /button_grid )================== // #

    @commands.hybrid_command(
        name="button_grid",
        description="Render a disabled button grid from slash arguments.",
    )
    @app_commands.describe(
        rows="Number of rows (1-5, Discord cap).",
        cols="Number of columns (1-5, Discord cap).",
        label_style="How to label each cell: coords (A1), numeric (0,1,2...), or blank.",
    )
    async def button_grid_cmd(
        self,
        context: Context,
        rows: int,
        cols: int,
        label_style: Literal["coords", "numeric", "blank"] = "coords",
    ) -> None:
        """Render an inert button grid from user-supplied dimensions.

        Every button is disabled with no callback -- the showcase is
        purely visual. Interactive button grids belong in gameplay
        examples like TicTacToe.
        """

        def cell_factory(row: int, col: int) -> discord.ui.Button:
            if label_style == "coords":
                label = f"{chr(ord('A') + row)}{col + 1}"
            elif label_style == "numeric":
                label = str(row * cols + col)
            else:
                label = "\u00b7"
            # Non-interactive indicators use discord.ui.Button directly.
            # StatefulButton is for interactive buttons with callbacks.
            return discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                disabled=True,
            )

        try:
            action_rows = button_grid(rows, cols, cell_factory)
        except (ValueError, TypeError) as exc:
            await context.send(f"\u26a0\ufe0f `{exc}`", ephemeral=True)
            return

        header = card(
            f"## button_grid `{rows}x{cols}`",
            TextDisplay(f"-# label_style=`{label_style}` \u2022 buttons are display-only"),
            color=discord.Color.green(),
        )
        await _send_showcase(context, [header, gap(), *action_rows])

    # // ==================( /grid_gallery )================== // #

    @commands.hybrid_command(
        name="grid_gallery",
        description="Render eight grid variations across three showcase messages.",
    )
    async def grid_gallery_cmd(self, context: Context) -> None:
        """Render a full variation set across three showcase messages.

        Covers the feature surface in one shot for gif capture:

            1. Blank square -- raw fill, no labels, no overlays.
            2. Wide rectangle -- 3x8 with numeric column headers only.
            3. Custom 0-indexed axis -- any Sequence[str] works as labels.
            4. Tall rectangle -- 6x3 with alpha row labels only.
            5. Fully labeled square -- alpha + numeric, custom corner.
            6. Bulk-assigned pattern -- iterable-of-keys assignment.
            7. 2x5 inert button strip -- rectangular button_grid.
            8. 5x5 inert button grid -- max component density.

        Split across three sends because Discord's 40-component View cap
        counts every nested container child recursively, and a 5x5
        button grid alone contributes 30 components.
        """
        # 1. Blank square: nothing overlaid, shows the raw fill primitive.
        blank = emoji_grid(4, 4, fill="\u2b1c")

        # 2. Wide rectangle with column headers only. fill_rect shows a
        # wide stripe that reads clearly in a non-square aspect ratio.
        wide = emoji_grid(3, 8, fill="\u2b1b", col_labels="numeric")
        wide.fill_rect((1, 0), (1, 7), "\U0001f7e6")

        # 3. Custom 0-indexed column labels -- demonstrates that axis
        # presets are optional; any Sequence[str] of the right length
        # works. Starts at 0 instead of the "numeric" preset's 1, using
        # the keycap-zero glyph that the preset deliberately omits.
        zero_indexed = [f"{d}\ufe0f\u20e3" for d in "0123456789"]
        custom_axis = emoji_grid(3, 10, fill="\u2b1b", col_labels=zero_indexed)
        custom_axis.fill_rect((1, 0), (1, 9), "\U0001f7e6")

        # 4. Tall rectangle with row labels only -- the mirror of #2,
        # exercising the "row labels without column headers" state.
        tall = emoji_grid(6, 3, fill="\u2b1c", row_labels="alpha")
        tall.fill_rect((0, 1), (5, 1), "\U0001f7e9")

        # 5. Fully labeled square with an explicit corner cell -- the
        # four-state axis matrix in its most decorated form.
        full = emoji_grid(
            4,
            4,
            fill="\u2b1b",
            row_labels="alpha",
            col_labels="numeric",
            corner="\U0001f3f4",
        )
        for i in range(4):
            full[(i, i)] = "\u2b50"

        # 6. Bulk iterable assignment -- one statement paints a heart.
        pattern = emoji_grid(5, 5, fill="\u2b1c")
        heart_cells = [
            (0, 1),
            (0, 3),
            (1, 0),
            (1, 1),
            (1, 2),
            (1, 3),
            (1, 4),
            (2, 0),
            (2, 1),
            (2, 2),
            (2, 3),
            (2, 4),
            (3, 1),
            (3, 2),
            (3, 3),
            (4, 2),
        ]
        pattern[heart_cells] = "\u2764\ufe0f"

        # 7. 2x5 inert button strip -- rectangular button_grid form.
        def strip_factory(row: int, col: int) -> discord.ui.Button:
            return discord.ui.Button(
                label=str(row * 5 + col),
                style=discord.ButtonStyle.primary,
                disabled=True,
            )

        strip_rows = button_grid(2, 5, strip_factory)

        # 8. 5x5 coord button grid -- max component density.
        def coord_factory(row: int, col: int) -> discord.ui.Button:
            return discord.ui.Button(
                label=f"{chr(ord('A') + row)}{col + 1}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
            )

        coord_rows = button_grid(5, 5, coord_factory)

        emoji_gallery = card(
            "## Grid Gallery",
            TextDisplay("-# Eight variations rendered from a single command."),
            divider(),
            TextDisplay("**1. Blank 4\u00d74**  `emoji_grid(4, 4, fill=...)`"),
            blank,
            divider(),
            TextDisplay("**2. Wide 3\u00d78**  `col_labels='numeric'` + fill_rect stripe"),
            wide,
            divider(),
            TextDisplay(
                "**3. Custom axis 3\u00d710**  0-indexed `col_labels=[0\ufe0f\u20e3..9\ufe0f\u20e3]` "
                "(any `Sequence[str]` works)"
            ),
            custom_axis,
            divider(),
            TextDisplay("**4. Tall 6\u00d73**  `row_labels='alpha'` + fill_rect column"),
            tall,
            divider(),
            TextDisplay(
                "**5. Labeled 4\u00d74**  `row_labels='alpha'`, "
                "`col_labels='numeric'`, custom corner, diagonal accent"
            ),
            full,
            divider(),
            TextDisplay("**6. Bulk assign 5\u00d75**  iterable-of-keys paints in one statement"),
            pattern,
            color=discord.Color.gold(),
        )

        # Split across three messages: Discord's 40-component View cap
        # counts every nested container child recursively, and a 5x5
        # button grid alone is 30 components. Each send fits comfortably.
        await _send_showcase(context, [emoji_gallery])

        strip_header = card(
            "## 7. button_grid 2\u00d75  (rectangular strip, primary style)",
            color=discord.Color.blurple(),
        )
        await _send_showcase(context, [strip_header, gap(), *strip_rows])

        coord_header = card(
            "## 8. button_grid 5\u00d75  (coord labels, max density)",
            color=discord.Color.green(),
        )
        await _send_showcase(context, [coord_header, gap(), *coord_rows])


async def setup(bot) -> None:
    await bot.add_cog(V2GridsExample(bot=bot))
