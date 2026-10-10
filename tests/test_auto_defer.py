"""Tests for auto-defer safety net on StatefulView."""

import asyncio
import io
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import pytest
from helpers import make_interaction as _make_interaction
from helpers import refused_by_closed_session, until

from cascadeui.components.inputs import Modal, TextInput
from cascadeui.state.singleton import get_store
from cascadeui.utils.responses import open_modal_safe, respond_safe
from cascadeui.views.base import RenderOutcome
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


class TestAckBackstopRecordsWhatItCosts:
    """Three outcomes, each recorded to match what it costs.

    The fired branch is the one worth a line: the handler used its whole
    budget and the interaction was rescued rather than lost, which is the
    state a surface passes through on its way to expiring and the only
    one that shows up while the user still sees everything working.
    """

    async def test_a_fired_backstop_names_the_surface(self, caplog):
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 0.05
        view.owner_only = False
        interaction = _make_interaction(is_done=False)

        async def slow_callback(inter):
            await asyncio.sleep(0.15)

        with caplog.at_level(logging.INFO, logger="cascadeui.views._interaction"):
            await view._scheduled_task(_make_item(slow_callback), interaction)

        interaction.response.defer.assert_called_once_with()
        assert "Auto-defer backstop acked for StatefulView" in caplog.text

    async def test_a_cancelled_backstop_stays_silent(self, caplog):
        """The healthy path is every click that answers in time; a line
        here would be one per interaction."""
        view = StatefulView(interaction=_make_interaction())
        view.auto_defer_delay = 0.30
        view.owner_only = False
        interaction = _make_interaction(is_done=False)

        async def prompt_callback(inter):
            await inter.response.defer()

        with caplog.at_level(logging.DEBUG, logger="cascadeui.views._interaction"):
            await view._scheduled_task(_make_item(prompt_callback), interaction)

        assert "Auto-defer backstop acked" not in caplog.text


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


class TestLibraryControlsAreAnswered:
    """With ``auto_defer`` off, a click on a control the library built is still
    answered when nothing else did: no code of the user's is there to answer a
    page turn, a tab, or wizard navigation whose render sent nothing."""

    async def test_a_click_on_the_active_tab_is_answered(self):
        from discord.ui import Container, TextDisplay

        from cascadeui import TabLayoutView

        async def tab_a():
            return [Container(TextDisplay("A"))]

        async def tab_b():
            return [Container(TextDisplay("B"))]

        class _Tabs(TabLayoutView):
            auto_defer = False

        view = _Tabs(interaction=_make_interaction(), tabs={"A": tab_a, "B": tab_b})
        await view.on_load()
        message = MagicMock(id=5)
        message.edit = AsyncMock(return_value=message)
        view._message = message
        view._last_tree_digest = view._compute_tree_digest()
        tab = next(c for c in view.walk_children() if getattr(c, "label", None) == "A")
        interaction = _make_interaction(message=MagicMock(id=5))
        interaction.data = {"custom_id": tab.custom_id}

        await view._scheduled_task(tab, interaction)

        interaction.response.edit_message.assert_not_awaited()
        interaction.response.defer.assert_awaited_once()

    async def test_a_repick_of_the_selected_dropdown_option_is_answered(self):
        from discord.ui import ActionRow  # noqa: F401
        from helpers import RenderableLayoutView

        from cascadeui import choice_row

        picks = []

        async def on_select(interaction, value):
            picks.append(value)

        class _Host(RenderableLayoutView):
            auto_defer = False

        view = _Host(interaction=_make_interaction())
        row = choice_row({f"opt{i}": i for i in range(7)}, on_select=on_select, selected=0)
        view.add_item(row)
        select = row.children[0]
        interaction = _make_interaction()
        interaction.data = {"custom_id": select.custom_id, "component_type": 3, "values": ["0"]}

        await view._scheduled_task(select, interaction)

        assert picks == []
        interaction.response.defer.assert_awaited_once()

    async def test_a_wizard_click_from_an_earlier_step_is_answered(self):
        from discord.ui import Container, TextDisplay

        from cascadeui.views.patterns import WizardLayoutView

        async def step():
            return [Container(TextDisplay("step"))]

        class _Wizard(WizardLayoutView):
            auto_defer = False

        view = _Wizard(
            interaction=_make_interaction(),
            steps=[{"name": "A", "builder": step}, {"name": "B", "builder": step}],
        )
        await view.on_load()
        stale_id = view._next_btn.custom_id
        view._current_step = 1
        view._sync_wizard_nav()
        interaction = _make_interaction()
        interaction.data = {"custom_id": stale_id}

        await view._scheduled_task(view._next_btn, interaction)

        assert view._current_step == 1
        interaction.response.defer.assert_awaited_once()

    async def test_a_callback_of_the_users_is_left_to_answer(self):
        """The rule reads whose code the callback is; a button built with the
        user's own callback keeps what ``auto_defer = False`` promises."""
        from cascadeui import StatefulButton

        view = StatefulView(interaction=_make_interaction())
        view.owner_only = False
        view.auto_defer = False
        calls = []

        async def mine(interaction):
            calls.append(interaction)

        button = StatefulButton(label="Mine", callback=mine)
        view.add_item(button)
        interaction = _make_interaction(is_done=False)

        await view._scheduled_task(button, interaction)

        assert calls == [interaction]
        interaction.response.defer.assert_not_awaited()


