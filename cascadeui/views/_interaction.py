# // ========================================( Modules )======================================== // #


import asyncio
import logging
import time
from typing import Optional

import discord
from discord import Interaction
from discord.ui import Item

from ..components.base import StatefulButton
from ..state.actions import ActionCreators
from ..utils.hooks import await_maybe
from ..utils.responses import (
    DISCORD_CALL_ERRORS,
    ack_backstop,
    describe_discord_error,
    elapsed_since,
    open_modal_safe,
    respond_safe,
    trailing_ack,
)

logger = logging.getLogger(__name__)


# // ========================================( Mixin )======================================== // #


class _InteractionMixin:
    """Interaction machinery for stateful views.

    Houses the auto-defer safety net, the serialized-callback wrapper,
    interaction response helpers (``respond``, ``open_modal``,
    ``_safe_defer``), and the ephemeral refresh handoff. None of these
    methods touch navigation, session, or instance-limit state directly;
    all cross-concern access goes through attributes on the composed
    ``_StatefulMixin``.

    Not a public class. ``_StatefulMixin`` inherits from this so the
    public ``StatefulView`` / ``StatefulLayoutView`` hierarchy is
    unchanged.
    """

    # // ==================( Auto-Defer Safety Net )================== // #

    async def _scheduled_task(self, item: Item, interaction: Interaction):
        """Override discord.py's internal dispatch to add auto-defer and serialization.

        Replicates View._scheduled_task with three additions:

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
           timer and the interaction goes unacknowledged.
        """
        try:
            item._refresh_state(interaction, interaction.data)  # type: ignore

            # ack_first: opt-in immediate ack, before the checks and the
            # callback. Reaches earlier than the auto-defer timer or any
            # user-side defer, so a callback that synchronously blocks the loop
            # still has its ack in flight. Trades the acting-view one-call
            # refresh path (edit-as-ack) for a guaranteed early ack.
            if self.ack_first:
                await self._safe_defer(interaction)

            # Arm the auto-defer timer BEFORE the access-control checks, not
            # after them. interaction_check is a documented override seam for
            # role-based access control, where a consumer runs an uncached
            # guild.fetch_member on the 3s interaction clock. Without the timer
            # armed first, a slow check has no ack backstop and Discord drops
            # the interaction (10062). A deferred interaction can still be
            # rejected: on_unauthorized routes through respond(), which posts
            # the rejection via followup once the slot is acked.
            defer_task = None
            if self.auto_defer and not interaction.response.is_done():
                defer_task = asyncio.create_task(self._auto_defer_timer(interaction))

            try:
                allow = await item._run_checks(interaction) and await self.interaction_check(
                    interaction
                )
                if not allow:
                    return

                if self.timeout:
                    self._BaseView__timeout_expiry = time.monotonic() + self.timeout  # type: ignore

                if self.serialize_interactions:
                    async with self._interaction_lock:
                        await item.callback(interaction)
                else:
                    await item.callback(interaction)
            finally:
                if defer_task is not None and not defer_task.done():
                    defer_task.cancel()

                # Acknowledge unresponded interactions so Discord does not
                # show "This interaction failed". Common when callbacks use
                # dispatch() → on_state_changed → refresh() which edits
                # the message via the channel endpoint, not the interaction.
                if self.auto_defer:
                    await trailing_ack(interaction, owner=self.__class__.__name__, log=logger)
        except Exception as e:
            return await await_maybe(self.on_error(interaction, e, item))

    _elapsed_since = staticmethod(elapsed_since)

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
        return await open_modal_safe(interaction, modal, fallback_message=fallback_message)

    async def _safe_defer(self, interaction: Interaction) -> None:
        """Defer the interaction if it hasn't been acknowledged yet.

        Mirrors how ``respond()`` absorbs the ``is_done()`` check for
        send operations. Prevents double-defer when auto-defer or
        ``serialize_interactions`` has already acknowledged the
        interaction before the callback runs.

        The ack is bounded by ``auto_defer_delay``: this call runs inside
        the interaction lock, and a Discord ack endpoint that stalls past
        the ack window would pin the lock on a hung socket. A defer that
        cannot land in time is useless anyway -- the auto-defer timer,
        running outside the lock, is the backstop -- so the stall is
        cancelled and swallowed rather than propagated.

        A failed ack is never propagated. ``NotFound`` (10062) means the
        interaction token is already gone -- expired, or a duplicate ack
        landed server-side -- so there is nothing left to acknowledge.
        The navigation-edit paths route their deferred ack through here;
        ``NotFound`` and any other HTTP ack failure are logged at debug and
        absorbed so a missed ack never reaches the callback's error path.
        """
        if not interaction.response.is_done():
            try:
                await asyncio.wait_for(
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
                # A ClientException subclass, on a different branch from
                # HTTPException entirely, so the
                # handler below cannot see it. The auto-defer timer can ack
                # inside this call's own await window: on 3.10/3.11
                # wait_for schedules the coroutine as a Task, which widens
                # that window enough to lose the race. The slot is acked
                # either way, which is all this method wanted.
                logger.debug(f"Ack defer raced an existing ack in {type(self).__name__}.")
            except DISCORD_CALL_ERRORS as e:
                logger.debug(
                    f"Ack defer failed in {type(self).__name__}: " f"{describe_discord_error(e)}"
                )

    # // ==================( Ephemeral Refresh )================== // #

    def _build_refresh_button(self) -> StatefulButton:
        """Build the button shown when an ephemeral session is about to expire.

        Override to customize beyond the ``refresh_button_*`` class attributes
        (e.g. row placement, custom_id). The callback must remain bound to
        :meth:`_reopen_ephemeral` for the handoff to work.
        """
        return StatefulButton(
            label=self.refresh_button_label,
            style=self.refresh_button_style,
            emoji=self.refresh_button_emoji,
            callback=self._reopen_ephemeral,
        )

    def _install_refresh_button(self, button: StatefulButton) -> None:
        """Install the refresh button into the cleared view.

        V1 adds it directly; the V2 mixin overrides this to wrap in ActionRow.
        """
        self.add_item(button)

    async def _schedule_ephemeral_refresh(self) -> None:
        """Background timer: arm the refresh button shortly before the
        original interaction token expires.

        Discord's interaction token lives for exactly 15 minutes (900s). The
        timer fires ``refresh_warning_seconds`` early so the swap edit still
        succeeds inside the token window.

        The wait is computed against ``_ephemeral_arm_deadline`` (stamped once
        at send), so a re-schedule after a failed-navigation rollback sleeps
        only the time remaining until the original deadline -- not a fresh
        window that would overshoot the 900s token cliff.
        """
        deadline = self._ephemeral_arm_deadline
        if deadline is not None:
            delay = max(1, deadline - time.monotonic())
        else:
            delay = max(1, 900 - self.refresh_warning_seconds)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self.is_finished() or self._refresh_armed or not self._message:
            return
        await self._arm_refresh_button()

    async def _arm_refresh_button(self) -> None:
        """Replace the view's children with a single refresh button.

        Best-effort: any error during the swap is logged and swallowed. The
        worst case is that the user sees the original (now-stale) view until
        their client times it out -- same as today's behavior without the flag.

        If Discord rejects the button's emoji (error 50035), retries once
        without the emoji so a bad user-supplied ``refresh_button_emoji``
        does not silently break the handoff.
        """
        if self._refresh_armed:
            return
        self._refresh_armed = True
        try:
            self.clear_items()
            self._install_refresh_button(self._build_refresh_button())
            # The cooldown paces background re-renders; an armed view has no
            # more of those, and this edit answers to a hard deadline: the
            # webhook token expires 90 seconds from here. Clearing the window
            # keeps the handoff from queueing behind the pacing a long
            # ``refresh_cooldown_ms`` imposes.
            #
            # Discord's rate-limit window is NOT cleared. A 429 here is the
            # one case the freeze in _handle_state_notification cannot repair:
            # the flag above is already set, so no notification will get
            # another edit through. It survives because _handle_rate_limit
            # queues the discarded edit to ship at the backoff boundary, and
            # the deferred render honors the armed flag by shipping the tree
            # as-is instead of rebuilding over the button.
            self._cooldown_not_before = 0.0
            await self.refresh()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            if e.code == 50035 and "emoji" in str(e).lower():
                logger.warning(
                    f"Refresh button emoji rejected by Discord "
                    f"({self.refresh_button_emoji!r}); retrying without emoji"
                )
                try:
                    self.clear_items()
                    button = self._build_refresh_button()
                    button.emoji = None
                    self._install_refresh_button(button)
                    await self.refresh()
                except Exception as retry_err:
                    logger.warning(f"Refresh button retry failed: {retry_err}")
            else:
                # By the T+810s arming point the webhook token is at or near
                # the 900s cliff, so an arming-edit failure is the expected
                # terminal state of an ephemeral view, not an error. DEBUG.
                logger.debug(f"Could not arm ephemeral refresh button: {e}")
        except Exception as e:
            logger.debug(f"Could not arm ephemeral refresh button: {e}")

    async def _reopen_ephemeral(self, interaction: Interaction) -> None:
        """Spawn a fresh ephemeral view via a new interaction token.

        The click that triggers this callback carries its own 15-minute
        token, independent of the original send. That fresh token is used
        to send the replacement ephemeral; the old message is then cleaned
        up best-effort.
        """
        if self._reopen_in_flight:
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

        # Capture selection state off the live instance before anything
        # mutates it. The replacement rebuilds from constructor kwargs, so
        # anything chosen since construction (page, tab, tier) must ride
        # this snapshot -- the same view_state hand-off pop() performs
        # from its nav-stack entry.
        nav_snapshot = self._capture_nav_state()

        # Construct the replacement view. _reopen_factory wins when set;
        # otherwise fall back to the captured push/pop kwargs snapshot.
        try:
            if self._reopen_factory is not None:
                new_view = self._reopen_factory()
                if asyncio.iscoroutine(new_view):
                    new_view = await new_view
            else:
                cls = type(self)
                kwargs = dict(getattr(self, "_init_kwargs", {}))
                kwargs.setdefault("user_id", self.user_id)
                kwargs.setdefault("guild_id", self.guild_id)
                new_view = cls(interaction=interaction, **kwargs)
        except Exception as e:
            # Name which of the two paths failed. Without a factory the
            # failing code is the view's own ``__init__`` or its captured
            # kwargs, and blaming a "refresh factory" sends the operator
            # looking for something they never wrote.
            source = (
                "reopen factory"
                if self._reopen_factory is not None
                else "reconstruction from kwargs"
            )
            logger.error(f"Ephemeral reopen failed for {type(self).__name__} ({source}): {e}")
            self._reopen_in_flight = False
            await await_maybe(self.on_reopen_failure(interaction, error=e))
            return

        if new_view is None:
            # Cleared before the hook, matching the two sibling failure
            # paths. The default ``on_reopen_failure`` exits, so the flag
            # would not be read again; an override that keeps the view
            # alive leaves every later click hitting the reentry
            # early-return above with no reopen ever in flight.
            self._reopen_in_flight = False
            await await_maybe(self.on_reopen_failure(interaction, error=None))
            return

        # Carry navigation identity onto the replacement before send() runs
        # on_load, matching pop's restore-before-on_load contract. A
        # mid-chain sub-view can reopen here (the arm deadline rides the
        # navigation chain), so without the carry its stack, root-class
        # accounting, and post-construction selection all reset to a
        # fresh-root default.
        new_view._nav_stack = list(self._nav_stack)
        new_view._instance_root_class = self._instance_root_class
        new_view._apply_nav_state(nav_snapshot)

        # Carry the session so the replacement rejoins the original session
        # rather than deriving a fresh isolated one: a reopen is the same
        # logical session continuing past the token cliff, not a new open, so
        # shared_data survives. SESSION_CREATED no-ops on the existing session
        # (send() runs before the old view's exit empties it), preserving its
        # shared_data.
        if self.session_id:
            new_view.session_id = self.session_id

        # The factory must survive every cycle, not just the first: left
        # unset, the NEXT reopen falls back to the kwargs path the factory
        # exists to avoid. A factory that installs its own wins.
        if new_view._reopen_factory is None:
            new_view._reopen_factory = self._reopen_factory

        # Re-inject the Back button for a mid-chain reopen. send() builds
        # from build_ui/on_load, which never adds it -- push() does that as
        # a separate step. Added before send() so the first render carries
        # it (the same button-then-on_load order push() uses); pattern
        # rebuild seams re-add it via _restore_navigation_artifacts.
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
        try:
            await new_view.send(ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to send refreshed ephemeral: {e}")
            self._reopen_in_flight = False
            return

        # Transfer the undo/redo timeline onto the replacement's state row.
        # Runs after send() (the new row exists) and before exit() (the old
        # row still does). Same VIEW_UPDATED shape _navigate_to uses for
        # push/pop, so the transfer goes through the reducer rather than
        # writing into the live state["views"] row in place.
        await self._carry_undo_stacks_to(new_view)

        # Carry participants onto the replacement so a multi-user ephemeral
        # keeps its membership across the reopen (mirrors _navigate_to's carry).
        # Runs after send() so new_view is registered; the membership guard
        # keeps it idempotent against any the replacement already auto-claimed.
        self._carry_participants_to(new_view)

        # Re-parent this view's own children onto the replacement -- the
        # same hand-off _settle_navigation performs after a confirmed
        # navigation edit. Without it, exit() below cascades into
        # _cleanup_attached_children and deletes children that should
        # outlive the reopen. attach_child prunes the source list as it
        # re-parents, so the exit cascade finds nothing.
        self._carry_attachments_to(new_view)

        # Best-effort cleanup of the old message. Inside the original token
        # window this succeeds; past 15:00 it fails silently and the stale
        # panel becomes a harmless orphan the user can dismiss.
        if self._message:
            try:
                await self._bounded(self._message.delete())
            except (discord.NotFound, *DISCORD_CALL_ERRORS, asyncio.TimeoutError):
                try:
                    await self._bounded(self._message.edit(**self._freeze_edit_kwargs()))
                except (discord.NotFound, *DISCORD_CALL_ERRORS, asyncio.TimeoutError):
                    pass

        await self.exit()
