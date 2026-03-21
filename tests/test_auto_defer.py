"""Tests for auto-defer safety net on StatefulView."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cascadeui.views.base import StatefulView
from cascadeui.state.singleton import get_store
from helpers import make_interaction as _make_interaction


def _make_item(callback):
    """Create a mock discord.ui item with the given callback."""
    item = MagicMock()
    item.callback = callback
    item._run_checks = AsyncMock(return_value=True)
    item._refresh_state = MagicMock()
    return item


# // ========================================( Timer Fires )======================================== // #


class TestAutoDeferFires:
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


# // ========================================( Timer Skipped )======================================== // #


class TestAutoDeferSkipped:
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

        # If we get here without hanging, the timer was cancelled
        interaction.response.defer.assert_called_once()


# // ========================================( Error Handling )======================================== // #


class TestAutoDeferErrorHandling:
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
