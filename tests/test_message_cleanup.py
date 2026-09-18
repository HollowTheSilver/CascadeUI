"""Tests for automatic view cleanup on message deletion."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
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

    def test_registers_every_deletion_listener(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)

        assert set(bot._listeners) == {
            "on_raw_message_delete",
            "on_raw_bulk_message_delete",
            "on_guild_channel_delete",
            "on_raw_thread_delete",
        }

    def test_idempotent(self):
        store = StateStore()
        bot = _make_bot()
        calls = []
        listen = bot.listen
        bot.listen = lambda name: (calls.append(name), listen(name))[1]
        store._install_message_cleanup(bot)
        store._install_message_cleanup(bot)

        # listen() ran once per event, not once per install.
        assert len(calls) == 4


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


# // ========================================( on_message_gone )======================================== // #


class TestOnMessageGoneHook:
    """refresh() observing a 404 nulls _message and fires on_message_gone."""

    async def test_default_is_noop(self):
        store = StateStore()
        view = _make_view(store)
        # Default returns None: no exit, no raise.
        assert await view.on_message_gone() is None

    async def test_refresh_404_nulls_message_and_fires_hook(self):
        store = StateStore()
        view = _make_view(store)
        view._last_tree_digest = None  # skip the render-hash short-circuit
        view._check_placement = lambda: None
        view._message.edit = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Not Found")
        )
        view.on_message_gone = AsyncMock()

        await view.refresh()

        view.on_message_gone.assert_awaited_once()
        assert view._message is None

    async def test_refresh_404_swallows_hook_error(self):
        store = StateStore()
        view = _make_view(store)
        view._last_tree_digest = None
        view._check_placement = lambda: None
        view._message.edit = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Not Found")
        )
        view.on_message_gone = AsyncMock(side_effect=RuntimeError("boom"))

        # A raising hook must not break refresh()'s non-raising contract.
        await view.refresh()

        assert view._message is None


# // ========================================( Channel and Thread Delete )======================================== // #


def _in(view, channel_id, parent_id=None):
    view._message.channel = MagicMock(spec=["id", "parent_id"], id=channel_id, parent_id=parent_id)
    return view


class TestChannelAndThreadDelete:
    """Discord sends no message deletions for a deleted channel or thread, so
    those events tear down the views whose messages went with it."""

    async def test_channel_delete_tears_down_views_in_it_and_its_threads(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        in_channel = _in(_make_view(store, message_id=1, user_id=1), 50)
        in_thread = _in(_make_view(store, message_id=2, user_id=2), 77, parent_id=50)
        elsewhere = _in(_make_view(store, message_id=3, user_id=3), 60)
        for view in (in_channel, in_thread, elsewhere):
            view.exit = AsyncMock()

        await bot._listeners["on_guild_channel_delete"](MagicMock(id=50))

        in_channel.exit.assert_awaited_once_with(delete_message=False)
        in_thread.exit.assert_awaited_once_with(delete_message=False)
        elsewhere.exit.assert_not_awaited()

    async def test_a_deleted_category_matches_no_channel_inside_it(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        view = _make_view(store, message_id=1)
        view._message.channel = MagicMock(spec=["id", "category_id"], id=50, category_id=40)
        view.exit = AsyncMock()

        await bot._listeners["on_guild_channel_delete"](MagicMock(id=40))

        view.exit.assert_not_awaited()

    async def test_thread_delete_tears_down_views_in_the_thread(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        in_thread = _in(_make_view(store, message_id=1, user_id=1), 77, parent_id=50)
        in_parent = _in(_make_view(store, message_id=2, user_id=2), 50)
        in_thread.exit = AsyncMock()
        in_parent.exit = AsyncMock()

        await bot._listeners["on_raw_thread_delete"](MagicMock(thread_id=77))

        in_thread.exit.assert_awaited_once_with(delete_message=False)
        in_parent.exit.assert_not_awaited()


class TestCleanupIsolation:
    """Every view on a deleted message is reached, whatever another one does."""

    async def test_one_raising_override_does_not_stop_the_others(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        failing = _make_view(store, message_id=100, user_id=1)
        failing.on_message_delete = AsyncMock(side_effect=RuntimeError("boom"))
        after = _make_view(store, message_id=200, user_id=2)
        after.exit = AsyncMock()

        await bot._listeners["on_raw_bulk_message_delete"](MagicMock(message_ids=[100, 200]))

        after.exit.assert_awaited_once_with(delete_message=False)

    async def test_a_view_another_hook_tore_down_is_not_called(self):
        """The hook's contract: a view already torn down is not called again.

        One view's hook can tear another down before the sweep reaches it, so
        the check belongs in the loop rather than in the snapshot, which is
        taken before any hook runs. Reachable with no override at all: exiting
        a parent exits the children attached to it.
        """
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        parent = _make_view(store, message_id=100, user_id=1)
        child = _make_view(store, message_id=200, user_id=1)
        child._attached_to = parent
        parent.attach_child(child)
        child.on_message_delete = AsyncMock(wraps=child.on_message_delete)

        await bot._listeners["on_raw_bulk_message_delete"](MagicMock(message_ids=[100, 200]))

        assert child.is_finished()
        assert child.id not in store._active_views
        child.on_message_delete.assert_not_awaited()

    async def test_a_lone_delete_reaches_every_view_on_the_message(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        first = _make_view(store, message_id=555, user_id=1)
        second = _make_view(store, message_id=555, user_id=2)
        first.exit = AsyncMock()
        second.exit = AsyncMock()

        await bot._listeners["on_raw_message_delete"](MagicMock(message_id=555))

        first.exit.assert_awaited_once()
        second.exit.assert_awaited_once()


# // ========================================( Edit-Observed Deletion )======================================== // #


def _deleted_under(view):
    view._last_tree_digest = None
    view._check_placement = lambda: None
    view._message.edit = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
    )
    return view


class TestTeardownAfterAnEditFindsTheMessageGone:
    """An edit that 404s tears the view down through ``on_message_delete``,
    with no gateway event needed."""

    async def test_the_view_is_fully_torn_down(self):
        store = StateStore()
        view = _deleted_under(_make_view(store))

        await view.refresh()
        await view._message_gone_task

        assert view.id not in store._active_views
        assert view.id not in store.subscribers
        assert view.is_finished()

    async def test_teardown_survives_a_dispatch_that_suspends(self):
        """exit() cancels the tasks the view owns; a teardown owned by the
        view would cancel itself at its first suspension and leave a ghost."""
        store = StateStore()

        async def suspending(action, state, next_fn):
            await asyncio.sleep(0)
            return await next_fn(action, state)

        store._add_middleware(suspending)
        view = _deleted_under(_make_view(store))

        await view.refresh()
        await view._message_gone_task

        assert view.id not in store._active_views
        assert view.id not in store.state.get("views", {})

    async def test_gone_hook_fires_before_the_teardown(self):
        store = StateStore()
        order = []

        class _Tracking(StatefulView):
            async def on_message_gone(self):
                order.append("gone")

            async def on_message_delete(self):
                order.append("delete")
                await super().on_message_delete()

        view = _Tracking(user_id=1, guild_id=2, state_store=store)
        view._message = MagicMock(id=9)
        store._register_view(view)
        _deleted_under(view)

        await view.refresh()
        await view._message_gone_task

        assert order == ["gone", "delete"]
        assert view.is_finished()

    async def test_a_gone_override_that_skips_super_is_still_torn_down(self):
        store = StateStore()

        class _Reconciles(StatefulView):
            async def on_message_gone(self):
                self.reconciled = True

        view = _Reconciles(user_id=1, guild_id=2, state_store=store)
        view._message = MagicMock(id=9)
        store._register_view(view)
        _deleted_under(view)

        await view.refresh()
        await view._message_gone_task

        assert view.reconciled and view.is_finished()
        assert view.id not in store._active_views

    async def test_a_gateway_teardown_first_leaves_nothing_for_the_edit_path(self):
        store = StateStore()
        bot = _make_bot()
        store._install_message_cleanup(bot)
        calls = []

        class _Counting(StatefulView):
            async def on_message_delete(self):
                calls.append(1)
                await super().on_message_delete()

        view = _Counting(user_id=1, guild_id=2, state_store=store)
        view._message = MagicMock(id=9)
        store._register_view(view)

        await bot._listeners["on_raw_message_delete"](MagicMock(message_id=9))
        # Scheduled anyway, as an in-flight edit whose 404 lands late would.
        view._schedule_message_gone_teardown()
        await view._message_gone_task

        assert calls == [1]

    async def test_a_rolled_back_source_whose_message_went_is_still_torn_down(self):
        """Navigation unsubscribes its source before the edit, so a 404 landing
        then reads the source as torn down and skips the teardown. When the
        navigation rolls back and recovers the source, the teardown is owed."""
        store = StateStore()
        source = _deleted_under(_make_view(store))
        await source._register_state()
        store._unsubscribe(source.id)

        await source.refresh()
        skipped = source._message_gone_task
        await skipped
        assert not source.is_finished()

        await source._rollback_navigation(None)
        assert source._message_gone_task is not skipped
        await source._message_gone_task

        assert source.id not in store._active_views
        assert source.is_finished()

    async def test_a_never_sent_source_survives_a_failed_navigation(self):
        store = StateStore()

        class _Destination(StatefulView):
            def __init__(self, **kwargs):
                raise RuntimeError("destination failed")

        source = _make_view(store)
        source._message = None
        store._register_view(source)
        await source._register_state()

        with pytest.raises(RuntimeError, match="destination failed"):
            await source.push(_Destination)
        await asyncio.sleep(0)

        assert source._message_gone_task is None
        assert not source.is_finished()
        assert source.id in store._active_views

    async def test_an_expired_ephemeral_token_is_not_a_deletion(self):
        store = StateStore()
        view = _make_view(store)
        view._last_tree_digest = None
        view._check_placement = lambda: None
        view._ephemeral = True
        view._message.edit = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=401), "Invalid Webhook Token")
        )

        await view.refresh()

        assert view._message_gone_task is None
        assert view.id in store._active_views
