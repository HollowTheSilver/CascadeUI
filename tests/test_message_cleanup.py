"""Tests for automatic view cleanup on message deletion."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import ActionRow
from helpers import RenderableLayoutView, make_interaction, until

from cascadeui.components.base import StatefulButton
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

    def test_records_the_bot(self):
        store = StateStore()
        bot = _make_bot()
        assert bot not in store._cleanup_listener_bots

        store._install_message_cleanup(bot)

        assert bot in store._cleanup_listener_bots

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

    async def test_a_plain_client_is_wired_without_listeners(self):
        # discord.Client has no listen(): the install raised, and a send
        # through one skipped the rest of its last stage.
        store = StateStore()
        client = discord.Client(intents=discord.Intents.none())
        store._install_message_cleanup(client)

        assert client in store._cleanup_listener_bots

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

        assert await view.refresh() == "no_message"
        assert view._message is None
        await view._message_gone_task

        view.on_message_gone.assert_awaited_once()

    async def test_a_raising_hook_is_logged_and_the_view_still_torn_down(self, caplog):
        store = StateStore()
        view = _make_view(store)
        view._last_tree_digest = None
        view._check_placement = lambda: None
        view._message.edit = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Not Found")
        )
        view.on_message_gone = AsyncMock(side_effect=RuntimeError("boom"))

        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            assert await view.refresh() == "no_message"
            await view._message_gone_task

        assert view._message is None
        assert view._torn_down()
        assert any("on_message_gone failed" in r.getMessage() for r in caplog.records)

    async def test_a_render_in_flight_when_the_views_own_exit_deletes_the_message(self):
        """The render's 404 answered the view's own delete, and the hook ran
        on the closed view: a hook that sends the view again logged an ERROR
        on an ordinary Exit."""
        store = StateStore()
        view = _make_view(store)
        view._last_tree_digest = None
        deleted = asyncio.Event()

        async def edit(**kwargs):
            await deleted.wait()
            raise discord.NotFound(MagicMock(status=404), "Unknown Message")

        async def delete():
            deleted.set()

        view._message.edit = AsyncMock(side_effect=edit)
        view._message.delete = AsyncMock(side_effect=delete)
        view.on_message_gone = AsyncMock()

        render = asyncio.create_task(view.refresh())
        await until(lambda: view._message.edit.await_count == 1)
        await view.exit(delete_message=True)

        assert await asyncio.wait_for(render, timeout=5) == "closed"
        view.on_message_gone.assert_not_awaited()
        assert view._message_gone_task is None

    async def test_an_on_message_gone_that_exits_the_view_finishes(self):
        """The exit's freeze waits for the view's renders in flight, so a hook
        called from inside the render that found the message gone never
        finished."""
        store = StateStore()
        view = _make_view(store)
        view._last_tree_digest = None
        view._message.edit = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
        )

        async def on_message_gone():
            await view.exit()

        view.on_message_gone = on_message_gone

        assert await asyncio.wait_for(view.refresh(), timeout=5) == "no_message"
        await asyncio.wait_for(view._message_gone_task, timeout=5)
        assert view.is_finished()
        assert not view._edits_pending

    async def test_a_webhook_edit_that_falls_through_after_a_concurrent_404(self):
        """The webhook edit awaited, a concurrent refresh found the message
        deleted and cleared it, and the fall-through to the channel endpoint
        then raised AttributeError on the cleared reference."""
        store = StateStore()
        view = _make_view(store)
        view._check_placement = lambda: None
        gate = asyncio.Event()

        async def webhook_edit(**kwargs):
            await gate.wait()
            raise discord.HTTPException(MagicMock(status=401), "token expired")

        view._webhook_message = MagicMock()
        view._webhook_message.edit = AsyncMock(side_effect=webhook_edit)
        view._message.edit = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Not Found")
        )

        embed_refresh = asyncio.create_task(view.refresh(embed=discord.Embed(title="a")))
        await asyncio.sleep(0)
        assert await view.refresh(content="b") == "no_message"
        gate.set()

        assert await asyncio.wait_for(embed_refresh, timeout=2) == "no_message"


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

    async def test_a_push_landing_during_the_teardown_closes_the_new_view(self, caplog):
        """The teardown exited the view the push started from; its exit waited
        out the push, found the panel handed on, and did nothing but warn,
        leaving the new view live with no message."""

        class _Dst(RenderableLayoutView):
            async def on_load(self):
                await asyncio.sleep(0.05)

        class _Src(RenderableLayoutView):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.go = StatefulButton(label="Go", custom_id="go", callback=self._go)
                self.add_item(ActionRow(self.go))

            async def _go(self, interaction):
                self.pushed = await self.push(_Dst, interaction=interaction)

        view = _Src(interaction=make_interaction(user_id=1, guild_id=100))
        await view.send()
        view._message.edit = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
        )
        view._last_tree_digest = None

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            pushing = asyncio.create_task(
                view._scheduled_task(view.go, make_interaction(user_id=1, guild_id=100))
            )
            await view.refresh()
            await pushing
            await view._message_gone_task

        assert view.pushed._torn_down()
        assert not [r for r in caplog.records if "handed its panel on" in r.getMessage()]

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

    async def test_a_source_whose_message_went_mid_push_is_torn_down_after_it_fails(self):
        """A push waits for an edit its source already started. That edit
        finding the message deleted tears the source down, and the teardown
        waits for the push to fail before it runs."""
        from helpers import make_interaction

        store = StateStore()
        source = _deleted_under(_make_view(store))
        await source._register_state()
        gate = asyncio.Event()
        deleted = discord.NotFound(MagicMock(status=404), "Unknown Message")

        async def in_flight(**kwargs):
            await gate.wait()
            raise deleted

        source._message.edit = AsyncMock(side_effect=in_flight)
        started = asyncio.create_task(source.refresh())
        await asyncio.sleep(0)

        class _Destination(StatefulView):
            pass

        nav = make_interaction(user_id=100, guild_id=200, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deleted)
        push = asyncio.create_task(source.push(_Destination, interaction=nav))
        await asyncio.sleep(0)
        gate.set()
        await started
        await push
        await source._message_gone_task

        assert source.is_finished()
        assert source.id not in store._active_views

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


class TestAMessageTheViewHasLeft:
    """A deletion of the message a view is leaving does not close the view.

    A view sent again moves to a new message. The old message's deletion,
    reported by the gateway or found by an edit still in flight, closed the
    view on its new message: frozen, or left with buttons that answered nothing.
    """

    @staticmethod
    def _interaction(message_id):
        interaction = make_interaction()
        message = MagicMock(id=message_id, channel=MagicMock(id=888))
        message.edit = AsyncMock(return_value=message)
        message.delete = AsyncMock()
        interaction.original_response = AsyncMock(return_value=message)
        return interaction, message

    @staticmethod
    def _panel(interaction, cls=RenderableLayoutView):
        view = cls(interaction=interaction, timeout=None)
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        return view

    @staticmethod
    def _not_found():
        return discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Message")

    async def test_a_gateway_delete_of_the_old_message_during_a_resend_leaves_the_view_live(self):
        first, old = self._interaction(1)
        view = self._panel(first)
        await view.send()
        second, new = self._interaction(2)
        posting, gate = asyncio.Event(), asyncio.Event()
        real_send = second.response.send_message

        async def send_slowly(*args, **kwargs):
            posting.set()
            await gate.wait()
            return await real_send(*args, **kwargs)

        second.response.send_message = send_slowly
        view.interaction = second
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(posting.wait(), 1)
        # The gateway reports each event on a task of its own.
        cleanup = asyncio.create_task(
            view.state_store._clean_up_deleted(lambda message: message.id == 1)
        )
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(cleanup, 1)

        assert await sending is not None
        assert view._message is new
        assert not view._torn_down()
        new.edit.assert_not_awaited()

    async def test_an_edit_that_finds_the_old_message_gone_during_a_resend_leaves_the_view_live(
        self,
    ):
        first, old = self._interaction(1)
        view = self._panel(first)
        await view.send()
        view._message = old
        editing, fail = asyncio.Event(), asyncio.Event()

        async def edit_then_404(**kwargs):
            editing.set()
            await fail.wait()
            raise self._not_found()

        old.edit = edit_then_404
        view._last_tree_digest = None
        refreshing = asyncio.create_task(view.refresh())
        await asyncio.wait_for(editing.wait(), 1)
        second, new = self._interaction(2)
        view.interaction = second
        # The send posts the new message, then waits for the edit in flight on
        # the old one before closing it; the 404 comes back meanwhile.
        sending = asyncio.create_task(view.send())
        for _ in range(50):
            if view._message is new:
                break
            await asyncio.sleep(0)
        assert view._message is new
        fail.set()
        await refreshing
        await asyncio.wait_for(sending, 1)

        assert view._message is new
        assert not view._torn_down()

    @pytest.mark.parametrize("render", ["refresh", "reload"])
    async def test_an_on_message_gone_that_sends_the_view_again_keeps_it_live(self, render):
        """Called from inside the render, the hook's send waited on that
        render's own turn: from reload() it raised, was logged, and the view
        was torn down."""
        first, old = self._interaction(1)
        second, new = self._interaction(2)

        class Reposts(RenderableLayoutView):
            async def on_message_gone(self):
                self.interaction = second
                await self.send()

        view = self._panel(first, Reposts)
        await view.send()
        view._message = old
        old.edit = AsyncMock(side_effect=self._not_found())
        view._last_tree_digest = None
        await getattr(view, render)()
        await asyncio.wait_for(view._message_gone_task, 5)

        assert view._message is new
        assert not view._torn_down()

    async def test_an_on_message_gone_that_sends_a_tab_view_again_keeps_it_live(self):
        from discord.ui import TextDisplay

        from cascadeui import TabLayoutView

        first, old = self._interaction(1)
        second, new = self._interaction(2)

        class Reposts(TabLayoutView):
            async def on_message_gone(self):
                self.interaction = second
                await self.send()

        view = Reposts(
            interaction=first,
            tabs={"A": lambda: [TextDisplay("a")], "B": lambda: [TextDisplay("b")]},
        )
        await view.send()
        view._message = old
        old.edit = AsyncMock(side_effect=self._not_found())
        await view.switch_tab("B")
        await asyncio.wait_for(view._message_gone_task, 5)

        assert view._message is new
        assert not view._torn_down()

    async def test_a_view_a_send_moved_meanwhile_is_not_reported_gone(self):
        """The edit's 404 landed while a send was posting the view anew: the
        hook reported a live view's message as gone."""
        first, old = self._interaction(1)
        view = self._panel(first)
        await view.send()
        view._message = old
        view.on_message_gone = AsyncMock()
        editing, fail = asyncio.Event(), asyncio.Event()

        async def edit_then_404(**kwargs):
            editing.set()
            await fail.wait()
            raise self._not_found()

        old.edit = edit_then_404
        view._last_tree_digest = None
        refreshing = asyncio.create_task(view.refresh())
        await asyncio.wait_for(editing.wait(), 1)
        second, new = self._interaction(2)
        posting, gate = asyncio.Event(), asyncio.Event()
        real_send = second.response.send_message

        async def send_slowly(*args, **kwargs):
            posting.set()
            await gate.wait()
            return await real_send(*args, **kwargs)

        second.response.send_message = send_slowly
        view.interaction = second
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(posting.wait(), 1)
        fail.set()
        assert await asyncio.wait_for(refreshing, 1) == "no_message"
        gate.set()
        await asyncio.wait_for(sending, 1)
        await asyncio.wait_for(view._message_gone_task, 1)

        assert view._message is new
        assert not view._torn_down()
        view.on_message_gone.assert_not_awaited()


