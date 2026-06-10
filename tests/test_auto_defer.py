"""Tests for auto-defer safety net on StatefulView."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from helpers import make_interaction as _make_interaction

from cascadeui.state.singleton import get_store
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
            # Simulate responding immediately — flip is_done so timer skips
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
