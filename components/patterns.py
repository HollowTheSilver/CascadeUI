
# // ========================================( Modules )======================================== // #


from typing import List, Dict, Any, Optional, Callable, Union
import discord
from discord import Interaction, ButtonStyle

from .base import StatefulButton, StatefulComponent
from .composition import CompositeComponent, register_component


# // ========================================( Classes )======================================== // #


class ConfirmationButtons(CompositeComponent):
    """A yes/no button pair for confirmations."""

    def __init__(self,
                 on_confirm: Optional[Callable] = None,
                 on_cancel: Optional[Callable] = None,
                 confirm_label: str = "Yes",
                 cancel_label: str = "No",
                 confirm_style: ButtonStyle = ButtonStyle.success,
                 cancel_style: ButtonStyle = ButtonStyle.danger) -> None:
        super().__init__()

        self.confirm_button = StatefulButton(
            label=confirm_label,
            style=confirm_style,
            callback=on_confirm
        )

        self.cancel_button = StatefulButton(
            label=cancel_label,
            style=cancel_style,
            callback=on_cancel
        )

        self.add_component(self.confirm_button)
        self.add_component(self.cancel_button)


# Register the component
register_component("confirmation_buttons", ConfirmationButtons)


class PaginationControls(CompositeComponent):
    """Navigation controls for paginated content."""

    def __init__(self,
                 page_count: int,
                 current_page: int = 0,
                 on_page_change: Optional[Callable] = None) -> None:
        super().__init__()
        self.page_count = max(1, page_count)
        self.current_page = min(max(0, current_page), self.page_count - 1)
        self.on_page_change = on_page_change

        # Create buttons
        self.prev_button = StatefulButton(
            label="Previous",
            style=ButtonStyle.secondary,
            disabled=self.current_page <= 0,
            callback=self._on_prev
        )

        self.indicator = discord.ui.Button(
            label=f"Page {self.current_page + 1}/{self.page_count}",
            style=ButtonStyle.secondary,
            disabled=True
        )

        self.next_button = StatefulButton(
            label="Next",
            style=ButtonStyle.secondary,
            disabled=self.current_page >= self.page_count - 1,
            callback=self._on_next
        )

        # Add buttons to composite
        self.add_component(self.prev_button)
        self.add_component(self.indicator)
        self.add_component(self.next_button)

    async def _on_prev(self, interaction: Interaction) -> None:
        """Handle previous button click."""
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()

            # Notify callback if provided
            if self.on_page_change:
                await self.on_page_change(interaction, self.current_page)
            else:
                await interaction.response.defer()

    async def _on_next(self, interaction: Interaction) -> None:
        """Handle next button click."""
        if self.current_page < self.page_count - 1:
            self.current_page += 1
            self._update_buttons()

            # Notify callback if provided
            if self.on_page_change:
                await self.on_page_change(interaction, self.current_page)
            else:
                await interaction.response.defer()

    def _update_buttons(self) -> None:
        """Update button states based on current page."""
        self.prev_button.disabled = (self.current_page == 0)
        self.next_button.disabled = (self.current_page == self.page_count - 1)
        self.indicator.label = f"Page {self.current_page + 1}/{self.page_count}"


# Register the component
register_component("pagination_controls", PaginationControls)


class FormLayout(CompositeComponent):
    """Layout for form fields with labels and inputs."""

    def __init__(self, fields: List[Dict[str, Any]], on_submit: Optional[Callable] = None) -> None:
        super().__init__()
        self.fields = fields
        self.field_components = {}

        # Create field components
        for field in fields:
            field_id = field.get("id", "field_" + str(len(self.field_components)))
            field_type = field.get("type", "string")
            field_label = field.get("label", field_id)
            required = field.get("required", False)

            # Create field based on type
            if field_type == "string":
                from .inputs import TextInput
                component = TextInput(
                    label=field_label,
                    required=required,
                    placeholder=field.get("placeholder")
                )
            elif field_type == "boolean":
                from .buttons import ToggleButton
                component = ToggleButton(
                    label=field_label,
                    toggled=field.get("default", False)
                )
            # Add more field types as needed

            self.field_components[field_id] = component
            self.add_component(component)

        # Add submit button
        if on_submit:
            from .buttons import SuccessButton
            submit_button = SuccessButton(label="Submit", callback=on_submit)
            self.add_component(submit_button)


# Register the component
register_component("form_layout", FormLayout)
