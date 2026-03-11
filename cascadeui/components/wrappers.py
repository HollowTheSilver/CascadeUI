
# // ========================================( Modules )======================================== // #


from typing import Callable, Optional, Any, Dict
from datetime import datetime, timedelta

import discord
from discord import Interaction


# // ========================================( Functions )======================================== // #


def with_loading_state(component: Any, loading_label: str = "Loading...") -> Any:
    """Add loading state to a component.

    Uses the interaction response to show the disabled/loading state immediately,
    then calls the original callback which must use followup (not response) for
    any messages it wants to send.

    The original callback receives a modified interaction where response is
    already consumed. Use interaction.followup.send() for replies.
    """
    original_callback = component.callback
    original_label = component.label if hasattr(component, "label") else None

    async def loading_callback(interaction: Interaction) -> None:
        # Set loading appearance
        component.disabled = True
        if hasattr(component, "label"):
            component.label = loading_label

        # Show loading state immediately via interaction response
        await interaction.response.edit_message(view=interaction.message.view)

        try:
            # Call original callback — it must use followup, not response
            await original_callback(interaction)
        finally:
            # Reset component state
            component.disabled = False
            if hasattr(component, "label") and original_label is not None:
                component.label = original_label

            # Update message to show restored state
            try:
                if interaction.message:
                    await interaction.message.edit(view=interaction.message.view)
            except Exception:
                pass

    component.callback = loading_callback
    return component


def with_confirmation(component: Any, title: str = "Confirm Action",
                      message: str = "Are you sure?") -> Any:
    """Add a confirmation step to a component.

    When the component is clicked, an ephemeral confirmation prompt is shown.
    If confirmed, the original callback is called with the confirmation
    interaction (not the original one, since that token may have expired).

    The original callback receives the confirmation button's interaction.
    """
    original_callback = component.callback

    async def confirmation_callback(interaction: Interaction) -> None:
        confirmation_view = discord.ui.View(timeout=60.0)

        async def on_confirm(confirm_interaction: Interaction) -> None:
            # Delete the confirmation message
            await confirm_interaction.response.defer()
            await confirm_interaction.message.delete()

            # Call original with the fresh confirmation interaction
            await original_callback(confirm_interaction)

        async def on_cancel(cancel_interaction: Interaction) -> None:
            # Acknowledge and delete
            await cancel_interaction.response.defer()
            await cancel_interaction.message.delete()

        confirm_button = discord.ui.Button(
            label="Yes",
            style=discord.ButtonStyle.success
        )
        confirm_button.callback = on_confirm

        cancel_button = discord.ui.Button(
            label="No",
            style=discord.ButtonStyle.danger
        )
        cancel_button.callback = on_cancel

        confirmation_view.add_item(confirm_button)
        confirmation_view.add_item(cancel_button)

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
    """Add a per-user cooldown period to a component."""
    original_callback = component.callback
    user_cooldowns: Dict[int, datetime] = {}

    async def cooldown_callback(interaction: Interaction) -> None:
        user_id = interaction.user.id
        now = datetime.now()
        cooldown_until = user_cooldowns.get(user_id)

        if cooldown_until and now < cooldown_until:
            remaining = (cooldown_until - now).total_seconds()
            await interaction.response.send_message(
                f"This action is on cooldown. Try again in {remaining:.1f} seconds.",
                ephemeral=True
            )
            return

        user_cooldowns[user_id] = now + timedelta(seconds=seconds)
        await original_callback(interaction)

    component.callback = cooldown_callback
    return component
