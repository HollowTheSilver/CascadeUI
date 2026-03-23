# // ========================================( Modules )======================================== // #


from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, Tuple

import discord
from discord import ButtonStyle, Interaction

# // ========================================( Functions )======================================== // #


def with_loading_state(
    component: Any,
    loading_label: str = "Loading...",
    loading_emoji: Optional[str] = None,
) -> Any:
    """Add loading state to a component.

    While the original callback runs, the component is disabled and its label
    is replaced with ``loading_label``. The view is edited immediately so the
    user sees the loading state without delay.

    The original callback receives an interaction whose response is already
    consumed. Use ``interaction.followup.send()`` for any replies.

    Args:
        component: The component to wrap.
        loading_label: Text shown on the button while loading.
        loading_emoji: Optional emoji shown on the button while loading.
    """
    original_callback = component.callback
    original_label = component.label if hasattr(component, "label") else None
    original_emoji = component.emoji if hasattr(component, "emoji") else None

    async def loading_callback(interaction: Interaction) -> None:
        view = component.view

        # Set loading appearance
        component.disabled = True
        if hasattr(component, "label"):
            component.label = loading_label
        if loading_emoji is not None and hasattr(component, "emoji"):
            component.emoji = loading_emoji

        # Show loading state immediately via interaction response
        await interaction.response.edit_message(view=view)

        try:
            await original_callback(interaction)
        except discord.InteractionResponded:
            raise RuntimeError(
                f"The callback wrapped by with_loading_state tried to use "
                f"interaction.response, which was already consumed to show "
                f"the loading state. Use interaction.followup.send() instead."
            )
        finally:
            # Reset component state
            component.disabled = False
            if hasattr(component, "label") and original_label is not None:
                component.label = original_label
            if hasattr(component, "emoji"):
                component.emoji = original_emoji

            # Update message to show restored state
            try:
                if interaction.message:
                    await interaction.message.edit(view=view)
            except Exception:
                pass

    component.callback = loading_callback
    return component


def with_confirmation(
    component: Any,
    title: str = "Confirm Action",
    message: str = "Are you sure?",
    color: discord.Color = discord.Color.yellow(),
    confirm_label: str = "Yes",
    cancel_label: str = "No",
    confirm_style: ButtonStyle = ButtonStyle.success,
    cancel_style: ButtonStyle = ButtonStyle.danger,
    confirmed_message: str = "Confirmed.",
    cancelled_message: str = "Cancelled.",
    on_cancel: Optional[Callable] = None,
    timeout: float = 60.0,
) -> Any:
    """Add a confirmation step to a component.

    When the component is clicked, an ephemeral confirmation prompt is shown.
    If confirmed, the prompt is edited to ``confirmed_message`` and the
    original callback is called. If cancelled, the prompt is edited to
    ``cancelled_message`` and the optional ``on_cancel`` callback is called.

    The original callback (and ``on_cancel``) receive the confirmation
    button's interaction with the response already consumed.
    Use ``interaction.followup.send()`` for any messages.

    Args:
        component: The component to wrap.
        title: Embed title for the confirmation prompt.
        message: Embed description for the confirmation prompt.
        color: Embed color for the confirmation prompt.
        confirm_label: Label for the confirm button.
        cancel_label: Label for the cancel button.
        confirm_style: Style for the confirm button.
        cancel_style: Style for the cancel button.
        confirmed_message: Text shown after confirming.
        cancelled_message: Text shown after cancelling.
        on_cancel: Optional async callback invoked on cancel.
        timeout: Seconds before the confirmation prompt expires.
    """
    original_callback = component.callback

    async def confirmation_callback(interaction: Interaction) -> None:
        confirmation_view = discord.ui.View(timeout=timeout)

        async def _on_confirm(confirm_interaction: Interaction) -> None:
            await confirm_interaction.response.edit_message(
                content=confirmed_message, embed=None, view=None
            )
            await original_callback(confirm_interaction)

        async def _on_cancel(cancel_interaction: Interaction) -> None:
            await cancel_interaction.response.edit_message(
                content=cancelled_message, embed=None, view=None
            )
            if on_cancel is not None:
                await on_cancel(cancel_interaction)

        confirm_button = discord.ui.Button(label=confirm_label, style=confirm_style)
        confirm_button.callback = _on_confirm

        cancel_button = discord.ui.Button(label=cancel_label, style=cancel_style)
        cancel_button.callback = _on_cancel

        confirmation_view.add_item(confirm_button)
        confirmation_view.add_item(cancel_button)

        embed = discord.Embed(title=title, description=message, color=color)

        await interaction.response.send_message(embed=embed, view=confirmation_view, ephemeral=True)

    component.callback = confirmation_callback
    return component


def with_cooldown(
    component: Any,
    seconds: int = 5,
    message: Optional[str] = None,
    scope: str = "user",
) -> Any:
    """Add a cooldown period to a component.

    While on cooldown, interactions are rejected with an ephemeral message.
    The original callback's interaction is passed through untouched.

    Args:
        component: The component to wrap.
        seconds: Duration of the cooldown in seconds.
        message: Custom cooldown message. Use ``{remaining}`` as a
            placeholder for the time left (e.g. ``"Wait {remaining}s"``).
        scope: Cooldown scope — ``"user"`` (per-user), ``"guild"``
            (per-guild, shared across all users in a server), or
            ``"global"`` (one cooldown for everyone).
    """
    original_callback = component.callback
    cooldowns: Dict[Any, datetime] = {}
    default_message = "This action is on cooldown. Try again in {remaining} seconds."

    def _get_key(interaction: Interaction) -> Any:
        if scope == "guild":
            return interaction.guild_id or interaction.user.id
        elif scope == "global":
            return "__global__"
        return interaction.user.id

    async def cooldown_callback(interaction: Interaction) -> None:
        key = _get_key(interaction)
        now = datetime.now()

        # Evict expired entries to prevent unbounded growth
        expired = [k for k, v in cooldowns.items() if now >= v]
        for k in expired:
            del cooldowns[k]

        cooldown_until = cooldowns.get(key)

        if cooldown_until and now < cooldown_until:
            remaining = (cooldown_until - now).total_seconds()
            text = (message or default_message).format(remaining=f"{remaining:.1f}")
            await interaction.response.send_message(text, ephemeral=True)
            return

        cooldowns[key] = now + timedelta(seconds=seconds)
        await original_callback(interaction)

    component.callback = cooldown_callback
    return component
