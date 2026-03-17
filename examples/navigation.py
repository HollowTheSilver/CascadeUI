
# // ========================================( Modules )======================================== // #


import discord
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import StatefulView, StatefulButton

import logging

logger = logging.getLogger(__name__)


# // ========================================( Helpers )======================================== // #


async def _navigate_back(view, interaction):
    """Pop the navigation stack and update the message with the previous view."""
    await interaction.response.defer()
    prev_view = await view.pop(interaction)
    if prev_view:
        embed = prev_view.build_embed() if hasattr(prev_view, "build_embed") else None
        kwargs = {"view": prev_view}
        if embed:
            kwargs["embed"] = embed
        msg = await interaction.edit_original_response(**kwargs)
        prev_view._message = msg


# // ========================================( Views )======================================== // #


class MainMenuView(StatefulView):
    """Root navigation view — the entry point of the nav stack."""

    auto_back_button = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulButton(
            label="Settings", style=discord.ButtonStyle.primary,
            emoji="⚙", callback=self.go_settings,
        ))
        self.add_item(StatefulButton(
            label="About", style=discord.ButtonStyle.secondary,
            emoji="ℹ", callback=self.go_about,
        ))
        self.add_exit_button()

    def build_embed(self):
        return discord.Embed(
            title="Main Menu",
            description="Choose a destination. The navigation stack tracks where you've been.",
            color=discord.Color.green(),
        )

    async def go_settings(self, interaction):
        await interaction.response.defer()
        new_view = await self.push(SettingsView, interaction)
        msg = await interaction.edit_original_response(
            embed=new_view.build_embed(), view=new_view,
        )
        new_view._message = msg

    async def go_about(self, interaction):
        await interaction.response.defer()
        new_view = await self.push(AboutView, interaction)
        msg = await interaction.edit_original_response(
            embed=new_view.build_embed(), view=new_view,
        )
        new_view._message = msg

    async def update_from_state(self, state):
        pass


class SettingsView(StatefulView):
    """Settings view with a toggle and deeper navigation."""

    auto_back_button = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dark_mode = False

        self.add_item(StatefulButton(
            label="Toggle Dark Mode", style=discord.ButtonStyle.primary,
            callback=self.toggle_dark,
        ))
        self.add_item(StatefulButton(
            label="Go Deeper", style=discord.ButtonStyle.secondary,
            emoji="⏩", callback=self.go_nested,
        ))
        self.add_item(StatefulButton(
            label="Back", style=discord.ButtonStyle.secondary,
            emoji="◀", row=4, callback=self.go_back,
        ))

    def build_embed(self):
        status = "ON" if self._dark_mode else "OFF"
        return discord.Embed(
            title="Settings",
            description=f"Dark Mode: **{status}**\n\nToggle preferences or go deeper into the stack.",
            color=discord.Color.dark_theme() if self._dark_mode else discord.Color.blue(),
        )

    async def toggle_dark(self, interaction):
        self._dark_mode = not self._dark_mode
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def go_nested(self, interaction):
        await interaction.response.defer()
        new_view = await self.push(NestedView, interaction)
        msg = await interaction.edit_original_response(
            embed=new_view.build_embed(), view=new_view,
        )
        new_view._message = msg

    async def go_back(self, interaction):
        await _navigate_back(self, interaction)

    async def update_from_state(self, state):
        pass


class NestedView(StatefulView):
    """A deeply nested view to test multi-level push/pop."""

    auto_back_button = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulButton(
            label="Back", style=discord.ButtonStyle.secondary,
            emoji="◀", row=4, callback=self.go_back,
        ))

    def build_embed(self):
        return discord.Embed(
            title="Nested View",
            description="You're two levels deep in the navigation stack.\n\nHit **Back** to unwind.",
            color=discord.Color.orange(),
        )

    async def go_back(self, interaction):
        await _navigate_back(self, interaction)

    async def update_from_state(self, state):
        pass


class AboutView(StatefulView):
    """Simple about page pushed from the main menu."""

    auto_back_button = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulButton(
            label="Back", style=discord.ButtonStyle.secondary,
            emoji="◀", row=4, callback=self.go_back,
        ))

    def build_embed(self):
        return discord.Embed(
            title="About",
            description=(
                "**CascadeUI Navigation Demo**\n\n"
                "This view was pushed onto the navigation stack.\n"
                "Hit Back to pop it off and return to the main menu."
            ),
            color=discord.Color.greyple(),
        )

    async def go_back(self, interaction):
        await _navigate_back(self, interaction)

    async def update_from_state(self, state):
        pass


# // ========================================( Cog )======================================== // #


class NavigationExample(commands.Cog, name="navigation_example"):
    """Navigation stack demo with push/pop between views."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="navtest",
        description="Navigation stack demo with push/pop."
    )
    async def navtest(self, context: Context) -> None:
        view = MainMenuView(context=context)
        await view.send(embed=view.build_embed())


async def setup(bot) -> None:
    await bot.add_cog(NavigationExample(bot=bot))