class TestCleanupDoesNotWaitOnOneSend:
    async def test_a_view_being_sent_does_not_hold_up_another_views_teardown(self):
        # The cleanup waits out a send in flight; waiting in snapshot order
        # held every later view in the same purge behind a slow send.
        first, _ = TestAMessageTheViewHasLeft._interaction(1)
        sending_view = TestAMessageTheViewHasLeft._panel(first)
        await sending_view.send()
        other_interaction, _ = TestAMessageTheViewHasLeft._interaction(2)
        other = TestAMessageTheViewHasLeft._panel(other_interaction)
        await other.send()

        resend, _ = TestAMessageTheViewHasLeft._interaction(3)
        posting, gate = asyncio.Event(), asyncio.Event()
        real_send = resend.response.send_message

        async def send_slowly(*args, **kwargs):
            posting.set()
            await gate.wait()
            return await real_send(*args, **kwargs)

        resend.response.send_message = send_slowly
        sending_view.interaction = resend
        sending = asyncio.create_task(sending_view.send())
        await asyncio.wait_for(posting.wait(), 1)
        cleanup = asyncio.create_task(
            other.state_store._clean_up_deleted(lambda message: message.channel.id == 888)
        )
        for _ in range(20):
            await asyncio.sleep(0)

        try:
            assert other._torn_down()
        finally:
            gate.set()
            await sending
            await asyncio.wait_for(cleanup, 1)


