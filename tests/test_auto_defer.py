"""Tests for auto-defer safety net on StatefulView."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import pytest
from helpers import make_interaction as _make_interaction

from cascadeui.components.inputs import Modal, TextInput
from cascadeui.state.singleton import get_store
from cascadeui.utils.responses import open_modal_safe, respond_safe
from cascadeui.views.view import StatefulView


def _make_item(callback):
    """Create a mock discord.ui item with the given callback."""
    item = MagicMock()
    item.callback = callback
    item._run_checks = AsyncMock(return_value=True)
    item._refresh_state = MagicMock()
    return item


def _http_error(status, code):
    """Build a ``discord.HTTPException`` carrying a specific status/code.

    The post-callback defer classifies errors by ``code`` (40060 is the
    benign already-acknowledged race), so tests need to control it directly.
    """

    class _Err(discord.HTTPException):
        def __init__(self):
            Exception.__init__(self, str(status))
            self.status = status
            self.code = code
            self.retry_after = 0

    return _Err()


# // ========================================( Timer Fires )======================================== // #


class TestAutoDeferFires:
    """Auto-defer timer fires when callbacks take longer than the delay threshold."""

    async def test_timer_defers_slow_callback(self):
        """Auto-defer fires when callback hasn't responded in time."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 0.05  # 50ms for fast tests
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        # Callback that sleeps longer than the defer delay
        async def slow_callback(inter):
            await asyncio.sleep(0.15)
            # By this point, auto-defer should have fired

        item = _make_item(slow_callback)
        await view._scheduled_task(item, interaction)

        interaction.response.defer.assert_called_once_with()

    async def test_timer_defers_without_ephemeral(self):
        """Auto-defer calls defer() without ephemeral (component interactions ignore it)."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 0.05
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        async def slow_callback(inter):
            await asyncio.sleep(0.15)

        item = _make_item(slow_callback)
        await view._scheduled_task(item, interaction)

        interaction.response.defer.assert_called_once_with()

    async def test_custom_delay_honored(self):
        """A shorter auto_defer_delay fires sooner."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 0.02
        view.owner_only = False

        interaction = _make_interaction(is_done=False)
        fired = False

        original_defer = interaction.response.defer

        async def tracking_defer(**kwargs):
            nonlocal fired
            fired = True
            return await original_defer(**kwargs)

        interaction.response.defer = tracking_defer

        async def slow_callback(inter):
            await asyncio.sleep(0.1)

        item = _make_item(slow_callback)
        await view._scheduled_task(item, interaction)

        assert fired


# // ========================================( Timer Arms Before Checks )======================================== // #


class TestAutoDeferArmsBeforeChecks:
    """The auto-defer timer is armed before ``interaction_check``, so a slow
    access-control check (an uncached ``guild.fetch_member`` in a role-based
    override) still gets an ack backstop inside the 3s window.
    """

    async def test_timer_fires_during_slow_interaction_check(self):
        """A slow ``interaction_check`` triggers the auto-defer timer while the
        check is still running, proving the timer is armed before the check.

        Under the old ordering (timer armed after the checks) no timer exists
        while the check runs, so ``defer`` has not been called when the check
        completes.
        """

        class SlowCheckView(StatefulView):
            async def interaction_check(self, interaction):
                # Simulate an uncached member/role lookup on the 3s clock.
                await asyncio.sleep(0.15)
                # Record whether the timer already acked mid-check.
                self._deferred_during_check = interaction.response.defer.called
                return True

        view = SlowCheckView(interaction=_make_interaction())
        view.auto_defer_delay = 0.05  # fires well before the 0.15s check ends
        view.owner_only = False
        view._deferred_during_check = None

        interaction = _make_interaction(is_done=False)

        async def fast_callback(inter):
            pass

        item = _make_item(fast_callback)
        await view._scheduled_task(item, interaction)

        assert view._deferred_during_check is True

    async def test_rejected_check_still_acks_via_post_callback_defer(self):
        """A silent-False check (an override that rejects without responding)
        now leaves the interaction acked, not stranded: the widened finally
        posts a defer once the check returns False.
        """

        class RejectView(StatefulView):
            async def interaction_check(self, interaction):
                return False  # reject, send nothing

        view = RejectView(interaction=_make_interaction())
        view.auto_defer_delay = 10  # timer would not fire on its own
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        called = False

        async def callback(inter):
            nonlocal called
            called = True

        item = _make_item(callback)
        await view._scheduled_task(item, interaction)

        assert called is False  # rejected: callback never ran
        interaction.response.defer.assert_called_once()  # but the interaction was acked


