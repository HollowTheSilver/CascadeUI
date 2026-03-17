
# // ========================================( Modules )======================================== // #


import asyncio
import discord
from discord.ext import commands
from discord.ext.commands import Context
import logging

from cascadeui import (
    StatefulView,
    StatefulButton,
    FormView,
    ConfirmationButtons,
    PaginationControls,
    with_loading_state,
    with_confirmation,
    Theme,
    register_theme,
    get_theme,
    set_default_theme,
    get_default_theme,
    choices,
    validate_fields,
    min_length,
    max_length,
    regex,
    min_value,
    max_value,
)
from cascadeui.components.inputs import Modal, TextInput


logger = logging.getLogger(__name__)


# Create a custom theme
my_theme = Theme("brand", {
    "primary_color": discord.Color.from_rgb(114, 137, 218),  # Discord blurple
    "secondary_color": discord.Color.from_rgb(153, 170, 181),
    "success_color": discord.Color.from_rgb(67, 181, 129),
    "danger_color": discord.Color.from_rgb(240, 71, 71),
    "header_emoji": "✨",
    "footer_text": "My Bot v1.0"
})

# Register and set as default
register_theme(my_theme)
set_default_theme("brand")


# // ========================================( Views )======================================== // #


class UserProfileView(StatefulView):
    def __init__(self, context):
        super().__init__(context=context)

        # Create save button with loading state
        save_button = StatefulButton(
            label="Save Profile",
            style=discord.ButtonStyle.primary,
            callback=self.save_profile
        )
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

    async def save_profile(self, interaction):
        """Handle save button click with loading state.

        Note: with_loading_state consumes the interaction response to show
        the loading UI. Use followup for any messages.
        """
        await asyncio.sleep(2)
        await interaction.followup.send("Profile saved successfully!", ephemeral=True)

    async def confirm_changes(self, interaction):
        """Handle confirmation."""
        await interaction.response.send_message("Changes confirmed!", ephemeral=True)

    async def cancel_changes(self, interaction):
        """Handle cancellation."""
        await interaction.response.send_message("Changes cancelled.", ephemeral=True)


# // ========================================( Cog )======================================== // #


