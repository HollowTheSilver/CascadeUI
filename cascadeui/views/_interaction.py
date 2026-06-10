# // ========================================( Modules )======================================== // #


import asyncio
import logging
import time
from typing import Optional

import discord
from discord import Interaction
from discord.ui import Item

from ..components.base import StatefulButton

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

            allow = await item._run_checks(interaction) and await self.interaction_check(
                interaction
            )
            if not allow:
                return

            if self.timeout:
                self._BaseView__timeout_expiry = time.monotonic() + self.timeout  # type: ignore

            defer_task = None
            if self.auto_defer:
                defer_task = asyncio.create_task(self._auto_defer_timer(interaction))

            try:
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
                if self.auto_defer and not interaction.response.is_done():
                    try:
                        await interaction.response.defer()
                    except discord.HTTPException as e:
                        # 40060 means Discord acknowledged a request the
                        # acting-view fast path cancelled locally (cancellation
                        # race) -- the interaction is already acked, so this is
                        # benign and routine. Any other status is a genuine ack
                        # failure the user saw as an interaction-failed toast.
                        if e.code == 40060:
                            logger.debug(
                                f"Post-callback defer raced an existing ack in "
                                f"{self.__class__.__name__} (40060)"
                            )
                        else:
                            logger.warning(
                                f"Post-callback defer failed in {self.__class__.__name__}: "
                                f"status={e.status} code={e.code}"
                            )
                    except Exception:
                        logger.debug(
                            f"Post-callback defer failed in {self.__class__.__name__} "
                            f"(interaction may have expired)"
                        )
        except Exception as e:
            return await self.on_error(interaction, e, item)

    async def _auto_defer_timer(self, interaction: Interaction):
        """Background timer that defers the interaction if the callback hasn't responded."""
        try:
            await asyncio.sleep(self.auto_defer_delay)
            if not interaction.response.is_done():
                await interaction.response.defer()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug(f"Auto-defer failed for interaction in {self.__class__.__name__}")

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
        if not interaction.response.is_done():
            await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)
        else:
            await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)

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
        """
        if not interaction.response.is_done():
            await interaction.response.send_modal(modal)
            return True
        else:
            msg = fallback_message or "Could not open the dialog. Please try again."
            await interaction.followup.send(msg, ephemeral=True)
            return False

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
        """
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
                logger.warning(f"Could not arm ephemeral refresh button: {e}")
        except Exception as e:
            logger.warning(f"Could not arm ephemeral refresh button: {e}")

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
                except discord.HTTPException:
                    pass
            return
        self._reopen_in_flight = True

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
            logger.error(f"Refresh factory failed for {type(self).__name__}: {e}")
            self._reopen_in_flight = False
            await self.on_reopen_failure(interaction, error=e)
            return

        if new_view is None:
            await self.on_reopen_failure(interaction, error=None)
            return

        # Carry the interaction into the new view's send() so the response
        # is the new ephemeral message.  send() will register and dispatch.
        new_view.interaction = interaction
        try:
            await new_view.send(ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to send refreshed ephemeral: {e}")
            self._reopen_in_flight = False
            return

        # Migrate the tracked-child slot from this instance to the refreshed
        # one. Without this transfer, a parent that called attach_child(self)
        # would still hold a reference to the (about-to-exit) old view, and
        # its _cleanup_attached_children pass would silently skip the new view as
        # "untracked" -- leaving an orphan ephemeral after the parent ends.
        parent = self._attached_to
        if parent is not None and not parent.is_finished():
            parent.attach_child(new_view)
            try:
                parent._attached_children.remove(self)
            except ValueError:
                pass
        self._attached_to = None

        # Best-effort cleanup of the old message. Inside the original token
        # window this succeeds; past 15:00 it fails silently and the stale
        # panel becomes a harmless orphan the user can dismiss.
        if self._message:
            try:
                await self._bounded(self._message.delete())
            except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError):
                try:
                    await self._bounded(self._message.edit(view=self))
                except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError):
                    pass

        await self.exit()
