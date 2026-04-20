"""Tests for automatic view cleanup on message deletion."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cascadeui.state.store import StateStore
from cascadeui.views.view import StatefulView


# // ========================================( Helpers )======================================== // #


def _make_bot():
    """Create a mock bot that captures listener registrations."""
    bot = MagicMock()
    bot._listeners = {}

    def _listen(event_name):
        def decorator(func):
            bot._listeners[event_name] = func
            return func
        return decorator

    bot.listen = _listen
    return bot


def _make_view(store, *, message_id=12345, user_id=100, guild_id=200):
    """Create a minimal StatefulView with a mock message attached."""

    class _TestView(StatefulView):
        pass

    with patch.object(StatefulView, "__init_subclass__", lambda **kw: None):
        pass

    view = _TestView(user_id=user_id, guild_id=guild_id, state_store=store)
    view._message = MagicMock(id=message_id)
    store._register_view(view)
    return view


# // ========================================( Install )======================================== // #


class TestInstallMessageCleanup:
    """_install_message_cleanup registers gateway listeners idempotently."""
    def test_sets_flag(self):
        store = StateStore()
        bot = _make_bot()
        assert not store._cleanup_listener_installed

        store._install_message_cleanup(bot)

        assert store._cleanup_listener_installed

    def test_registers_both_listeners(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)

        assert "on_raw_message_delete" in bot._listeners
        assert "on_raw_bulk_message_delete" in bot._listeners

    def test_idempotent(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        store._install_message_cleanup(bot)

        # listen() was only called twice (once per event), not four times.
        assert len(bot._listeners) == 2


# // ========================================( Single Delete )======================================== // #


class TestSingleMessageDelete:
    """on_raw_message_delete triggers on_message_delete for matching views."""
    async def test_matching_message_triggers_hook(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        view = _make_view(store, message_id=555)
        view.exit = AsyncMock()

        payload = MagicMock(message_id=555)
        await bot._listeners["on_raw_message_delete"](payload)

        view.exit.assert_awaited_once_with(delete_message=False)

    async def test_non_matching_message_ignored(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        view = _make_view(store, message_id=555)
        view.exit = AsyncMock()

        payload = MagicMock(message_id=999)
        await bot._listeners["on_raw_message_delete"](payload)

        view.exit.assert_not_awaited()

    async def test_view_without_message_skipped(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        view = _make_view(store, message_id=555)
        view._message = None
        view.exit = AsyncMock()

        payload = MagicMock(message_id=555)
        await bot._listeners["on_raw_message_delete"](payload)

        view.exit.assert_not_awaited()


# // ========================================( Bulk Delete )======================================== // #


class TestBulkMessageDelete:
    """on_raw_bulk_message_delete triggers cleanup for all matching views."""
    async def test_bulk_triggers_matching_views(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)

        view_a = _make_view(store, message_id=100, user_id=1)
        view_b = _make_view(store, message_id=200, user_id=2)
        view_c = _make_view(store, message_id=300, user_id=3)
        view_a.exit = AsyncMock()
        view_b.exit = AsyncMock()
        view_c.exit = AsyncMock()

        payload = MagicMock(message_ids=[100, 300, 999])
        await bot._listeners["on_raw_bulk_message_delete"](payload)

        view_a.exit.assert_awaited_once_with(delete_message=False)
        view_b.exit.assert_not_awaited()
        view_c.exit.assert_awaited_once_with(delete_message=False)


# // ========================================( Hook )======================================== // #


class TestOnMessageDeleteHook:
    """Default on_message_delete calls exit(delete_message=False)."""
    async def test_default_calls_exit(self):
        store = StateStore()
        view = _make_view(store)
        view.exit = AsyncMock()

        await view.on_message_delete()

        view.exit.assert_awaited_once_with(delete_message=False)

    async def test_nulls_message_before_exit(self):
        store = StateStore()
        view = _make_view(store)
        captured_msg = []

        async def _spy_exit(**kwargs):
            captured_msg.append(view._message)

        view.exit = AsyncMock(side_effect=_spy_exit)

        await view.on_message_delete()

        # _message should be None by the time exit() runs
        assert captured_msg == [None]

    async def test_custom_override(self):
        store = StateStore()
        log = []

        class _Custom(StatefulView):
            async def on_message_delete(self):
                log.append("custom")
                await self.exit(delete_message=False)

        view = _Custom(user_id=100, guild_id=200, state_store=store)
        view._message = MagicMock(id=777)
        view.exit = AsyncMock()
        store._register_view(view)

        bot = _make_bot()
        store._install_message_cleanup(bot)

        payload = MagicMock(message_id=777)
        await bot._listeners["on_raw_message_delete"](payload)

        assert log == ["custom"]
        view.exit.assert_awaited_once()
