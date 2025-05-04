
# // ========================================( Modules )======================================== // #


from typing import Optional, Callable, Any

import discord
from discord import ButtonStyle, Interaction

from .base import StatefulButton
from ..state.actions import ActionCreators


# // ========================================( Classes )======================================== // #


class PrimaryButton(StatefulButton):
    """A primary-styled button with state management."""

    def __init__(self, label: str, callback: Optional[Callable] = None, **kwargs):
        kwargs.setdefault("style", ButtonStyle.primary)
        super().__init__(label=label, callback=callback, **kwargs)


class SecondaryButton(StatefulButton):
    """A secondary-styled button with state management."""

    def __init__(self, label: str, callback: Optional[Callable] = None, **kwargs):
        kwargs.setdefault("style", ButtonStyle.secondary)
        super().__init__(label=label, callback=callback, **kwargs)


class SuccessButton(StatefulButton):
    """A success-styled button with state management."""

    def __init__(self, label: str, callback: Optional[Callable] = None, **kwargs):
        kwargs.setdefault("style", ButtonStyle.success)
        super().__init__(label=label, callback=callback, **kwargs)


class DangerButton(StatefulButton):
    """A danger-styled button with state management."""

    def __init__(self, label: str, callback: Optional[Callable] = None, **kwargs):
        kwargs.setdefault("style", ButtonStyle.danger)
        super().__init__(label=label, callback=callback, **kwargs)


class LinkButton(discord.ui.Button):
    """A link button that doesn't require a callback."""

    def __init__(self, label: str, url: str, **kwargs):
        kwargs.setdefault("style", ButtonStyle.link)
        super().__init__(label=label, url=url, **kwargs)


class ToggleButton(StatefulButton):
    """A button that toggles between two states."""

    def __init__(self, label: str, toggled_label: str = None,
                 toggled: bool = False, callback: Optional[Callable] = None, **kwargs):
        self.original_label = label
        self.toggled_label = toggled_label or f"{label} ✓"
        self.is_toggled = toggled

        # Set initial style
        if toggled:
            kwargs.setdefault("style", ButtonStyle.success)
            current_label = self.toggled_label
        else:
            kwargs.setdefault("style", ButtonStyle.secondary)
            current_label = self.original_label

        # Create the button
        super().__init__(label=current_label, **kwargs)

        # Store original callback and create toggle wrapper
        self.user_callback = callback
        self.callback = self._create_toggle_callback()

    def _create_toggle_callback(self):
        """Create a callback that handles toggling."""

        async def toggle_callback(interaction: Interaction):
            # Toggle state
            self.is_toggled = not self.is_toggled

            # Update button appearance
            self.label = self.toggled_label if self.is_toggled else self.original_label
            self.style = ButtonStyle.success if self.is_toggled else ButtonStyle.secondary

            # Dispatch state update
            view = interaction.client._view_store.get_view_from_message(interaction.message.id)
            if view and hasattr(view, "dispatch"):
                component_id = getattr(self, "custom_id", None) or str(id(self))
                payload = ActionCreators.component_interaction(
                    component_id=component_id,
                    view_id=view.id,
                    user_id=interaction.user.id,
                    value=self.is_toggled
                )
                await view.dispatch("COMPONENT_INTERACTION", payload)

            # Call user callback if provided
            if self.user_callback:
                await self.user_callback(interaction)
            else:
                # Default to defer if no callback
                await interaction.response.defer()

        return toggle_callback
