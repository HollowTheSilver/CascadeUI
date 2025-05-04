
# // ========================================( Modules )======================================== // #


import asyncio
from typing import Optional, Callable, Dict, Any

import discord
from discord import Interaction, TextStyle

from .base import StatefulComponent
from ..state.actions import ActionCreators


# // ========================================( Classes )======================================== // #


class TextInput(StatefulComponent):
    """A modal text input with state management."""

    def __init__(self, label: str, placeholder: Optional[str] = None,
                 default: Optional[str] = None, required: bool = True,
                 min_length: Optional[int] = None, max_length: Optional[int] = None,
                 style: TextStyle = TextStyle.short):
        self.label = label
        self.placeholder = placeholder
        self.default = default
        self.required = required
        self.min_length = min_length
        self.max_length = max_length
        self.style = style
        self.custom_id = f"input_{label.lower().replace(' ', '_')}"

    def create_discord_component(self):
        """Create the actual Discord UI component."""
        return discord.ui.TextInput(
            label=self.label,
            placeholder=self.placeholder,
            default=self.default,
            required=self.required,
            min_length=self.min_length,
            max_length=self.max_length,
            style=self.style,
            custom_id=self.custom_id
        )


class Modal(discord.ui.Modal, StatefulComponent):
    """A modal dialog with stateful inputs."""

    def __init__(self, title: str, inputs: list, callback: Optional[Callable] = None,
                 timeout: Optional[float] = None, **kwargs):
        super().__init__(title=title, timeout=timeout)

        self.view_id = kwargs.get("view_id")
        self.inputs = {}

        # Add inputs
        for input_item in inputs:
            if isinstance(input_item, TextInput):
                # Create Discord component
                discord_input = input_item.create_discord_component()
                self.add_item(discord_input)
                self.inputs[input_item.custom_id] = input_item
            else:
                # Assume it's already a Discord component
                self.add_item(input_item)
                self.inputs[input_item.custom_id] = input_item

        # Store callback
        self.user_callback = callback

    async def on_submit(self, interaction: Interaction):
        """Handle modal submission."""
        # Collect values
        values = {}
        for child in self.children:
            # Check the type of component to handle attributes properly
            if isinstance(child, discord.ui.TextInput):
                values[child.custom_id] = child.value
            elif hasattr(child, 'custom_id') and hasattr(child, 'value'):
                values[child.custom_id] = child.value
            elif hasattr(child, 'custom_id') and hasattr(child, 'values'):
                values[child.custom_id] = child.values

        # Dispatch state update
        if self.view_id:
            from ..state.store import get_store
            store = get_store()

            payload = {
                "view_id": self.view_id,
                "values": values,
                "user_id": interaction.user.id,
            }

            await store.dispatch("MODAL_SUBMITTED", payload, source_id=self.view_id)

        # Call user callback if provided
        if self.user_callback:
            await self.user_callback(interaction, values)
        else:
            # Default acknowledge
            await interaction.response.defer()


class InputField:
    """Helper class to define form input fields."""

    def __init__(self, type: str, id: str, label: str, required: bool = False, **options):
        self.type = type
        self.id = id
        self.label = label
        self.required = required
        self.options = options

    def to_dict(self):
        """Convert to dictionary for FormView."""
        return {
            "type": self.type,
            "id": self.id,
            "label": self.label,
            "required": self.required,
            **self.options
        }


def create_text_field(id: str, label: str, required: bool = False,
                      placeholder: Optional[str] = None):
    """Create a text input field definition."""
    return InputField("string", id, label, required,
                      placeholder=placeholder)


def create_select_field(id: str, label: str, options: list, required: bool = False,
                        placeholder: Optional[str] = None):
    """Create a select field definition."""
    return InputField("select", id, label, required,
                      options=options, placeholder=placeholder)


def create_boolean_field(id: str, label: str, required: bool = False):
    """Create a boolean field definition."""
    return InputField("boolean", id, label, required)
