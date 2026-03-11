
# // ========================================( Modules )======================================== // #


from typing import List, Dict, Any, Optional, Union

import discord
from discord import Interaction, Embed

from .base import StatefulView
from ..components.base import StatefulButton, StatefulSelect


# // ========================================( Classes )======================================== // #


class FormView(StatefulView):
    """A view for collecting form data from users.

    Supports field types: "select", "boolean".
    String fields should use a Modal workflow (see TextInput / Modal in components.inputs).
    """

    def __init__(self, *args, title="Form", fields=None, on_submit=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.title = title
        self.fields = fields or []
        self.on_submit = on_submit
        self.values = {}

        # Create form controls synchronously — no race condition
        self._create_form_controls()

    def _create_form_controls(self):
        """Create form controls based on field definitions."""
        for i, field in enumerate(self.fields):
            field_type = field.get("type", "string")
            field_id = field.get("id")
            field_label = field.get("label", field_id)
            field_required = field.get("required", False)

            if field_type == "select":
                options = [
                    discord.SelectOption(
                        label=opt.get("label"),
                        value=opt.get("value"),
                        description=opt.get("description")
                    )
                    for opt in field.get("options", [])
                ]

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=1,
                    custom_id=f"form_{field_id}"
                )

                # Capture field_id and select per-iteration via default args
                def make_select_callback(fid, sel):
                    async def callback(interaction):
                        self.values[fid] = sel.values[0]
                        await interaction.response.defer()
                        await self._update_form_display()
                    return callback

                select.callback = make_select_callback(field_id, select)
                self.add_item(select)

            elif field_type == "boolean":
                row = min(i, 4)  # Action rows 0-4

                yes_button = StatefulButton(
                    label=f"{field_label}: Yes",
                    style=discord.ButtonStyle.success,
                    custom_id=f"form_{field_id}_yes",
                    row=row
                )

                no_button = StatefulButton(
                    label=f"{field_label}: No",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"form_{field_id}_no",
                    row=row
                )

                # Capture field_id per-iteration via default arg
                def make_bool_callback(fid, value):
                    async def callback(interaction):
                        self.values[fid] = value
                        await interaction.response.defer()
                        await self._update_form_display()
                    return callback

                yes_button.callback = make_bool_callback(field_id, True)
                no_button.callback = make_bool_callback(field_id, False)

                self.add_item(yes_button)
                self.add_item(no_button)

            # "string" type is intentionally not supported inline — Discord does not
            # allow text input inside Views, only inside Modals. Use a Modal workflow
            # or the components.inputs.Modal class for string collection.

        # Add submit button
        submit_button = StatefulButton(
            label="Submit",
            style=discord.ButtonStyle.primary,
            custom_id="form_submit",
            row=min(len(self.fields), 4)
        )

        async def submit_callback(interaction):
            valid, message = self._validate_form()

            if valid:
                if self.on_submit:
                    await self.on_submit(interaction, self.values)
                else:
                    await interaction.response.send_message(
                        f"Form submitted with values: {self.values}",
                        ephemeral=True
                    )
            else:
                await interaction.response.send_message(
                    f"Please complete all required fields: {message}",
                    ephemeral=True
                )

        submit_button.callback = submit_callback
        self.add_item(submit_button)

    def _validate_form(self):
        """Validate form data."""
        missing_fields = []

        for field in self.fields:
            if field.get("required", False):
                field_id = field.get("id")
                if field_id not in self.values or self.values[field_id] is None:
                    missing_fields.append(field.get("label", field_id))

        if missing_fields:
            return False, ", ".join(missing_fields)
        return True, ""

    async def _update_form_display(self):
        """Update the form display with current values."""
        embed = discord.Embed(title=self.title)

        for field in self.fields:
            field_id = field.get("id")
            field_label = field.get("label", field_id)
            field_value = self.values.get(field_id, "Not set")

            if field.get("type") == "boolean":
                field_value = "Yes" if field_value is True else "No" if field_value is False else "Not set"

            embed.add_field(
                name=f"{field_label} {'*' if field.get('required', False) else ''}",
                value=str(field_value),
                inline=False
            )

        if self.message:
            await self.message.edit(embed=embed, view=self)

    async def update_from_state(self, state):
        """Update form display when state changes."""
        await self._update_form_display()


class PaginatedView(StatefulView):
    """A view for paginated content."""

    def __init__(self, *args, pages=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.pages = pages or []
        self.current_page = 0

        # Add navigation buttons
        self._add_navigation_buttons()

    def _add_navigation_buttons(self):
        """Add navigation buttons to the view."""
        prev_button = StatefulButton(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            custom_id="paginated_prev",
            disabled=True
        )

        async def prev_callback(interaction):
            self.current_page = max(0, self.current_page - 1)
            await interaction.response.defer()
            await self._update_page()

        prev_button.callback = prev_callback
        self.add_item(prev_button)

        page_indicator = discord.ui.Button(
            label=f"Page 1/{max(len(self.pages), 1)}",
            style=discord.ButtonStyle.gray,
            custom_id="paginated_indicator",
            disabled=True
        )
        self.add_item(page_indicator)

        next_button = StatefulButton(
            label="Next",
            style=discord.ButtonStyle.secondary,
            custom_id="paginated_next",
            disabled=len(self.pages) <= 1
        )

        async def next_callback(interaction):
            self.current_page = min(len(self.pages) - 1, self.current_page + 1)
            await interaction.response.defer()
            await self._update_page()

        next_button.callback = next_callback
        self.add_item(next_button)

    async def _update_page(self):
        """Update the current page display."""
        if not self.pages:
            return

        page = self.pages[self.current_page]

        # Update button states
        for item in self.children:
            if getattr(item, "custom_id", None) == "paginated_prev":
                item.disabled = self.current_page == 0
            elif getattr(item, "custom_id", None) == "paginated_next":
                item.disabled = self.current_page >= len(self.pages) - 1
            elif getattr(item, "custom_id", None) == "paginated_indicator":
                item.label = f"Page {self.current_page + 1}/{len(self.pages)}"

        if self.message:
            await self.message.edit(
                embed=page if isinstance(page, discord.Embed) else None,
                content=page if isinstance(page, str) else None,
                view=self
            )

    async def update_from_state(self, state):
        """Update pagination when state changes."""
        await self._update_page()