# // ========================================( Ack First )======================================== // #


class TestAckFirst:
    """``ack_first`` acks the interaction before the checks and callback run,
    so a callback that synchronously blocks the loop still lands its ack.
    """

    async def test_ack_first_defers_before_callback(self):
        class _AckFirst(StatefulView):
            ack_first = True

        view = _AckFirst(interaction=_make_interaction())
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        async def flip_done(*args, **kwargs):
            interaction.response.is_done.return_value = True

        interaction.response.defer = AsyncMock(side_effect=flip_done)

        acked_before_callback = {}

        async def callback(inter):
            acked_before_callback["value"] = inter.response.is_done()

        item = _make_item(callback)
        await view._scheduled_task(item, interaction)

        assert acked_before_callback["value"] is True

    async def test_default_does_not_pre_defer(self):
        """The default (ack_first=False) leaves the response slot open for the
        callback, preserving the acting-view one-call refresh path.
        """
        view = StatefulView(interaction=_make_interaction())
        view.owner_only = False
        view.auto_defer_delay = 10  # timer would not fire during the fast callback

        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock()

        acked_before_callback = {}

        async def callback(inter):
            acked_before_callback["value"] = inter.response.is_done()

        item = _make_item(callback)
        await view._scheduled_task(item, interaction)

        assert acked_before_callback["value"] is False


# // ========================================( Post-Callback Defer )======================================== // #


class TestPostCallbackDefer:
    """Post-callback defer catches fast callbacks that never touch the interaction response."""

    async def test_fast_callback_deferred_after_completion(self):
        """Fast callbacks that don't respond are deferred after completion.

        This covers the dispatch → on_state_changed → refresh() pattern
        where the callback edits the message via the channel endpoint
        (not the interaction response) and finishes before the timer fires.
        """
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 10  # Timer would never fire
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        async def dispatch_style_callback(inter):
            # Simulates: self.dispatch(...) → on_state_changed → refresh()
            # Uses message.edit(), never touches interaction.response
            pass

        item = _make_item(dispatch_style_callback)
        await view._scheduled_task(item, interaction)

        # Post-callback defer fires because the callback didn't respond
        interaction.response.defer.assert_called_once()

    async def test_post_defer_skipped_when_callback_responds(self):
        """Post-callback defer does not fire when the callback already responded."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 10
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        async def responding_callback(inter):
            await inter.response.defer()

        item = _make_item(responding_callback)
        await view._scheduled_task(item, interaction)

        # Only the callback's own defer call, not a post-callback one
        interaction.response.defer.assert_called_once()

    async def test_post_defer_skipped_when_auto_defer_disabled(self):
        """With auto_defer=False, post-callback defer does not fire."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer = False
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        async def silent_callback(inter):
            pass

        item = _make_item(silent_callback)
        await view._scheduled_task(item, interaction)

        interaction.response.defer.assert_not_called()

    async def test_post_defer_40060_race_logged_at_debug(self, caplog):
        """A 40060 (already acknowledged) on the post-callback defer is the
        benign cancellation race the acting-view fast path can produce.
        Logged at debug, never warning, so a successful fast-path edit does
        not spam warnings on every interaction.
        """
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 10
        view.owner_only = False

        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock(side_effect=_http_error(400, 40060))

        async def silent_callback(inter):
            pass

        item = _make_item(silent_callback)
        with caplog.at_level(logging.DEBUG, logger="cascadeui.views._interaction"):
            await view._scheduled_task(item, interaction)

        assert any(
            rec.levelno == logging.DEBUG and "40060" in rec.getMessage() for rec in caplog.records
        )
        assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)

    async def test_post_defer_genuine_failure_logged_at_warning(self, caplog):
        """A non-40060 HTTP failure is a real ack failure -- the user saw an
        interaction-failed toast -- so it surfaces at warning with the
        status and code instead of vanishing at debug.
        """
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 10
        view.owner_only = False

        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock(side_effect=_http_error(404, 10062))

        async def silent_callback(inter):
            pass

        item = _make_item(silent_callback)
        with caplog.at_level(logging.WARNING, logger="cascadeui.views._interaction"):
            await view._scheduled_task(item, interaction)

        assert any(
            rec.levelno == logging.WARNING and "status=404" in rec.getMessage()
            for rec in caplog.records
        )


