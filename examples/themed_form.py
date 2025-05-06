
# // ========================================( Modules )======================================== // #


import discord
from discord.ext import commands
from discord.ext.commands import Context
from utilities.logger import AsyncLogger
from typing import (
    List,
    Optional,
    TypeVar,
)

from cascadeui import (
    StatefulView,
    StatefulButton,
    ConfirmationButtons,
    PaginationControls,
    with_loading_state,
    Theme,
    register_theme,
    set_current_theme,
    get_current_theme
)


# \\ Logger \\

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")


# \\ Generics \\

ThemedFormExampleCog = TypeVar('ThemedFormExampleCog', bound='ThemedFormExample')


# Create a custom theme
my_theme = Theme("brand", {
    "primary_color": discord.Color.from_rgb(114, 137, 218),  # Discord blurple
    "secondary_color": discord.Color.from_rgb(153, 170, 181),
    "success_color": discord.Color.from_rgb(67, 181, 129),
    "danger_color": discord.Color.from_rgb(240, 71, 71),
    "header_emoji": "✨",
    "footer_text": "My Bot v1.0"
})

# Register and use theme
register_theme(my_theme)
set_current_theme("brand")


# // ========================================( Views )======================================== // #


class UserProfileView(StatefulView):
    def __init__(self, context):
        super().__init__(context=context)

        # Create save button with loading state
        save_button = StatefulButton(
            label="Save Profile",
            style=discord.ButtonStyle.primary,
            callback=self.save_profile  # Set callback directly here
        )

        # Add loading state to button
        save_button = with_loading_state(save_button)

        # Add confirmation buttons with themed styling
        confirmation = ConfirmationButtons(
            on_confirm=self.confirm_changes,
            on_cancel=self.cancel_changes,
            confirm_label="Confirm",
            cancel_label="Cancel"
        )

        # Add components to the view
        self.add_item(save_button)
        confirmation.add_to_view(self)

        # Create initial embed with theme
        theme = get_current_theme()
        self.embed = discord.Embed(
            title="User Profile",
            description="Edit your profile settings below"
        )
        theme.apply_to_embed(self.embed)

    async def save_profile(self, interaction):
        """Handle save button click with loading state."""
        # Defer the interaction properly
        await interaction.response.defer()

        # Simulate some processing
        import asyncio
        await asyncio.sleep(2)

        # Update the UI now that processing is complete
        await interaction.followup.send("Profile saved successfully!", ephemeral=True)

    async def confirm_changes(self, interaction):
        """Handle confirmation."""
        await interaction.response.send_message("Changes confirmed!", ephemeral=True)

    async def cancel_changes(self, interaction):
        """Handle cancellation."""
        await interaction.response.send_message("Changes cancelled.", ephemeral=True)


# // ========================================( Cog )======================================== // #


class ThemedFormExample(commands.Cog, name="themed_form_example"):
    """
        Example discord extension class.

    """

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="profile",  # Note: must be lowercase
        description="Display an interactive counter example user interface."
    )
    async def profile(self, context: Context) -> None:
        """
        Display a themed form view.

        :param context: The command context.
        """
        view = UserProfileView(context=context)
        await context.send(embed=view.embed, view=view)


async def setup(bot) -> None:
    cog: ThemedFormExampleCog = ThemedFormExample(bot=bot)
    await bot.add_cog(cog)