class ThemedFormExample(commands.Cog, name="themed_form_example"):
    """Example discord extension class."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="profile",
        description="Display a themed profile view."
    )
    async def profile(self, context: Context) -> None:
        """Display a themed form view."""
        view = UserProfileView(context=context)

        embed = discord.Embed(
            title="User Profile",
            description="Edit your profile settings below"
        )
        view.get_theme().apply_to_embed(embed)

        await view.send(embed=embed)

    @commands.hybrid_command(
        name="themetest",
        description="Test theme switching with CascadeUI."
    )
    async def themetest(self, context):
        """Test theme switching functionality with distinct color palettes."""

        class ThemeSwitcherView(StatefulView):
            def __init__(self, ctx):
                super().__init__(context=ctx)

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

            def build_embed(self):
                """Build the embed with the view's current theme."""
                theme = self.get_theme()

                embed = discord.Embed(
                    title="Theme Switcher",
                    description=f"Current theme: **{theme.name}**",
                )
                theme.apply_to_embed(embed)

                theme_info = {
                    "default": ("🔵", "🟢", "🔴", "Standard Discord-like colors"),
                    "dark": ("🟣", "🔵", "🟠", "Rich, darker aesthetic"),
                    "light": ("🟡", "🟢", "🟠", "Bright, softer palette"),
                }
                primary_emoji, success_emoji, danger_emoji, theme_desc = theme_info.get(
                    theme.name, ("⚪", "⚪", "⚪", "Custom theme")
                )

                embed.add_field(name="Theme Style", value=theme_desc, inline=False)
                embed.add_field(name="Primary", value=f"{primary_emoji} {theme.name}", inline=True)
                embed.add_field(name="Success", value=f"{success_emoji} {theme.name}", inline=True)
                embed.add_field(name="Danger", value=f"{danger_emoji} {theme.name}", inline=True)
                embed.add_field(
                    name="Color Hex Codes",
                    value=(
                        f"Primary: #{theme.get_style('primary_color').value:06x}\n"
                        f"Success: #{theme.get_style('success_color').value:06x}\n"
                        f"Danger: #{theme.get_style('danger_color').value:06x}"
                    ),
                    inline=False
                )
                embed.add_field(
                    name="Theme Features",
                    value=(
                        f"Header Emoji: {theme.get_style('header_emoji', 'None')}\n"
                        f"Footer Text: {theme.get_style('footer_text', 'None')}"
                    ),
                    inline=False
                )
                return embed

            async def _switch_theme(self, interaction, theme_name):
                await interaction.response.defer()
                self.theme = get_theme(theme_name)
                if self.message:
                    await self.message.edit(embed=self.build_embed(), view=self)

            async def set_default_theme(self, interaction):
                await self._switch_theme(interaction, "default")

            async def set_dark_theme(self, interaction):
                await self._switch_theme(interaction, "dark")

            async def set_light_theme(self, interaction):
                await self._switch_theme(interaction, "light")

        view = ThemeSwitcherView(context)
        await view.send(embed=view.build_embed())

    @commands.hybrid_command(
        name="componenttest",
        description="Test CascadeUI component wrappers."
    )
    async def componenttest(self, context):
        """Test component composition functionality."""

        class ComponentTestView(StatefulView):
            def __init__(self, ctx):
                super().__init__(context=ctx)

                confirmation = ConfirmationButtons(
                    on_confirm=self.on_confirm,
                    on_cancel=self.on_cancel,
                    confirm_label="Approve",
                    cancel_label="Decline"
                )
                confirmation.add_to_view(self)

                save_button = StatefulButton(
                    label="Process Data",
                    style=discord.ButtonStyle.primary,
                    callback=self.process_data
                )
                save_button = with_loading_state(save_button, loading_label="Processing...")
                self.add_item(save_button)

                delete_button = StatefulButton(
                    label="Delete Items",
                    style=discord.ButtonStyle.danger,
                    callback=self.delete_items
                )
                delete_button = with_confirmation(
                    delete_button,
                    title="Confirm Deletion",
                    message="Are you sure you want to delete these items?",
                    color=discord.Color.red(),
                    confirm_label="Delete",
                    cancel_label="Keep",
                    confirm_style=discord.ButtonStyle.danger,
                    confirmed_message="Items deleted successfully!",
                    cancelled_message="Items kept safe.",
                )
                self.add_item(delete_button)

            async def on_confirm(self, interaction):
                await interaction.response.send_message("Action confirmed!", ephemeral=True)

            async def on_cancel(self, interaction):
                await interaction.response.send_message("Action cancelled.", ephemeral=True)

            async def process_data(self, interaction):
                # with_loading_state consumes response — use followup
                await asyncio.sleep(2)
                await interaction.followup.send("Data processed successfully!", ephemeral=True)

            async def delete_items(self, interaction):
                # confirmed_message handles user feedback — just do the work here
                pass  # e.g. await db.delete_items(interaction.user.id)

        view = ComponentTestView(context)
        embed = discord.Embed(
            title="Component Test",
            description="Test various composite components and wrappers."
        )
        view.get_theme().apply_to_embed(embed)

        await view.send(embed=embed)

    @commands.hybrid_command(
        name="paginationtest",
        description="Test CascadeUI pagination."
    )
    async def paginationtest(self, context):
        """Test pagination controls."""

        class PaginationTestView(StatefulView):
            def __init__(self, ctx):
                super().__init__(context=ctx)

                self.items_list = [
                    "Apple", "Banana", "Cherry", "Durian", "Elderberry",
                    "Fig", "Grape", "Honeydew", "Imbe", "Jackfruit",
                    "Kiwi", "Lemon", "Mango", "Nectarine", "Orange"
                ]
                self.items_per_page = 5
                self.current_page = 0
                self.total_pages = (len(self.items_list) + self.items_per_page - 1) // self.items_per_page

                pagination = PaginationControls(
                    page_count=self.total_pages,
                    current_page=self.current_page,
                    on_page_change=self.change_page
                )
                pagination.add_to_view(self)

            def build_embed(self):
                start_idx = self.current_page * self.items_per_page
                end_idx = min(start_idx + self.items_per_page, len(self.items_list))
                current_items = self.items_list[start_idx:end_idx]

                embed = discord.Embed(
                    title="Fruit List",
                    description=f"Page {self.current_page + 1} of {self.total_pages}"
                )

                for i, item in enumerate(current_items, start=start_idx + 1):
                    embed.add_field(name=f"Item {i}", value=item, inline=False)

                self.get_theme().apply_to_embed(embed)
                return embed

            async def change_page(self, interaction, page_num):
                self.current_page = page_num
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

        view = PaginationTestView(context)
        await view.send(embed=view.build_embed())

    @commands.hybrid_command(
        name="formtest",
        description="Test CascadeUI FormView with validation."
    )
    async def formtest(self, context):
        """Test FormView with field validation.

        Submit without filling required fields to see validation errors.
        """

        fields = [
            {
                "id": "role",
                "type": "select",
                "label": "Preferred Role",
                "required": True,
                "options": [
                    {"label": "Developer", "value": "dev"},
                    {"label": "Designer", "value": "design"},
                    {"label": "Manager", "value": "manager"},
                ],
                "validators": [choices(["dev", "design", "manager"])],
            },
            {
                "id": "subscribe",
                "type": "boolean",
                "label": "Subscribe to newsletter",
                "required": False,
            },
            {
                "id": "terms",
                "type": "boolean",
                "label": "Accept Terms",
                "required": True,
            },
        ]

        async def on_submit(interaction, values):
            await interaction.response.send_message(
                f"Form submitted with values: {values}",
                ephemeral=True,
            )

        view = FormView(
            context=context,
            title="User Preferences",
            fields=fields,
            on_submit=on_submit,
        )

        embed = discord.Embed(
            title="User Preferences Form",
            description="Please fill out the information below.\nFields marked * are required.",
        )
        view.get_theme().apply_to_embed(embed)

        await view.send(embed=embed)

    @commands.hybrid_command(
        name="validatetest",
        description="Test text field validation with a modal."
    )
    async def validatetest(self, context):
        """Test per-field text validation through a Modal dialog."""

        field_defs = [
            {
                "id": "input_username",
                "label": "Username",
                "validators": [
                    min_length(3),
                    max_length(20),
                    regex(r"^[a-zA-Z0-9_]+$", "Alphanumeric and underscores only"),
                ],
            },
            {
                "id": "input_age",
                "label": "Age",
                "validators": [min_value(13), max_value(120)],
            },
        ]

        class ValidateFormView(StatefulView):
            def __init__(self, ctx):
                super().__init__(context=ctx)
                self.add_item(StatefulButton(
                    label="Open Registration Form",
                    style=discord.ButtonStyle.primary,
                    callback=self.open_form,
                ))
                self.add_exit_button()

            async def open_form(self, interaction):
                modal = Modal(
                    title="Registration",
                    inputs=[
                        TextInput(
                            label="Username",
                            placeholder="3-20 chars, alphanumeric",
                            min_length=1,
                            max_length=20,
                        ),
                        TextInput(
                            label="Age",
                            placeholder="Must be 13-120",
                            min_length=1,
                            max_length=3,
                        ),
                    ],
                    callback=self._on_submit,
                    view_id=self.id,
                )
                await interaction.response.send_modal(modal)

            async def _on_submit(self, interaction, values):
                errors = await validate_fields(values, field_defs)

                if errors:
                    embed = discord.Embed(
                        title="Validation Failed",
                        color=discord.Color.red(),
                    )
                    label_map = {"input_username": "Username", "input_age": "Age"}
                    for field_id, field_errors in errors.items():
                        name = label_map.get(field_id, field_id)
                        messages = "\n".join(e.message for e in field_errors)
                        embed.add_field(name=name, value=messages, inline=False)
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    username = values.get("input_username", "?")
                    age = values.get("input_age", "?")
                    await interaction.response.send_message(
                        f"Registration successful! Username: **{username}**, Age: **{age}**",
                        ephemeral=True,
                    )

            async def update_from_state(self, state):
                pass

        view = ValidateFormView(context)
        embed = discord.Embed(
            title="Validation Test",
            description="Click the button below to open a form with field validation.",
        )
        view.get_theme().apply_to_embed(embed)
        await view.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(ThemedFormExample(bot=bot))