class TestClicksOnADisabledItemAreDropped:
    """Discord offers no click on a disabled component, so one that arrives is stale.

    It was sent from an older render where the item was enabled: a board
    that has since ended, or a cell that has since been played. Running it
    acted on state the user never saw, so it is answered and dropped.
    """

    def _view_with_button(self, calls, gate=None):
        from cascadeui import StatefulButton

        view = StatefulView(interaction=_make_interaction())
        view.owner_only = False

        async def play(interaction):
            calls.append(interaction)
            if gate is not None:
                await gate.wait()
            button.disabled = True

        button = StatefulButton(label="Cell", callback=play)
        view.add_item(button)
        return view, button

    async def test_a_click_on_a_disabled_item_is_answered_and_dropped(self, caplog):
        caplog.set_level(logging.DEBUG, logger="cascadeui")
        calls = []
        view, button = self._view_with_button(calls)
        # No lock to queue behind, so only the check at dispatch can drop it.
        view.serialize_interactions = False
        button.disabled = True
        interaction = _make_interaction(is_done=False)

        await view._scheduled_task(button, interaction)

        assert calls == []
        interaction.response.defer.assert_awaited()
        assert any(
            "Dropped a click" in r.getMessage() and "disabled on screen" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    async def test_a_click_on_a_closed_view_is_logged_when_dropped(self, caplog):
        caplog.set_level(logging.DEBUG, logger="cascadeui")
        calls = []
        view, button = self._view_with_button(calls)
        view.serialize_interactions = False
        await view.exit()

        await view._scheduled_task(button, _make_interaction(is_done=False))

        assert calls == []
        assert any(
            "Dropped a click" in r.getMessage() and "the view has closed" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    async def test_a_refused_click_is_logged_with_the_user(self, caplog):
        caplog.set_level(logging.DEBUG, logger="cascadeui")
        calls = []
        view, button = self._view_with_button(calls)
        interaction = _make_interaction(is_done=False)
        view.allowed_users = {interaction.user.id + 1}

        await view._scheduled_task(button, interaction)

        assert calls == []
        assert any(
            "Dropped a click" in r.getMessage()
            and f"refused user {interaction.user.id}" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    async def test_a_click_a_controls_owner_only_refuses_is_logged(self, caplog):
        from discord.ui import ActionRow
        from helpers import RenderableLayoutView

        from cascadeui import StatefulButton

        caplog.set_level(logging.DEBUG, logger="cascadeui")
        calls = []

        class _View(RenderableLayoutView):
            owner_only = False
            auto_defer_delay = 0.05

        async def mine(interaction):
            calls.append(interaction)

        view = _View(interaction=_make_interaction(user_id=1))
        button = StatefulButton(label="Mine", custom_id="mine", owner_only=True, callback=mine)
        view.add_item(ActionRow(button))
        await view.send()

        await view._scheduled_task(button, _make_interaction(user_id=99))

        assert calls == []
        assert any(
            "Dropped a click" in r.getMessage() and "owner_only refused user 99" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    async def test_a_dropped_click_is_answered_without_auto_defer(self):
        # No callback of the user's ran for it, so nothing else could answer it.
        calls = []
        view, button = self._view_with_button(calls)
        view.serialize_interactions = False
        view.auto_defer = False
        button.disabled = True
        interaction = _make_interaction(is_done=False)

        await view._scheduled_task(button, interaction)

        assert calls == []
        interaction.response.defer.assert_awaited()

    @pytest.mark.parametrize("auto_defer", [True, False])
    async def test_a_click_queued_behind_one_that_disabled_the_item_is_dropped(
        self, auto_defer, caplog
    ):
        caplog.set_level(logging.DEBUG, logger="cascadeui")
        calls = []
        gate = asyncio.Event()
        view, button = self._view_with_button(calls, gate)
        view.auto_defer = auto_defer
        first, second = _make_interaction(), _make_interaction()

        running = asyncio.create_task(view._scheduled_task(button, first))
        await until(lambda: calls)
        # The second click passes dispatch while the item is still enabled and
        # waits on the lock; the first disables the item before releasing it.
        queued = asyncio.create_task(view._scheduled_task(button, second))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not button.disabled
        gate.set()
        await asyncio.gather(running, queued)

        assert calls == [first]
        second.response.defer.assert_awaited()
        assert any(
            "Dropped a click" in r.getMessage() and "disabled on screen" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    async def test_a_click_queued_behind_a_push_from_elsewhere_is_dropped(self, caplog):
        """A Back click waiting for the lock ran after a push from outside the
        view (a command, a background task) handed the panel on, and its pop()
        logged an ERROR telling the user to fix code they did not write."""
        from helpers import RenderableLayoutView

        caplog.set_level(logging.DEBUG, logger="cascadeui")
        loading = asyncio.Event()

        class Detail(RenderableLayoutView):
            auto_back_button = True

        class Other(RenderableLayoutView):
            async def on_load(self):
                await loading.wait()

        root = RenderableLayoutView(interaction=_make_interaction())
        await root.send()
        child = await root.push(Detail, interaction=_make_interaction())
        back = next(i for i in child.walk_children() if getattr(i, "_cascadeui_back_button", False))
        await child._interaction_lock.acquire()
        click = asyncio.create_task(child._scheduled_task(back, _make_interaction()))
        for _ in range(10):
            await asyncio.sleep(0)
        pushing = asyncio.create_task(child.push(Other, interaction=_make_interaction()))
        for _ in range(10):
            await asyncio.sleep(0)
        child._interaction_lock.release()
        for _ in range(10):
            await asyncio.sleep(0)
        loading.set()
        await asyncio.wait_for(asyncio.gather(click, pushing), 5)

        records = [r for r in caplog.records if r.name.startswith("cascadeui")]
        assert [r.getMessage() for r in records if r.levelno >= logging.ERROR] == []
        assert any("Dropped a click" in r.getMessage() for r in records)

    async def test_a_click_during_a_send_of_the_view_runs_at_once(self):
        """Every click waited, inside the lock, for a send of the view in
        flight, so a button that opens a modal got the fallback reply while
        the panel was sent again."""
        from discord.ui import ActionRow
        from helpers import RenderableLayoutView

        from cascadeui.components.base import StatefulButton

        loading = None
        opened = []

        class Panel(RenderableLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                edit = StatefulButton(label="Edit", custom_id="p:edit", callback=self._edit)
                self.add_item(ActionRow(edit))

            async def on_load(self):
                if loading is not None:
                    await loading.wait()

            async def _edit(self, interaction):
                modal = Modal(title="Edit", inputs=[TextInput(label="Name")])
                opened.append(await self.open_modal(interaction, modal))

        view = Panel(interaction=_make_interaction())
        await view.send()
        loading = asyncio.Event()
        again = _make_interaction()
        again.original_response.return_value.id = 1001
        view.interaction = again
        sending = asyncio.create_task(view.send())
        for _ in range(10):
            await asyncio.sleep(0)
        button = next(i for i in view.walk_children() if getattr(i, "custom_id", None) == "p:edit")
        click = _make_interaction(message=view.message)
        clicking = asyncio.create_task(view._scheduled_task(button, click))
        try:
            try:
                await until(lambda: opened, timeout=2)
            except asyncio.TimeoutError:
                pass
            assert opened == [True], "the click waited for the send to finish"
            assert not sending.done()
        finally:
            loading.set()
        await asyncio.wait_for(asyncio.gather(sending, clicking), 5)

        click.response.send_modal.assert_awaited_once()


class TestContentOfARenderDeferredDuringASend:
    """A click runs while its view is being sent again, and a V1 click
    renders through ``refresh(embed=...)``. That refresh is deferred until
    the send finishes, and the replay renders from state with no keywords,
    so the click's embed reached no message."""

    @staticmethod
    def _panel(loading):
        from cascadeui.components.base import StatefulButton

        class Panel(StatefulView):
            auto_defer_delay = 0.05

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.count = 0
                self.add_item(StatefulButton(label="+1", custom_id="p:inc", callback=self._inc))

            async def on_load(self):
                if loading.is_set() is False and getattr(self, "_hold", False):
                    await loading.wait()

            async def _inc(self, interaction):
                self.count += 1
                await self.refresh(embed=discord.Embed(title=f"count={self.count}"))

        return Panel

    @staticmethod
    def _titles(*mocks):
        return [
            call.kwargs["embed"].title
            for mock in mocks
            for call in mock.await_args_list
            if call.kwargs.get("embed") is not None
        ]

    async def _click_during_resend(self, panel_cls, loading):
        view = panel_cls(interaction=_make_interaction())
        await view.send(embed=discord.Embed(title="count=0"))
        old = view.message
        view._hold = True
        again = _make_interaction()
        again.original_response.return_value.id = 1001
        view.interaction = again
        sending = asyncio.create_task(view.send(embed=discord.Embed(title="reposted")))
        for _ in range(10):
            await asyncio.sleep(0)
        button = next(i for i in view.children if getattr(i, "custom_id", None) == "p:inc")
        click = _make_interaction(message=old)
        clicking = asyncio.create_task(view._scheduled_task(button, click))
        await until(lambda: view.count == 1, timeout=2)
        loading.set()
        await asyncio.wait_for(asyncio.gather(sending, clicking), 5)
        await view.state_store._flush_notifications()
        for _ in range(20):
            await asyncio.sleep(0)
        new = view.message
        assert new is not old
        return view, new, again

    async def test_the_clicks_embed_reaches_the_new_message(self):
        loading = asyncio.Event()
        view, new, again = await self._click_during_resend(self._panel(loading), loading)

        shipped = self._titles(new.edit, again.edit_original_response)
        assert shipped[-1:] == [
            "count=1"
        ], f"the click's embed never reached the message: {shipped}"

    async def test_a_render_after_the_send_wins_over_the_held_content(self):
        loading = asyncio.Event()
        base = self._panel(loading)

        class Rendering(base):
            subscribed_actions = {"COMPONENT_INTERACTION"}

            async def on_state_changed(self, state):
                await self.refresh(embed=discord.Embed(title=f"state count={self.count}"))

        # The click's dispatch, after its refresh, is a state change deferred
        # during the send: the replay renders it, and it sets the embed last.
        view, new, again = await self._click_during_resend(Rendering, loading)

        shipped = self._titles(new.edit, again.edit_original_response)
        assert (
            shipped and shipped[-1] == "state count=1"
        ), f"older held content shipped over a newer render: {shipped}"

    # Sent from a context, so every message is the return of context.send.

    @staticmethod
    def _message(message_id):
        message = MagicMock(id=message_id, channel=MagicMock(id=888))
        message.edit = AsyncMock(return_value=message)
        message.delete = AsyncMock()
        return message

    @staticmethod
    def _context(post):
        context = MagicMock()
        context.author = MagicMock(id=1)
        context.guild = MagicMock(id=100)
        context.send = AsyncMock(side_effect=post)
        return context

    @staticmethod
    def _gated():
        from cascadeui.components.base import StatefulButton

        class Gated(StatefulView):
            auto_defer_delay = 0.05
            load_gate = None
            presend_gate = None
            presend_answer = True

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(StatefulButton(label="+1", custom_id="g:inc", callback=self._noop))

            async def _noop(self, interaction):
                pass

            async def on_pre_send(self, interaction):
                if self.presend_gate is not None:
                    await self.presend_gate.wait()
                return self.presend_answer

            async def on_load(self):
                if self.load_gate is not None:
                    await self.load_gate.wait()

        return Gated

    @staticmethod
    def _live_after_edits(message):
        """Record, per edit that names a view, whether it shipped an enabled control."""
        message.live = []

        async def edit(**kwargs):
            if "view" in kwargs:
                view = kwargs["view"]
                children = [] if view is None else view.children
                message.live.append(any(getattr(c, "disabled", True) is False for c in children))
            return message

        message.edit = AsyncMock(side_effect=edit)
        return message

    @staticmethod
    def _content(message):
        shipped = []
        for call in message.edit.await_args_list:
            item = {}
            if call.kwargs.get("embed") is not None:
                item["embed"] = call.kwargs["embed"].title
            if "embeds" in call.kwargs:
                item["embeds"] = [e.title for e in call.kwargs["embeds"]]
            if item:
                shipped.append(item)
        return shipped

    @staticmethod
    async def _settle(view):
        await asyncio.sleep(0.2)
        await view.state_store._flush_notifications()
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_embed_and_embeds_deferred_in_one_send_ship_as_one_field(self):
        """Held content merged per keyword, so embed=A and then embeds=[B]
        deferred during one send replayed as one edit carrying both, which
        discord.py refuses: an ERROR, and neither reached the message."""
        first, second = self._message(1000), self._message(2000)
        context = self._context([first, second])
        view = self._gated()(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
        for _ in range(10):
            await asyncio.sleep(0)
        await view.refresh(embed=discord.Embed(title="A"))
        await view.refresh(embeds=[discord.Embed(title="B")])
        view.load_gate.set()
        await asyncio.wait_for(sending, 5)
        await self._settle(view)

        assert self._content(second) == [
            {"embeds": ["B"]}
        ], f"the held embed and embeds did not ship as one field: {self._content(second)}"

    async def test_a_resend_that_posted_nothing_shows_what_it_deferred_where_the_view_stays(self):
        """A render deferred during a re-send that on_pre_send vetoed stayed
        owed, so the panel still live did not show it; a later send then
        re-stamped the held embed as its own and edited it over the embed
        that send posted."""
        first, third = self._message(1000), self._message(3000)
        context = self._context([first, third])
        view = self._gated()(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        view.presend_gate = asyncio.Event()
        view.presend_answer = False
        vetoed = asyncio.create_task(view.send(embed=discord.Embed(title="vetoed")))
        for _ in range(10):
            await asyncio.sleep(0)
        await view.refresh(embed=discord.Embed(title="A"))
        view.presend_gate.set()
        assert await asyncio.wait_for(vetoed, 5) is None
        await self._settle(view)

        assert self._content(first) == [
            {"embed": "A"}
        ], f"the deferred embed never reached the message the view stayed on: {self._content(first)}"

        view.presend_gate = None
        view.presend_answer = True
        await view.send(embed=discord.Embed(title="B"))
        await self._settle(view)
        assert self._content(third) == [], f"older content covered the send: {self._content(third)}"

    async def test_content_held_past_a_send_that_did_not_post_does_not_cover_the_next(self):
        """When a reload held the view's turn, a vetoed re-send could not
        replay what it deferred, and the next send re-stamped that content
        as its own: the older embed was edited over the one it posted."""
        first, third = self._message(1000), self._message(3000)
        context = self._context([first, third])
        view = self._gated()(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        reloading = asyncio.create_task(view.reload())
        for _ in range(10):
            await asyncio.sleep(0)
        assert view._reload_task is reloading
        view.presend_gate = asyncio.Event()
        view.presend_answer = False
        vetoed = asyncio.create_task(view.send(embed=discord.Embed(title="vetoed")))
        for _ in range(10):
            await asyncio.sleep(0)
        await view.refresh(embed=discord.Embed(title="A"))
        view.presend_gate.set()
        assert await asyncio.wait_for(vetoed, 5) is None

        view.presend_gate = None
        view.presend_answer = True
        second = asyncio.create_task(view.send(embed=discord.Embed(title="B")))
        for _ in range(10):
            await asyncio.sleep(0)
        view.load_gate.set()
        view.load_gate = None
        assert await asyncio.wait_for(second, 5) is third
        await asyncio.wait_for(reloading, 5)
        await self._settle(view)

        assert self._content(third) == [], f"older content covered the send: {self._content(third)}"

    async def test_content_deferred_during_the_next_send_does_not_carry_older_content(self):
        """A refresh deferred during the next send merged into the content held
        from the send before it, so the older embed took the new send's mark
        and was edited over the embed that send posted."""
        first, third = self._message(1000), self._message(3000)
        context = self._context([first, third])
        view = self._gated()(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        reloading = asyncio.create_task(view.reload())
        for _ in range(10):
            await asyncio.sleep(0)
        view.presend_gate = asyncio.Event()
        view.presend_answer = False
        vetoed = asyncio.create_task(view.send(embed=discord.Embed(title="vetoed")))
        for _ in range(10):
            await asyncio.sleep(0)
        await view.refresh(embed=discord.Embed(title="A"))
        view.presend_gate.set()
        assert await asyncio.wait_for(vetoed, 5) is None

        view.presend_gate = None
        view.presend_answer = True
        second = asyncio.create_task(view.send(embed=discord.Embed(title="B")))
        for _ in range(10):
            await asyncio.sleep(0)
        await view.refresh(content="during B")
        view.load_gate.set()
        view.load_gate = None
        assert await asyncio.wait_for(second, 5) is third
        await asyncio.wait_for(reloading, 5)
        await self._settle(view)

        assert self._content(third) == [], f"older content covered the send: {self._content(third)}"
        shipped = [c.kwargs["content"] for c in third.edit.await_args_list if "content" in c.kwargs]
        assert shipped == ["during B"], f"the content deferred during the send was lost: {shipped}"

    async def test_content_held_past_a_send_does_not_cover_the_next_from_the_same_task(self):
        """The hold named the task running the send, so one task sending twice
        re-stamped content held from its first send as the second's own, and
        the older embed was edited over the one the second send posted."""
        first, third = self._message(1000), self._message(3000)
        context = self._context([first, third])
        view = self._gated()(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        reloading = asyncio.create_task(view.reload())
        for _ in range(10):
            await asyncio.sleep(0)
        gate = view.presend_gate = asyncio.Event()
        view.presend_answer = False
        second_started = asyncio.Event()

        async def send_twice():
            vetoed = await view.send(embed=discord.Embed(title="vetoed"))
            await asyncio.sleep(0.05)
            view.presend_gate = None
            view.presend_answer = True
            second_started.set()
            return vetoed, await view.send(embed=discord.Embed(title="B"))

        sending = asyncio.create_task(send_twice())
        for _ in range(10):
            await asyncio.sleep(0)
        await view.refresh(embed=discord.Embed(title="A"))
        gate.set()
        await asyncio.wait_for(second_started.wait(), 5)
        for _ in range(10):
            await asyncio.sleep(0)
        view.load_gate.set()
        view.load_gate = None
        vetoed, posted = await asyncio.wait_for(sending, 5)
        await asyncio.wait_for(reloading, 5)
        await self._settle(view)

        assert vetoed is None and posted is third
        assert self._content(third) == [], f"older content covered the send: {self._content(third)}"

    async def test_a_held_allowed_mentions_leaves_the_next_deferred_refresh_working(self):
        """The per-field merge looked every held keyword up as a content field,
        so a held allowed_mentions made the next deferred refresh() raise
        KeyError, and that update was lost."""
        first, second = self._message(1000), self._message(2000)
        context = self._context([first, second])
        view = self._gated()(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
        for _ in range(10):
            await asyncio.sleep(0)
        quiet = discord.AllowedMentions.none()
        await view.refresh(content="tick 1", allowed_mentions=quiet)
        await view.refresh(content="tick 2", allowed_mentions=quiet)
        view.load_gate.set()
        await asyncio.wait_for(sending, 5)
        await self._settle(view)

        shipped = [
            c.kwargs["content"] for c in second.edit.await_args_list if "content" in c.kwargs
        ]
        assert shipped == ["tick 2"], f"the newest deferred content did not ship: {shipped}"

    async def test_content_deferred_while_a_first_send_loads_reaches_the_message(self):
        """The release after on_load replayed while the first send was still
        posting: the view had no message, so the held embed was dropped."""
        posted = self._message(2000)

        async def post(**kwargs):
            # A real post is an HTTP request, so the send suspends here.
            await asyncio.sleep(0.05)
            return posted

        context = self._context(post)
        view = self._gated()(context=context)
        view.load_gate = asyncio.Event()
        sending = asyncio.create_task(view.send(embed=discord.Embed(title="S")))
        for _ in range(10):
            await asyncio.sleep(0)
        await view.refresh(embed=discord.Embed(title="A"))
        view.load_gate.set()
        await asyncio.wait_for(sending, 5)
        await self._settle(view)

        assert self._content(posted) == [
            {"embed": "A"}
        ], f"the embed deferred during the load was dropped: {self._content(posted)}"

    async def test_content_replayed_as_the_next_send_starts_does_not_cover_it(self):
        """The replay after a send renders from state before it ships the
        content held during the send. A send of the view that began during
        that render held the older embed as its own and edited it over the
        embed that send posted."""
        first, second, third = self._message(1000), self._message(2000), self._message(3000)

        async def slow_edit(**kwargs):
            await asyncio.sleep(0.2)
            return second

        second.edit = AsyncMock(side_effect=slow_edit)
        context = self._context([first, second, third])

        class Rendering(self._gated()):
            renders = 0

            async def on_state_changed(self, state):
                # A changed label, so the state render edits the message.
                self.renders += 1
                self.children[0].label = f"+1 ({self.renders})"
                await self.refresh()

        view = Rendering(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        resend = asyncio.create_task(view.send(embed=discord.Embed(title="S2")))
        for _ in range(10):
            await asyncio.sleep(0)
        await view.refresh(embed=discord.Embed(title="A"))
        view.load_gate.set()
        await asyncio.wait_for(resend, 5)
        view.load_gate = None
        await asyncio.wait_for(view.send(embed=discord.Embed(title="B")), 5)
        await self._settle(view)

        assert (
            self._content(third) == []
        ), f"older content was edited over the embed the send posted: {self._content(third)}"

    async def test_a_stalled_click_render_does_not_cover_the_embed_a_resend_posts(self):
        """A click's one-request edit stalled and a re-send of the view began
        before the click re-sent its render. The re-sent render was held as
        the new send's own and edited over the embed that send posted."""
        first, second = self._message(1000), self._message(2000)
        context = self._context([first, second])

        class Clicky(self._gated()):
            async def _noop(self, interaction):
                await self.refresh(embed=discord.Embed(title="click"))

        view = Clicky(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        hang = asyncio.Event()

        async def stuck(**kwargs):
            await hang.wait()

        # The context's author owns the view.
        click = _make_interaction(user_id=1, message=first)
        click.response.edit_message = AsyncMock(side_effect=stuck)
        button = next(i for i in view.children if getattr(i, "custom_id", None) == "g:inc")
        view.load_gate = asyncio.Event()
        try:
            clicking = asyncio.create_task(view._scheduled_task(button, click))
            await until(lambda: click.response.edit_message.await_count == 1, timeout=2)
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="B")))
            for _ in range(10):
                await asyncio.sleep(0)
            await asyncio.wait_for(clicking, 5)
            view.load_gate.set()
            await asyncio.wait_for(sending, 5)
        finally:
            view.load_gate.set()
            hang.set()
        await self._settle(view)

        assert (
            self._content(second) == []
        ), f"the stalled click's embed was edited over the re-send's: {self._content(second)}"

    async def test_a_stalled_render_resent_during_a_send_keeps_what_the_send_holds(self):
        """A click's render stalled before a send began and was re-sent while
        the send held another task's newer embed. The re-sent render replaced
        that embed in the hold, and neither reached the new message."""
        first, second = self._message(1000), self._message(2000)
        context = self._context([first, second])

        class Clicky(self._gated()):
            async def _noop(self, interaction):
                await self.refresh(embed=discord.Embed(title="click"))

        view = Clicky(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        hang = asyncio.Event()

        async def stuck(**kwargs):
            await hang.wait()

        click = _make_interaction(user_id=1, message=first)
        click.response.edit_message = AsyncMock(side_effect=stuck)
        button = next(i for i in view.children if getattr(i, "custom_id", None) == "g:inc")
        view.load_gate = asyncio.Event()
        try:
            clicking = asyncio.create_task(view._scheduled_task(button, click))
            await until(lambda: click.response.edit_message.await_count == 1, timeout=2)
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="B")))
            for _ in range(10):
                await asyncio.sleep(0)
            await view.refresh(embed=discord.Embed(title="A"))
            await asyncio.wait_for(clicking, 5)
            view.load_gate.set()
            await asyncio.wait_for(sending, 5)
        finally:
            view.load_gate.set()
            hang.set()
        await self._settle(view)

        assert self._content(second) == [
            {"embed": "A"}
        ], f"the embed deferred during the send did not reach its message: {self._content(second)}"

    @staticmethod
    def _final(message):
        """What ``message`` shows after its edits, for the content and embed fields."""
        state = {}
        for call in message.edit.await_args_list:
            if call.kwargs.get("embed") is not None:
                state["embed"] = call.kwargs["embed"].title
            if "content" in call.kwargs:
                state["content"] = call.kwargs["content"]
        return state

    @pytest.mark.parametrize("field", ["content", "embed"])
    @pytest.mark.parametrize("order", ["own_first", "stall_first"])
    async def test_a_vetoed_send_keeps_a_stalled_render_for_the_message_it_stays_on(
        self, order, field
    ):
        """A send that did not post left the view on its message. A click
        render that stalled before the send belongs there beside the content
        another task deferred during the send, and beneath that content where
        both set the embed. Whichever was held first, the click's content was
        dropped."""
        first, second = self._message(1000), self._message(2000)
        context = self._context([first, second])
        clicked = (
            {"content": "click"} if field == "content" else {"embed": discord.Embed(title="click")}
        )
        expected = {"embed": "A", "content": "click"} if field == "content" else {"embed": "A"}

        class Clicky(self._gated()):
            async def _noop(self, interaction):
                await self.refresh(**clicked)

        view = Clicky(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        hang = asyncio.Event()

        async def stuck(**kwargs):
            await hang.wait()

        click = _make_interaction(user_id=1, message=first)
        click.response.edit_message = AsyncMock(side_effect=stuck)
        button = next(i for i in view.children if getattr(i, "custom_id", None) == "g:inc")
        view.presend_gate = asyncio.Event()
        view.presend_answer = False
        try:
            clicking = asyncio.create_task(view._scheduled_task(button, click))
            await until(lambda: click.response.edit_message.await_count == 1, timeout=2)
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="B")))
            for _ in range(10):
                await asyncio.sleep(0)
            if order == "own_first":
                await view.refresh(embed=discord.Embed(title="A"))
                await asyncio.wait_for(clicking, 5)
            else:
                await asyncio.wait_for(clicking, 5)
                await view.refresh(embed=discord.Embed(title="A"))
            view.presend_gate.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            view.presend_gate.set()
            hang.set()
        await self._settle(view)

        assert (
            self._final(first) == expected
        ), f"the message the view stayed on shows the wrong content: {self._final(first)}"

    async def test_a_vetoed_send_keeps_what_the_send_before_it_held(self):
        """One task sent the view twice in a row, so the second send began
        before the replay of what the first send held. The second send held
        content of its own and was vetoed, and its content replaced the first
        send's, which belonged on the message the view stayed on."""
        first, second, third = self._message(1000), self._message(2000), self._message(3000)
        context = self._context([first, second, third])
        view = self._gated()(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        vetoing = asyncio.Event()

        async def both():
            await view.send(embed=discord.Embed(title="S2"))
            view.load_gate = None
            view.presend_gate = vetoing
            view.presend_answer = False
            return await view.send(embed=discord.Embed(title="B"))

        sending = asyncio.create_task(both())
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            await view.refresh(embed=discord.Embed(title="A"))
            view.load_gate.set()
            await until(lambda: view.presend_gate is vetoing and view._sending_task is sending)
            await view.refresh(content="X")
            vetoing.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            view.load_gate = None
            vetoing.set()
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), 5)
        await self._settle(view)

        assert self._final(second) == {
            "embed": "A",
            "content": "X",
        }, f"the message the view stayed on lost content: {self._final(second)}"

    @pytest.mark.parametrize("replayed", [False, True], ids=["held_left", "replayed"])
    async def test_a_vetoed_send_keeps_what_an_earlier_send_held(self, replayed):
        """Content held during one send waits for the replay after it. A later
        send that began before that replay finished held content of its own
        and was vetoed. The earlier content was dropped, though it belonged on
        the message the view stayed on. In the replayed case a state change
        deferred during the first send makes the replay render from state,
        slowly, and the later send begins while that render is in flight."""
        first, second, third = self._message(1000), self._message(2000), self._message(3000)

        async def slow_edit(**kwargs):
            await asyncio.sleep(0.2)
            return second

        second.edit = AsyncMock(side_effect=slow_edit)
        context = self._context([first, second, third])

        class Rendering(self._gated()):
            renders = 0

            async def on_state_changed(self, state):
                # A changed label, so the replay's state render edits the message.
                self.renders += 1
                self.children[0].label = f"+1 ({self.renders})"
                await self.refresh()

        view = Rendering(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        resend = asyncio.create_task(view.send(embed=discord.Embed(title="S2")))
        for _ in range(10):
            await asyncio.sleep(0)
        if replayed:
            # A state notification, deferred while the send runs, asked for
            # before A, so the replay renders it first.
            await view._render_from_state()
        await view.refresh(embed=discord.Embed(title="A"))
        view.load_gate.set()
        await asyncio.wait_for(resend, 5)
        view.load_gate = None
        view.presend_gate = asyncio.Event()
        view.presend_answer = False
        vetoed = asyncio.create_task(view.send(embed=discord.Embed(title="B")))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            if replayed:
                # The replay's state render lands, then A is held again for the
                # replay after this send.
                await asyncio.wait_for(view.task_manager.wait_tasks(view.id), 5)
                assert view.renders == 1
            await view.refresh(content="X")
            view.presend_gate.set()
            assert await asyncio.wait_for(vetoed, 5) is None
        finally:
            view.presend_gate.set()
        # The replay after the vetoed send ships the held edits.
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), 5)
        await self._settle(view)

        assert self._final(second) == {
            "embed": "A",
            "content": "X",
        }, f"the message the view stayed on lost content: {self._final(second)}"

    async def test_content_a_post_replaced_does_not_return_after_the_next_send(self):
        """A click render that stalled before a send was held as older than it.
        The send posted, which replaced it, but the hold outlived the post: the
        next send of the view was vetoed, and the click's content shipped onto
        the posted message, where it never belonged."""
        first, second, third = self._message(1000), self._message(2000), self._message(3000)
        context = self._context([first, second, third])

        class Clicky(self._gated()):
            entered = ()

            async def _noop(self, interaction):
                await self.refresh(content="click")

            async def on_load(self):
                self.entered += (("load", self.load_gate),)
                await super().on_load()

            async def on_pre_send(self, interaction):
                self.entered += (("presend", self.presend_gate),)
                return await super().on_pre_send(interaction)

        view = Clicky(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        hang = asyncio.Event()

        async def stuck(**kwargs):
            await hang.wait()

        click = _make_interaction(user_id=1, message=first)
        click.response.edit_message = AsyncMock(side_effect=stuck)
        button = next(i for i in view.children if getattr(i, "custom_id", None) == "g:inc")
        posting, vetoing = asyncio.Event(), asyncio.Event()

        async def sends():
            view.load_gate = posting
            await view.send(embed=discord.Embed(title="S2"))
            view.load_gate = None
            view.presend_gate = vetoing
            view.presend_answer = False
            return await view.send(embed=discord.Embed(title="S3"))

        try:
            clicking = asyncio.create_task(view._scheduled_task(button, click))
            await until(lambda: click.response.edit_message.await_count == 1, timeout=2)
            sending = asyncio.create_task(sends())
            await until(lambda: ("load", posting) in view.entered)
            await asyncio.wait_for(clicking, 5)
            await view.refresh(embed=discord.Embed(title="A"))
            posting.set()
            await until(lambda: ("presend", vetoing) in view.entered)
            await view.refresh(embed=discord.Embed(title="Y"))
            vetoing.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            posting.set()
            vetoing.set()
            hang.set()
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), 5)
        await self._settle(view)

        assert self._final(second) == {
            "embed": "Y"
        }, f"content the post replaced came back on its message: {self._final(second)}"

    async def test_what_a_vetoed_send_held_does_not_outlive_the_next_post(self):
        """One task sent the view again right after a vetoed send, so the
        content the vetoed send held was still waiting when the second send
        posted, which replaced it. A reload queued at that send's release kept
        it waiting past the post, and a stalled click re-sent during a third
        send gave it the click's newer number: the vetoed third send shipped
        it onto the second send's message."""
        first, second = self._message(1000), self._message(2000)
        posting, slow = asyncio.Event(), []
        posts = iter([first, second])

        async def post(*args, **kwargs):
            if slow:
                await posting.wait()
            return next(posts)

        class Clicky(self._gated()):
            # Long enough that the stall below ends when the test says so.
            auto_defer_delay = 2.9
            entered = ()

            async def _noop(self, interaction):
                await self.refresh(embed=discord.Embed(title="click"))

            async def on_pre_send(self, interaction):
                self.entered += (("presend", self.presend_gate),)
                return await super().on_pre_send(interaction)

        context = self._context(post)
        view = Clicky(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        vetoing, third_send, vetoing_again, loading, stalled = (asyncio.Event() for _ in range(5))

        async def stall(**kwargs):
            await stalled.wait()
            raise asyncio.TimeoutError()

        async def sends():
            view.presend_gate, view.presend_answer = vetoing, False
            vetoed = await view.send(embed=discord.Embed(title="B1"))
            view.presend_gate, view.presend_answer = None, True
            slow.append(True)
            posted = await view.send(embed=discord.Embed(title="S2"))
            await third_send.wait()
            view.presend_gate, view.presend_answer = vetoing_again, False
            return vetoed, posted, await view.send(embed=discord.Embed(title="B3"))

        click = _make_interaction(user_id=1, message=second)
        click.response.edit_message = AsyncMock(side_effect=stall)
        button = next(i for i in view.children if getattr(i, "custom_id", None) == "g:inc")
        try:
            sending = asyncio.create_task(sends())
            await until(lambda: ("presend", vetoing) in view.entered)
            await view.refresh(content="h1")
            vetoing.set()
            await until(lambda: context.send.await_count == 2)
            view.load_gate = loading
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._turn_claims == 1)
            posting.set()
            await until(lambda: view.message is second and view._sending_task is None)
            clicking = asyncio.create_task(view._scheduled_task(button, click))
            await until(lambda: click.response.edit_message.await_count == 1, timeout=2)
            third_send.set()
            await until(lambda: ("presend", vetoing_again) in view.entered)
            stalled.set()
            await asyncio.wait_for(clicking, 5)
            vetoing_again.set()
            assert await asyncio.wait_for(sending, 5) == (None, second, None)
            loading.set()
            await asyncio.wait_for(reloading, 5)
        finally:
            for gate in (vetoing, posting, third_send, vetoing_again, loading, stalled):
                gate.set()
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), 5)
        await self._settle(view)

        assert self._final(second) == {
            "embed": "click"
        }, f"content the post replaced came back on its message: {self._final(second)}"

    async def test_a_refused_older_edit_still_ships_the_sends_own_content(self):
        """Content older than a vetoed send ships before the send's own. When
        Discord refused that older edit, the send's own content was never sent
        and the failure was logged as a subscriber error."""
        first, second = self._message(1000), self._message(2000)
        landed = []

        async def edit(**kwargs):
            if kwargs.get("content") == "click":
                raise discord.HTTPException(MagicMock(status=400, reason="Bad Request"), "refused")
            if kwargs.get("embed") is not None:
                landed.append(kwargs["embed"].title)
            return first

        first.edit = AsyncMock(side_effect=edit)
        context = self._context([first, second])

        class Clicky(self._gated()):
            async def _noop(self, interaction):
                await self.refresh(content="click")

        view = Clicky(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        hang = asyncio.Event()

        async def stuck(**kwargs):
            await hang.wait()

        click = _make_interaction(user_id=1, message=first)
        click.response.edit_message = AsyncMock(side_effect=stuck)
        button = next(i for i in view.children if getattr(i, "custom_id", None) == "g:inc")
        view.presend_gate = asyncio.Event()
        view.presend_answer = False
        try:
            clicking = asyncio.create_task(view._scheduled_task(button, click))
            await until(lambda: click.response.edit_message.await_count == 1, timeout=2)
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="B")))
            for _ in range(10):
                await asyncio.sleep(0)
            await view.refresh(embed=discord.Embed(title="A"))
            await asyncio.wait_for(clicking, 5)
            view.presend_gate.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            view.presend_gate.set()
            hang.set()
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), 5)
        await self._settle(view)

        assert "A" in landed, f"the send's own content never shipped: {landed}"

    async def test_a_click_edit_that_fails_after_a_resend_stays_off_the_new_message(self):
        """A click's one-request edit was in flight when the view was sent
        again, then failed with a 500. The edit fell back to the view's
        message as it was by then, and the click's embed landed on the
        message the send had just posted."""
        first, second = self._message(1000), self._message(2000)
        context = self._context([first, second])

        class Clicky(self._gated()):
            outcome = None

            async def _noop(self, interaction):
                self.outcome = await self.refresh(embed=discord.Embed(title="click"))

        view = Clicky(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        release = asyncio.Event()

        async def failing(**kwargs):
            await release.wait()
            raise discord.HTTPException(MagicMock(status=500, reason="Server Error"), "boom")

        click = _make_interaction(user_id=1, message=first)
        click.response.edit_message = AsyncMock(side_effect=failing)
        button = next(i for i in view.children if getattr(i, "custom_id", None) == "g:inc")
        try:
            clicking = asyncio.create_task(view._scheduled_task(button, click))
            await until(lambda: click.response.edit_message.await_count == 1, timeout=2)
            # The send waits for the click's edit before it closes the message
            # it left, so the edit fails once the new message is posted.
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view.message is second, timeout=2)
            release.set()
            await asyncio.wait_for(clicking, 5)
            assert await asyncio.wait_for(sending, 5) is second
        finally:
            release.set()
        await self._settle(view)

        assert view.outcome is RenderOutcome.DROPPED
        assert self._content(second) == [], f"the click's embed reached the new message"

    async def test_held_content_replays_in_the_order_it_was_asked_for(self):
        """Two refreshes held during a send set different parts of the
        message, and the replay sends them as they were made, older first."""
        first = self._message(1000)
        view = self._gated()(context=self._context([first]))
        await view.send(embed=discord.Embed(title="S0"))
        view.presend_gate = asyncio.Event()
        view.presend_answer = False
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._sending_task is not None, timeout=2)
            await view.refresh(content="one")
            await view.refresh(embed=discord.Embed(title="two"))
            view.presend_gate.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            view.presend_gate.set()
        await self._settle(view)

        parts = [
            "content" if "content" in c.kwargs else "embed"
            for c in first.edit.await_args_list
            if "content" in c.kwargs or c.kwargs.get("embed") is not None
        ]
        assert parts == ["content", "embed"], parts

    async def test_a_click_edit_that_fails_after_an_interaction_resend_stays_off_it(self):
        """The same, with the view sent again from an interaction: the click's
        embed took the webhook handle that send set and landed on the message
        it had just posted."""
        first, second = self._message(1000), self._message(2000)

        class Clicky(self._gated()):
            outcome = None

            async def _noop(self, interaction):
                self.outcome = await self.refresh(embed=discord.Embed(title="click"))

        view = Clicky(context=self._context([first]))
        await view.send(embed=discord.Embed(title="S0"))
        release = asyncio.Event()

        async def failing(**kwargs):
            await release.wait()
            raise discord.HTTPException(MagicMock(status=500, reason="Server Error"), "boom")

        click = _make_interaction(user_id=1, message=first)
        click.response.edit_message = AsyncMock(side_effect=failing)
        posted = MagicMock(spec=discord.InteractionMessage)
        posted.id = 2000
        posted.edit = AsyncMock(return_value=posted)
        posted.channel.fetch_message = AsyncMock(return_value=second)
        again = _make_interaction(user_id=1)
        again.original_response = AsyncMock(return_value=posted)
        button = next(i for i in view.children if getattr(i, "custom_id", None) == "g:inc")
        try:
            clicking = asyncio.create_task(view._scheduled_task(button, click))
            await until(lambda: click.response.edit_message.await_count == 1, timeout=2)
            view.context = None
            view.interaction = again
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view.message is second, timeout=2)
            assert view._webhook_message is posted
            release.set()
            await asyncio.wait_for(clicking, 5)
            assert await asyncio.wait_for(sending, 5) is second
        finally:
            release.set()
        await self._settle(view)

        assert view.outcome is RenderOutcome.DROPPED
        clicked = [
            c for c in posted.edit.await_args_list if getattr(c.kwargs.get("embed"), "title", None)
        ]
        assert clicked == [], "the click's embed reached the new message"
        assert self._content(second) == []

    async def test_a_webhook_edit_that_fails_after_a_resend_keeps_the_new_handle(self):
        """An embed edit through the interaction webhook was in flight when the
        view was sent again from an interaction, then failed as an expired
        token. It cleared the webhook handle the send had just set, so the new
        message's embed edits went through the channel, which ignores them."""
        first, second = self._message(1000), self._message(2000)
        context = self._context([first, second])
        view = self._gated()(context=context)
        await view.send(embed=discord.Embed(title="S0"))
        release = asyncio.Event()

        async def expired(**kwargs):
            await release.wait()
            raise discord.HTTPException(MagicMock(status=401, reason="Unauthorized"), "expired")

        old_hook = MagicMock(id=1000)
        old_hook.edit = AsyncMock(side_effect=expired)
        new_hook = MagicMock(id=2000)
        view._webhook_message = old_hook
        try:
            editing = asyncio.create_task(view.refresh(embed=discord.Embed(title="old")))
            await until(lambda: old_hook.edit.await_count == 1, timeout=2)
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view.message is second, timeout=2)
            # What an interaction send sets for the message it posted.
            view._webhook_message = new_hook
            release.set()
            assert await asyncio.wait_for(editing, 5) is RenderOutcome.DROPPED
            assert await asyncio.wait_for(sending, 5) is second
        finally:
            release.set()

        assert view._webhook_message is new_hook

    # Mention rules: a V1 edit rebuilds mentions from its content and a V2 edit
    # from its tree, each under that edit's own rules (Discord's Edit Message
    # reference). Replaying held calls owes the rules their own edits had.

    _USERS = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)
    _QUIET = discord.AllowedMentions.none()

    @staticmethod
    def _rules(message, key):
        """The mention rules of each edit of ``message`` that carried ``key``."""
        return [
            call.kwargs["allowed_mentions"].to_dict()
            for call in message.edit.await_args_list
            if key in call.kwargs
        ]

    async def _two_refreshes_during_a_send(self, view_cls, first, second, calls):
        context = self._context([first, second])
        view = view_cls(context=context)
        await view.send()
        view.load_gate = asyncio.Event()
        sending = asyncio.create_task(view.send())
        for _ in range(10):
            await asyncio.sleep(0)
        for kwargs in calls:
            await view.refresh(**kwargs)
        view.load_gate.set()
        await asyncio.wait_for(sending, 5)
        await self._settle(view)
        return view

    def _quiet_v1(self):
        class Quiet(self._gated()):
            allowed_mentions = discord.AllowedMentions.none()

        return Quiet

    @staticmethod
    def _quiet_v2():
        from cascadeui.views.layout import StatefulLayoutView

        class QuietLayout(StatefulLayoutView):
            auto_defer_delay = 0.05
            allowed_mentions = discord.AllowedMentions.none()
            load_gate = None
            presend_gate = None
            presend_answer = True
            seed_gate = None
            seeding = False

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(discord.ui.TextDisplay("<@5> and <@6>"))

            async def on_pre_send(self, interaction):
                if self.presend_gate is not None:
                    await self.presend_gate.wait()
                return self.presend_answer

            async def seed_initial_state(self, state):
                if self.seed_gate is not None:
                    self.seeding = True
                    await self.seed_gate.wait()

            async def on_load(self):
                if self.load_gate is not None:
                    await self.load_gate.wait()

        return QuietLayout

    async def test_held_content_keeps_the_mention_rules_it_was_given_with(self):
        """A held call's mention rules were applied to a later call's content
        that set none, so its mentions were parsed under rules that content
        never had."""
        first, second = self._message(1000), self._message(2000)
        await self._two_refreshes_during_a_send(
            self._quiet_v1(),
            first,
            second,
            [
                {"content": "<@5> one", "allowed_mentions": self._USERS},
                {"content": "<@6> two"},
            ],
        )

        shipped = [
            c.kwargs["content"] for c in second.edit.await_args_list if "content" in c.kwargs
        ]
        assert shipped == ["<@6> two"], shipped
        assert self._rules(second, "content") == [
            self._QUIET.to_dict()
        ], "the newer content was parsed under the older call's mention rules"

    async def test_a_v1_edit_without_content_does_not_change_its_mention_rules(self):
        """Mention rules passed with an embed-only call were applied to content
        held from an earlier call. Discord rebuilds V1 mentions only when the
        content is edited, so that call's rules never governed it."""
        first, second = self._message(1000), self._message(2000)
        await self._two_refreshes_during_a_send(
            self._quiet_v1(),
            first,
            second,
            [
                {"content": "<@5> one"},
                {"embed": discord.Embed(title="E"), "allowed_mentions": self._USERS},
            ],
        )

        assert self._rules(second, "content") == [
            self._QUIET.to_dict()
        ], "the held content was parsed under an embed-only call's mention rules"

    async def test_a_v2_replay_uses_the_newest_calls_mention_rules(self):
        """A V2 edit rebuilds mentions from the whole tree, so the newest call
        governs. A held call's rules outlived a newer call that set none."""
        first, second = self._message(1000), self._message(2000)
        await self._two_refreshes_during_a_send(
            self._quiet_v2(),
            first,
            second,
            [
                {"attachments": [], "allowed_mentions": self._USERS},
                {"attachments": []},
            ],
        )

        assert self._rules(second, "attachments") == [
            self._QUIET.to_dict()
        ], "the replayed tree was parsed under an older call's mention rules"

    async def test_a_v2_replay_applies_mention_rules_a_newer_call_set(self):
        """The newer call of two set mention rules and passed no V1 content.
        A V2 edit rebuilds mentions from the tree under that call's rules, so
        the replay owes them even though no ``content`` came with them."""
        first, second = self._message(1000), self._message(2000)
        await self._two_refreshes_during_a_send(
            self._quiet_v2(),
            first,
            second,
            [
                {"attachments": []},
                {"attachments": [], "allowed_mentions": self._USERS},
            ],
        )

        assert self._rules(second, "attachments") == [
            self._USERS.to_dict()
        ], "the replayed tree dropped the mention rules its newest call set"

    async def test_a_later_v2_refresh_that_changes_nothing_keeps_the_mention_rules(self):
        """A plain refresh() of a V2 view whose tree did not change ships no
        edit, so it changes no mention rules; the held call's rules stand."""
        first, second = self._message(1000), self._message(2000)
        await self._two_refreshes_during_a_send(
            self._quiet_v2(),
            first,
            second,
            [{"attachments": [], "allowed_mentions": self._USERS}, {}],
        )

        assert self._rules(second, "attachments") == [
            self._USERS.to_dict()
        ], "a refresh() that edits nothing replaced the held call's mention rules"

    async def test_a_vetoed_send_replays_a_held_call_under_its_rules(self):
        """A V2 call held while a send was vetoed replays on the message the
        view stays on. The replayed edit carries no rules of its own, and the
        rules the call was made with were left with the vetoed send, so the
        edit went out under the view's default."""
        first = self._message(1000)
        view = self._quiet_v2()(context=self._context([first]))
        await view.send()
        view.presend_gate = asyncio.Event()
        view.presend_answer = False
        try:
            sending = asyncio.create_task(view.send())
            await until(lambda: view._sending_task is not None, timeout=2)
            held = await view.refresh(attachments=[], allowed_mentions=self._USERS)
            assert held is RenderOutcome.DEFERRED
            view.presend_gate.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            view.presend_gate.set()
        await self._settle(view)

        assert view.message is first
        assert self._rules(first, "attachments") == [
            self._USERS.to_dict()
        ], "the held call replayed under the view's default rules"

    async def test_a_send_cancelled_before_posting_replays_a_held_call_under_its_rules(self):
        """A send cancelled while another task's reload held the turn it
        waits for, before anything was posted, left the rules of a V2 call
        held during it behind, and the call replayed under the view's
        default."""
        from cascadeui.views.base import _RELOAD_WAITING

        first = self._message(1000)
        view = self._quiet_v2()(context=self._context([first]))
        await view.send()
        view.seed_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send())
            await until(lambda: view.seeding, timeout=2)
            # Takes the turn the send gave up after its own on_load().
            view.load_gate = asyncio.Event()
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.seed_gate.set()
            await until(lambda: _RELOAD_WAITING.get(sending) is view, timeout=2)
            held = await view.refresh(attachments=[], allowed_mentions=self._USERS)
            assert held is RenderOutcome.DEFERRED
            sending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sending
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
        finally:
            view.seed_gate.set()
            if view.load_gate is not None:
                view.load_gate.set()
        await self._settle(view)

        assert view.message is first
        assert self._rules(first, "attachments") == [
            self._USERS.to_dict()
        ], "the held call replayed under the view's default rules"

    async def test_a_state_render_the_replay_defers_again_keeps_its_place(self):
        """The replay after a send found the turn taken by a reload that
        started as the send ended. Its state render waited for that reload
        and a held call with mention rules shipped first. Numbered anew, the
        state render came after that call, changed the text, and put the
        view's default rules back, though the state changed first."""
        from cascadeui.views.layout import StatefulLayoutView

        shipped = []

        class Counting(StatefulLayoutView):
            auto_defer_delay = 0.05
            allowed_mentions = discord.AllowedMentions.none()
            veto_gate = None
            load_gate = None
            reloading = None

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.count = 0
                self.add_item(discord.ui.TextDisplay("<@5> count=0"))

            async def on_pre_send(self, interaction):
                if self.veto_gate is None:
                    return True
                await self.veto_gate.wait()
                # Queued ahead of the replay the vetoed send schedules.
                self.reloading = asyncio.create_task(self.reload())
                return False

            async def on_load(self):
                if self.load_gate is not None:
                    await self.load_gate.wait()

            async def on_state_changed(self, state):
                self.children[0].content = f"<@5> count={self.count}"
                await self.refresh()

        first = self._message(1000)

        async def recording(**kwargs):
            rules = kwargs.get("allowed_mentions")
            shipped.append(
                (view.children[0].content, "attachments" in kwargs, rules and rules.to_dict())
            )
            return first

        first.edit = AsyncMock(side_effect=recording)
        view = Counting(context=self._context([first]))
        await view.send()
        view.veto_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send())
            await until(lambda: view._sending_task is not None, timeout=2)
            view.count = 1
            await view._render_from_state()
            held = await view.refresh(attachments=[], allowed_mentions=self._USERS)
            assert held is RenderOutcome.DEFERRED
            view.load_gate = asyncio.Event()
            view.veto_gate.set()
            assert await asyncio.wait_for(sending, 5) is None
            await until(lambda: any(attached for _, attached, _ in shipped), timeout=2)
            view.load_gate.set()
            await asyncio.wait_for(view.reloading, 5)
        finally:
            view.veto_gate.set()
            if view.load_gate is not None:
                view.load_gate.set()
        await self._settle(view)

        assert shipped[0][:2] == ("<@5> count=0", True), "the held call did not ship first"
        assert shipped[-1] == ("<@5> count=1", False, self._USERS.to_dict()), shipped

    async def test_the_sends_own_rebuild_is_not_a_change_after_a_held_call(self):
        """A call held while the send loaded set mention rules, the send's
        on_load then rebuilt the tree (new text, a button with a generated id
        the post renames), and a refresh() with no keywords held after the
        post changed nothing. The rebuild belongs to the send, so the held
        rules stand."""
        from cascadeui.components.base import StatefulButton
        from cascadeui.views.layout import StatefulLayoutView

        first, second = self._message(1000), self._message(2000)
        posting = asyncio.Event()
        posts = iter([first, second])

        async def post(**kwargs):
            message = next(posts)
            if message is second:
                await posting.wait()
            return message

        class Loaded(StatefulLayoutView):
            auto_defer_delay = 0.05
            allowed_mentions = discord.AllowedMentions.none()
            load_gate = None
            loads = 0

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._compose()

            def _compose(self):
                # Built outside build_ui(), so the button keeps discord.py's
                # random id until a render ships the tree.
                self.clear_items()
                self.add_item(discord.ui.TextDisplay(f"<@5> load {self.loads}"))
                self.add_item(discord.ui.ActionRow(StatefulButton(label="go", callback=self._go)))

            async def _go(self, interaction):
                pass

            async def on_load(self):
                if self.load_gate is not None:
                    await self.load_gate.wait()
                self.loads += 1
                self._compose()

        context = self._context(post)
        view = Loaded(context=context)
        await view.send()
        view.load_gate = asyncio.Event()
        sending = asyncio.create_task(view.send())
        try:
            await until(lambda: view._reload_task is sending, timeout=2)
            held = await view.refresh(attachments=[], allowed_mentions=self._USERS)
            assert held is RenderOutcome.DEFERRED
            view.load_gate.set()
            await until(lambda: context.send.await_count == 2, timeout=2)
            assert await view.refresh() is RenderOutcome.DEFERRED
            posting.set()
            assert await asyncio.wait_for(sending, 5) is second
        finally:
            view.load_gate.set()
            posting.set()
        await self._settle(view)

        assert self._rules(second, "attachments") == [
            self._USERS.to_dict()
        ], "the send's own rebuild replaced the held call's mention rules"

    @staticmethod
    def _texted():
        from cascadeui.views.layout import StatefulLayoutView

        class Texted(StatefulLayoutView):
            auto_defer_delay = 0.05
            allowed_mentions = discord.AllowedMentions.none()
            load_gate = None
            render_gate = None
            rendering = False

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.text = "<@5> t0"
                self.add_item(discord.ui.TextDisplay(self.text))

            async def on_load(self):
                if self.load_gate is not None:
                    await self.load_gate.wait()

            async def on_state_changed(self, state):
                if self.render_gate is not None:
                    self.rendering = True
                    await self.render_gate.wait()
                self.children[0].content = self.text
                await self.refresh()

        return Texted

    async def test_a_late_render_held_by_the_next_send_keeps_a_newer_calls_rules(self):
        """The replay of a state change deferred during one send was still
        rendering when the next send began, and a V2 call with mention rules
        was held during that send. The late render, asked for before the
        call, was held after it and replaced the call's rules, so the call's
        attachments went out under the view's default."""
        first, second, third = self._message(1000), self._message(2000), self._message(3000)
        view = self._texted()(context=self._context([first, second, third]))
        await view.send()
        view.load_gate = asyncio.Event()
        view.render_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send())
            await until(lambda: view._reload_task is sending, timeout=2)
            view.text = "<@5> s1"
            await view._render_from_state()
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is second
            await until(lambda: view.rendering, timeout=2)
            view.load_gate = asyncio.Event()
            resending = asyncio.create_task(view.send())
            await until(lambda: view._sending_task is resending, timeout=2)
            held = await view.refresh(attachments=[], allowed_mentions=self._USERS)
            assert held is RenderOutcome.DEFERRED
            view.render_gate.set()
            await until(lambda: view._render_task is None, timeout=2)
            view.load_gate.set()
            assert await asyncio.wait_for(resending, 5) is third
        finally:
            view.load_gate.set()
            view.render_gate.set()
        await self._settle(view)

        assert [c.kwargs["allowed_mentions"].to_dict() for c in third.edit.await_args_list] == [
            self._USERS.to_dict()
        ], "an older render held by the send replaced the newer call's mention rules"

    async def test_a_task_a_late_render_starts_renders_as_its_own_call(self):
        """A state render replayed after a send started a task that fetched
        and then set an embed. The task copied the render's context and
        rendered as that older render, so an embed a click set meanwhile
        kept the newest call's embed off the message."""

        class Spawning(self._gated()):
            title = None
            fetched = None
            task = None

            async def on_state_changed(self, state):
                if self.title is None:
                    return
                if self.fetched is not None and self.task is None:
                    self.task = asyncio.create_task(self._fetch())
                await self.refresh(embed=discord.Embed(title=self.title))

            async def _fetch(self):
                await self.fetched.wait()
                await self.refresh(embed=discord.Embed(title="F"))

        first, second = self._message(1000), self._message(2000)
        view = Spawning(context=self._context([first, second]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        view.fetched = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            view.title = "st"
            await view._render_from_state()
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is second
            await until(lambda: view.task is not None, timeout=2)
            assert await view.refresh(embed=discord.Embed(title="C")) is RenderOutcome.RENDERED
            view.fetched.set()
            await asyncio.wait_for(view.task, 5)
        finally:
            view.load_gate.set()
            view.fetched.set()
        await self._settle(view)

        assert self._content(second)[-1:] == [
            {"embed": "F"}
        ], f"the spawned task's embed did not reach the message: {self._content(second)}"

    async def test_a_held_plain_refresh_keeps_its_place_after_a_replayed_edit_fails(self):
        """During a send, a state change was deferred, a plain refresh()
        changed the tree, and a call set mention rules. The replayed state
        render's edit failed at the transport, which clears the render
        baseline, so the held plain refresh shipped too. Numbered anew, it
        came after the call that set the rules and put the default back."""
        first, second = self._message(1000), self._message(2000)
        failures = [aiohttp.ClientConnectionError("connection reset")]

        async def edit(**kwargs):
            if failures:
                raise failures.pop()
            return second

        second.edit = AsyncMock(side_effect=edit)
        view = self._texted()(context=self._context([first, second]))
        await view.send()
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send())
            await until(lambda: view._reload_task is sending, timeout=2)
            view.text = "<@5> s1"
            await view._render_from_state()
            view.children[0].content = "<@5> tb"
            assert await view.refresh() is RenderOutcome.DEFERRED
            assert await view.refresh(allowed_mentions=self._USERS) is RenderOutcome.DEFERRED
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is second
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert not failures, "the replayed state render made no edit"
        last = second.edit.await_args_list[-1].kwargs["allowed_mentions"]
        assert (
            last.to_dict() == self._USERS.to_dict()
        ), "the held plain refresh put the default rules back over the newer call's"

    async def test_a_close_during_a_send_still_shows_what_it_held(self):
        """An exit() made while a re-send loaded was carried out by the send,
        which posted nothing. The replay of an embed held meanwhile was a task
        the close's teardown cancelled, so the embed never reached the
        message the view stayed on."""
        first = self._live_after_edits(self._message(1000))
        second = self._message(2000)
        view = self._gated()(context=self._context([first, second]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            assert await view.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            assert await asyncio.wait_for(view.exit(), 5) is True
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert view.message is first
        assert self._content(first) == [{"embed": "H"}], self._content(first)
        assert first.live and not first.live[-1], f"the message kept live controls: {first.live}"

    async def test_a_close_during_the_post_still_shows_what_the_send_held(self):
        """An exit() made while a re-send's post was in flight: the send
        posted and then closed the new message, and the close cancelled the
        replay of an embed held while the send loaded, so the embed never
        reached the message it was asked for on."""
        first = self._message(1000)
        second = self._live_after_edits(self._message(2000))
        posting, posted = asyncio.Event(), asyncio.Event()
        posts = iter([first, second])

        async def post(**kwargs):
            message = next(posts)
            if message is second:
                posted.set()
                await posting.wait()
            return message

        view = self._gated()(context=self._context(post))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            assert await view.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            view.load_gate.set()
            await asyncio.wait_for(posted.wait(), 5)
            assert await asyncio.wait_for(view.exit(), 5) is True
            posting.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            view.load_gate.set()
            posting.set()
        await self._settle(view)

        assert self._content(second) == [{"embed": "H"}], self._content(second)
        assert second.live and not second.live[-1], f"the message kept live controls: {second.live}"

    async def test_a_timeout_during_a_send_still_shows_what_it_held(self):
        """on_timeout() called while a re-send loaded: as with an exit(), the
        close's teardown cancelled the replay of an embed held meanwhile."""
        first = self._live_after_edits(self._message(1000))
        second = self._message(2000)
        view = self._gated()(context=self._context([first, second]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            assert await view.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            await asyncio.wait_for(view.on_timeout(), 5)
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert self._content(first) == [{"embed": "H"}], self._content(first)
        assert first.live and not first.live[-1], f"the message kept live controls: {first.live}"

    async def test_a_delete_during_a_send_edits_nothing_before_deleting(self):
        """A close that deletes the message leaves no replay running to edit
        it first with content held during the send, even when its teardown
        waits on a hook."""
        first, second = self._message(1000), self._message(2000)
        view = self._gated()(context=self._context([first, second]))
        await view.send(embed=discord.Embed(title="S0"))

        async def slow_hook(action, state):
            for _ in range(5):
                await asyncio.sleep(0)

        view.state_store.on("view_destroyed", slow_hook)
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            assert await view.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            assert await asyncio.wait_for(view.exit(delete_message=True), 5) is True
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is None
        finally:
            view.load_gate.set()
            view.state_store.off("view_destroyed", slow_hook)
        await self._settle(view)

        first.delete.assert_awaited_once()
        assert first.edit.await_count == 0, first.edit.await_args_list

    async def test_a_raising_state_render_leaves_held_content_to_ship(self, caplog):
        """A state change and an embed were both deferred during a re-send.
        The replay ran the state render and the held content under one try,
        so an on_state_changed that raised also dropped the embed."""

        class Raising(self._gated()):
            async def on_state_changed(self, state):
                raise KeyError("missing row")

        first, second = self._message(1000), self._message(2000)
        view = Raising(context=self._context([first, second]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            assert await view.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is second
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert any("missing row" in r.getMessage() for r in caplog.records), "nothing raised"
        assert self._content(second)[-1:] == [{"embed": "H"}], self._content(second)

    async def test_a_held_plain_refresh_that_fails_leaves_held_content_to_ship(self):
        """A plain refresh() and an embed were both held during a re-send, and
        the replayed plain refresh's edit failed. Its raise left the replay
        before the embed shipped."""
        first, second = self._message(1000), self._message(2000)
        posts = iter([first, second])
        failures = [discord.HTTPException(MagicMock(status=503, reason="Unavailable"), "busy")]

        async def post(**kwargs):
            message = next(posts)
            if message is second:
                # Another task changes the tree while the post is in flight.
                view.children[0].label = "changed"
            return message

        async def edit(**kwargs):
            if "embed" not in kwargs and failures:
                raise failures.pop()
            return second

        second.edit = AsyncMock(side_effect=edit)
        view = self._gated()(context=self._context(post))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            assert await view.refresh() is RenderOutcome.DEFERRED
            assert await view.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is second
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert not failures, "the plain refresh never reached the message"
        assert self._content(second)[-1:] == [{"embed": "H"}], self._content(second)

    async def test_a_late_render_made_through_gather_keeps_its_number(self):
        """A state change and then an embed were deferred during a re-send.
        The replayed on_state_changed rendered through asyncio.gather(), whose
        child task did not count as the late render, so the render took a
        number newer than the embed's and the embed never shipped."""

        class Gathering(self._gated()):
            async def on_state_changed(self, state):
                await asyncio.gather(self.refresh(embed=discord.Embed(title="state")))

        first, second = self._message(1000), self._message(2000)
        view = Gathering(context=self._context([first, second]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            assert await view.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is second
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert self._content(second)[-1:] == [{"embed": "H"}], self._content(second)

    @staticmethod
    def _cascade_warnings(caplog):
        return [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui") and r.levelno >= logging.WARNING
        ]

    async def test_a_task_a_late_render_starts_is_its_own_call_while_it_runs(self):
        """A state change was deferred behind a reload, a click then set an
        embed, and the replayed state render started a task that set another
        embed while the render was still running. That task took the late
        render's older number, so its embed, the newest, was dropped under
        the click's."""
        state_fetch, spawn_fetch, in_render = asyncio.Event(), asyncio.Event(), asyncio.Event()
        results = []

        class Spawning(self._gated()):
            title = None
            task = None

            async def on_state_changed(self, state):
                if self.title is None:
                    return
                if self.task is None:
                    self.task = asyncio.create_task(self._later())
                in_render.set()
                await state_fetch.wait()
                await self.refresh(embed=discord.Embed(title=self.title))

            async def _later(self):
                await spawn_fetch.wait()
                results.append(await self.refresh(embed=discord.Embed(title="F")))

        first = self._message(1000)
        view = Spawning(context=self._context([first]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.title = "st"
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            assert await view.refresh(embed=discord.Embed(title="C")) is RenderOutcome.RENDERED
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await asyncio.wait_for(in_render.wait(), 2)
            spawn_fetch.set()
            await until(lambda: results, timeout=2)
            state_fetch.set()
        finally:
            view.load_gate.set()
            spawn_fetch.set()
            state_fetch.set()
        await self._settle(view)

        assert results == [RenderOutcome.RENDERED], results
        assert self._content(first)[-1:] == [{"embed": "F"}], self._content(first)

    async def test_every_refresh_a_late_render_makes_takes_its_number(self):
        """A replayed state render made two refreshes after a click had set
        the content. The second took a number of its own, so the content the
        late render asked for put back what the newer click replaced."""

        class Twice(self._gated()):
            go = False

            async def on_state_changed(self, state):
                if self.go:
                    self.go = False
                    await self.refresh(embed=discord.Embed(title="st"))
                    await self.refresh(content="B")

        first = self._message(1000)
        view = Twice(context=self._context([first]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.go = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            assert await view.refresh(content="C") is RenderOutcome.RENDERED
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
        finally:
            view.load_gate.set()
        await self._settle(view)

        contents = [
            c.kwargs["content"] for c in first.edit.await_args_list if "content" in c.kwargs
        ]
        assert contents[-1:] == ["C"], contents

    async def test_a_render_that_coalesced_into_a_late_render_is_a_call_of_its_own(self):
        """A replayed state render, numbered for a change deferred behind a
        reload, showed nothing; a click set an embed while it fetched; and a
        newer state change coalesced into it. The re-run kept the late
        render's older number, so the newest change's embed was dropped under
        the click's."""
        fetching, fetched = asyncio.Event(), asyncio.Event()

        class Coalescing(self._gated()):
            armed = False
            title = None

            async def on_state_changed(self, state):
                if not self.armed:
                    return
                title = self.title
                fetching.set()
                await fetched.wait()
                if title is not None:
                    await self.refresh(embed=discord.Embed(title=title))

        first = self._message(1000)
        view = Coalescing(context=self._context([first]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.armed = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await asyncio.wait_for(fetching.wait(), 2)
            assert await view.refresh(embed=discord.Embed(title="C")) is RenderOutcome.RENDERED
            view.title = "N"
            await view._render_from_state()
            assert view._update_pending, "the newer change did not coalesce"
            fetched.set()
        finally:
            view.load_gate.set()
            fetched.set()
        await self._settle(view)

        assert self._content(first)[-1:] == [{"embed": "N"}], self._content(first)

    async def test_a_task_a_late_render_starts_is_its_own_call_once_the_render_returns(self):
        """A replayed state render started a task, and after the render had
        returned and a click had set an embed, that task refreshed through
        asyncio.gather(). The gathered refresh took the late render's older
        number, so its embed, the newest, was dropped under the click's, and
        every later one the task made would have been too."""
        tick = asyncio.Event()
        results = []

        class Ticking(self._gated()):
            go = False
            task = None

            async def on_state_changed(self, state):
                if self.go and self.task is None:
                    self.task = asyncio.create_task(self._tick())

            async def _tick(self):
                await tick.wait()
                (outcome,) = await asyncio.gather(self.refresh(embed=discord.Embed(title="T")))
                results.append(outcome)

        first = self._message(1000)
        view = Ticking(context=self._context([first]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.go = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            assert await view.refresh(embed=discord.Embed(title="C")) is RenderOutcome.RENDERED
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await until(lambda: view.task is not None, timeout=2)
            # The replayed render has returned; the task it started renders now.
            await self._settle(view)
            tick.set()
            await until(lambda: results, timeout=2)
        finally:
            view.load_gate.set()
            tick.set()
        await self._settle(view)

        assert results == [RenderOutcome.RENDERED], results
        assert self._content(first)[-1:] == [{"embed": "T"}], self._content(first)

    async def test_a_coalesced_render_deferred_after_a_late_render_is_a_call_of_its_own(self):
        """A replayed state render, numbered for a change deferred behind a
        reload, showed nothing; a click set an embed while it fetched; a newer
        state change coalesced into it; and another reload then took the
        turn. The coalesced re-run was deferred under the late render's older
        number, so when that reload released, the newest change's embed was
        dropped under the click's."""
        fetching, fetched = asyncio.Event(), asyncio.Event()

        class Coalescing(self._gated()):
            armed = False
            title = None

            async def on_state_changed(self, state):
                if not self.armed:
                    return
                title = self.title
                fetching.set()
                await fetched.wait()
                if title is not None:
                    await self.refresh(embed=discord.Embed(title=title))

        first = self._message(1000)
        view = Coalescing(context=self._context([first]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.armed = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await asyncio.wait_for(fetching.wait(), 2)
            assert await view.refresh(embed=discord.Embed(title="C")) is RenderOutcome.RENDERED
            view.title = "N"
            await view._render_from_state()
            assert view._update_pending, "the newer change did not coalesce"
            view.load_gate = asyncio.Event()
            # Waiting for the render to finish, the reload claims the turn.
            again = asyncio.create_task(view.reload())
            await until(lambda: view._turn_claims, timeout=2)
            fetched.set()
            await until(lambda: view._reload_task is again, timeout=2)
            assert view._deferred_origin, "the coalesced re-run was not deferred"
            view.load_gate.set()
            await asyncio.wait_for(again, 5)
        finally:
            view.load_gate.set()
            fetched.set()
        await self._settle(view)

        assert self._content(first)[-1:] == [{"embed": "N"}], self._content(first)

    async def test_another_views_render_inside_a_late_render_keeps_its_number(self):
        """A replayed state render, numbered for a change deferred behind a
        reload, updated a second view's session. That view rendered inline,
        and a notification that coalesced into its render re-ran it, which
        cleared the late render's number in the task they shared, so the late
        render's embed covered the newer click's."""
        b_gate, in_b = asyncio.Event(), asyncio.Event()

        class Other(self._gated()):
            subscribed_actions = None
            armed = False
            runs = 0

            async def on_state_changed(self, state):
                if not self.armed:
                    return
                self.runs += 1
                if self.runs == 1:
                    in_b.set()
                    await b_gate.wait()

        class Late(self._gated()):
            go = False
            other = None

            async def on_state_changed(self, state):
                if not self.go:
                    return
                self.go = False
                await self.other.update_session(seen=1)
                await self.refresh(embed=discord.Embed(title="st"))

        first = self._message(1000)
        view = Late(context=self._context([first]))
        await view.send(embed=discord.Embed(title="S0"))
        other = Other(context=self._context([self._message(1100)]))
        await other.send(embed=discord.Embed(title="B0"))
        view.other = other
        await self._settle(view)
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.go = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            assert await view.refresh(embed=discord.Embed(title="C")) is RenderOutcome.RENDERED
            other.armed = True
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await asyncio.wait_for(in_b.wait(), 3)
            await other._render_from_state()
            assert other._update_pending, "the second view's notification did not coalesce"
            b_gate.set()
        finally:
            view.load_gate.set()
            b_gate.set()
        await self._settle(view)
        await self._settle(other)

        assert other.runs == 2, other.runs
        assert self._content(first)[-1:] == [{"embed": "C"}], self._content(first)

    async def test_a_late_render_that_refreshes_twice_ships_the_later_mention_rules(self):
        """A state change deferred behind a reload was rendered late through
        two refreshes, the second with its own mention rules. The two share
        the late render's number, and the second's rules were ignored as
        though older, so the result went out under the first call's."""
        users = discord.AllowedMentions(everyone=False, users=True, roles=False)
        roles = discord.AllowedMentions(everyone=False, users=False, roles=True)

        class Twice(self._quiet_v2()):
            go = False

            async def on_state_changed(self, state):
                if self.go:
                    self.go = False
                    self.children[0].content = "<@5> x"
                    await self.refresh(allowed_mentions=users)
                    self.children[0].content = "<@5> y"
                    await self.refresh(allowed_mentions=roles)

        first = self._message(1000)
        view = Twice(context=self._context([first]))
        await view.send()
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.go = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert self._rules(first, "view")[-2:] == [users.to_dict(), roles.to_dict()], self._rules(
            first, "view"
        )

    async def test_a_replayed_render_that_navigates_sends_nothing_held_after_it(self, caplog):
        """During a re-send a plain refresh() was held and a state change was
        deferred. The replay's state render pushed another view, which took
        the message, and the replay then sent the held refresh through the
        view that had handed it on, warning about a call the user never
        made."""

        class Results(StatefulView):
            pass

        class Game(self._gated()):
            over = False
            pushed = None

            async def on_state_changed(self, state):
                if self.over:
                    self.over = False
                    self.pushed = await self.push(Results)

        first, second = self._message(1000), self._message(2000)
        view = Game(context=self._context([first, second]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            assert await view.refresh() is RenderOutcome.DEFERRED
            view.over = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is second
            await until(lambda: view.pushed is not None, timeout=2)
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert view.current_view is view.pushed
        assert not self._cascade_warnings(caplog), self._cascade_warnings(caplog)

    async def test_held_content_a_closed_bot_cannot_send_is_not_a_warning(self, caplog):
        """An embed held during a re-send was still to ship when the view's
        bot closed. Its edit failed on the closed session and was logged as a
        warning, which no other edit the library makes after its bot closed
        is."""
        closed = []
        first, second = self._message(1000), self._message(2000)

        async def edit(**kwargs):
            closed.append(True)
            await refused_by_closed_session()

        second.edit = AsyncMock(side_effect=edit)
        view = self._gated()(context=self._context([first, second]))
        view._client_closed = lambda: bool(closed)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            assert await view.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is second
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert second.edit.await_count == 1, "the held embed was never tried"
        assert not self._cascade_warnings(caplog), self._cascade_warnings(caplog)

    async def test_a_render_a_closed_bot_cannot_send_is_not_an_error(self, caplog):
        """A state change reached a view after its bot had closed, and the
        render's edit failed on the closed session. The failure reached the
        store as an error with a traceback, which no other edit the library
        makes after its bot closed is."""
        closed = []

        class Rendering(self._gated()):
            async def on_state_changed(self, state):
                await self.refresh(embed=discord.Embed(title="st"))

        first = self._message(1000)

        async def edit(**kwargs):
            if closed:
                await refused_by_closed_session()
            return first

        first.edit = AsyncMock(side_effect=edit)
        view = Rendering(context=self._context([first]))
        view._client_closed = lambda: bool(closed)
        await view.send(embed=discord.Embed(title="S0"))
        closed.append(True)
        await view._handle_state_notification(view.state_store.state, {"type": "TEST"})

        assert first.edit.await_count >= 1, "the render never reached the closed session"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, [r.getMessage() for r in errors]

    async def test_a_postponed_render_a_closed_bot_cannot_send_is_not_an_error(self, caplog):
        """A state change was deferred behind a reload, and the bot closed
        before the reload released. The replayed render's edit failed on the
        closed session and was logged as an error with a traceback."""
        closed, tried = [], []

        class Rendering(self._gated()):
            go = False

            async def on_state_changed(self, state):
                if self.go:
                    await self.refresh(embed=discord.Embed(title="st"))

        first = self._message(1000)

        async def edit(**kwargs):
            if closed and "embed" in kwargs:
                tried.append(True)
                await refused_by_closed_session()
            return first

        first.edit = AsyncMock(side_effect=edit)
        view = Rendering(context=self._context([first]))
        view._client_closed = lambda: bool(closed)
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.go = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            closed.append(True)
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert tried, "the replayed render never reached the closed session"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, [r.getMessage() for r in errors]

    async def test_a_freeze_after_held_v2_content_ends_under_the_views_rules(self):
        """A V2 view was sent again, another task's refresh with a file and
        its own mention rules was held during the post, and the view was
        closed meanwhile. The close froze the new message under the view's
        rules, and the held edit, sent after it, put its own rules back, so
        the frozen panel ended under rules its last render never set."""
        from cascadeui.components.base import StatefulButton

        users = discord.AllowedMentions(everyone=False, users=True, roles=False)
        first, second = self._message(1000), self._message(2000)
        posting, posted = asyncio.Event(), asyncio.Event()
        posts = iter([first, second])

        async def post(**kwargs):
            message = next(posts)
            if message is second:
                posting.set()
                await posted.wait()
            return message

        class WithControls(self._quiet_v2()):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                # Something to disable, so the close sends a freeze.
                button = StatefulButton(label="+1", custom_id="q:inc", callback=self._noop)
                self.add_item(discord.ui.ActionRow(button))

            async def _noop(self, interaction):
                pass

        view = WithControls(context=self._context(post))
        await view.send()
        try:
            sending = asyncio.create_task(view.send())
            await asyncio.wait_for(posting.wait(), 2)
            held = await view.refresh(
                attachments=[discord.File(io.BytesIO(b"h"), filename="h.txt")],
                allowed_mentions=users,
            )
            assert held is RenderOutcome.DEFERRED
            closing = asyncio.create_task(view.exit())
            for _ in range(10):
                await asyncio.sleep(0)
            posted.set()
            await asyncio.wait_for(sending, 5)
            await asyncio.wait_for(closing, 5)
        finally:
            posted.set()
        await self._settle(view)

        edits = [c.kwargs for c in second.edit.await_args_list if "view" in c.kwargs]
        shipped = [(sorted(e), e["allowed_mentions"].to_dict()) for e in edits]
        # The held edit lands before or after the freeze depending on the
        # interpreter's scheduling; either way the message ends under the
        # view's rules with the file on it.
        assert any("attachments" in e for e in edits), f"the held edit never shipped: {shipped}"
        assert shipped[-1][1] == discord.AllowedMentions.none().to_dict(), shipped

    async def test_a_late_render_that_closes_its_view_keeps_newer_mention_rules(self):
        """A state change was deferred behind a reload, and while it waited
        another task refreshed the view under its own mention rules. The late
        render then closed the view and put up a bare final card. The freeze
        recorded the view's rules over the newer call's, so the card went out
        under rules older than the ones the message already had."""
        from cascadeui.components.base import StatefulButton

        users = discord.AllowedMentions(everyone=False, users=True, roles=False)
        shown = []

        class Late(self._quiet_v2()):
            go = False
            outcome = None

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                button = StatefulButton(label="+1", custom_id="q:inc", callback=self._noop)
                self.add_item(discord.ui.ActionRow(button))

            async def _noop(self, interaction):
                pass

            async def on_state_changed(self, state):
                if self.go:
                    self.go = False
                    await self.exit()
                    self.children[0].content = "over"
                    self.outcome = await self.refresh()

        first = self._message(1000)

        async def edit(**kwargs):
            if "view" in kwargs:
                shown.append((kwargs["view"].children[0].content, kwargs.get("allowed_mentions")))
            return first

        first.edit = AsyncMock(side_effect=edit)
        view = Late(context=self._context([first]))
        await view.send()
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.go = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            view.children[0].content = "newer"
            await view.refresh(allowed_mentions=users)
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await until(lambda: view.outcome is not None, timeout=2)
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert view.outcome is RenderOutcome.RENDERED, view.outcome
        text, rules = shown[-1]
        assert text == "over", shown
        assert rules is not None and rules.to_dict() == users.to_dict(), shown

    async def test_content_held_before_a_replayed_render_that_navigates_lands_first(self):
        """During a re-send an embed was held and then a state change was
        deferred. The replay's state render pushed another view first, and
        the embed, asked for before that render, was dropped instead of
        reaching the message ahead of the push."""

        class Results(StatefulView):
            pass

        class Game(self._gated()):
            over = False
            pushed = None

            async def on_state_changed(self, state):
                if self.over:
                    self.over = False
                    self.pushed = await self.push(Results)

        first, second = self._message(1000), self._message(2000)
        view = Game(context=self._context([first, second]))
        await view.send(embed=discord.Embed(title="S0"))
        view.load_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send(embed=discord.Embed(title="S1")))
            await until(lambda: view._reload_task is sending, timeout=2)
            assert await view.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            view.over = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            view.load_gate.set()
            assert await asyncio.wait_for(sending, 5) is second
            await until(lambda: view.pushed is not None, timeout=2)
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert {"embed": "H"} in self._content(second), self._content(second)

    @pytest.mark.parametrize("close", ["exit", "timeout"])
    async def test_a_late_render_that_closes_its_view_still_ships_its_final_card(self, close):
        """A state change deferred behind a reload ended the session: its
        render closed the view, then put up a final card with its own mention
        rules. The card was skipped as older than the close's freeze, and then,
        once it shipped, it went out under the freeze's rules instead of its
        own."""
        from cascadeui.components.base import StatefulButton

        roles = discord.AllowedMentions(everyone=False, users=False, roles=True)
        shown = []

        class Late(self._quiet_v2()):
            go = False
            outcome = None

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                button = StatefulButton(label="+1", custom_id="q:inc", callback=self._noop)
                self.add_item(discord.ui.ActionRow(button))

            async def _noop(self, interaction):
                pass

            async def on_state_changed(self, state):
                if self.go:
                    self.go = False
                    await (self.exit() if close == "exit" else self.on_timeout())
                    self.children[0].content = "over"
                    self.outcome = await self.refresh(allowed_mentions=roles)

        first = self._message(1000)

        async def edit(**kwargs):
            if "view" in kwargs:
                view = kwargs["view"]
                live = [
                    item
                    for item in view.walk_children()
                    if isinstance(item, discord.ui.Button) and not item.disabled
                ]
                shown.append((view.children[0].content, bool(live), kwargs.get("allowed_mentions")))
            return first

        first.edit = AsyncMock(side_effect=edit)
        view = Late(context=self._context([first]))
        await view.send()
        view.load_gate = asyncio.Event()
        try:
            reloading = asyncio.create_task(view.reload())
            await until(lambda: view._reload_task is reloading, timeout=2)
            view.go = True
            await view._render_from_state()
            assert view._deferred_origin, "the state render was not deferred"
            view.load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await until(lambda: view.outcome is not None, timeout=2)
        finally:
            view.load_gate.set()
        await self._settle(view)

        assert view.outcome is RenderOutcome.RENDERED, view.outcome
        text, live, rules = shown[-1]
        assert (text, live) == ("over", False), shown
        assert rules.to_dict() == roles.to_dict(), shown

    async def test_a_stopped_child_its_parent_closes_still_shows_what_it_held(self):
        """A child sent again had an embed held during its post, and user code
        stopped it while the replay's edit was in flight. The parent's exit
        then tore the stopped child down, which cancelled the replay, so the
        embed never reached the child's message."""
        parent = self._gated()(context=self._context([self._message(500)]))
        await parent.send(embed=discord.Embed(title="P"))
        first, second = self._message(1000), self._message(2000)
        post_started, posting, edit_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
        posts = iter([first, second])
        landed = []

        async def post(**kwargs):
            message = next(posts)
            if message is second:
                post_started.set()
                await posting.wait()
            return message

        async def edit(**kwargs):
            await edit_gate.wait()
            embed = kwargs.get("embed")
            landed.append(embed.title if embed else None)
            return second

        second.edit = AsyncMock(side_effect=edit)
        child = self._gated()(context=self._context(post), parent=parent)
        await child.send(embed=discord.Embed(title="S0"))
        try:
            sending = asyncio.create_task(child.send(embed=discord.Embed(title="S1")))
            await asyncio.wait_for(post_started.wait(), 2)
            assert await child.refresh(embed=discord.Embed(title="H")) is RenderOutcome.DEFERRED
            posting.set()
            assert await asyncio.wait_for(sending, 5) is second
            await until(lambda: any(not t.done() for t in child._deliveries), timeout=2)
            child.stop()
            assert await asyncio.wait_for(parent.exit(), 5) is True
            edit_gate.set()
            await child.task_manager.wait_tasks(child.id)
        finally:
            posting.set()
            edit_gate.set()

        assert child._torn_down()
        assert "H" in landed, f"the held embed never reached the child's message: {landed}"

    async def test_a_late_call_held_by_a_vetoed_send_keeps_its_rules(self):
        """A state change arrived while a re-send's post was in flight, and
        its replay's on_state_changed made a V2 call with mention rules while
        the next send was being vetoed. The view stayed on its message, and
        the call's attachments replayed under the view's default instead of
        the rules it was made with."""
        users = self._USERS

        class Late(self._texted()):
            presend_gate = None

            async def on_pre_send(self, interaction):
                if self.presend_gate is None:
                    return True
                await self.presend_gate.wait()
                return False

            async def on_state_changed(self, state):
                if self.render_gate is not None:
                    self.rendering = True
                    await self.render_gate.wait()
                await self.refresh(attachments=[], allowed_mentions=users)

        first, second = self._message(1000), self._message(2000)
        posting, posted = asyncio.Event(), asyncio.Event()
        posts = iter([first, second])

        async def post(**kwargs):
            message = next(posts)
            if message is second:
                posted.set()
                await posting.wait()
            return message

        view = Late(context=self._context(post))
        await view.send()
        view.render_gate = asyncio.Event()
        try:
            sending = asyncio.create_task(view.send())
            await asyncio.wait_for(posted.wait(), 5)
            # Deferred, numbered after the post serialized its tree.
            await view._render_from_state()
            posting.set()
            assert await asyncio.wait_for(sending, 5) is second
            await until(lambda: view.rendering, timeout=2)
            view.presend_gate = asyncio.Event()
            vetoed = asyncio.create_task(view.send())
            await until(lambda: view._sending_task is vetoed, timeout=2)
            view.render_gate.set()
            await until(lambda: view._render_task is None, timeout=2)
            view.presend_gate.set()
            assert await asyncio.wait_for(vetoed, 5) is None
        finally:
            posting.set()
            view.render_gate.set()
            if view.presend_gate is not None:
                view.presend_gate.set()
        await self._settle(view)

        assert view.message is second
        assert self._rules(second, "attachments") == [
            users.to_dict()
        ], "the late call replayed under the view's default rules"


class TestClicksFromAnEarlierRenderAreAnswered:
    """A generated id follows the button's label, so a label that counts
    something gives the button a new id on each render. A click sent from
    the render before carried an id discord.py no longer knew, and it was
    dropped unanswered: "This interaction failed" (a rematch vote's second
    click, on the live test bot)."""

    @staticmethod
    def _bot():
        # A real client, never logged in: the send wires only a discord.Client.
        return discord.Client(intents=discord.Intents.none())

    @staticmethod
    def _game(calls):
        from discord.ui import ActionRow
        from helpers import RenderableLayoutView

        from cascadeui.components.base import StatefulButton

        class Game(RenderableLayoutView):
            auto_defer_delay = 0.05

            def __init__(self, **kwargs):
                self.votes = 0
                super().__init__(**kwargs)
                self.build_ui()

            def build_ui(self):
                self.clear_items()
                label = f"Rematch ({self.votes}/2)" if self.votes else "Rematch"
                self.add_item(ActionRow(StatefulButton(label=label, callback=self._rematch)))

            async def _rematch(self, interaction):
                calls.append(interaction)

        return Game

    @staticmethod
    def _button_id(view):
        return next(i.custom_id for i in view.walk_children() if hasattr(i, "custom_id"))

    async def _voted(self, bot, calls):
        interaction = _make_interaction()
        interaction.client = bot
        view = self._game(calls)(interaction=interaction)
        await view.send()
        store = bot._connection._view_store
        message_id = view.message.id
        store.add_view(view, message_id)
        before = self._button_id(view)
        view.votes = 1
        view.build_ui()
        await view.refresh()
        # discord.py registers the view again when the edit lands.
        store.add_view(view, message_id)
        assert (discord.ComponentType.button.value, before) not in store._views[message_id]
        return view, store, before

    async def test_a_click_from_before_the_label_changed_is_answered(self, caplog):
        caplog.set_level(logging.DEBUG, logger="cascadeui")
        calls = []
        view, store, before = await self._voted(self._bot(), calls)
        click = _make_interaction(message=MagicMock(id=view.message.id))

        store.dispatch_view(discord.ComponentType.button.value, before, click)
        try:
            await until(lambda: click.response.defer.await_count, timeout=2)
        except asyncio.TimeoutError:
            pass

        assert click.response.defer.await_count == 1, "the click went unanswered"
        assert calls == []
        assert any(
            "Dropped a click" in r.getMessage() and "not on the message any more" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    async def test_a_click_on_a_button_removed_before_its_edit_landed_is_answered(self, caplog):
        """remove_item() detaches a button at once, while discord.py keeps it
        routable until the edit lands; a click in between found no view and
        was discarded with only discord.py's warning."""
        from discord.ui import ActionRow
        from helpers import RenderableLayoutView

        from cascadeui.components.base import StatefulButton

        class Panel(RenderableLayoutView):
            auto_defer_delay = 0.05

        caplog.set_level(logging.DEBUG, logger="cascadeui")
        calls = []
        bot = self._bot()
        interaction = _make_interaction()
        interaction.client = bot
        view = Panel(interaction=interaction)
        keep = StatefulButton(label="Keep", custom_id="keep", callback=calls.append)
        gone = ActionRow(StatefulButton(label="Go", custom_id="go", callback=calls.append))
        view.add_item(ActionRow(keep))
        view.add_item(gone)
        await view.send()
        store = bot._connection._view_store
        store.add_view(view, view.message.id)
        view.remove_item(gone)
        assert (
            store._views[view.message.id][(discord.ComponentType.button.value, "go")].view is None
        )
        click = _make_interaction(message=MagicMock(id=view.message.id))

        store.dispatch_view(discord.ComponentType.button.value, "go", click)
        try:
            await until(lambda: click.response.defer.await_count, timeout=2)
        except asyncio.TimeoutError:
            pass

        assert click.response.defer.await_count == 1, "the click went unanswered"
        assert calls == []

    async def test_a_bot_handler_that_answers_the_click_keeps_the_response_slot(self):
        """discord.py dispatches ``on_interaction`` after the view store has
        looked the click up, so an answer made at once took the slot from a
        bot's own handler for unknown clicks, whose reply then failed with
        40060."""
        calls = []
        bot = self._bot()
        view, store, before = await self._voted(bot, calls)
        click = _make_interaction(message=MagicMock(id=view.message.id))
        handled = []

        @bot.event
        async def on_interaction(interaction):
            if not interaction.response.is_done():
                await interaction.response.send_message("handled by the bot")
                handled.append(interaction)

        # Entering the client gives it a loop to schedule events on; it does
        # not log in. The two calls are the order discord.py's
        # parse_interaction_create uses.
        async with bot:
            store.dispatch_view(discord.ComponentType.button.value, before, click)
            bot.dispatch("interaction", click)
            await asyncio.sleep(view.auto_defer_delay * 4)

        assert handled == [click], "the bot's own handler found the click already answered"
        click.response.defer.assert_not_awaited()
        assert calls == []

    async def test_a_click_on_the_current_render_still_runs(self, caplog):
        caplog.set_level(logging.DEBUG, logger="cascadeui")
        calls = []
        view, store, _ = await self._voted(self._bot(), calls)
        click = _make_interaction(message=MagicMock(id=view.message.id))

        store.dispatch_view(discord.ComponentType.button.value, self._button_id(view), click)
        await until(lambda: calls, timeout=2)

        assert calls == [click]
        assert not any("not on the message any more" in r.getMessage() for r in caplog.records)

    async def test_a_stale_click_on_a_plain_discord_view_is_left_to_discord(self):
        bot = self._bot()
        get_store()._answer_stale_clicks(bot)
        store = bot._connection._view_store
        plain = discord.ui.View()
        plain.add_item(discord.ui.Button(label="Go", custom_id="go"))
        store.add_view(plain, 555)
        click = _make_interaction(message=MagicMock(id=555))

        store.dispatch_view(discord.ComponentType.button.value, "gone", click)
        for _ in range(10):
            await asyncio.sleep(0)

        click.response.defer.assert_not_awaited()

    async def test_an_id_a_dynamic_item_matches_is_left_to_it(self):
        """A roles panel's buttons are dynamic items, and answering their
        click here would take the response slot their own reply needs."""
        import re

        calls = []
        view, store, before = await self._voted(self._bot(), calls)
        click = _make_interaction(message=MagicMock(id=view.message.id))
        store._dynamic_items[re.compile(r"role:(?P<id>\d+)")] = object

        found = get_store()._stale_click_view(store, 2, "role:42", click)

        assert found is None
        assert get_store()._stale_click_view(store, 2, before, click) is view

    async def test_a_store_a_bots_clear_replaced_is_wrapped_at_the_next_send(self):
        from discord.ui.view import ViewStore

        bot = self._bot()
        calls = []
        await self._voted(bot, calls)
        # What a bot's clear() does.
        replaced = ViewStore(bot._connection)
        bot._connection._view_store = replaced

        await self._voted(bot, calls)

        assert "dispatch_view" in vars(replaced)


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
        assert not any(
            rec.levelno >= logging.WARNING and rec.name.startswith("cascadeui")
            for rec in caplog.records
        )

    async def test_post_defer_genuine_failure_logged_at_warning(self, caplog):
        """A non-40060 HTTP failure is a real ack failure (the user saw an
        interaction-failed toast), so it surfaces at warning with the
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
        """``safe_defer`` cancels a stalled defer at ``auto_defer_delay`` and
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
        await view.safe_defer(interaction)  # must return, not hang
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

        await view.safe_defer(interaction)  # must not raise

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

        await view.safe_defer(interaction)  # must not raise

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

    async def test_a_file_the_lost_reply_read_is_sent_whole_as_a_followup(self):
        """The attempt that lost the ack race had read the file, and the
        followup uploaded it from where that read stopped: zero bytes."""
        import io

        payload = b"\x89PNG-weekly-report"
        interaction = _make_interaction(is_done=False)

        async def lost_race(*args, file=None, **kwargs):
            file.fp.read()
            raise discord.HTTPException(MagicMock(status=400), {"code": 40060, "message": "x"})

        interaction.response.send_message = AsyncMock(side_effect=lost_race)
        uploaded = []

        async def followup(*args, file=None, **kwargs):
            uploaded.append(file.fp.read())

        interaction.followup.send = AsyncMock(side_effect=followup)

        await respond_safe(
            interaction, "report", file=discord.File(io.BytesIO(payload), filename="report.png")
        )

        assert uploaded == [payload]


class TestSafeDeferSwallowsInteractionResponded:
    """``InteractionResponded`` is a sibling of ``HTTPException``.

    The auto-defer timer can ack inside this call's own await window, and
    on 3.10/3.11 ``wait_for`` widens that window enough to lose the race.
    The slot ends up acked either way, which is all ``safe_defer`` wanted,
    so its docstring promise that a failed ack is never propagated has to
    hold for this type too.
    """

    async def test_interaction_responded_is_absorbed(self):
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock(
            side_effect=discord.InteractionResponded(MagicMock())
        )

        await view.safe_defer(interaction)

    def test_interaction_responded_is_not_an_http_exception(self):
        """The reason the explicit clause is needed rather than inherited."""
        assert not issubclass(discord.InteractionResponded, discord.HTTPException)


class TestSafeDeferRename:
    """safe_defer() is the public name; the guides taught the private
    _safe_defer(), which keeps working with a warning until it is removed."""

    async def test_the_old_name_warns_and_still_defers(self):
        view = StatefulView(interaction=_make_interaction())
        interaction = _make_interaction(is_done=False)

        with pytest.warns(DeprecationWarning, match=r"call safe_defer\(\) instead") as caught:
            await view._safe_defer(interaction)

        # Attributed to the caller's line, which Python's default filters decide by.
        assert caught[0].filename == __file__
        interaction.response.defer.assert_awaited_once()

    async def test_an_override_of_the_old_name_runs_for_the_library_ack(self):
        acked = []
        with pytest.warns(DeprecationWarning, match=r"rename it safe_defer\(\)"):

            class _Legacy(StatefulView):
                ack_first = True

                async def _safe_defer(self, interaction):
                    acked.append(interaction)
                    await super()._safe_defer(interaction)

        view = _Legacy(interaction=_make_interaction())
        view.owner_only = False
        interaction = _make_interaction(is_done=False)

        with pytest.warns(DeprecationWarning, match=r"call safe_defer\(\) instead"):
            await view._scheduled_task(_make_item(AsyncMock()), interaction)

        assert acked == [interaction]
        interaction.response.defer.assert_awaited_once()


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
