
# // ========================================( Modules )======================================== // #


from typing import List, Dict, Any, Optional, Union

import discord
import asyncio
from discord import Interaction, Embed
from discord.ext.commands import Context

from .base import StatefulView
from ..components.base import StatefulButton, StatefulSelect


# // ========================================( Classes )======================================== // #


class FormView(StatefulView):
    """A view for collecting form data from users."""
    
    def __init__(self, *args, title="Form", fields=None, on_submit=None, **kwargs):
        super().__init__(*args, **kwargs)
        
        self.title = title
        self.fields = fields or []
        self.on_submit = on_submit
        self.values = {}
        
        # Create form controls
        asyncio.create_task(self._create_form_controls())
    
    async def _create_form_controls(self):
        """Create form controls based on field definitions."""
        for i, field in enumerate(self.fields):
            field_type = field.get("type", "string")
            field_id = field.get("id")
            field_label = field.get("label", field_id)
            field_required = field.get("required", False)
            
            if field_type == "select":
                options = [
                    discord.SelectOption(label=opt.get("label"), value=opt.get("value"), description=opt.get("description"))
                    for opt in field.get("options", [])
                ]
                
                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=1,
                    custom_id=f"form_{field_id}"
                )
                
                async def create_select_callback(field_id):
                    async def callback(interaction):
                        # Store value
                        self.values[field_id] = select.values[0]
                        await interaction.response.defer()
                        
                        # Update form display
                        await self._update_form_display()
                    return callback
                
                select.callback = await create_select_callback(field_id)
                self.add_item(select)
            
            elif field_type == "boolean":
                row = i % 5  # Up to 5 buttons per row
                
                yes_button = StatefulButton(
                    label="Yes",
                    style=discord.ButtonStyle.success,
                    custom_id=f"form_{field_id}_yes",
                    row=row
                )
                
                no_button = StatefulButton(
                    label="No",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"form_{field_id}_no",
                    row=row
                )
                
                async def create_bool_callback(field_id, value):
                    async def callback(interaction):
                        # Store value
                        self.values[field_id] = value
                        await interaction.response.defer()
                        
                        # Update form display
                        await self._update_form_display()
                    return callback
                
                yes_button.callback = await create_bool_callback(field_id, True)
                no_button.callback = await create_bool_callback(field_id, False)
                
                self.add_item(yes_button)
                self.add_item(no_button)
        
        # Add submit button
        submit_button = StatefulButton(
            label="Submit",
            style=discord.ButtonStyle.primary,
            custom_id="form_submit",
            row=len(self.fields) % 5
        )
        
        async def submit_callback(interaction):
            # Validate form
            valid, message = self._validate_form()
            
            if valid:
                # Call submit handler if provided
                if self.on_submit:
                    await self.on_submit(interaction, self.values)
                else:
                    # Default behavior: show values
                    await interaction.response.send_message(
                        f"Form submitted with values: {self.values}",
                        ephemeral=True
                    )
            else:
                # Show validation error
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
        # Create embed with form status
        embed = discord.Embed(title=self.title)
        
        for field in self.fields:
            field_id = field.get("id")
            field_label = field.get("label", field_id)
            field_value = self.values.get(field_id, "Not set")
            
            if field.get("type") == "boolean":
                field_value = "Yes" if field_value is True else "No" if field_value is False else "Not set"
            
            embed.add_field(
                name=f"{field_label} {'*' if field.get('required', False) else ''}",
                value=field_value,
                inline=False
            )
        
        # Update message
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
        # Previous button
        prev_button = StatefulButton(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            custom_id="paginated_prev",
            disabled=True  # Disabled on first page
        )
        
        async def prev_callback(interaction):
            self.current_page = max(0, self.current_page - 1)
            await interaction.response.defer()
            await self._update_page()
        
        prev_button.callback = prev_callback
        self.add_item(prev_button)
        
        # Page indicator (non-interactive)
        page_indicator = discord.ui.Button(
            label=f"Page 1/{len(self.pages)}",
            style=discord.ButtonStyle.gray,
            custom_id="paginated_indicator",
            disabled=True
        )
        self.add_item(page_indicator)
        
        # Next button
        next_button = StatefulButton(
            label="Next",
            style=discord.ButtonStyle.secondary,
            custom_id="paginated_next",
            disabled=len(self.pages) <= 1  # Disabled if only one page
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
        
        # Get current page content
        page = self.pages[self.current_page]
        
        # Update button states
        for item in self.children:
            if getattr(item, "custom_id", None) == "paginated_prev":
                item.disabled = self.current_page == 0
            elif getattr(item, "custom_id", None) == "paginated_next":
                item.disabled = self.current_page >= len(self.pages) - 1
            elif getattr(item, "custom_id", None) == "paginated_indicator":
                item.label = f"Page {self.current_page + 1}/{len(self.pages)}"
        
        # Update message
        if self.message:
            await self.message.edit(embed=page if isinstance(page, discord.Embed) else None, 
                               content=page if isinstance(page, str) else None,
                               view=self)
    
    async def update_from_state(self, state):
        """Update pagination when state changes."""
        await self._update_page()
