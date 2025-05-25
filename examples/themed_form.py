
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
    FormView,
    FormLayout,
    ConfirmationButtons,
    PaginationControls,
    with_loading_state,
    with_confirmation,
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

    @commands.hybrid_command(
        name="themetest",  # Note: must be lowercase
        description="Example CascadeUI"
    )
    async def themetest(self, context):
        """Test theme switching functionality with distinct color palettes."""

        class ThemeSwitcherView(StatefulView):
            def __init__(self, context):
                super().__init__(context=context)

                # Add theme buttons
                self.add_item(StatefulButton(
                    label="Default Theme",
                    style=discord.ButtonStyle.primary,
                    callback=self.set_default_theme
                ))

                self.add_item(StatefulButton(
                    label="Dark Theme",
                    style=discord.ButtonStyle.secondary,
                    callback=self.set_dark_theme
                ))

                self.add_item(StatefulButton(
                    label="Light Theme",
                    style=discord.ButtonStyle.secondary,
                    callback=self.set_light_theme
                ))

                # Create initial embed with current theme
                self.update_embed()

            async def update_embed(self):
                """Update the embed with current theme info."""
                theme = get_current_theme()

                # Create embed with theme colors
                self.embed = discord.Embed(
                    title="Theme Switcher",
                    description=f"Current theme: **{theme.name}**",
                )

                # Apply current theme
                theme.apply_to_embed(self.embed)

                # Choose appropriate emojis based on theme colors
                if theme.name == "default":
                    primary_emoji = "🔵"  # Blue
                    success_emoji = "🟢"  # Green
                    danger_emoji = "🔴"  # Red
                    theme_desc = "Standard Discord-like colors"
                elif theme.name == "dark":
                    # Updated Dark Theme with more distinctive colors
                    primary_emoji = "🟣"  # Purple
                    success_emoji = "🔵"  # Blue/Cyan
                    danger_emoji = "🟠"  # Orange
                    theme_desc = "Rich, darker aesthetic"
                elif theme.name == "light":
                    # Updated Light Theme with brighter colors
                    primary_emoji = "🟡"  # Yellow
                    success_emoji = "🟢"  # Green
                    danger_emoji = "🟠"  # Orange
                    theme_desc = "Bright, softer palette"
                else:
                    # For custom themes, use default emojis
                    primary_emoji = "⚪"  # Generic
                    success_emoji = "⚪"  # Generic
                    danger_emoji = "⚪"  # Generic
                    theme_desc = "Custom theme"

                # Add theme description
                self.embed.add_field(
                    name="Theme Style",
                    value=theme_desc,
                    inline=False
                )

                # Add color indicators with emojis
                self.embed.add_field(
                    name="Primary Color",
                    value=f"{primary_emoji} {theme.name} Primary",
                    inline=True
                )
                self.embed.add_field(
                    name="Success Color",
                    value=f"{success_emoji} {theme.name} Success",
                    inline=True
                )
                self.embed.add_field(
                    name="Danger Color",
                    value=f"{danger_emoji} {theme.name} Danger",
                    inline=True
                )

                # Add hex codes for exact colors
                self.embed.add_field(
                    name="Color Hex Codes",
                    value=f"Primary: #{theme.get_style('primary_color').value:06x}\n" +
                          f"Success: #{theme.get_style('success_color').value:06x}\n" +
                          f"Danger: #{theme.get_style('danger_color').value:06x}",
                    inline=False
                )

                # Add theme features
                self.embed.add_field(
                    name="Theme Features",
                    value=f"Header Emoji: {theme.get_style('header_emoji', 'None')}\n" +
                          f"Footer Text: {theme.get_style('footer_text', 'None')}",
                    inline=False
                )

                # Update message if it exists
                if self.message:
                    await self.message.edit(embed=self.embed, view=self)

            async def set_default_theme(self, interaction):
                await interaction.response.defer()
                set_current_theme("default")
                await self.update_embed()

            async def set_dark_theme(self, interaction):
                await interaction.response.defer()
                set_current_theme("dark")
                await self.update_embed()

            async def set_light_theme(self, interaction):
                await interaction.response.defer()
                set_current_theme("light")
                await self.update_embed()

        # Create and send the view
        view = ThemeSwitcherView(context)
        view.embed = discord.Embed(title="Loading themes...")
        await context.send(embed=view.embed, view=view)

    @commands.hybrid_command(
        name="componenttest",  # Note: must be lowercase
        description="Example CascadeUI"
    )
    async def componenttest(self, context):
        """Test component composition functionality."""

        class ComponentTestView(StatefulView):
            def __init__(self, context):
                super().__init__(context=context)

                # Add confirmation buttons as a composite component
                confirmation = ConfirmationButtons(
                    on_confirm=self.on_confirm,
                    on_cancel=self.on_cancel,
                    confirm_label="Approve",
                    cancel_label="Decline"
                )
                confirmation.add_to_view(self)

                # Add button with loading state
                save_button = StatefulButton(
                    label="Process Data",
                    style=discord.ButtonStyle.primary,
                    callback=self.process_data
                )
                save_button = with_loading_state(save_button)
                self.add_item(save_button)

                # Add button with confirmation wrapper
                delete_button = StatefulButton(
                    label="Delete Items",
                    style=discord.ButtonStyle.danger,
                    callback=self.delete_items
                )
                delete_button = with_confirmation(
                    delete_button,
                    title="Confirm Deletion",
                    message="Are you sure you want to delete these items?"
                )
                self.add_item(delete_button)

                # Create the embed
                self.embed = discord.Embed(
                    title="Component Test",
                    description="Test various composite components and wrappers."
                )
                theme = get_current_theme()
                theme.apply_to_embed(self.embed)

            async def on_confirm(self, interaction):
                await interaction.response.send_message("Action confirmed!", ephemeral=True)

            async def on_cancel(self, interaction):
                await interaction.response.send_message("Action cancelled.", ephemeral=True)

            async def process_data(self, interaction):
                # This will show the loading state due to the wrapper
                await interaction.response.defer()

                # Simulate processing
                import asyncio
                await asyncio.sleep(2)

                await interaction.followup.send("Data processed successfully!", ephemeral=True)

            async def delete_items(self, interaction):
                # This is called after confirmation
                await interaction.response.send_message("Items deleted successfully!", ephemeral=True)

        # Create and send the view
        view = ComponentTestView(context)
        await context.send(embed=view.embed, view=view)

    @commands.hybrid_command(
        name="paginationtest",  # Note: must be lowercase
        description="Example CascadeUI"
    )
    async def paginationtest(self, context):
        """Test pagination controls."""

        class PaginationTestView(StatefulView):
            def __init__(self, context):
                super().__init__(context=context)

                # Sample data
                self.items = [
                    "Apple", "Banana", "Cherry", "Durian", "Elderberry",
                    "Fig", "Grape", "Honeydew", "Imbe", "Jackfruit",
                    "Kiwi", "Lemon", "Mango", "Nectarine", "Orange"
                ]
                self.items_per_page = 5
                self.current_page = 0

                # Calculate total pages
                self.total_pages = (len(self.items) + self.items_per_page - 1) // self.items_per_page

                # Add pagination controls
                pagination = PaginationControls(
                    page_count=self.total_pages,
                    current_page=self.current_page,
                    on_page_change=self.change_page
                )
                pagination.add_to_view(self)

                # Create initial embed
                self.update_embed()

            def update_embed(self):
                # Get items for current page
                start_idx = self.current_page * self.items_per_page
                end_idx = min(start_idx + self.items_per_page, len(self.items))
                current_items = self.items[start_idx:end_idx]

                # Create embed
                self.embed = discord.Embed(
                    title="Fruit List",
                    description=f"Page {self.current_page + 1} of {self.total_pages}"
                )

                # Add items to embed
                for i, item in enumerate(current_items, start=start_idx + 1):
                    self.embed.add_field(
                        name=f"Item {i}",
                        value=item,
                        inline=False
                    )

                # Apply theme
                theme = get_current_theme()
                theme.apply_to_embed(self.embed)

            async def change_page(self, interaction, page_num):
                # Update current page
                self.current_page = page_num

                # Update embed
                self.update_embed()

                # Update message
                await interaction.response.edit_message(embed=self.embed)

        # Create and send the view
        view = PaginationTestView(context)
        await context.send(embed=view.embed, view=view)

    @commands.hybrid_command(
        name="formtest",  # Note: must be lowercase
        description="Example CascadeUI"
    )
    async def formtest(self, context):
        """Test FormView specialized view."""

        # Form fields definition
        fields = [
            {
                "id": "name",
                "type": "string",
                "label": "Your Name",
                "required": True
            },
            {
                "id": "favorite_color",
                "type": "string",
                "label": "Favorite Color",
                "required": False
            },
            {
                "id": "subscribe",
                "type": "boolean",
                "label": "Subscribe to newsletter",
                "default": False
            }
        ]

        # Create FormView (instead of StatefulView + FormLayout)
        view = FormView(
            context=context,
            title="User Profile",
            fields=fields,
            on_submit=lambda interaction, values: interaction.response.send_message(
                f"Form submitted with values: {values}",
                ephemeral=True
            )
        )

        # Apply theme
        theme = get_current_theme()
        embed = discord.Embed(
            title="User Profile Form",
            description="Please fill out the information below."
        )
        theme.apply_to_embed(embed)

        # Send the view
        await context.send(embed=embed, view=view)


async def setup(bot) -> None:
    cog: ThemedFormExampleCog = ThemedFormExample(bot=bot)
    await bot.add_cog(cog)
