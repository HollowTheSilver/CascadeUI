"""
V2 Dashboard — CascadeUI V2 Layout Showcase
=============================================

A multi-tab dashboard demonstrating V2 layout capabilities that are
impossible with V1 embeds:

    - Multiple containers with different accent colors in one message
    - Sections with button accessories (text + action on the same line)
    - Tabbed navigation via TabLayoutView
    - Separators for visual hierarchy between content blocks
    - State-driven module toggles with live visual feedback
    - Session limiting (one dashboard per user per guild)
    - V2 convenience helpers for concise component assembly

In V1, you get one embed (one color) with buttons separated below.
In V2, every content block can have its own color, its own inline
buttons, and its own visual identity — all in a single message.

Commands:
    /v2dashboard   Open the dashboard

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import logging

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, Container, TextDisplay

from cascadeui import (
    SessionLimitError,
    StatefulButton,
    TabLayoutView,
    action_section,
    card,
    divider,
    gap,
    key_value,
    toggle_section,
)

logger = logging.getLogger(__name__)


# // ========================================( Dashboard )======================================== // #


class DashboardView(TabLayoutView):
    """Multi-tab dashboard showcasing V2 layout features.

    Three tabs demonstrate different V2 component patterns:

        Overview  — Multiple themed containers with section accessories
        Modules   — State-driven toggles with live visual feedback
        About     — Rich text layout with separators and containers
    """

    session_limit = 1
    session_scope = "user_guild"
    session_policy = "replace"

    def __init__(self, *args, **kwargs):
        # Module toggle states (instance-level, not Redux state — keeps
        # this example focused on V2 layout rather than state management)
        self._modules = {
            "Moderation": True,
            "Logging": True,
            "Welcome Messages": False,
            "Auto-Role": False,
            "Leveling": True,
        }

        tabs = {
            "\U0001f4ca Overview": self.build_overview,
            "\U0001f9e9 Modules": self.build_modules,
            "\u2139\ufe0f About": self.build_about,
        }
        super().__init__(*args, tabs=tabs, **kwargs)

    # // ==================( Helpers )================== // #

    def _exit_row(self):
        """Build an exit button ActionRow for the bottom of each tab."""
        btn = StatefulButton(
            label="Close",
            style=discord.ButtonStyle.secondary,
            emoji="\u274c",
            callback=self._close_dashboard,
        )
        return ActionRow(btn)

    async def _close_dashboard(self, interaction):
        await interaction.response.defer()
        await self.exit()

    @property
    def _guild(self):
        """Resolve the guild from whichever context is available."""
        if self.interaction and self.interaction.guild:
            return self.interaction.guild
        if self.context and self.context.guild:
            return self.context.guild
        return None

    # // ==================( Overview Tab )================== // #

    async def build_overview(self):
        """Server stats with action buttons as section accessories.

        Demonstrates:
            - card() helper for titled containers
            - action_section() for text + button on one line
            - key_value() for formatted stats
            - gap() for invisible gaps between containers
        """
        guild = self._guild
        name = guild.name if guild else "Unknown Server"
        members = guild.member_count if guild else "?"
        channels = len(guild.channels) if guild else "?"
        roles = len(guild.roles) if guild else "?"
        enabled = sum(1 for v in self._modules.values() if v)

        # Server stats card — green accent
        stats = card(
            f"## {name}",
            action_section(
                f"**Members:** {members}",
                label="Refresh",
                callback=self._refresh_overview,
                emoji="\U0001f504",
            ),
            divider(),
            key_value(
                {
                    "Channels": channels,
                    "Roles": roles,
                    "Active Modules": f"{enabled}/{len(self._modules)}",
                }
            ),
            color=discord.Color.green(),
        )

        # Quick actions card — blurple accent
        actions = card(
            "## Quick Actions",
            action_section(
                "View and manage active bot modules",
                label="Modules",
                callback=self._go_to_modules,
                style=discord.ButtonStyle.primary,
            ),
            color=discord.Color.blurple(),
        )

        return [stats, gap(), actions, self._exit_row()]

    async def _refresh_overview(self, interaction):
        """Refresh the overview tab to update live stats."""
        await interaction.response.defer()
        await self._refresh_tabs()

    async def _go_to_modules(self, interaction):
        """Switch to the Modules tab from the Overview quick action."""
        await interaction.response.defer()
        await self.switch_tab("\U0001f9e9 Modules")

    # // ==================( Modules Tab )================== // #

    async def build_modules(self):
        """Toggleable module list with visual on/off indicators.

        Demonstrates:
            - toggle_section() for enable/disable rows
            - Dynamic component tree rebuilt on each toggle
        """
        enabled = sum(1 for v in self._modules.values() if v)
        items: list = [TextDisplay(f"## Bot Modules\n{enabled} of {len(self._modules)} enabled")]

        for name, active in self._modules.items():
            emoji = "\u2705" if active else "\u274c"
            items.append(
                toggle_section(
                    f"{emoji} **{name}**",
                    active=active,
                    callback=self._make_toggle(name),
                )
            )

        return [Container(*items, accent_colour=discord.Color.purple()), self._exit_row()]

    def _make_toggle(self, module_name):
        """Create a toggle callback for a specific module."""

        async def callback(interaction):
            await interaction.response.defer()
            self._modules[module_name] = not self._modules[module_name]
            await self._refresh_tabs()

        return callback

    # // ==================( About Tab )================== // #

    async def build_about(self):
        """Rich text layout demonstrating V2 visual features.

        Demonstrates:
            - card() with long-form markdown content
            - gap() for invisible gaps between containers
            - Subheading text via Discord's -# markdown
        """
        info = card(
            "## About This Dashboard",
            TextDisplay(
                "This dashboard demonstrates **V2 Components**, Discord's "
                "layout system for rich, structured messages.\n\n"
                "### What's different from V1?\n"
                "- **Multiple colored containers** in one message\n"
                "- **Sections** with action buttons on the same line as text\n"
                "- **Separators** for clean visual hierarchy\n"
                "- **Buttons inside containers**, not floating below"
            ),
            color=discord.Color.gold(),
        )

        tech = card(
            "## Built With",
            TextDisplay(
                "**CascadeUI** \u2014 Stateful Discord UI framework\n"
                "**discord.py 2.7+** \u2014 V2 component support\n\n"
                "-# TabLayoutView \u2022 Section \u2022 Container \u2022 StatefulButton"
            ),
            color=discord.Color.dark_grey(),
        )

        return [info, gap(), tech, self._exit_row()]

    async def update_from_state(self, state):
        pass


# // ========================================( Cog )======================================== // #


class V2DashboardExample(commands.Cog, name="v2_dashboard_example"):
    """V2 dashboard showcasing multi-container layouts and sections."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2dashboard",
        description="Open a V2 dashboard with tabs, sections, and themed containers.",
    )
    async def v2dashboard(self, context: Context) -> None:
        """Open a multi-tab V2 dashboard.

        Three tabs demonstrate layout patterns that are impossible in V1:
        multiple colored containers, sections with inline action buttons,
        and state-driven toggles — all in one message.
        """
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        try:
            view = DashboardView(context=context)
            await view.send()
        except SessionLimitError:
            await context.send("You already have a dashboard open.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(V2DashboardExample(bot=bot))
