# // ========================================( Modules )======================================== // #


import asyncio
import logging
import time
from contextvars import ContextVar
from typing import Optional

import discord
from discord import Interaction
from discord.ui import Item

from ..components.base import StatefulButton
from ..state.store import _CURRENT_INTERACTION
from ..utils.deprecation import REMOVED_IN, warn_deprecated
from ..utils.hooks import await_maybe
from ..utils.responses import (
    DISCORD_CALL_ERRORS,
    _collect_stalled_renders,
    ack_backstop,
    describe_discord_error,
    open_modal_safe,
    respond_safe,
    ship_stalled_renders,
    trailing_ack,
)
from ..utils.tasks import _bounded_wait

logger = logging.getLogger(__name__)

# The arrival number of the click being handled, or None outside a click.
_CLICK_ORDER: ContextVar[Optional[int]] = ContextVar("cascadeui_click_order", default=None)

_SELECT_COMPONENT_TYPES = frozenset(
    t.value
    for t in (
        discord.ComponentType.string_select,
        discord.ComponentType.user_select,
        discord.ComponentType.role_select,
        discord.ComponentType.mentionable_select,
        discord.ComponentType.channel_select,
    )
)


def _picked_message_id(interaction: Interaction) -> Optional[int]:
    """The id of the message ``interaction`` picked a select option on, or None."""
    data = getattr(interaction, "data", None)
    if not isinstance(data, dict) or data.get("component_type") not in _SELECT_COMPONENT_TYPES:
        return None
    return getattr(interaction.message, "id", None)


# A defer acknowledges an interaction without answering the user.
_DEFERRED_RESPONSES = frozenset(
    {
        discord.InteractionResponseType.deferred_channel_message,
        discord.InteractionResponseType.deferred_message_update,
    }
)

# Wait before re-attempting an arming edit that never reached Discord. Short
# because the webhook token dies ~90 seconds after the handoff arms, and the
# view has no other way to put the refresh button on screen once armed.
_ARMING_RETRY_SECONDS = 5.0

# Owner of the answer to a click from an earlier render, apart from the view
# it came to, so that view's teardown does not cancel the answer.
_STALE_CLICK_TASK_OWNER = "view_stale_click"


def _library_control(item) -> bool:
    """Whether ``item``'s callback is one the library defined.

    A page turn, a tab, wizard navigation, or the Exit button answers its click
    only when its render ships, and none of the user's code runs to answer it
    otherwise. Read from the module the callback was defined in, the one
    signal a component carries of whose code it runs.
    """
    callback = getattr(item, "_cascadeui_user_callback", None)
    callback = getattr(callback, "func", callback)
    callback = getattr(callback, "__func__", callback)
    module = getattr(callback, "__module__", None)
    return isinstance(module, str) and module.startswith("cascadeui.")


# // ========================================( Mixin )======================================== // #