# // ========================================( What a Render Reports After a Close )======================================== // #


class TestARenderTellsADeletionFromAClose:
    """A render that found its view torn down reported NO_MESSAGE for every
    close, so a caller could not tell a message deleted out from under the
    view from a close the view made itself, except by reading is_finished()
    before the deletion's teardown task had run."""

    _interaction = staticmethod(TestAMessageTheViewHasLeft._interaction)
    _panel = staticmethod(TestAMessageTheViewHasLeft._panel)

    async def test_a_reload_after_the_view_exits_reports_closed(self):
        interaction, _ = self._interaction(1)
        view = self._panel(interaction)
        await view.send()
        await view.exit(delete_message=False)

        assert await view.reload() == "closed"

    async def test_a_reload_after_a_gateway_deletion_reports_no_message(self):
        interaction, _ = self._interaction(1)
        view = self._panel(interaction)
        await view.send()
        await view.state_store._clean_up_deleted(lambda message: message.id == 1)

        assert view._torn_down()
        assert await view.reload() == "no_message"

    async def test_a_render_after_a_404_and_its_teardown_reports_no_message(self):
        interaction, message = self._interaction(1)
        view = self._panel(interaction)
        await view.send()
        message.edit = AsyncMock(side_effect=TestAMessageTheViewHasLeft._not_found())
        view._last_tree_digest = None

        assert await view.refresh() == "no_message"
        await until(view._torn_down)
        assert await view.reload() == "no_message"
        assert await view.refresh() == "no_message"

    async def test_a_view_sent_again_after_a_deletion_reports_its_own_close(self):
        first, _ = self._interaction(1)
        second, _ = self._interaction(2)

        class _Reposts(RenderableLayoutView):
            async def on_message_delete(self):
                self.interaction = second
                await self.send()

        view = self._panel(first, _Reposts)
        await view.send()
        await view.state_store._clean_up_deleted(lambda message: message.id == 1)
        assert not view._torn_down()
        await view.exit(delete_message=False)

        assert await view.reload() == "closed"

    async def test_a_view_given_a_new_message_reports_its_own_close(self):
        first, _ = self._interaction(1)
        _, replacement = self._interaction(2)

        class _KeepsTheView(RenderableLayoutView):
            async def on_message_delete(self):
                self.message = replacement

        view = self._panel(first, _KeepsTheView)
        await view.send()
        await view.state_store._clean_up_deleted(lambda message: message.id == 1)
        await view.exit(delete_message=False)

        assert await view.reload() == "closed"

    async def test_a_hook_that_clears_the_message_through_the_setter_keeps_the_deletion(self):
        """The setter cleared the mark even when given None, so an
        on_message_delete() that wrote self.message = None reported CLOSED."""
        interaction, _ = self._interaction(1)

        class _ClearsThroughTheSetter(RenderableLayoutView):
            async def on_message_delete(self):
                self.message = None
                await self.exit(delete_message=False)

        view = self._panel(interaction, _ClearsThroughTheSetter)
        await view.send()
        await view.state_store._clean_up_deleted(lambda message: message.id == 1)

        assert view._torn_down()
        assert await view.reload() == "no_message"

    async def test_the_view_a_push_handed_a_deleted_message_reports_no_message(self):
        """The source's hook closed the push's destination before the
        destination's own turn, which skipped it as torn down without marking
        the deletion, so its renders reported CLOSED."""
        interaction, _ = self._interaction(1)
        loading = asyncio.Event()

        class _Destination(RenderableLayoutView):
            async def on_load(self):
                await loading.wait()

        source = self._panel(interaction)
        await source.send()
        push = asyncio.create_task(source.push(_Destination))
        await until(lambda: source._away_for_navigation)
        cleanup = asyncio.create_task(
            source.state_store._clean_up_deleted(lambda message: message.id == 1)
        )
        for _ in range(5):
            await asyncio.sleep(0)
        loading.set()
        destination = await asyncio.wait_for(push, 2)
        await asyncio.wait_for(cleanup, 2)

        assert destination._torn_down()
        assert await destination.reload() == "no_message"
