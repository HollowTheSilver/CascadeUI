# // ========================================( Modules )======================================== // #


import asyncio
from typing import Optional, Callable, Dict, Any, List

import discord
from discord import Interaction, TextStyle

from .base import StatefulComponent
from ..state.actions import ActionCreators
from ..validation import validate_fields, ValidationResult


# // ========================================( Classes )======================================== // #


class TextInput(StatefulComponent):
    """A modal text input with state management."""

    def __init__(
        self,
        label: str,
        placeholder: Optional[str] = None,
        default: Optional[str] = None,
        required: bool = True,
        min_length: Optional[int] = None,
        max_length: Optional[int] = None,
        style: TextStyle = TextStyle.short,
    ):
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
            custom_id=self.custom_id,
        )


class Modal(discord.ui.Modal, StatefulComponent):
    """A modal dialog with stateful inputs and optional validation.

    Parameters
    ----------
    title:
        The modal title shown to the user.
    inputs:
        List of :class:`TextInput` instances or raw ``discord.ui.TextInput`` items.
    callback:
        Async function called with ``(interaction, values)`` after validation passes.
        If omitted, the interaction is deferred automatically.
    validators:
        Optional dict mapping ``custom_id`` to a list of validator functions.
        Each validator receives ``(value, field_def, all_values)`` and returns
        a :class:`~cascadeui.ValidationResult`. On failure, an ephemeral message
        is sent listing the errors and the callback is not invoked.
    timeout:
        Modal timeout in seconds (``None`` for no timeout).
    view_id:
        If provided, a ``MODAL_SUBMITTED`` action is dispatched to the store.
    """

    def __init__(
        self,
        title: str,
        inputs: list,
        callback: Optional[Callable] = None,
        validators: Optional[Dict[str, list]] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ):
        super().__init__(title=title, timeout=timeout)

        self.view_id = kwargs.get("view_id")
        self.inputs = {}
        self.validators = validators or {}

        # Add inputs
        for input_item in inputs:
            if isinstance(input_item, TextInput):
                discord_input = input_item.create_discord_component()
                self.add_item(discord_input)
                self.inputs[input_item.custom_id] = input_item
            else:
                self.add_item(input_item)
                self.inputs[input_item.custom_id] = input_item

        # Store callback
        self.user_callback = callback

    async def on_submit(self, interaction: Interaction):
        """Handle modal submission with optional validation."""
        # Collect values
        values = {}
        for child in self.children:
            if isinstance(child, discord.ui.TextInput):
                values[child.custom_id] = child.value
            elif hasattr(child, "custom_id") and hasattr(child, "value"):
                values[child.custom_id] = child.value
            elif hasattr(child, "custom_id") and hasattr(child, "values"):
                values[child.custom_id] = child.values

        # Run validation if validators were provided
        if self.validators:
            field_defs = [
                {"id": field_id, "validators": field_validators}
                for field_id, field_validators in self.validators.items()
            ]
            errors = await validate_fields(values, field_defs)
            if errors:
                lines = []
                for field_id, field_errors in errors.items():
                    for err in field_errors:
                        lines.append(f"**{field_id}**: {err.message}")
                await interaction.response.send_message("\n".join(lines), ephemeral=True)
                return

        # Dispatch state update
        if self.view_id:
            from ..state.singleton import get_store

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
            # Safety net: defer if the callback forgot to respond
            if not interaction.response.is_done():
                await interaction.response.defer()
        else:
            await interaction.response.defer()