class _InteractionMixin:
    """Interaction machinery for stateful views.

    Houses the auto-defer safety net, the serialized-callback wrapper,
    interaction response helpers (``respond``, ``open_modal``,
    ``safe_defer``), and the ephemeral refresh handoff. None of these
    methods touch navigation, session, or instance-limit state directly;
    all cross-concern access goes through attributes on the composed
    ``_StatefulMixin``.

    Not a public class. ``_StatefulMixin`` inherits from this so the
    public ``StatefulView`` / ``StatefulLayoutView`` hierarchy is
    unchanged.
    """

    # Clicks this view has received, counted before the lock (see _arrived_after).
    _clicks_received: int = 0
    # custom_id -> _clicks_received when that control's last click settled
    # (see components.base.run_unless_repeat); created on first use.
    _click_marks: Optional[dict] = None
    # The render number when a select pick on this view's message arrived. The
    # client shows the pick until the message is edited, so refresh() ships even
    # an unchanged tree until a render asked for after the pick lands.
    _pick_owed: Optional[int] = None

    # // ==================( Auto-Defer Safety Net )================== // #

    async def _scheduled_task(self, item: Item, interaction: Interaction):
        """Override discord.py's internal dispatch to add auto-defer and serialization.

        Replicates View._scheduled_task with these additions:

        1. **Auto-defer timer** -- defers the interaction if the callback hasn't
           responded within ``auto_defer_delay`` seconds (safety net for slow
           callbacks).
        2. **Interaction lock** -- when ``serialize_interactions`` is True, rapid
           button clicks are processed one at a time. This prevents racing
           ``message.edit()`` calls that cause "This interaction failed" errors.
           The auto-defer timer runs *outside* the lock so queued interactions
           are deferred before the 3-second Discord timeout.
        3. **Post-callback defer** -- after the callback finishes, if the
           interaction still hasn't been responded to, defer immediately.
           Callbacks that use ``dispatch() → on_state_changed → refresh()``
           edit the message via the channel REST endpoint, not the interaction
           response. Without this fallback, fast callbacks (< 2.5s) cancel the
           timer and the interaction goes unacknowledged. Runs whenever
           ``auto_defer`` is True. A dropped click, or a click on a control
           the library built, gets it whatever ``auto_defer`` is set to,
           since no code of the user's answers those.
        4. **Stale-click drop** -- a click is numbered on arrival, waits out a
           push or pop in flight, and is acknowledged and dropped without
           running its callback when the view has closed or navigated away,
           or the item is disabled.
        """
        # Numbered on arrival, before the lock, so a callback can tell a click
        # queued behind another from one sent after it finished.
        self._clicks_received += 1
        order = _CLICK_ORDER.set(self._clicks_received)
        # A dropped click ran no callback of the user's that could answer it.
        dropped = False
        try:
            try:
                item._refresh_state(interaction, interaction.data)  # type: ignore

                # Before the checks and the callback, so a callback that blocks
                # the loop still has its ack in flight.
                if self.ack_first:
                    await self.safe_defer(interaction)

                # Armed before the access checks: an interaction_check that
                # fetches a member runs on the 3s clock too, and Discord drops an
                # interaction left unacked (10062). A deferred interaction can
                # still be refused: on_unauthorized replies through a followup.
                defer_task = None
                if self.auto_defer and not interaction.response.is_done():
                    defer_task = asyncio.create_task(self._auto_defer_timer(interaction))

                try:
                    allow = await item._run_checks(interaction) and await self.interaction_check(
                        interaction
                    )
                    if not allow:
                        self._log_dropped_click(
                            item, f"an interaction check refused user {interaction.user.id}"
                        )
                        return

                    # A click on a view that navigated away or closed would act on
                    # a message another view owns (a second Back press would rebuild
                    # the parent twice), so it is dropped and answered by the
                    # trailing ack, once a navigation in flight has decided.
                    await self._wait_out_navigation("A click")
                    # Discord offers no click on a disabled component, so one that
                    # reaches a disabled item was sent from an older render where
                    # it was enabled, and is dropped the same way.
                    if self._closed() or getattr(item, "disabled", False) is True:
                        self._log_dropped_click(item, self._drop_reason())
                        dropped = True
                        return

                    if self.timeout:
                        self._BaseView__timeout_expiry = time.monotonic() + self.timeout  # type: ignore

                    if self.serialize_interactions:
                        async with self._interaction_lock:
                            # Queued behind a callback that navigated, exited, or
                            # disabled this item, or behind a push or pop started
                            # from elsewhere while it waited: once those have
                            # decided, it is dropped like a later click. A send in
                            # flight is not waited out, so a button that opens a
                            # modal still opens it while the panel is re-sent.
                            await self._wait_out_navigation("A click")
                            if self._closed() or getattr(item, "disabled", False) is True:
                                self._log_dropped_click(item, self._drop_reason())
                                dropped = True
                                return
                            await self._run_click_callback(item, interaction)
                    else:
                        await self._run_click_callback(item, interaction)
                finally:
                    if defer_task is not None and not defer_task.done():
                        defer_task.cancel()

                    # Acknowledge unresponded interactions so Discord does not
                    # show "This interaction failed". Common when callbacks use
                    # dispatch() → on_state_changed → refresh() which edits
                    # the message via the channel endpoint, not the interaction.
                    if self.auto_defer or dropped or _library_control(item):
                        await trailing_ack(interaction, owner=self.__class__.__name__, log=logger)
            except Exception as e:
                return await await_maybe(self.on_error(interaction, e, item))
        finally:
            _CLICK_ORDER.reset(order)

    def _drop_reason(self) -> str:
        return "the view has closed" if self._closed() else "the control is disabled on screen"

    def _log_dropped_click(self, item, reason: str) -> None:
        """Record a click the library answers without running its callback."""
        custom_id = item if isinstance(item, str) else getattr(item, "custom_id", None)
        logger.debug(f"Dropped a click on {type(self).__name__} ({custom_id!r}): {reason}")

    def _answer_stale_click(self, custom_id: str, interaction: Interaction) -> None:
        """Acknowledge a click whose control is no longer on this view's message.

        It was sent from a render the view has since replaced, so nothing
        runs, as for a click on a control disabled on screen, and the answer
        keeps Discord from showing "This interaction failed". It waits
        ``auto_defer_delay`` first and answers only if nothing else has:
        discord.py dispatches ``on_interaction`` after this, and a bot's own
        handler for the click keeps the response slot.
        """
        self._log_dropped_click(custom_id, "the control is not on the message any more")

        async def answer_if_unanswered():
            await asyncio.sleep(self.auto_defer_delay)
            await trailing_ack(interaction, owner=type(self).__name__, log=logger)

        self.task_manager.create_task(_STALE_CLICK_TASK_OWNER, answer_if_unanswered())

    async def _run_click_callback(self, item: Item, interaction: Interaction) -> None:
        with _collect_stalled_renders() as stalled:
            message = self._message
            if message is not None and _picked_message_id(interaction) == message.id:
                self._pick_owed = self._render_seq
            try:
                await item.callback(interaction)
            finally:
                # Inside the interaction lock, so the next queued click renders
                # after it, and after a raise too, as a render that landed would.
                if stalled:
                    # Bounded, since every queued click waits on the lock; the
                    # backstop still acks if this cannot.
                    try:
                        await _bounded_wait(
                            trailing_ack(interaction, owner=self.__class__.__name__, log=logger),
                            timeout=max(0.5, self.auto_defer_delay),
                        )
                    except asyncio.TimeoutError:
                        logger.debug(
                            f"Ack after a stalled render timed out in {type(self).__name__}"
                        )
                    await ship_stalled_renders(stalled)

    def _arrived_after(self, mark: float) -> bool:
        """Whether the click being handled reached this view after ``mark``.

        ``mark`` is a value of ``_clicks_received`` taken when something
        settled. A click numbered at or below it was sent from the screen as
        it stood before then, and a call not made from a click counts as
        after.
        """
        order = _CLICK_ORDER.get()
        return order is None or order > mark

    def _mark_after_refusal(self, mark: int) -> int:
        """The mark a refused click leaves, from ``mark`` as it stood.

        A refusal is not a result a later click repeats: a click fixing the
        input can be queued between two tries. So it covers one click, the
        next to reach this view, and only if that arrived before the refusal
        settled: the second click of a double-click, sent before the error was
        on screen. A call not made from a click leaves ``mark`` as it is.
        """
        order = _CLICK_ORDER.get()
        if order is None:
            return mark
        return max(mark, min(order + 1, self._clicks_received))

    async def _auto_defer_timer(self, interaction: Interaction):
        """Background timer that defers the interaction if the callback hasn't responded.

        Warns on expiry: this timer normally acks in time, so a miss means
        the loop was congested through the whole 3s window (hot-path I/O
        elsewhere, a slow ack POST), which is worth surfacing at its source
        rather than only through the downstream post-callback echo.
        """
        await ack_backstop(
            interaction,
            self.auto_defer_delay,
            owner=self.__class__.__name__,
            log=logger,
            warn_on_expiry=True,
        )

    async def respond(
        self,
        interaction: Interaction,
        content: Optional[str] = None,
        *,
        ephemeral: bool = False,
        **kwargs,
    ) -> None:
        """Send an interaction response, falling back to followup if already deferred.

        When ``serialize_interactions`` is enabled, queued interactions may
        be auto-deferred before their callback runs. Direct calls to
        ``interaction.response.send_message()`` raise ``InteractionResponded``
        in that case. This method checks ``interaction.response.is_done()``
        and routes to ``interaction.followup.send()`` transparently.

        Safe to call in any callback regardless of auto-defer state::

            # Always works, no manual is_done() check needed
            await self.respond(interaction, "Not your turn!", ephemeral=True)

        Parameters
        ----------
        interaction:
            The interaction to respond to.
        content:
            Text content of the response.
        ephemeral:
            Whether the response is ephemeral (only visible to the user).
        **kwargs:
            Additional keyword arguments forwarded to ``send_message``
            or ``followup.send`` (e.g. ``embed=``, ``view=``).
        """
        # A stateful view handed over as a raw view= kwarg skips _send_pipeline,
        # so it renders but is never registered: invisible to the inspector,
        # instance limits, and state cleanup, and its timeout fires a
        # VIEW_DESTROYED for a view that was never created.
        passed_view = kwargs.get("view")
        if passed_view is not None and hasattr(passed_view, "_send_pipeline"):
            name = type(passed_view).__name__
            logger.warning(
                f"{name} was passed to respond() as view=. Stateful views must be "
                f"sent through their own send() to be registered. "
                f"Fix: await {name}(..., interaction=interaction).send(ephemeral=True)"
            )

        await respond_safe(interaction, content, ephemeral=ephemeral, **kwargs)

    async def open_modal(
        self,
        interaction: Interaction,
        modal: discord.ui.Modal,
        *,
        fallback_message: Optional[str] = None,
    ) -> bool:
        """Open a modal dialog, with a fallback if the response slot is consumed.

        ``send_modal()`` must be the first response to an interaction -- it
        cannot follow a ``defer()``. Under ``serialize_interactions``, queued
        interactions may be auto-deferred before the callback runs. This
        method checks ``interaction.response.is_done()`` and sends an
        ephemeral fallback instead of raising ``InteractionResponded``.

        Returns ``True`` if the modal was sent, ``False`` if the fallback
        fired. Callers that need to branch on this can check the return
        value; most can ignore it.

        Parameters
        ----------
        interaction:
            The interaction to respond to.
        modal:
            The modal to open.
        fallback_message:
            Ephemeral text sent when the response slot is already consumed.
            Defaults to ``"Could not open the dialog. Please try again."``.

        Raises
        ------
        ValueError:
            The modal has no components. Discord rejects a zero-component
            modal with HTTP 400; this converts it into a directed build-time
            error. Checked here, not in ``Modal.__init__``, because a
            ``Modal(inputs=[])`` followed by ``add_item()`` is a valid build
            path and the tree is only complete at open time.
        """
        # A Modal reads this on submission, to tell the user when this view
        # closed while they were typing.
        modal._opened_by = self
        return await open_modal_safe(interaction, modal, fallback_message=fallback_message)

    async def _answer_if_closed(self, interaction: Interaction) -> bool:
        """Tell the user a modal submission reached this view after it closed.

        A modal outlives the view that opened it: the view can time out or
        close while the user is still typing, and the submission then
        updates nothing on screen. Returns ``True`` when the view has
        closed, so a caller whose only work is on this view can stop.

        Runs ``on_session_ended()`` unless the interaction already carries a
        reply, or the view handed its panel on and the view that took its
        place is still open, since the session goes on there. Whatever it
        leaves unsaid, it still acknowledges the submission, because Discord
        shows an error on a modal it never hears back about.
        """
        if not self._closed():
            return False
        response = interaction.response
        if response.is_done() and response.type not in _DEFERRED_RESPONSES:
            return True
        successor = self._last_successor()
        if successor is self or successor._closed():
            await self._call_hook_safe(self.on_session_ended, interaction)
        await self.safe_defer(interaction)
        return True

    async def _render_as_answer(self, interaction: Interaction, render) -> None:
        """Await ``render()`` as the answer to a library modal's submission.

        A plain ``discord.ui.Modal`` reaches ``on_submit`` with no ack timer
        and no acting interaction bound, so a re-render there could only
        answer the submission and then edit the message separately, through
        an endpoint that is not ordered with later clicks. Binding the
        submission lets ``refresh()`` answer it with the edit in one
        request, as it does a click. The backstop acks a render too slow for
        that, the trailing ack answers one that shipped no edit, and an edit
        abandoned to keep the ack in time is sent again once it is in.
        """
        backstop = asyncio.create_task(self._auto_defer_timer(interaction))
        token = _CURRENT_INTERACTION.set(interaction)
        stalled: dict = {}
        try:
            with _collect_stalled_renders() as stalled:
                await render()
        finally:
            _CURRENT_INTERACTION.reset(token)
            if not backstop.done():
                backstop.cancel()
            await trailing_ack(interaction, owner=type(self).__name__, log=logger)
            await ship_stalled_renders(stalled)

    async def safe_defer(self, interaction: Interaction) -> None:
        """Defer the interaction if it hasn't been acknowledged yet.

        Call it at the top of a callback whose work routinely runs past a
        second and a half, so the click is answered at once. A callback
        that rebuilds and calls ``refresh()`` quickly should not: the
        defer takes the response slot, and the edit then goes through the
        channel instead of answering the click in one request.

        Mirrors how ``respond()`` absorbs the ``is_done()`` check for
        send operations. Prevents double-defer when auto-defer or
        ``serialize_interactions`` has already acknowledged the
        interaction before the callback runs.

        The ack is bounded by ``auto_defer_delay``: this call runs inside
        the interaction lock, and a Discord ack endpoint that stalls past
        the ack window would pin the lock on a hung socket. A defer that
        cannot land in time is useless anyway (the auto-defer timer,
        running outside the lock, is the backstop), so the stall is
        cancelled and swallowed rather than propagated.

        A failed ack is never propagated. ``NotFound`` (10062) means the
        interaction token is already gone (expired, or a duplicate ack
        landed server-side), so there is nothing left to acknowledge.
        The navigation-edit paths route their deferred ack through here;
        ``NotFound`` and any other HTTP ack failure are logged at debug and
        absorbed so a missed ack never reaches the callback's error path.
        """
        if not interaction.response.is_done():
            try:
                await _bounded_wait(
                    interaction.response.defer(), timeout=max(0.5, self.auto_defer_delay)
                )
            except asyncio.TimeoutError:
                logger.debug(
                    f"Ack defer stalled past {self.auto_defer_delay}s in "
                    f"{type(self).__name__}; auto-defer timer backstops the ack."
                )
            except discord.NotFound:
                logger.debug(
                    f"Ack defer hit a dead interaction (10062) in "
                    f"{type(self).__name__}; nothing left to acknowledge."
                )
            except discord.InteractionResponded:
                # Not an HTTPException, so the handler below cannot see it. The
                # auto-defer timer can ack inside this call (more often before
                # Python 3.12, where the bound runs it as a Task); the slot is
                # acked either way, which is all this method wanted.
                logger.debug(f"Ack defer raced an existing ack in {type(self).__name__}.")
            except DISCORD_CALL_ERRORS as e:
                logger.debug(
                    f"Ack defer failed in {type(self).__name__}: " f"{describe_discord_error(e)}"
                )

    async def _safe_defer(self, interaction: Interaction) -> None:
        """Deprecated name of :meth:`safe_defer`, to be removed.

        Runs the library's defer, so an override of the old name that
        starts from ``super()._safe_defer()`` keeps working.
        """
        warn_deprecated(
            f"_safe_defer() is deprecated and will be removed in {REMOVED_IN}: "
            f"call safe_defer() instead."
        )
        await _InteractionMixin.safe_defer(self, interaction)

    # // ==================( Ephemeral Refresh )================== // #

    @property
    def _refresh_handoff(self) -> Optional[bool]:
        """Effective refresh-handoff policy for this view.

        ``auto_refresh_ephemeral`` is the author's declaration and the
        library never assigns to it; an explicit ``True``/``False``
        (including a ``set_class_attribute`` pin applied after send)
        always wins. ``None`` falls through to
        ``_refresh_handoff_resolved``, the library's own resolution:
        derived from ``timeout`` at each ephemeral send, inherited from
        the immediate source on push/pop. ``None`` from this property
        means nothing has resolved the policy yet.
        """
        declared = self.auto_refresh_ephemeral
        if declared is not None:
            return declared
        return self._refresh_handoff_resolved

    def build_refresh_button(self) -> StatefulButton:
        """Build the Continue button shown when an ephemeral session is about to expire.

        Override to customize beyond the ``refresh_button_*`` class
        attributes (row placement, a custom ``custom_id``). Start from
        ``super().build_refresh_button()`` or keep the callback: the button
        must still call the handoff, since pressing it is what sends the
        view :meth:`build_reopen_view` returns. May be sync or async. Return
        the button itself, not a row: a V2 view wraps it in one. A button
        that cannot be built or placed is logged, and the panel stays as it
        was instead of arming.
        """
        return StatefulButton(
            label=self.refresh_button_label,
            style=self.refresh_button_style,
            emoji=self.refresh_button_emoji,
            callback=self._reopen_ephemeral,
        )

    def _build_refresh_button(self) -> StatefulButton:
        """Deprecated name of :meth:`build_refresh_button`, to be removed.

        Returns the library's default button, so an override of the old
        name that starts from ``super()._build_refresh_button()`` keeps
        working.
        """
        warn_deprecated(
            f"_build_refresh_button() is deprecated and will be removed in {REMOVED_IN}: "
            f"call build_refresh_button() instead."
        )
        return _InteractionMixin.build_refresh_button(self)

    def _install_refresh_button(self, button: StatefulButton) -> None:
        """Install the refresh button into the cleared view.

        V1 adds it directly; the V2 mixin overrides this to wrap in ActionRow.
        """
        self.add_item(button)

    def _schedule_ephemeral_refresh(self) -> None:
        """Schedule arming the refresh button shortly before the original
        interaction token expires.

        Discord's interaction token lives for exactly 15 minutes (900s). The
        timer fires ``refresh_warning_seconds`` early so the swap edit still
        succeeds inside the token window.

        The wait is computed against ``_ephemeral_arm_deadline`` (stamped at
        the original ephemeral send and carried across navigation), so a
        re-schedule after a failed-navigation rollback waits
        only the time remaining until the original deadline -- not a fresh
        window that would overshoot the 900s token cliff.

        Scheduling is a plain call and the wait a loop callback, so no task
        waits for the deadline: one still pending when a program ends is
        reported as destroyed, while a pending callback goes with the loop.

        Four conditions are re-read at the deadline rather than trusted from
        before it, because each can change during the wait: the view can
        finish or lose its message, a same-instance send can make the
        managed message public, the effective handoff policy can be pinned
        off or re-derived against a shorter timeout, and a same-instance
        ephemeral re-send can stamp a new deadline with its own timer. Any
        one of them means this timer no longer describes the view it waited
        for, so it arms nothing and returns.
        """
        if self._refresh_handoff is False:
            # Refused already by the send pipeline and both navigation gates
            # for every pinned-off view; this only catches a direct schedule
            # call. ``is False`` rather than falsy, because an unresolved
            # ``None`` must keep the timer running.
            return
        deadline = self._ephemeral_arm_deadline
        if deadline is None:
            # Every scheduler stamps or requires a deadline before this
            # runs; reaching here without one means a direct call with
            # nothing to arm against.
            return
        delay = max(1, deadline - time.monotonic())
        # One timer per view: an earlier one waits for a deadline a re-send
        # replaced, or for this same one.
        self.task_manager._cancel_timers(self.id)
        self.task_manager._call_later(self.id, delay, self._arm_when_due, deadline)

    def _arm_when_due(self, deadline: float) -> None:
        """At the deadline, arm the refresh button if the view still owes it."""
        if self._handoff_still_owed(deadline):
            self.create_task(self._arm_at_deadline(deadline))

    async def _arm_at_deadline(self, deadline: float) -> None:
        async with self._within_reload_turn("the refresh handoff"):
            # Checked again with the turn held: a send of this instance, or a
            # push or pop from it, can start while the arming waits for it.
            if self._stand_down_while_away() or not self._handoff_still_owed(deadline):
                return
            await self._arm_refresh_button()

    def _stand_down_while_away(self) -> bool:
        """Leave the arming to a rollback while a push or pop from this view is in flight.

        Arming then would edit the message the navigation is about to hand
        over. A navigation that lands cancels this timer and schedules the
        destination's; one that rolls back arms here.
        """
        if self._away_for_navigation:
            self._arm_after_rollback = True
            return True
        return False

    def _handoff_still_owed(self, deadline: float) -> bool:
        """Whether the timer armed against ``deadline`` still describes this view."""
        if self.is_finished() or self._refresh_armed or not self._message:
            return False
        if not self._ephemeral:
            # A same-instance public re-send flips this; arming would put the
            # reopen button over a live public panel.
            return False
        if self._refresh_handoff is False:
            # set_class_attribute can pin the policy off, or a second
            # ephemeral send can re-derive it against a shorter timeout.
            return False
        # Deadline equality stands in for timer identity: every send clears or
        # restamps it, and navigation carries it unchanged. Arming against a
        # stale one would clear a new panel's children early and freeze it.
        return self._ephemeral_arm_deadline == deadline

    async def _arm_refresh_button(self) -> None:
        """Replace the view's children with a single refresh button.

        Best-effort: any error during the swap is logged and swallowed. The
        worst case is that the user sees the original (now-stale) view until
        their client times it out, as happens to a view with the handoff off.

        If Discord rejects the button's emoji (error 50035), retries once
        without the emoji so a bad user-supplied ``refresh_button_emoji``
        does not silently break the handoff.

        Runs under the view's reload turn. A reload or load already running
        finishes first; one queued behind the arming finds the view armed
        and leaves the button in place. Otherwise an ``on_load`` in flight
        rebuilds the tree over the button, and the armed flag then drops
        every notification that could put it back.
        """
        async with self._within_reload_turn("the refresh handoff"):
            await self._arm_refresh_button_in_turn()

    def _warn_refresh_button_failed(self) -> None:
        logger.warning(
            f"build_refresh_button() failed in {type(self).__name__}; "
            f"the ephemeral refresh handoff is not armed",
            exc_info=True,
        )

    async def _arm_refresh_button_in_turn(self) -> None:
        """Body of :meth:`_arm_refresh_button`, run under the reload turn."""
        if self._refresh_armed:
            return
        deadline = self._ephemeral_arm_deadline
        try:
            button = await await_maybe(self.build_refresh_button())
        except Exception:
            self._warn_refresh_button_failed()
            return
        # An edit already on its way would land over the button, and an armed
        # view renders nothing that could put it back. The wait gives up at half
        # the token's remaining life and arms anyway, so a stuck edit cannot
        # keep the button off until the token is gone.
        now = time.monotonic()
        expires = (now if deadline is None else deadline) + self.refresh_warning_seconds
        await self._wait_for_own_edits(
            "Arming the Continue button on",
            timeout=max(0.0, (expires - now) / 2),
            until_idle=True,
        )
        # The timer's checks held when it called this, and the build and the
        # wait above are awaits: an exit() meanwhile would otherwise be frozen
        # over a Continue button, and a re-send would be armed early. A close
        # that can still be undone arms the button when it is.
        if self._closed():
            self._arm_after_close = not self.is_finished()
            return
        if deadline is not None and not self._handoff_still_owed(deadline):
            return
        # The swapped tree is checked before the flag is set, with nothing
        # awaited from the wait to here: once armed, the view drops every
        # notification, so a button that fails here would leave a frozen
        # panel with no Continue button on it.
        previous = list(self.children)
        try:
            self.clear_items()
            self._install_refresh_button(button)
            self._check_placement()
        except Exception:
            self._put_children(previous)
            self._warn_refresh_button_failed()
            return
        self._armed_children = list(self.children)
        self._refresh_armed = True
        try:
            # The cooldown is cleared: an armed view has no background renders
            # left to pace, and the token expires refresh_warning_seconds from
            # here. The rate-limit window is not. A 429 queues this edit for the
            # boundary, and that deferred render ships the tree as it is, button
            # included.
            self._cooldown_not_before = 0.0
            await self.refresh()
            if self.refresh_degraded:
                # Once armed, nothing else can put the button on screen. A
                # transport failure carries no retry-after, so the retry runs on
                # a short fixed window of its own, not the rate-limit one.
                self._queue_deferred_refresh(_ARMING_RETRY_SECONDS)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            if e.code == 50035 and "emoji" in str(e).lower():
                logger.warning(
                    f"Refresh button emoji rejected by Discord "
                    f"({self.refresh_button_emoji!r}); retrying without emoji"
                )
                try:
                    # Built before the tree is cleared, so a render from another
                    # task during the build still finds the armed tree.
                    button = await await_maybe(self.build_refresh_button())
                    button.emoji = None
                    self.clear_items()
                    self._install_refresh_button(button)
                    self._armed_children = list(self.children)
                    await self.refresh()
                except Exception as retry_err:
                    logger.warning(f"Refresh button retry failed: {retry_err}")
            elif e.status >= 500:
                self._retry_arming(e)
            else:
                # By the T+810s arming point the webhook token is at or near
                # the 900s cliff, so an arming-edit failure is the expected
                # terminal state of an ephemeral view, not an error. DEBUG.
                logger.debug(f"Could not arm ephemeral refresh button: {e}")
        except Exception:
            logger.warning(
                f"Could not arm the ephemeral refresh button in {type(self).__name__}",
                exc_info=True,
            )

    def _retry_arming(self, error: discord.HTTPException) -> None:
        """Queue the arming edit again after a server error.

        discord.py has already retried the 5xx itself by then, and the token
        can still carry an edit, so the arming tries again as it does after a
        transport drop. Callers pass only a 5xx; past the cliff the edit
        answers 401, which ends the chain.
        """
        logger.debug(f"Arming edit hit a server error in {type(self).__name__}; retrying: {error}")
        self._queue_deferred_refresh(_ARMING_RETRY_SECONDS)

    def build_reopen_view(self, interaction: Interaction):
        """Build the view the Continue button sends in place of this one.

        Called on this view when the user presses Continue, the button an
        ephemeral panel shows before its 15-minute window closes. The view
        it returns is sent as a new ephemeral message under ``interaction``,
        the Continue click, and takes this view's place: its navigation
        stack, the selection ``get_nav_state()`` reports, its session, undo
        history, participants, and attached views carry over, and
        ``current_view`` on this view reaches it.

        The default constructs this view's class again with the keyword
        arguments it was constructed with and sets again what
        ``set_class_attribute()`` set on this view, the same reconstruction
        ``pop()`` performs. Override it when that is not the right view: a
        constructor with side effects (one that creates a record), a
        replacement that needs a live reference this view holds, or a
        different view to continue with. Return ``None`` to end the session
        instead, and ``on_reopen_failure()`` runs with ``error=None``. May be
        ``async def``.
        """
        kwargs = dict(getattr(self, "_init_kwargs", {}))
        kwargs.setdefault("user_id", self.user_id)
        kwargs.setdefault("guild_id", self.guild_id)
        # parent= is stripped from the captured kwargs like the other
        # framework-managed ones; a child that reads its parent while it is
        # built needs the link then, and the send attaches it.
        parent = self.parent
        if parent is not None and not parent._closed():
            kwargs.setdefault("parent", parent)
        new_view = type(self)(interaction=interaction, **kwargs)
        new_view._apply_class_overrides(self._class_overrides)
        return new_view

    async def _reopen_ephemeral(self, interaction: Interaction) -> None:
        """Spawn a fresh ephemeral view via a new interaction token.

        The click that triggers this callback carries its own 15-minute
        token, independent of the original send. That fresh token is used
        to send the replacement ephemeral; the old message is then cleaned
        up best-effort.

        A reopen continues the same panel past the token cliff, so the
        replacement takes this view's navigation stack and root accounting,
        the selection ``get_nav_state()`` captures, its session (and so its
        ``shared_data``), a Back button when it is mid-chain and
        ``auto_back_button`` is set, the undo timeline, participants, and
        attached views. The old message is deleted while its token lives;
        past that it can be neither deleted nor edited, and stays as it is.
        """
        if self._reopen_in_flight:
            self._log_dropped_click(
                (interaction.data or {}).get("custom_id"), "a Continue is already being handled"
            )
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except (*DISCORD_CALL_ERRORS, discord.InteractionResponded) as e:
                    logger.debug(
                        f"Reopen reentry ack failed in {type(self).__name__}: "
                        f"{describe_discord_error(e)}"
                    )
            return
        self._reopen_in_flight = True
        self._reopen_task = asyncio.current_task()
        self._reopen_settled.clear()
        try:
            await self._continue_into_replacement(interaction)
        finally:
            self._reopen_task = None
            self._reopen_settled.set()
            # A Continue that handed nothing on (cut off, refused, failed)
            # leaves the button working for the next click.
            if self._successor is None:
                self._reopen_in_flight = False

    async def _wait_out_reopen(self, method: str) -> None:
        """Wait for a Continue on this view running in another task.

        An ``exit()``, a timeout, or a navigation landing while the
        replacement is sent would act on this view and its attached views,
        and the replacement would go live without them. Once a Continue that
        sent its replacement has finished, the call finds a view that has
        handed its panel on, as after a push. A wait past the threshold the
        navigation waits use is logged, since a replacement whose send or
        ``build_reopen_view()`` never finishes holds the call with it.
        """
        from ._navigation import _NAVIGATION_WAIT_WARN_SECONDS

        if self._reopen_task is not None and self._reopen_task is not asyncio.current_task():
            watchdog = asyncio.get_running_loop().call_later(
                _NAVIGATION_WAIT_WARN_SECONDS,
                self._warn_long_reopen_wait,
                method,
                _NAVIGATION_WAIT_WARN_SECONDS,
            )
            try:
                await self._reopen_settled.wait()
            finally:
                watchdog.cancel()

    def _warn_long_reopen_wait(self, method: str, seconds: float) -> None:
        logger.warning(
            f"{method} on {type(self).__name__} has waited {seconds:g}s for the view's "
            f"Continue to finish. If build_reopen_view(), the replacement's on_load(), or "
            f"its send never finishes, or waits on this call (by awaiting a task that "
            f"closes or navigates this view), the wait never ends."
        )

    async def _continue_into_replacement(self, interaction: Interaction) -> None:
        """Build and send the replacement, then close this view: the Continue itself."""
        # Before anything mutates this view: the replacement rebuilds from
        # constructor kwargs, which cannot carry what was chosen since.
        nav_snapshot = self._capture_nav_state()

        try:
            new_view = await await_maybe(self.build_reopen_view(interaction))
            if new_view is self:
                # Continuing into the view being replaced would make it its
                # own successor.
                raise RuntimeError(
                    "build_reopen_view() returned the view it was replacing. Fix: "
                    "return a new view each time it is called."
                )
            if new_view is not None and not isinstance(new_view, _InteractionMixin):
                what = (
                    f"the class {new_view.__name__}"
                    if isinstance(new_view, type)
                    else f"a {type(new_view).__name__}"
                )
                raise TypeError(
                    f"build_reopen_view() returned {what}, not a CascadeUI view. Fix: "
                    f"return a new StatefulView or StatefulLayoutView instance, or None "
                    f"to end the session."
                )
            if new_view is not None and new_view._used_before():
                raise RuntimeError(
                    f"build_reopen_view() returned a {type(new_view).__name__} that was "
                    f"already sent, pushed, or closed. A view instance goes on one message "
                    f"once. Fix: return a new instance each time it is called."
                )
        except Exception as e:
            # Name which code failed: the default rebuild runs the view's own
            # __init__ with its captured kwargs, and naming an override the
            # operator never wrote would send them looking for it.
            default = (
                getattr(self.build_reopen_view, "__func__", None)
                is _InteractionMixin.build_reopen_view
            )
            source = "reconstruction from kwargs" if default else "build_reopen_view()"
            logger.error(f"Ephemeral reopen failed for {type(self).__name__} ({source}): {e}")
            self._reopen_in_flight = False
            await await_maybe(self.on_reopen_failure(interaction, error=e))
            return

        if new_view is None:
            # Cleared before the hook: an override that keeps the view alive
            # would otherwise send every later click to the reentry return.
            self._reopen_in_flight = False
            await await_maybe(self.on_reopen_failure(interaction, error=None))
            return

        # Before send() runs on_load, as pop() restores before on_load.
        new_view._nav_stack = list(self._nav_stack)
        new_view._instance_root_class = self._instance_root_class
        new_view._instance_root_scope = self._instance_root_scope
        new_view._instance_root_limit = self._instance_root_limit
        new_view._apply_nav_state(nav_snapshot)

        # SESSION_CREATED leaves the existing session as it is, since send()
        # runs before this view's exit empties it.
        if self.session_id:
            new_view.session_id = self.session_id

        # push() adds the Back button as a separate step that send() never
        # runs; added before the send, so the first render carries it.
        if new_view._nav_stack and new_view.auto_back_button:
            if getattr(new_view, "_auto_back_item", None) is None:
                new_view._add_back_button()

        # A reopen is a 1-for-1 swap for the dying instance, not a second
        # instance. Tell send()'s instance-limit check to exclude self, so a
        # limited view under replace policy does not self-replace mid-send and
        # tear itself down before the carries above transfer to the replacement.
        new_view._replacing_view_id = self.id

        # Carry the interaction into the new view's send() so the response
        # is the new ephemeral message.  send() will register and dispatch.
        new_view.interaction = interaction
        cancelled = False
        try:
            sent = await new_view.send(ephemeral=True)
        except asyncio.CancelledError:
            # A send cancelled after Discord accepted the message still leaves
            # the replacement live, so the panel is handed over before the
            # cancel goes on; one cancelled before that was rolled back.
            if new_view._torn_down():
                raise
            sent, cancelled = new_view._message, True
        except Exception as e:
            logger.error(f"Failed to send refreshed ephemeral: {e}")
            return
        if sent is None:
            # Refused (its on_pre_send(), an instance or participant limit) or
            # closed while it was sent: nothing took over, so this view keeps
            # its session and children, and Continue can be clicked again.
            logger.debug(
                f"Ephemeral reopen of {type(self).__name__} left no live replacement; "
                f"the current view stays."
            )
            return

        # The old message is this view's to delete, so the view lets go of it
        # for good: the gateway can report the deletion before the call
        # returns, and a render's edit can come back 404, and through
        # on_message_delete() either would close the replacement.
        from .base import _MESSAGE_DELETE, _HeldCancel

        message = self._message
        if message:
            shown = self._shown_components()
            self._message = None
            self._message_closed = max(self._message_closed, _MESSAGE_DELETE)

        # The replacement is live, so the hand-over always finishes: a step that
        # raises is logged and the next one runs, and a cancel waits for the
        # end, as the steps after a send's post do.
        cut = _HeldCancel()
        try:
            # The replacement takes the panel before anything is awaited, since
            # it can be clicked, closed, or pushed from already. None of these
            # suspends.
            await new_view._post_send_step(
                "carrying the participants over", lambda: self._carry_participants_to(new_view), cut
            )
            await new_view._post_send_step(
                "carrying the attached views over",
                lambda: self._carry_attachments_to(new_view),
                cut,
            )
            self._successor = new_view
            # Calls on this view waiting for the Continue go on, and find the
            # panel handed on; what is left is this view's own close.
            self._reopen_task = None
            self._reopen_settled.set()

            # After send() (the new row exists) and before exit() (the old one
            # still does).
            await new_view._post_send_step(
                "carrying the undo history over", lambda: self._carry_undo_stacks_to(new_view), cut
            )
            gone = True
            if message:
                gone = await new_view._post_send_step(
                    "deleting the message it replaced",
                    lambda: self._delete_replaced_message(message),
                    cut,
                )
            await new_view._post_send_step(
                "carrying undo steps taken since", lambda: self._settle_undo_carry(new_view), cut
            )

            # The library's own exit, which the handed-over warning leaves alone.
            self._library_exits += 1
            try:
                await new_view._post_send_step(
                    "closing the view it replaced", lambda: self.exit(delete_message=False), cut
                )
            finally:
                self._library_exits -= 1
            if not gone:
                await new_view._post_send_step(
                    "freezing the message it replaced",
                    lambda: self._close_left_message(message, True, shown, freeze=True),
                    cut,
                )
        finally:
            cut.release()
        if cut or cancelled:
            raise asyncio.CancelledError()

    async def _delete_replaced_message(self, message) -> bool:
        """Delete the message a Continue replaced, and say whether it is gone."""
        try:
            await self._bounded(message.delete())
        except discord.NotFound:
            pass
        except (*DISCORD_CALL_ERRORS, asyncio.TimeoutError):
            return False
        except RuntimeError as e:
            if not self._closed_session(e):
                raise
            return False
        return True
