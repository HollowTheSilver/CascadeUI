# // ========================================( Modules )======================================== // #


import logging
import time
from typing import Any, Callable, Dict, Optional

import discord
from discord import ButtonStyle, Interaction

from ..utils.hooks import await_maybe
from ..utils.responses import DISCORD_CALL_ERRORS, describe_discord_error
from .types import EmojiInput

logger = logging.getLogger(__name__)

# // ========================================( Constants )======================================== // #


_VALID_COOLDOWN_SCOPES = frozenset({"user", "guild", "user_guild", "global"})

# (view class, cooldown key) pairs already reported as colliding. A shared
# deadline is a property of the class's build, not of the click that exposed
# it, so one line per shape says everything a repeat would.
_shared_cooldown_warned: set = set()


# // ========================================( Helpers )======================================== // #


def _claim_wrap(component: Any, wrapper: str) -> bool:
    """Record that *wrapper* has wrapped *component*; report a repeat.

    Every wrapper installs itself by reassigning ``component.callback``
    around the previous one. A reactive view calls its build method on each
    render, so wrapping a component that outlives the rebuild stacks a new
    layer every time: five renders leave five nested cooldowns, and five
    nested confirmations demand five clicks of "Yes" for one action. The
    nesting is unbounded and grows with session length.

    Returns ``True`` when *wrapper* is already installed, so the caller can
    hand the component back untouched. Re-wrapping with different arguments
    keeps the first call's arguments; a build method that wraps the same
    component every render is the case this exists for, and it passes the
    same arguments every time.
    """
    wrapped = getattr(component, "_cascadeui_wrapped", None)
    if wrapped is None:
        wrapped = set()
        component._cascadeui_wrapped = wrapped
    if wrapper in wrapped:
        return True

    # Name the callback the user wrote, before this wrapper buries it.
    # ``with_cooldown`` keys its deadlines on that identity, and a wrapper
    # installed first leaves only its own closure behind: one shared by
    # every component that wrapper touched. ``StatefulComponent`` stamps this
    # already, so the first claim wins and later wrappers read through to the
    # same callback.
    if getattr(component, "_cascadeui_user_callback", None) is None:
        component._cascadeui_user_callback = getattr(component, "callback", None)

    wrapped.add(wrapper)
    return False


class _ConfirmationView(discord.ui.View):
    """Inner view for ``with_confirmation``.

    Disables its buttons on timeout so a late click reflects expiry instead of
    erroring with "This interaction failed". ``message`` is captured after the
    prompt is sent so the disabled state can be edited onto it.
    """

    def __init__(self, timeout: float):
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except DISCORD_CALL_ERRORS as e:
                logger.debug(
                    f"with_confirmation prompt timeout edit failed: " f"{describe_discord_error(e)}"
                )


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
    if _claim_wrap(component, "with_loading_state"):
        return component

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
            except Exception as e:
                logger.debug(
                    f"with_loading_state pre-edit refresh failed on {type(view).__name__}: {e}"
                )
        elif not interaction.response.is_done():
            try:
                await interaction.response.edit_message(view=view)
            except (*DISCORD_CALL_ERRORS, discord.InteractionResponded) as e:
                # Matches the stateful branch above: showing the loading
                # state is cosmetic, and failing to show it must not
                # cancel the action the click asked for.
                logger.debug(
                    f"with_loading_state pre-edit failed on "
                    f"{type(view).__name__}: {describe_discord_error(e)}"
                )

        try:
            await await_maybe(original_callback(interaction))
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
            except Exception as e:
                logger.debug(
                    f"with_loading_state restore edit failed on {type(view).__name__}: {e}"
                )

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
    if _claim_wrap(component, "with_confirmation"):
        return component

    original_callback = component.callback

    async def confirmation_callback(interaction: Interaction) -> None:
        confirmation_view = _ConfirmationView(timeout=timeout)

        async def _on_confirm(confirm_interaction: Interaction) -> None:
            try:
                try:
                    await confirm_interaction.response.edit_message(
                        content=confirmed_message, embed=None, view=None
                    )
                except (*DISCORD_CALL_ERRORS, discord.InteractionResponded):
                    # A failed prompt edit (deleted message, dead ack, transient
                    # 5xx, a rate limit, a slot something else already took)
                    # must not cancel the confirmed action -- the contract is
                    # "on confirm, run the callback". Swallow the cosmetic edit
                    # failure and proceed; the callback acks the slot if it is
                    # still open.
                    pass
                await await_maybe(original_callback(confirm_interaction))
            finally:
                confirmation_view.stop()

        async def _on_cancel(cancel_interaction: Interaction) -> None:
            try:
                try:
                    await cancel_interaction.response.edit_message(
                        content=cancelled_message, embed=None, view=None
                    )
                except (*DISCORD_CALL_ERRORS, discord.InteractionResponded):
                    # Same containment as _on_confirm: a failed prompt edit must
                    # not skip the on_cancel hook.
                    pass
                if on_cancel is not None:
                    await await_maybe(on_cancel(cancel_interaction))
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
        prompt_message = None
        if isinstance(view, _StatefulMixin):
            await view.respond(interaction, embed=embed, view=confirmation_view, ephemeral=True)
        elif not interaction.response.is_done():
            await interaction.response.send_message(
                embed=embed, view=confirmation_view, ephemeral=True
            )
        else:
            prompt_message = await interaction.followup.send(
                embed=embed, view=confirmation_view, ephemeral=True
            )

        # Capture the prompt so on_timeout can disable its buttons. The
        # followup path returns the message directly; the response-slot path
        # resolves it via original_response(). Best-effort -- if the slot was
        # pre-consumed on the respond() path, on_timeout still disables the
        # buttons in memory.
        if prompt_message is None:
            try:
                prompt_message = await interaction.original_response()
            except DISCORD_CALL_ERRORS as e:
                logger.debug(
                    f"with_confirmation could not capture prompt message: "
                    f"{describe_discord_error(e)}"
                )
        confirmation_view.message = prompt_message

    component.callback = confirmation_callback
    return component