# // ========================================( Timer Skipped )======================================== // #


class TestAutoDeferSkipped:
    """Auto-defer timer is skipped or cancelled when the callback responds first."""

    async def test_timer_skipped_when_callback_responds_fast(self):
        """If the callback responds before the timer, defer is not called by auto-defer."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 0.1
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        async def fast_callback(inter):
            # Simulate responding immediately: flip is_done so timer skips
            await inter.response.defer()
            inter.response.is_done.return_value = True

        item = _make_item(fast_callback)
        await view._scheduled_task(item, interaction)

        # defer was called once by the callback itself, not by auto-defer
        interaction.response.defer.assert_called_once()

    async def test_timer_skipped_when_auto_defer_disabled(self):
        """With auto_defer=False, no auto-defer fires even on slow callbacks."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer = False
        view.auto_defer_delay = 0.01
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        async def slow_callback(inter):
            await asyncio.sleep(0.05)

        item = _make_item(slow_callback)
        await view._scheduled_task(item, interaction)

        interaction.response.defer.assert_not_called()

    async def test_timer_cancelled_on_fast_completion(self):
        """The timer task is cancelled when the callback finishes quickly."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 10  # Would never fire naturally
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        async def fast_callback(inter):
            await inter.response.defer()
            inter.response.is_done.return_value = True

        item = _make_item(fast_callback)
        await view._scheduled_task(item, interaction)

        # Reaching this point confirms the timer was cancelled
        interaction.response.defer.assert_called_once()


# // ========================================( Error Handling )======================================== // #


class TestAutoDeferErrorHandling:
    """Auto-defer handles expired interactions and callback errors gracefully."""

    async def test_handles_expired_interaction_gracefully(self):
        """If the interaction expired, the auto-defer timer catches the error."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 0.01
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        import discord

        interaction.response.defer = AsyncMock(side_effect=discord.NotFound(MagicMock(), ""))

        # Timer should not crash
        await view._auto_defer_timer(interaction)

    async def test_ack_miss_logs_warning_with_elapsed(self, caplog):
        """When the timer's own defer hits 10062 (the interaction expired before
        the ack landed), it surfaces at WARNING with elapsed-since-creation, so
        an operator sees the ack missed and by how much, not only the downstream
        post-callback echo.
        """
        import datetime

        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 0.01
        view.owner_only = False

        interaction = _make_interaction(is_done=False)
        interaction.created_at = discord.utils.utcnow() - datetime.timedelta(seconds=4)
        interaction.response.defer = AsyncMock(side_effect=discord.NotFound(MagicMock(), ""))

        with caplog.at_level(logging.WARNING, logger="cascadeui.views._interaction"):
            await view._auto_defer_timer(interaction)

        assert any(
            rec.levelno == logging.WARNING and "since interaction creation" in rec.getMessage()
            for rec in caplog.records
        )

    async def test_safe_defer_bounds_a_stalled_ack(self):
        """``_safe_defer`` cancels a stalled defer at ``auto_defer_delay`` and
        swallows the timeout, so a hung Discord ack endpoint cannot pin the
        interaction lock on the socket lifetime.
        """
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 0.05
        view.owner_only = False

        interaction = _make_interaction(is_done=False)

        async def stall(*args, **kwargs):
            await asyncio.sleep(60)

        interaction.response.defer = AsyncMock(side_effect=stall)

        before = time.monotonic()
        await view._safe_defer(interaction)  # must return, not hang
        elapsed = time.monotonic() - before

        assert elapsed < 2.0  # bounded by auto_defer_delay, not the 60s stall

    async def test_safe_defer_swallows_dead_interaction(self):
        """A dead interaction (10062) on the ack must not propagate. The token
        is already gone, so there is nothing to acknowledge -- the navigation
        deferred-edit path routes its ack through here, and a raised NotFound
        would surface a recoverable ack miss as an unhandled callback error.
        """
        import discord

        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 1.0
        view.owner_only = False

        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock(side_effect=discord.NotFound(MagicMock(), ""))

        await view._safe_defer(interaction)  # must not raise

    async def test_safe_defer_swallows_other_http_ack_failure(self):
        """Any non-NotFound HTTP ack failure (e.g. 40060 already-acked) is also
        absorbed: a defer that cannot land is useless, and the auto-defer timer
        outside the lock is the backstop.
        """
        import discord

        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 1.0
        view.owner_only = False

        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=400), {"code": 40060})
        )

        await view._safe_defer(interaction)  # must not raise

    async def test_callback_error_still_triggers_on_error(self):
        """When the callback raises, on_error is called even with auto-defer active."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 1.0
        view.owner_only = False
        view.on_error = AsyncMock()

        interaction = _make_interaction(is_done=False)

        async def failing_callback(inter):
            raise ValueError("test error")

        item = _make_item(failing_callback)
        await view._scheduled_task(item, interaction)

        view.on_error.assert_called_once()
        args = view.on_error.call_args[0]
        assert args[0] is interaction
        assert isinstance(args[1], ValueError)


# // ========================================( Default Config )======================================== // #


class TestAutoDeferDefaults:
    """Default auto-defer configuration values and subclass overrides."""

    async def test_default_auto_defer_enabled(self):
        """Auto-defer is enabled by default."""
        view = StatefulView(interaction=_make_interaction())
        assert view.auto_defer is True

    async def test_default_delay(self):
        """Default delay is 2.5 seconds."""
        view = StatefulView(interaction=_make_interaction())
        assert view.auto_defer_delay == 2.5

    async def test_subclass_can_disable(self):
        """Subclass can set auto_defer = False."""

        class NoAutoDefer(StatefulView):
            auto_defer = False

        view = NoAutoDefer(interaction=_make_interaction())
        assert view.auto_defer is False


# // ========================================( Respond Helper )======================================== // #


class TestRespond:
    """respond() routes to interaction.response or followup based on is_done state."""

    async def test_uses_response_when_not_done(self):
        """respond() routes to interaction.response.send_message when available."""
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=False)

        await view.respond(interaction, "hello", ephemeral=True)

        interaction.response.send_message.assert_called_once_with("hello", ephemeral=True)
        interaction.followup.send.assert_not_called()

    async def test_uses_followup_when_done(self):
        """respond() falls back to interaction.followup.send when already deferred."""
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=True)

        await view.respond(interaction, "hello", ephemeral=True)

        interaction.followup.send.assert_called_once_with("hello", ephemeral=True)
        interaction.response.send_message.assert_not_called()

    async def test_forwards_kwargs(self):
        """respond() passes extra kwargs (embed, view, etc.) through."""
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=False)
        mock_embed = MagicMock()

        await view.respond(interaction, embed=mock_embed, ephemeral=True)

        interaction.response.send_message.assert_called_once_with(
            None, ephemeral=True, embed=mock_embed
        )

    async def test_forwards_kwargs_to_followup(self):
        """respond() passes extra kwargs through to followup path too."""
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=True)
        mock_embed = MagicMock()

        await view.respond(interaction, embed=mock_embed, ephemeral=True)

        interaction.followup.send.assert_called_once_with(None, ephemeral=True, embed=mock_embed)

    async def test_non_ephemeral(self):
        """respond() works for public (non-ephemeral) responses."""
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=False)

        await view.respond(interaction, "public message")

        interaction.response.send_message.assert_called_once_with("public message", ephemeral=False)

    async def test_content_only(self):
        """respond() works with just content, no kwargs."""
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=True)

        await view.respond(interaction, "fallback")

        interaction.followup.send.assert_called_once_with("fallback", ephemeral=False)


class TestOpenModal:
    """open_modal() sends modal or ephemeral fallback based on response slot availability."""

    async def test_sends_modal_when_slot_available(self):
        """open_modal() sends the modal when the response slot is free."""
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=False)
        modal = MagicMock()

        result = await view.open_modal(interaction, modal)

        assert result is True
        interaction.response.send_modal.assert_called_once_with(modal)
        interaction.followup.send.assert_not_called()

    async def test_sends_fallback_when_slot_consumed(self):
        """open_modal() sends an ephemeral fallback when already deferred."""
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=True)
        modal = MagicMock()

        result = await view.open_modal(interaction, modal)

        assert result is False
        interaction.response.send_modal.assert_not_called()
        interaction.followup.send.assert_called_once_with(
            "Could not open the dialog. Please try again.", ephemeral=True
        )

    async def test_custom_fallback_message(self):
        """open_modal() uses custom fallback text when provided."""
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=True)
        modal = MagicMock()

        await view.open_modal(interaction, modal, fallback_message="Try later.")

        interaction.followup.send.assert_called_once_with("Try later.", ephemeral=True)


class TestRespondDeleteAfter:
    """``delete_after`` works on both respond paths.

    ``Webhook.send`` does not take it, and which path runs depends on
    whether the ack backstop fired first, outside the caller's control.
    """

    async def test_send_message_path_forwards_natively(self):
        from cascadeui.utils.responses import respond_safe

        interaction = _make_interaction(is_done=False)
        await respond_safe(interaction, "hi", delete_after=5)

        assert interaction.response.send_message.await_args.kwargs["delete_after"] == 5

    async def test_followup_path_deletes_on_a_timer(self):
        from cascadeui.utils.responses import respond_safe

        interaction = _make_interaction(is_done=True)
        message = MagicMock()
        message.delete = AsyncMock()
        interaction.followup.send = AsyncMock(return_value=message)

        await respond_safe(interaction, "hi", delete_after=0.02)

        # delete_after is consumed here, not handed to a method without it.
        assert "delete_after" not in interaction.followup.send.await_args.kwargs
        assert interaction.followup.send.await_args.kwargs["wait"] is True
        await asyncio.sleep(0.08)
        assert message.delete.await_count == 1

    async def test_followup_path_without_delete_after_is_unchanged(self):
        from cascadeui.utils.responses import respond_safe

        interaction = _make_interaction(is_done=True)
        interaction.followup.send = AsyncMock()

        await respond_safe(interaction, "hi", ephemeral=True)

        assert "wait" not in interaction.followup.send.await_args.kwargs


class TestSafeDeferSwallowsInteractionResponded:
    """``InteractionResponded`` is a sibling of ``HTTPException``.

    The auto-defer timer can ack inside this call's own await window, and
    on 3.10/3.11 ``wait_for`` widens that window enough to lose the race.
    The slot ends up acked either way, which is all ``_safe_defer`` wanted,
    so its docstring promise that a failed ack is never propagated has to
    hold for this type too.
    """

    async def test_interaction_responded_is_absorbed(self):
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock(
            side_effect=discord.InteractionResponded(MagicMock())
        )

        await view._safe_defer(interaction)

    def test_interaction_responded_is_not_an_http_exception(self):
        """The reason the explicit clause is needed rather than inherited."""
        assert not issubclass(discord.InteractionResponded, discord.HTTPException)


class TestRespondersDegradeOnTransportFailure:
    """A reply that never left the host must not become an error card.

    These run inside component callbacks, where an escaping exception
    reaches ``on_error``. A dropped notice is the cheaper failure.
    """

    async def test_respond_safe_swallows_and_does_not_retry_on_followup(self, caplog):
        interaction = _make_interaction()
        interaction.response.send_message.side_effect = aiohttp.ClientOSError(104, "reset")

        with caplog.at_level(logging.WARNING):
            await respond_safe(interaction, "hello")

        # Not retried: whether the first send landed is unknowable here, and a
        # duplicate reply is worse than a missing transient notice.
        interaction.followup.send.assert_not_called()
        assert "did not reach Discord" in caplog.text

    async def test_respond_safe_swallows_a_failing_followup(self, caplog):
        interaction = _make_interaction()
        interaction.response.is_done.return_value = True
        interaction.followup.send.side_effect = aiohttp.ClientOSError(104, "reset")

        with caplog.at_level(logging.WARNING):
            await respond_safe(interaction, "hello")

        assert "did not reach Discord" in caplog.text

    async def test_open_modal_safe_reports_non_delivery(self, caplog):
        interaction = _make_interaction()
        interaction.response.send_modal.side_effect = aiohttp.ClientOSError(104, "reset")
        modal = Modal(title="T", inputs=[TextInput(label="x")])

        with caplog.at_level(logging.WARNING):
            opened = await open_modal_safe(interaction, modal)

        assert opened is False
        # The fallback notice would travel the same broken connection.
        interaction.followup.send.assert_not_called()
        assert "did not reach Discord" in caplog.text
