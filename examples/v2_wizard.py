"""
V2 Wizard — CascadeUI V2 Multi-Step Flow
==========================================

A server setup wizard demonstrating WizardLayoutView, CascadeUI's V2
multi-step pattern. Each step has a builder function returning V2
components. Steps can have validators that gate progression.

V2 advantages shown here:

    - Step content as V2 component trees (Container, TextDisplay, etc.)
    - Step indicators and back/next/finish navigation
    - Per-step validation with error feedback
    - V2 convenience helpers (card, key_value, divider)
    - Accent colors that change per step for visual distinction

Commands:
    /v2wizard   Start the setup wizard

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import logging

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    StatefulButton,
    StatefulSelect,
    WizardLayoutView,
    card,
    divider,
    key_value,
)

logger = logging.getLogger(__name__)


# // ========================================( View )======================================== // #


class SetupWizardView(WizardLayoutView):
    """A 4-step server setup wizard.


    Steps:
        1. Welcome       — Introduction, no validation
        2. Moderation    — Select a moderation level
        3. Notifications — Toggle notification channels
        4. Confirmation  — Review selections and finish
    """

    session_limit = 1

    def __init__(self, *args, **kwargs):
        self._mod_level = None
        self._notify_joins = True
        self._notify_leaves = False
        self._notify_bans = True

        steps = [
            {"name": "Welcome", "builder": self.build_welcome},
            {
                "name": "Moderation",
                "builder": self.build_moderation,
                "validator": self.validate_moderation,
            },
            {"name": "Notifications", "builder": self.build_notifications},
            {"name": "Confirm", "builder": self.build_confirm},
        ]

        super().__init__(*args, steps=steps, on_finish=self.finish, **kwargs)

    # // ==================( Step 1: Welcome )================== // #

    async def build_welcome(self):
        return [
            card(
                "## Server Setup",
                TextDisplay(
                    "Welcome to the setup wizard! This will walk you through "
                    "configuring your server's moderation and notification settings.\n\n"
                    "Use **Next** to proceed through each step. You can go **Back** "
                    "at any time to change your selections."
                ),
                color=discord.Color.blurple(),
            ),
        ]

    # // ==================( Step 2: Moderation )================== // #

    async def build_moderation(self):
        select = StatefulSelect(
            placeholder="Choose moderation level...",
            options=[
                discord.SelectOption(
                    label="Relaxed",
                    value="relaxed",
                    description="Minimal auto-moderation",
                ),
                discord.SelectOption(
                    label="Standard",
                    value="standard",
                    description="Filter spam and slurs",
                ),
                discord.SelectOption(
                    label="Strict",
                    value="strict",
                    description="Aggressive filtering with slow mode",
                ),
            ],
            callback=self._on_mod_select,
        )

        current = f"Current: **{self._mod_level or 'Not selected'}**"

        return [
            card(
                "## Moderation Level",
                TextDisplay("Choose how aggressively the bot should moderate chat.\n\n" + current),
                color=discord.Color.orange(),
            ),
            ActionRow(select),
        ]

    async def _on_mod_select(self, interaction):
        self._mod_level = interaction.data["values"][0]
        await interaction.response.defer()
        await self._refresh_wizard()

    async def validate_moderation(self):
        if not self._mod_level:
            return False, "Please select a moderation level before continuing."
        return True, ""

    # // ==================( Step 3: Notifications )================== // #

    async def build_notifications(self):
        def toggle_btn(label, active, callback):
            emoji = "\u2705" if active else "\u274c"
            style = discord.ButtonStyle.success if active else discord.ButtonStyle.secondary
            return StatefulButton(label=f"{emoji} {label}", style=style, callback=callback)

        return [
            card(
                "## Notification Channels",
                TextDisplay(
                    "Choose which events should post notifications.\n"
                    "Toggle each event type below."
                ),
                divider(),
                ActionRow(
                    toggle_btn("Joins", self._notify_joins, self._toggle_joins),
                    toggle_btn("Leaves", self._notify_leaves, self._toggle_leaves),
                    toggle_btn("Bans", self._notify_bans, self._toggle_bans),
                ),
                color=discord.Color.green(),
            ),
        ]

    async def _toggle_joins(self, interaction):
        self._notify_joins = not self._notify_joins
        await interaction.response.defer()
        await self._refresh_wizard()

    async def _toggle_leaves(self, interaction):
        self._notify_leaves = not self._notify_leaves
        await interaction.response.defer()
        await self._refresh_wizard()

    async def _toggle_bans(self, interaction):
        self._notify_bans = not self._notify_bans
        await interaction.response.defer()
        await self._refresh_wizard()

    # // ==================( Step 4: Confirmation )================== // #

    async def build_confirm(self):
        notify_items = []
        if self._notify_joins:
            notify_items.append("Joins")
        if self._notify_leaves:
            notify_items.append("Leaves")
        if self._notify_bans:
            notify_items.append("Bans")

        return [
            card(
                "## Review & Confirm",
                key_value(
                    {
                        "Moderation": (self._mod_level or "none").title(),
                        "Notifications": ", ".join(notify_items) if notify_items else "None",
                    }
                ),
                divider(),
                TextDisplay(
                    "Click **Finish** to apply these settings, or go **Back** to make changes."
                ),
                color=discord.Color.purple(),
            ),
        ]

    # // ==================( Finish )================== // #

    async def finish(self, interaction):
        notify_items = []
        if self._notify_joins:
            notify_items.append("Joins")
        if self._notify_leaves:
            notify_items.append("Leaves")
        if self._notify_bans:
            notify_items.append("Bans")

        await interaction.response.send_message(
            f"**Setup complete!**\n"
            f"Moderation: {(self._mod_level or 'none').title()}\n"
            f"Notifications: {', '.join(notify_items) or 'None'}",
            ephemeral=True,
        )
        await self.exit()


# // ========================================( Cog )======================================== // #


class V2WizardExample(commands.Cog, name="v2_wizard_example"):
    """V2 wizard demonstrating multi-step flows with validation."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2wizard",
        description="Start a V2 setup wizard with step-by-step configuration.",
    )
    async def v2wizard(self, context: Context) -> None:
        """Start a server setup wizard using V2 components.

        Four steps: welcome, moderation level, notification toggles,
        and a confirmation review. Each step uses V2 containers with
        distinct accent colors.
        """
        view = SetupWizardView(context=context)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2WizardExample(bot=bot))