def with_cooldown(
    component: Any,
    seconds: float = 5,
    message: Optional[str] = None,
    scope: str = "user",
    key: Optional[str] = None,
) -> Any:
    """Add a cooldown period to a component.

    While on cooldown, interactions are rejected with an ephemeral message.
    The original callback's interaction is passed through untouched.

    This is the per-user spam guard: it throttles one expensive control for
    one clicker. It is the right tool where ``refresh_cooldown_ms`` is the
    wrong one: that attribute paces a whole view's background re-renders,
    so using it to deter spam punishes every viewer for one user's clicking.

    Deadlines are held on the owning view, not in this call, so they survive
    the component being rebuilt. A reactive view constructs a fresh component
    on every render and wraps it again, which would otherwise discard the
    deadline recorded on the previous click before the next one lands. And
    since an accepted click is what triggers the rebuild, the cooldown would
    never fire at all. Deadlines last as long as the view instance: a fresh
    open, or a pop that reconstructs the view, starts clean. A component with
    no view falls back to per-call storage.

    Args:
        component: The component to wrap.
        seconds: Duration of the cooldown in seconds. Fractional values
            work: the rejection notice reports the remainder to one
            decimal, so a sub-second guard reads as "0.2s" rather than
            rounding away to nothing.
        message: Custom cooldown message. Use ``{remaining}`` as a
            placeholder for the time left (e.g. ``"Wait {remaining}s"``).
        scope: Cooldown scope -- ``"user"`` (per-user), ``"guild"``
            (per-guild, shared across all users in a server),
            ``"user_guild"`` (per-user-per-guild, independent cooldowns
            in each server), or ``"global"`` (one cooldown for everyone).
            Matches the four-value scope grammar used by
            ``instance_scope`` and ``state_scope``.
        key: Names the deadline this component reads on the owning view.
            Defaults to the ``custom_id`` when the caller passed one,
            otherwise to the wrapped callback's qualified name. Both are
            stable across rebuilds. Pass an explicit key when neither names
            this control alone: several components wired to one callback,
            or callables minted per item (factory closures, lambdas,
            partials), which all carry the same qualified name. Controls
            that share a name share a deadline and throttle each other.

    Raises:
        ValueError: If ``scope`` is not one of the valid cooldown scopes.
    """
    if scope not in _VALID_COOLDOWN_SCOPES:
        raise ValueError(
            f"with_cooldown(scope={scope!r}) is not a valid cooldown scope. "
            f"Valid scopes: {sorted(_VALID_COOLDOWN_SCOPES)}"
        )

    if _claim_wrap(component, "with_cooldown"):
        return component

    original_callback = component.callback
    # Fallback store for a component with no owning view. A view-attached
    # component never reads this: its deadlines live on the view, which is
    # what carries them across a rebuild.
    orphan_cooldowns: Dict[Any, float] = {}
    default_message = "This action is on cooldown. Try again in {remaining} seconds."
    # Resolved once, at wrap time. A rebuild hands back a fresh component
    # object, so the key has to name something the render did not create.
    #
    # A custom_id the caller passed is exactly that: their own name for this
    # control, stable by construction and unique inside the view (duplicates
    # already fail placement validation). ``_stabilize_custom_ids`` reads the
    # same flag for the same reason, so the escape hatch wins here too.
    #
    # An auto-generated custom_id is not a name: at wrap time it is still
    # random hex, and the stabilizer later derives it from the label, so a
    # trigger that relabels between states (Enable / Disable) would start a
    # fresh cooldown on every flip.
    #
    # Failing that, what the component DOES is the one thing a rebuild keeps.
    # Read through to the caller's own function, since a StatefulButton's
    # ``callback`` is the stateful wrapper, whose qualname every stateful
    # component in the library shares. Callables minted per item (factory
    # closures, lambdas, partials) all carry one qualname, so those name
    # their control through ``custom_id=`` or ``key=``.
    _keyed_on = getattr(component, "_cascadeui_user_callback", None) or original_callback
    if key:
        cooldown_key = key
    elif getattr(component, "_provided_custom_id", False) is True:
        cooldown_key = component.custom_id
    else:
        cooldown_key = getattr(_keyed_on, "__qualname__", None) or repr(component)

    # Recorded so a rejected click can ask whether the control it just
    # throttled is the one the clicker pressed. Only a defaulted key can be
    # wrong this way: an explicit key or custom_id names one control by
    # construction.
    component._cascadeui_cooldown_key = cooldown_key
    component._cascadeui_cooldown_key_defaulted = not (
        key or getattr(component, "_provided_custom_id", False) is True
    )

    def _get_key(interaction: Interaction) -> Any:
        if scope == "guild":
            return interaction.guild_id or interaction.user.id
        elif scope == "user_guild":
            return (interaction.user.id, interaction.guild_id)
        elif scope == "global":
            return "__global__"
        return interaction.user.id

    def _warn_if_key_is_shared(view: Any) -> None:
        """Report a deadline that answers for controls the caller meant to split.

        Callables minted per item carry one qualified name, so a loop of
        buttons defaults to one deadline and the first click throttles the
        rest. Several controls wired deliberately to one callback are the
        supported shape this stays quiet on: a bound method is a fresh
        object on every attribute read, but two reads of the same method
        compare equal, so the set below collapses them.
        """
        walk = getattr(view, "walk_children", None)
        try:
            items = list(walk()) if callable(walk) else list(getattr(view, "children", ()))
        except Exception:
            return

        sharers = [
            item
            for item in items
            if getattr(item, "_cascadeui_cooldown_key", None) == cooldown_key
            and getattr(item, "_cascadeui_cooldown_key_defaulted", False)
        ]
        if len(sharers) < 2:
            return
        callbacks = {
            getattr(item, "_cascadeui_user_callback", None) or item.callback for item in sharers
        }
        if len(callbacks) < 2:
            return

        marker = (type(view).__qualname__, cooldown_key)
        if marker in _shared_cooldown_warned:
            return
        _shared_cooldown_warned.add(marker)
        logger.warning(
            f"{len(sharers)} controls in {type(view).__qualname__} share the cooldown "
            f"name {cooldown_key!r}, so throttling one throttles the others. Their "
            f"callbacks differ, so this is a name collision rather than deliberate "
            f"sharing: callables built per item carry one qualified name. Give each "
            f"control a custom_id= or a with_cooldown(key=...)."
        )

    def _deadlines() -> Dict[Any, float]:
        """Deadlines for this component, read at click time.

        The view is resolved per click rather than captured at wrap time,
        because a component is wrapped before it is added to its view.
        """
        view = getattr(component, "view", None)
        if view is None:
            return orphan_cooldowns
        store = getattr(view, "_cascadeui_cooldowns", None)
        if store is None:
            store = {}
            view._cascadeui_cooldowns = store
        return store.setdefault(cooldown_key, {})

    async def cooldown_callback(interaction: Interaction) -> None:
        cooldowns = _deadlines()
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
            if view is not None:
                _warn_if_key_is_shared(view)
            if isinstance(view, _StatefulMixin):
                await view.respond(interaction, text, ephemeral=True)
            elif not interaction.response.is_done():
                await interaction.response.send_message(text, ephemeral=True)
            else:
                await interaction.followup.send(text, ephemeral=True)
            return

        cooldowns[key] = now + seconds
        await await_maybe(original_callback(interaction))

    component.callback = cooldown_callback
    return component
