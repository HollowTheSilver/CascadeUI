"""
V2 Form — CascadeUI V2 Form & Validation
==========================================

Demonstrates FormLayoutView with text input via Modal, field validation,
and all supported field types. A registration form that collects:

    - Username          (text, via Modal — min/max length, alphanumeric regex)
    - Email             (text, via Modal — regex validation)
    - Role              (select dropdown with choices validator)
    - Experience level  (select dropdown)
    - Accept Terms      (boolean toggle, required)

V2 advantages shown here:

    - Container-based display with card(), key_value(), divider()
    - Overridden _rebuild_display() for custom form presentation
    - Select fields and boolean toggles built from field definitions
    - Modal integration for text input (string fields)
    - Per-field validation via CascadeUI's validator system
    - Validation errors shown as ephemeral embeds on submit

Commands:
    /v2form   Open the registration form

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
    FormLayoutView,
    Modal,
    StatefulButton,
    TextInput,
    card,
    choices,
    divider,
    key_value,
    max_length,
    min_length,
    regex,
)

logger = logging.getLogger(__name__)


# // ========================================( View )======================================== // #


class RegistrationFormView(FormLayoutView):
    """A registration form with text input, dropdowns, toggles, and validation.

    Shows how FormLayoutView handles select/boolean fields automatically,
    and how to add text input via a Modal button alongside the form controls.
    Overrides _rebuild_display() to use card(), key_value(), and divider()
    for a richer V2 presentation. Validators run on submit.
    """

    session_limit = 1

    def __init__(self, *args, **kwargs):
        fields = [
            {
                "id": "role",
                "label": "Role",
                "type": "select",
                "required": True,
                "options": [
                    {"label": "Developer", "value": "developer", "description": "Write code"},
                    {"label": "Designer", "value": "designer", "description": "Create visuals"},
                    {"label": "Manager", "value": "manager", "description": "Lead projects"},
                    {"label": "Community", "value": "community", "description": "Grow community"},
                ],
                "placeholder": "What's your role?",
                "validators": [choices(["developer", "designer", "manager", "community"])],
            },
            {
                "id": "experience",
                "label": "Experience",
                "type": "select",
                "required": True,
                "options": [
                    {"label": "Beginner (< 1 year)", "value": "beginner"},
                    {"label": "Intermediate (1-3 years)", "value": "intermediate"},
                    {"label": "Advanced (3+ years)", "value": "advanced"},
                ],
                "placeholder": "Your experience level...",
            },
            {
                "id": "terms",
                "label": "Accept Terms of Service",
                "type": "boolean",
                "required": True,
            },
        ]

        super().__init__(
            *args,
            title="Registration Form",
            fields=fields,
            on_submit=self._handle_submit,
            **kwargs,
        )

        # Text field validators run inside the Modal callback, but we also
        # check for missing text fields on form submit
        self._text_validators = {
            "username": [
                min_length(3),
                max_length(20),
                regex(r"^[a-zA-Z0-9_]+$", "Alphanumeric and underscores only"),
            ],
            "email": [
                min_length(5),
                max_length(100),
                regex(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", "Must be a valid email address"),
            ],
        }

    def _create_form_controls(self):
        """Add a text input button before the standard select/boolean controls."""
        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Fill in Username & Email",
                    style=discord.ButtonStyle.secondary,
                    emoji="\u270f\ufe0f",
                    callback=self._open_text_modal,
                )
            )
        )

        # Standard select and boolean controls from FormLayoutView
        super()._create_form_controls()

    def _rebuild_display(self):
        """Override the default display with card(), key_value(), and divider().

        Demonstrates how to customize FormLayoutView's presentation using
        V2 convenience helpers while preserving the form's interactive controls.
        """
        bool_icon = lambda v: "\u2705" if v is True else "\u274c" if v is False else "\u2796"
        v = self.values

        # Build the status display
        field_data = {
            "\U0001f464 Username": v.get("username", "-"),
            "\U0001f4e7 Email": v.get("email", "-"),
            "\U0001f3af Role": v.get("role", "-").title() if v.get("role") else "-",
            "\U0001f4ca Experience": v.get("experience", "-").title() if v.get("experience") else "-",
        }

        terms_status = bool_icon(v.get("terms"))
        filled = sum(1 for val in [v.get("username"), v.get("role"), v.get("experience")] if val)
        terms_accepted = v.get("terms") is True

        # Snapshot interactive ActionRows before clearing
        action_rows = [c for c in self.children if isinstance(c, ActionRow)]
        self.clear_items()

        # Status card with field values
        self.add_item(
            card(
                "## \U0001f4cb Registration Form",
                key_value(field_data),
                divider(),
                TextDisplay(f"Terms of Service: {terms_status}"),
                TextDisplay(f"-# {filled}/3 fields completed | Terms: {'accepted' if terms_accepted else 'pending'}"),
                color=discord.Color.green() if (filled == 3 and terms_accepted) else discord.Color.og_blurple(),
            )
        )

        # Re-add interactive ActionRows
        for row in action_rows:
            self.add_item(row)

    async def _open_text_modal(self, interaction):
        """Open a Modal to collect text fields with validation."""
        modal = Modal(
            title="Registration Details",
            inputs=[
                TextInput(
                    label="Username",
                    placeholder="3-20 chars, alphanumeric and underscores",
                    min_length=3,
                    max_length=20,
                    default=self.values.get("username", ""),
                ),
                TextInput(
                    label="Email",
                    placeholder="you@example.com",
                    min_length=5,
                    max_length=100,
                    default=self.values.get("email", ""),
                ),
            ],
            callback=self._on_text_submitted,
            view_id=self.id,
            validators={
                "input_username": self._text_validators["username"],
                "input_email": self._text_validators["email"],
            },
        )
        await interaction.response.send_modal(modal)

    async def _on_text_submitted(self, interaction, modal_values):
        """Handle validated text input from the Modal."""
        self.values["username"] = modal_values.get("input_username", "")
        self.values["email"] = modal_values.get("input_email", "")

        await interaction.response.defer()
        await self._update_form_display()

    async def _handle_submit(self, interaction, values):
        """Handle the completed form submission.

        Checks that text fields were filled (Modal validates format,
        but the user might not have opened the modal at all).
        """
        missing = []
        if not values.get("username"):
            missing.append("Username")
        if not values.get("email"):
            missing.append("Email")
        if not values.get("terms"):
            missing.append("Accept Terms of Service")

        if missing:
            embed = discord.Embed(
                title="Missing Required Fields",
                description="\n".join(f"- **{f}**" for f in missing),
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="\u2705 Registration Complete!",
            color=discord.Color.green(),
        )
        embed.add_field(name="Username", value=values["username"], inline=True)
        embed.add_field(name="Email", value=values["email"], inline=True)
        embed.add_field(name="Role", value=values["role"].title(), inline=True)
        embed.add_field(name="Experience", value=values["experience"].title(), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self.exit()


# // ========================================( Cog )======================================== // #


class V2FormExample(commands.Cog, name="v2_form_example"):
    """V2 registration form with text input, dropdowns, and validation."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2form",
        description="Open a V2 registration form with text input and validation.",
    )
    async def v2form(self, context: Context) -> None:
        """Open a registration form using V2 components.

        Fill in text fields via a Modal dialog, select your role and
        experience, and accept the terms. Validation runs on each
        step and again on submit.
        """
        view = RegistrationFormView(context=context)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2FormExample(bot=bot))
