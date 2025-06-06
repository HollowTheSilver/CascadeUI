
# // ========================================( Modules )======================================== // #


from typing import Callable, Optional, Any
import discord
from discord import Interaction


# // ========================================( Functions )======================================== // #


def with_loading_state(component: Any) -> Any:
    """Add loading state to a component."""
    original_callback = component.callback
    original_label = component.label if hasattr(component, "label") else "Button"

    async def loading_callback(interaction: Interaction) -> None:
        # Show loading state
        component.disabled = True
        if hasattr(component, "label"):
            component.label = "Loading..."

        # DON'T defer - let the original callback handle interaction response
        try:
            # Get the view if possible
            if hasattr(interaction, "message") and interaction.message:
                try:
                    # Try to update the message directly
                    await interaction.response.edit_message(view=interaction.message.view)
                except Exception:
                    # If that fails, don't try to respond again
                    pass

            # Call original callback
            await original_callback(interaction)

        finally:
            # Reset component state
            component.disabled = False
            if hasattr(component, "label"):
                component.label = original_label

            # Try to update message after operation is complete
            if hasattr(interaction, "message") and interaction.message:
                try:
                    await interaction.message.edit(view=interaction.message.view)
                except Exception:
                    pass  # Ignore errors in cleanup

    component.callback = loading_callback
    return component


def with_confirmation(component: Any, title: str = "Confirm Action",
                      message: str = "Are you sure?") -> Any:
    """Add a confirmation step to a component."""
    original_callback = component.callback

    async def confirmation_callback(interaction: Interaction) -> None:
        # Create confirmation view
        from ..views.base import StatefulView

        confirmation_view = StatefulView(timeout=60.0)

        async def on_confirm(i: Interaction) -> None:
            # Clean up confirmation message
            await i.message.delete()

            # Call original callback
            await original_callback(interaction)

        async def on_cancel(i: Interaction) -> None:
            # Just delete the confirmation message
            await i.message.delete()

        # Add confirmation buttons
        from .patterns import ConfirmationButtons
        confirmation_buttons = ConfirmationButtons(
            on_confirm=on_confirm,
            on_cancel=on_cancel
        )
        confirmation_buttons.add_to_view(confirmation_view)

        # Show confirmation
        embed = discord.Embed(
            title=title,
            description=message,
            color=discord.Color.yellow()
        )

        await interaction.response.send_message(
            embed=embed,
            view=confirmation_view,
            ephemeral=True
        )

    component.callback = confirmation_callback
    return component


def with_cooldown(component: Any, seconds: int = 5) -> Any:
    """Add a cooldown period to a component."""
    from datetime import datetime, timedelta

    original_callback = component.callback
    cooldown_until: Optional[datetime] = None

    async def cooldown_callback(interaction: Interaction) -> None:
        nonlocal cooldown_until

        # Check if component is on cooldown
        now = datetime.now()
        if cooldown_until and now < cooldown_until:
            remaining = (cooldown_until - now).total_seconds()
            await interaction.response.send_message(
                f"This action is on cooldown. Try again in {remaining:.1f} seconds.",
                ephemeral=True
            )
            return

        # Set cooldown
        cooldown_until = now + timedelta(seconds=seconds)

        # Call original callback
        await original_callback(interaction)

    component.callback = cooldown_callback
    return component
