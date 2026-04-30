# // ========================================( Modules )======================================== // #


import time
from typing import Any, Callable, Dict, Optional

import discord
from discord import ButtonStyle, Interaction

from .types import EmojiInput

# // ========================================( Constants )======================================== // #


_VALID_COOLDOWN_SCOPES = frozenset({"user", "guild", "user_guild", "global"})


# // ========================================( Functions )======================================== // #


def with_loading_state(
    component: Any,
    loading_label: str = "Loading...",
    loading_emoji: EmojiInput = None,
) -> Any:
    """Add loading state to a component.

    While the original callback runs, the component is disabled and its label
    is replaced with ``loading_label``. Loading UX requires consuming the
    interaction response slot to ship the disabled state immediately, which
    is mutually exclusive with the acting-view fast path in ``refresh()`` --
    opting into loading feedback opts out of the one-HTTP-call refresh for
    this click. The subsequent state-driven refresh falls through to the
    channel endpoint, which is the correct trade for callbacks expected to
    take long enough to warrant a spinner.

    For ``_StatefulMixin`` views, the pre-edit and restore route through
    ``view.refresh()`` so rate-limit backoff, render-hash skipping, and
    cooldown stamping all participate in the wrapper's edits.

    The original callback receives an interaction whose response may already
    be consumed. Use ``self.respond(interaction, ...)`` for any replies --
    it routes through ``interaction.response`` or ``interaction.followup``
    automatically.

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

        component.disabled = True
        if hasattr(component, "label"):
            component.label = loading_label
        if loading_emoji is not None and hasattr(component, "emoji"):
            component.emoji = loading_emoji

        # Route pre-edit through view.refresh() for stateful views so the
        # library's throttle/digest/backoff path handles the edit. Falls
        # through to direct response.edit_message for plain discord.ui
        # views, and silently skips when the response slot is already
        # consumed (auto-defer fired, or the callback opened the slot).
        from ..views.base import _StatefulMixin

        if isinstance(view, _StatefulMixin) and view._message is not None:
            try:
                await view.refresh()
            except Exception:
                pass
        elif not interaction.response.is_done():
            try:
                await interaction.response.edit_message(view=view)
            except discord.InteractionResponded:
                pass

        try:
            await original_callback(interaction)
        except discord.InteractionResponded:
            raise RuntimeError(
                f"The callback wrapped by with_loading_state tried to use "
                f"interaction.response, which was already consumed to show "
                f"the loading state. Use interaction.followup.send() instead."
            )
        finally:
            component.disabled = False
            if hasattr(component, "label") and original_label is not None:
                component.label = original_label
            if hasattr(component, "emoji"):
                component.emoji = original_emoji

            # Restore edit goes through refresh() for stateful views so
            # the restore participates in cooldown throttling + 429 backoff
            # rather than racing with state-driven refreshes; plain views
            # fall back to the interaction-message edit path.
            try:
                if hasattr(view, "is_finished") and view.is_finished():
                    pass
                elif isinstance(view, _StatefulMixin) and view._message is not None:
                    await view.refresh()
                elif interaction.message:
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
    Both terminal paths call ``stop()`` on the inner confirmation View so
    the timeout task is cancelled immediately instead of lingering until
    the natural expiry.

    The original callback (and ``on_cancel``) receive the confirmation
    button's interaction with the response already consumed.
    Use ``self.respond(interaction, ...)`` for any replies.

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
            try:
                await confirm_interaction.response.edit_message(
                    content=confirmed_message, embed=None, view=None
                )
                await original_callback(confirm_interaction)
            finally:
                confirmation_view.stop()

        async def _on_cancel(cancel_interaction: Interaction) -> None:
            try:
                await cancel_interaction.response.edit_message(
                    content=cancelled_message, embed=None, view=None
                )
                if on_cancel is not None:
                    await on_cancel(cancel_interaction)
            finally:
                confirmation_view.stop()

        confirm_button = discord.ui.Button(label=confirm_label, style=confirm_style)
        confirm_button.callback = _on_confirm

        cancel_button = discord.ui.Button(label=cancel_label, style=cancel_style)
        cancel_button.callback = _on_cancel

        confirmation_view.add_item(confirm_button)
        confirmation_view.add_item(cancel_button)

        embed = discord.Embed(title=title, description=message, color=color)

        # Route the prompt through view.respond() when the parent is a
        # CascadeUI view so the library's is_done()-absorbing helper does
        # the branching; plain discord.ui views retain the inline branch.
        from ..views.base import _StatefulMixin

        view = component.view
        if isinstance(view, _StatefulMixin):
            await view.respond(interaction, embed=embed, view=confirmation_view, ephemeral=True)
        elif not interaction.response.is_done():
            await interaction.response.send_message(
                embed=embed, view=confirmation_view, ephemeral=True
            )
        else:
            await interaction.followup.send(embed=embed, view=confirmation_view, ephemeral=True)

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
        scope: Cooldown scope -- ``"user"`` (per-user), ``"guild"``
            (per-guild, shared across all users in a server),
            ``"user_guild"`` (per-user-per-guild, independent cooldowns
            in each server), or ``"global"`` (one cooldown for everyone).
            Matches the four-value scope grammar used by
            ``instance_scope`` and ``state_scope``.

    Raises:
        ValueError: If ``scope`` is not one of the valid cooldown scopes.
    """
    if scope not in _VALID_COOLDOWN_SCOPES:
        raise ValueError(
            f"with_cooldown(scope={scope!r}) is not a valid cooldown scope. "
            f"Valid scopes: {sorted(_VALID_COOLDOWN_SCOPES)}"
        )

    original_callback = component.callback
    # Monotonic clock avoids DST / NTP-skew unlocking cooldowns early.
    cooldowns: Dict[Any, float] = {}
    default_message = "This action is on cooldown. Try again in {remaining} seconds."

    def _get_key(interaction: Interaction) -> Any:
        if scope == "guild":
            return interaction.guild_id or interaction.user.id
        elif scope == "user_guild":
            return (interaction.user.id, interaction.guild_id)
        elif scope == "global":
            return "__global__"
        return interaction.user.id

    async def cooldown_callback(interaction: Interaction) -> None:
        key = _get_key(interaction)
        now = time.monotonic()

        expired = [k for k, v in cooldowns.items() if now >= v]
        for k in expired:
            del cooldowns[k]

        deadline = cooldowns.get(key)

        if deadline is not None and now < deadline:
            remaining = deadline - now
            text = (message or default_message).format(remaining=f"{remaining:.1f}")

            from ..views.base import _StatefulMixin

            view = component.view
            if isinstance(view, _StatefulMixin):
                await view.respond(interaction, text, ephemeral=True)
            elif not interaction.response.is_done():
                await interaction.response.send_message(text, ephemeral=True)
            else:
                await interaction.followup.send(text, ephemeral=True)
            return

        cooldowns[key] = now + seconds
        await original_callback(interaction)

    component.callback = cooldown_callback
    return component
