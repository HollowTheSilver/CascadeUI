"""Tests for StatefulLayoutView (V2 base class)."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import ActionRow, Container, LayoutView, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.components.base import StatefulButton, StatefulSelect
from cascadeui.state.singleton import get_store
from cascadeui.state.store import _CURRENT_INTERACTION
from cascadeui.views.base import _StatefulMixin, _view_class_registry
from cascadeui.views.layout import DisplayLayoutView, StatefulLayoutView


class TestStatefulLayoutViewInit:
    """Basic init and inheritance tests."""

    def test_is_subclass_of_layout_view(self):
        assert issubclass(StatefulLayoutView, LayoutView)

    def test_is_subclass_of_mixin(self):
        assert issubclass(StatefulLayoutView, _StatefulMixin)

    def test_init_with_required_kwargs(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.state_store is not None
        assert view.user_id == 100
        assert view.guild_id == 200
        # Default polarity: session_continuity is False, so the derived
        # session_id carries a per-instance UUID suffix after the user id.
        prefix = f"{StatefulLayoutView._class_session_key()}:user_100:"
        assert view.session_id.startswith(prefix)
        assert len(view.session_id) == len(prefix) + 8

    def test_subscribes_to_state_on_init(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        store = get_store()
        assert view.id in store.subscribers

    def test_default_subscribed_actions(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.subscribed_actions == set()

    def test_auto_defer_defaults(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.auto_defer is True
        assert view.auto_defer_delay == 2.5

    def test_owner_only_defaults(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.owner_only is True


class TestMakeExitButton:
    """``make_exit_button`` returns an unattached button; ``add_exit_button`` wraps it."""

    def test_returns_unattached_button(self):
        from cascadeui.components.base import StatefulButton

        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        btn = view.make_exit_button(label="Close")
        after = len(list(view.children))

        assert isinstance(btn, StatefulButton)
        assert btn.label == "Close"
        assert after == before  # not attached

    def test_add_exit_button_still_attaches(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        view.add_exit_button()
        after = len(list(view.children))
        assert after == before + 1  # ActionRow wrapper added


class TestMakeBackButton:
    """``make_back_button`` returns an unattached Back button (V1 + V2)."""

    def test_returns_unattached_button(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        btn = view.make_back_button()
        after = len(list(view.children))

        assert isinstance(btn, StatefulButton)
        assert btn.label == "Back"
        assert after == before  # not attached

    def test_custom_label_and_custom_id(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        btn = view.make_back_button(label="Return", custom_id="nav_back")

        assert btn.label == "Return"
        assert btn.custom_id == "nav_back"

    def test_add_back_button_uses_helper(self):
        # The auto back button is built via make_back_button, so it carries
        # the same Back label and is stashed for rebuild restoration.
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        view._add_back_button()

        assert view._auto_back_item in list(view.children)


class TestMakeNavRow:
    """``make_nav_row`` combines Back + Exit into one ActionRow (V2)."""

    def test_returns_actionrow_with_both(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row()

        assert isinstance(row, ActionRow)
        assert [c.label for c in row.children] == ["Back", "Exit"]

    def test_back_only(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row(exit=False)

        assert [c.label for c in row.children] == ["Back"]

    def test_exit_only(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row(back=False)

        assert [c.label for c in row.children] == ["Exit"]

    def test_custom_labels(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row(back_label="Prev", exit_label="Close")

        assert [c.label for c in row.children] == ["Prev", "Close"]

    def test_custom_emoji_and_style(self):
        import discord

        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row(
            back_label="Leagues",
            back_emoji="\U0001f3e0",
            back_style=discord.ButtonStyle.primary,
        )
        back = list(row.children)[0]
        assert back.label == "Leagues"
        assert str(back.emoji) == "\U0001f3e0"
        assert back.style == discord.ButtonStyle.primary

    def test_default_emoji_matches_button_helpers(self):
        # The defaults mirror make_back_button / make_exit_button so the
        # shorthand and the manual composition render identically.
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row()
        back, exit_ = list(row.children)
        assert str(back.emoji) == str(view.make_back_button().emoji)
        assert str(exit_.emoji) == str(view.make_exit_button().emoji)

    def test_both_false_raises(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        with pytest.raises(ValueError, match="at least one"):
            view.make_nav_row(back=False, exit=False)

    def test_not_attached(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        view.make_nav_row()
        after = len(list(view.children))

        assert after == before  # returns the row, does not attach


class TestStatefulLayoutViewSubclass:
    """Subclass registration and kwargs auto-capture."""

    def test_subclass_registered_in_view_class_registry(self):
        class _TestLayoutPanel(StatefulLayoutView):
            pass

        key = _TestLayoutPanel._class_session_key()
        assert key in _view_class_registry
        assert _view_class_registry[key] is _TestLayoutPanel

    def test_init_kwargs_auto_captured(self):
        class _CustomLayout(StatefulLayoutView):
            def __init__(self, *args, title="default", **kwargs):
                self.title = title
                super().__init__(*args, **kwargs)

        interaction = _make_interaction()
        view = _CustomLayout(interaction=interaction, title="Dashboard")

        assert view.title == "Dashboard"
        assert view._init_kwargs == {"title": "Dashboard"}

    def test_non_reconstructible_kwargs_excluded(self):
        class _AnotherLayout(StatefulLayoutView):
            def __init__(self, *args, label="x", **kwargs):
                self.label = label
                super().__init__(*args, **kwargs)

        interaction = _make_interaction()
        view = _AnotherLayout(interaction=interaction, label="test")

        # interaction is non-reconstructible, should be excluded
        assert "interaction" not in view._init_kwargs
        assert view._init_kwargs == {"label": "test"}


class TestStatefulLayoutViewComponentBudget:
    """add_item re-messages discord.py's 40-component cap in the library's style."""

    def test_under_40_adds_normally(self):
        view = StatefulLayoutView()
        view.add_item(Container(*[TextDisplay(f"t{i}") for i in range(10)]))
        assert view._total_children == 11

    def test_over_40_raises_friendly_message(self):
        view = StatefulLayoutView()
        view.add_item(Container(*[TextDisplay(f"a{i}") for i in range(10)]))  # 11 nodes
        with pytest.raises(ValueError) as exc_info:
            view.add_item(Container(*[TextDisplay(f"b{i}") for i in range(40)]))
        msg = str(exc_info.value)
        assert "40-component" in msg
        assert "control_buttons" in msg
        assert "already at 11 components" in msg

    def test_chains_discord_py_error(self):
        # The friendly error chains discord.py's terse original via `from`.
        view = StatefulLayoutView()
        with pytest.raises(ValueError) as exc_info:
            view.add_item(Container(*[TextDisplay(f"x{i}") for i in range(41)]))
        cause = exc_info.value.__cause__
        assert isinstance(cause, ValueError)
        assert "maximum number of children exceeded" in str(cause)

    def test_unrelated_value_error_passes_through(self, monkeypatch):
        # A ValueError that is NOT the component-budget cap is re-raised
        # untouched -- the override only re-messages the 40-component error.
        def _boom(self, item):
            raise ValueError("some unrelated problem")

        monkeypatch.setattr(LayoutView, "add_item", _boom)
        view = StatefulLayoutView()
        with pytest.raises(ValueError, match="some unrelated problem"):
            view.add_item(TextDisplay("hi"))


class TestStatefulLayoutViewDispatch:
    """State dispatch and batch tests."""

    async def test_dispatch_forwards_to_store(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        result = await view.dispatch("VIEW_UPDATED", {"view_id": view.id})
        assert result is not None

    async def test_batch_returns_store_batch(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        batch = view.batch()
        assert batch is not None

    async def test_scoped_state_empty_without_scope(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.scoped_state == {}


class TestStatefulLayoutViewSend:
    """V2 ``StatefulLayoutView.send()`` basic routing, file forwarding,
    and rollback close. Sibling of ``TestStatefulViewSend`` in
    ``test_view_init.py``; same shape, V2 base path.
    """

    async def test_send_via_interaction(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        message = await view.send()

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args
        assert call_kwargs.kwargs["view"] is view

    async def test_send_registers_view(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        await view.send()

        assert view.id in store._active_views

    async def test_send_no_content_embed_params(self):
        """V2 send() has no content/embed/embeds params."""
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        # Only ephemeral is accepted
        message = await view.send(ephemeral=False)
        assert message is not None

    async def test_send_rollback_on_failure(self):
        interaction = _make_interaction()
        interaction.response.send_message = AsyncMock(side_effect=Exception("fail"))
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        with pytest.raises(Exception, match="fail"):
            await view.send()

        assert view.id not in store._active_views

    async def test_send_requires_context_or_interaction(self):
        view = StatefulLayoutView()

        with pytest.raises(RuntimeError, match="requires either"):
            await view.send()

    async def test_send_forwards_files_to_discord(self):
        """``files=`` reaches the underlying send call alongside ``view=``."""
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        photo = MagicMock(spec=discord.File)

        await view.send(files=[photo])

        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs["files"] == [photo]
        assert call_kwargs["view"] is view

    async def test_send_forwards_single_file_to_discord(self):
        """``file=`` (singular) reaches the underlying send call."""
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        photo = MagicMock(spec=discord.File)

        await view.send(file=photo)

        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs["file"] is photo

    async def test_send_omits_files_when_unset(self):
        """Send-kwargs carry no ``file``/``files`` keys unless callers supplied them."""
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        await view.send()

        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert "files" not in call_kwargs
        assert "file" not in call_kwargs

    async def test_send_failure_closes_file_handles(self):
        """Caller-supplied file handles get closed when the send raises before HTTP."""
        interaction = _make_interaction()
        interaction.response.send_message = AsyncMock(side_effect=Exception("fail"))
        view = StatefulLayoutView(interaction=interaction)
        photo1 = MagicMock(spec=discord.File)
        photo2 = MagicMock(spec=discord.File)

        with pytest.raises(Exception, match="fail"):
            await view.send(files=[photo1, photo2])

        photo1.close.assert_called_once()
        photo2.close.assert_called_once()

    async def test_send_failure_closes_singular_file(self):
        """``file=`` (singular) also gets closed on rollback."""
        interaction = _make_interaction()
        interaction.response.send_message = AsyncMock(side_effect=Exception("fail"))
        view = StatefulLayoutView(interaction=interaction)
        photo = MagicMock(spec=discord.File)

        with pytest.raises(Exception, match="fail"):
            await view.send(file=photo)

        photo.close.assert_called_once()


class TestSeedInitialState:
    """The seed_initial_state hook fires after registration, before notification."""

    async def test_default_hook_is_no_op(self):
        # The default hook does nothing -- existing views ship unchanged.
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        message = await view.send()
        assert message is not None

    async def test_hook_receives_state_dict(self):
        interaction = _make_interaction()
        captured = {}

        class SeedingView(StatefulLayoutView):
            async def seed_initial_state(self, state):
                captured["state"] = state

        view = SeedingView(interaction=interaction)
        await view.send()

        assert "state" in captured
        assert isinstance(captured["state"], dict)
        assert "views" in captured["state"]

    async def test_hook_runs_after_register_view(self):
        # When the hook fires, the view is already in the active registry
        # so the slot it seeds can reference its own view_id safely.
        interaction = _make_interaction()
        store = get_store()
        observed = {}

        class SeedingView(StatefulLayoutView):
            async def seed_initial_state(self, state):
                observed["registered"] = self.id in store._active_views

        view = SeedingView(interaction=interaction)
        await view.send()

        assert observed["registered"] is True

    async def test_hook_can_dispatch_inside_send_batch(self):
        # Dispatches from inside the seed hook collapse into the batch's
        # BATCH_COMPLETE notification rather than firing as a separate
        # subscriber pass. Verified by checking the action fires without
        # error inside the send pipeline.
        interaction = _make_interaction()
        store = get_store()

        async def _seed_reducer(action, state):
            new = {**state}
            app = {**state.get("application", {})}
            app["seeded_value"] = action["payload"].get("value")
            new["application"] = app
            return new

        store._register_reducer("SEED_TEST_ACTION", _seed_reducer)

        class SeedingView(StatefulLayoutView):
            async def seed_initial_state(self, state):
                await self.dispatch("SEED_TEST_ACTION", {"value": "seeded"})

        try:
            view = SeedingView(interaction=interaction)
            await view.send()

            assert store.state["application"].get("seeded_value") == "seeded"
        finally:
            store._unregister_reducer("SEED_TEST_ACTION")

    async def test_hook_failure_propagates(self):
        # If the override raises, the send pipeline surfaces the error. No
        # silent swallowing -- a broken seed should break the send so the
        # subclass author sees the bug immediately.
        interaction = _make_interaction()

        class BrokenSeedView(StatefulLayoutView):
            async def seed_initial_state(self, state):
                raise RuntimeError("seed broke")

        view = BrokenSeedView(interaction=interaction)
        with pytest.raises(RuntimeError, match="seed broke"):
            await view.send()


class TestOnLoadHook:
    """The on_load hook runs an async preload before the view is displayed.

    Sibling of seed_initial_state, but earlier in the pipeline: on_load
    fires before placement validation and the Discord send so the first
    render reflects loaded data, where seed_initial_state seeds state
    slots after registration.
    """

    async def test_default_hook_is_no_op(self):
        # Views without async preload ship unchanged -- the default does nothing.
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        message = await view.send()
        assert message is not None

    async def test_hook_runs_during_send(self):
        interaction = _make_interaction()
        observed = {}

        class LoadingView(StatefulLayoutView):
            async def on_load(self):
                observed["loaded"] = True

        view = LoadingView(interaction=interaction)
        await view.send()

        assert observed.get("loaded") is True

    async def test_hook_runs_before_message_ships(self):
        # The preload completes before the Discord send, so a view can
        # build its tree against loaded data and have that be what ships.
        interaction = _make_interaction()
        order = []

        original_send = interaction.response.send_message

        async def _tracking_send(*args, **kwargs):
            order.append("send")
            return await original_send(*args, **kwargs)

        interaction.response.send_message = AsyncMock(side_effect=_tracking_send)

        class LoadingView(StatefulLayoutView):
            async def on_load(self):
                order.append("on_load")

        view = LoadingView(interaction=interaction)
        await view.send()

        assert order == ["on_load", "send"]

    async def test_hook_runs_before_placement_check(self):
        # on_load builds the tree, so it must run before _check_placement
        # validates it. Adding a child in on_load and asserting the
        # placement check saw it proves the ordering.
        interaction = _make_interaction()
        seen_children = {}

        class LoadingView(StatefulLayoutView):
            async def on_load(self):
                self.add_item(ActionRow(StatefulButton(label="Loaded")))

            def _check_placement(self):
                seen_children["count"] = len(list(self.children))
                return super()._check_placement()

        view = LoadingView(interaction=interaction)
        await view.send()

        assert seen_children.get("count", 0) >= 1

    async def test_hook_failure_propagates(self):
        # A broken preload breaks the send so the subclass author sees it.
        interaction = _make_interaction()

        class BrokenLoadView(StatefulLayoutView):
            async def on_load(self):
                raise RuntimeError("load broke")

        view = BrokenLoadView(interaction=interaction)
        with pytest.raises(RuntimeError, match="load broke"):
            await view.send()

    async def test_reload_runs_on_load_then_refresh(self):
        # reload() is the out-of-band convenience: on_load, then refresh.
        interaction = _make_interaction()
        order = []

        class LoadingView(StatefulLayoutView):
            async def on_load(self):
                order.append("on_load")

            async def refresh(self, **kwargs):
                order.append("refresh")

        view = LoadingView(interaction=interaction)
        await view.send()
        order.clear()

        await view.reload()

        assert order == ["on_load", "refresh"]


class TestOnLoadDurationWarning:
    """A slow on_load (over auto_defer_delay) logs a one-per-class warning so a
    render-path preload that competes with interaction acks becomes visible. The
    no-op default is skipped entirely (zero overhead).
    """

    async def test_slow_on_load_warns_once(self, caplog):
        from cascadeui.views import base as base_mod

        base_mod._slow_on_load_warned.clear()

        class SlowView(StatefulLayoutView):
            auto_defer_delay = 0.01  # tight budget so a small real delay overruns

            async def on_load(self):
                await asyncio.sleep(0.05)  # reliably over 0.01s

        view = SlowView(interaction=_make_interaction())
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view._run_on_load()

        warns = [r for r in caplog.records if "on_load() took" in r.message]
        assert len(warns) == 1
        assert "SlowView" in warns[0].message

    async def test_fast_on_load_no_warning(self, caplog):
        from cascadeui.views import base as base_mod

        base_mod._slow_on_load_warned.clear()

        class QuickView(StatefulLayoutView):
            async def on_load(self):  # near-instant, under the 2.5s default budget
                pass

        view = QuickView(interaction=_make_interaction())
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view._run_on_load()

        assert not [r for r in caplog.records if "on_load() took" in r.message]

    async def test_dedup_per_class(self, caplog):
        from cascadeui.views import base as base_mod

        base_mod._slow_on_load_warned.clear()

        class SlowView(StatefulLayoutView):
            auto_defer_delay = 0.01

            async def on_load(self):
                await asyncio.sleep(0.05)

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await SlowView(interaction=_make_interaction())._run_on_load()
            await SlowView(interaction=_make_interaction())._run_on_load()

        warns = [r for r in caplog.records if "on_load() took" in r.message]
        assert len(warns) == 1  # second slow run, same class -> no second warning

    async def test_default_noop_no_warning(self, caplog):
        # The default no-op on_load is skipped entirely -- no timing, no warning,
        # even with a tight budget that a timed no-op might trip.
        from cascadeui.views import base as base_mod

        base_mod._slow_on_load_warned.clear()

        class DefaultView(StatefulLayoutView):
            auto_defer_delay = 0.0001  # default on_load -- never timed, so never warns

        view = DefaultView(interaction=_make_interaction())
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view._run_on_load()

        assert not [r for r in caplog.records if "on_load() took" in r.message]


class TestOnPreSendHook:
    """on_pre_send is the pre-send veto gate: it runs first in the send
    pipeline and aborts the send cleanly when it returns False.
    """

    async def test_default_hook_allows_send(self):
        # The default returns True, so a view without an override sends normally.
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        message = await view.send()
        assert message is not None

    async def test_veto_aborts_send(self):
        # Returning False aborts: no message ships, send() returns None.
        interaction = _make_interaction()

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                return False

        view = GatedView(interaction=interaction)
        message = await view.send()

        assert message is None
        interaction.response.send_message.assert_not_called()

    async def test_falsy_non_false_return_also_vetoes(self):
        # The gate is `if not await on_pre_send(...)`, so any falsy value
        # (None, 0) vetoes, not only an explicit False.
        interaction = _make_interaction()

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                return None

        view = GatedView(interaction=interaction)
        message = await view.send()

        assert message is None
        interaction.response.send_message.assert_not_called()

    async def test_veto_runs_before_on_load(self):
        # The gate runs first, so a veto skips the on_load preload entirely.
        interaction = _make_interaction()
        order = []

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                order.append("pre_send")
                return False

            async def on_load(self):
                order.append("on_load")

        view = GatedView(interaction=interaction)
        await view.send()

        assert order == ["pre_send"]  # on_load never ran

    async def test_hook_receives_triggering_interaction(self):
        interaction = _make_interaction()
        seen = {}

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                seen["interaction"] = interaction
                return True

        view = GatedView(interaction=interaction)
        await view.send()

        assert seen["interaction"] is interaction

    async def test_veto_leaves_no_state(self):
        # A vetoed send leaves zero side effects: the view stops, its
        # subscriber is removed, and it never registers in either registry.
        interaction = _make_interaction()
        store = get_store()

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                return False

        view = GatedView(interaction=interaction)
        await view.send()

        assert view.is_finished()
        assert view.id not in store.subscribers
        assert view.id not in store.state["views"]
        assert view.id not in store._active_views

    async def test_override_can_respond_through_open_slot(self):
        # The response slot is still open inside on_pre_send, so an override
        # can tell the user why the send was vetoed.
        interaction = _make_interaction()

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                await self.respond(interaction, "Not allowed", ephemeral=True)
                return False

        view = GatedView(interaction=interaction)
        message = await view.send()

        assert message is None
        interaction.response.send_message.assert_called_once()


class TestOnTimeoutLogLevel:
    """on_timeout downgrades the expected ephemeral token-expiry edit
    failure to DEBUG; a non-ephemeral edit failure stays WARNING. The
    branch is gated on ``_ephemeral``, so any edit exception exercises it.
    """

    async def test_ephemeral_edit_failure_logs_debug(self, caplog):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        await view.send()
        view._ephemeral = True
        view._message = MagicMock()
        view._message.edit = AsyncMock(side_effect=RuntimeError("Invalid Webhook Token"))

        with caplog.at_level(logging.DEBUG, logger="cascadeui.views.base"):
            await view.on_timeout()

        debug_records = [
            r for r in caplog.records if "Skipped disabling components" in r.getMessage()
        ]
        assert debug_records
        assert all(r.levelno == logging.DEBUG for r in debug_records)
        # The old WARNING line must not fire for the ephemeral case.
        assert not [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "disable components" in r.getMessage()
        ]

    async def test_non_ephemeral_edit_failure_logs_warning(self, caplog):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        await view.send()
        view._ephemeral = False
        view._message = MagicMock()
        view._message.edit = AsyncMock(side_effect=RuntimeError("boom"))

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view.on_timeout()

        warning_records = [
            r for r in caplog.records if "Could not disable components on timeout" in r.getMessage()
        ]
        assert warning_records
        assert all(r.levelno == logging.WARNING for r in warning_records)


class TestStatefulLayoutViewInteraction:
    """Interaction check and owner_only tests."""

    async def test_owner_only_rejects_other_user(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)

        other_interaction = _make_interaction(user_id=999)
        result = await view.interaction_check(other_interaction)

        assert result is False

    async def test_owner_only_allows_owner(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)

        same_interaction = _make_interaction(user_id=100)
        result = await view.interaction_check(same_interaction)

        assert result is True

    async def test_owner_only_disabled(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)
        view.owner_only = False

        other_interaction = _make_interaction(user_id=999)
        result = await view.interaction_check(other_interaction)

        assert result is True


class TestStatefulLayoutViewCleanup:
    """Exit and cleanup tests."""

    async def test_exit_unregisters_view(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()
        store._register_view(view)

        assert view.id in store._active_views

        await view.exit()

        assert view.id not in store._active_views

    async def test_exit_unsubscribes(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        assert view.id in store.subscribers

        await view.exit()

        assert view.id not in store.subscribers


class TestStableCustomIds:
    """``_stabilize_custom_ids`` rewrites auto-generated ids post-build.

    discord.py assigns a random hex custom_id to any Button/Select without
    an explicit ``custom_id=``. Every ``build_ui()`` rebuild produces
    fresh UUIDs, which causes the ViewStore dispatch table to churn and
    creates a race window where pending user clicks reference evicted
    entries. Stabilization rewrites auto-generated ids to deterministic
    anchors so repeat rebuilds produce identical dispatch keys.
    """

    def _make_view_with_build(self, build_fn):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        return _V(interaction=_make_interaction())

    def test_explicit_custom_id_preserved(self):
        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire", custom_id="user_chosen_id"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()

        btn = next(view.walk_children())
        # ActionRow first, button second
        button = [c for c in view.walk_children() if isinstance(c, StatefulButton)][0]
        assert button.custom_id == "user_chosen_id"

    def test_link_and_premium_buttons_not_stabilized(self):
        # Link (url) and premium (sku_id) buttons forbid a custom_id; the
        # stabilizer must skip both, or Discord 400s (code 50035, "custom id
        # and url cannot both be specified").
        import discord

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(discord.ui.Button(label="Docs", url="https://example.com")))
            view.add_item(
                ActionRow(discord.ui.Button(style=discord.ButtonStyle.premium, sku_id=123456789))
            )
            view.add_item(ActionRow(StatefulButton(label="Fire")))

        view = self._make_view_with_build(build)
        view.build_ui()

        buttons = [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]
        link = next(b for b in buttons if b.url)
        premium = next(b for b in buttons if getattr(b, "sku_id", None))
        interactive = next(b for b in buttons if not b.url and not getattr(b, "sku_id", None))
        assert link.custom_id is None
        assert premium.custom_id is None
        assert interactive.custom_id is not None

    def test_auto_generated_id_rewritten_with_content_anchor(self):
        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire"),
                    StatefulButton(label="Close"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()

        buttons = [c for c in view.walk_children() if isinstance(c, StatefulButton)]
        # Content-unique -> content-only id, anchored to view prefix.
        prefix = view.id[:8]
        assert buttons[0].custom_id.startswith(f"{prefix}:")
        assert "Fire" in buttons[0].custom_id
        assert "Close" in buttons[1].custom_id
        # Different buttons get different ids.
        assert buttons[0].custom_id != buttons[1].custom_id

    def test_repeat_build_produces_identical_ids(self):
        """The core promise: rebuild must not churn the dispatch table."""

        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire"),
                    StatefulButton(label="Close"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()
        first = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        view.build_ui()
        second = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        assert first == second

    def test_colliding_content_uses_position_anchor(self):
        """A 3x3 grid of identical-callback cells must disambiguate by position.

        Mirrors the TicTacToe grid pattern: many buttons share one callback
        family and an identical (empty) label. Each cell's id must be
        anchored to its tree coordinates so a label change on one cell
        does not shift the ids of the others.
        """

        def build(view):
            view.clear_items()
            for _ in range(3):
                view.add_item(
                    ActionRow(
                        StatefulButton(label=""),
                        StatefulButton(label=""),
                        StatefulButton(label=""),
                    )
                )

        view = self._make_view_with_build(build)
        view.build_ui()
        ids = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        # All nine cells must have distinct ids despite identical content.
        assert len(set(ids)) == 9

    def test_label_change_does_not_shift_neighbor_ids(self):
        """The regression the hybrid algorithm exists to prevent.

        Turn 1: nine empty cells -> collide on content -> position-anchored.
        Turn 2: one cell gets "X" -> its content key becomes unique.
        The remaining eight empty cells must keep their turn 1 ids.
        """
        marks = [""] * 9

        def build(view):
            view.clear_items()
            for row in range(3):
                view.add_item(
                    ActionRow(*(StatefulButton(label=marks[row * 3 + c]) for c in range(3)))
                )

        view = self._make_view_with_build(build)
        view.build_ui()
        turn1 = {
            i: c.custom_id
            for i, c in enumerate(b for b in view.walk_children() if isinstance(b, StatefulButton))
        }

        marks[4] = "X"  # center cell played
        view.build_ui()
        turn2 = {
            i: c.custom_id
            for i, c in enumerate(b for b in view.walk_children() if isinstance(b, StatefulButton))
        }

        # The played cell's id is allowed to change (it is now disabled
        # and no click will ever route to it again).
        # Every other cell's id must be identical to turn 1.
        for i in range(9):
            if i == 4:
                continue
            assert turn1[i] == turn2[i], f"cell {i} id shifted: {turn1[i]} -> {turn2[i]}"


class TestStableCustomIdsAtRefresh:
    """``refresh()`` stabilizes custom_ids for rebuild paths that bypass ``build_ui``.

    Tab switches, paginated page flips, wizard/form step advances, and menu
    category changes all rebuild the component tree outside ``build_ui``.
    Without the refresh-time stabilization, fresh interactive items carry
    ``os.urandom(16).hex()`` ids and the ViewStore dispatch table churns on
    every message.edit -- dropping any in-flight click that arrived after
    the edit but before the client rendered the new payload.
    """

    def _make_view(self):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Initial")))

        view = _V(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.id = 12345
        return view

    async def test_refresh_stabilizes_ids_for_rebuild_outside_build_ui(self):
        view = self._make_view()

        # Simulate a tab/paginated-style rebuild: clear + add new items.
        # These bypass ``build_ui`` so the __init_subclass__ wrapper does
        # not run ``_stabilize_custom_ids``.
        view.clear_items()
        view.add_item(
            ActionRow(
                StatefulButton(label="Enable"),
                StatefulButton(label="Clear Samples"),
            )
        )

        await view.refresh()

        buttons = [c for c in view.walk_children() if isinstance(c, StatefulButton)]
        prefix = view.id[:8]
        for btn in buttons:
            assert btn.custom_id.startswith(
                f"{prefix}:"
            ), f"custom_id {btn.custom_id!r} missing stable prefix"
        assert "Enable" in buttons[0].custom_id
        assert "Clear Samples" in buttons[1].custom_id

    async def test_repeat_rebuild_outside_build_ui_produces_identical_ids(self):
        """The dispatch-table-churn regression this fix exists to prevent."""
        view = self._make_view()

        def rebuild():
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Enable"),
                    StatefulButton(label="Clear Samples"),
                )
            )

        rebuild()
        await view.refresh()
        first = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        rebuild()
        await view.refresh()
        second = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        assert first == second

    async def test_refresh_is_idempotent_on_already_stable_ids(self):
        """A second refresh must not mutate already-stabilized ids."""
        view = self._make_view()

        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Action")))
        await view.refresh()
        first = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        # No rebuild -- same items, second refresh.
        await view.refresh()
        second = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        assert first == second

    async def test_refresh_preserves_explicit_custom_ids_during_rebuild(self):
        """Items with ``_provided_custom_id = True`` must remain untouched."""
        view = self._make_view()

        view.clear_items()
        view.add_item(
            ActionRow(
                StatefulButton(label="Auto"),
                StatefulButton(label="Explicit", custom_id="user_chosen"),
            )
        )
        await view.refresh()

        buttons = [c for c in view.walk_children() if isinstance(c, StatefulButton)]
        assert buttons[1].custom_id == "user_chosen"
        # Auto button gets stable prefix, untouched button keeps explicit id.
        assert buttons[0].custom_id != buttons[1].custom_id


class TestRenderHashShortCircuit:
    """``refresh()`` skips the Discord REST edit when the component tree
    has not changed since the last successful send/refresh.

    Every Battleship shot wakes 4 subscribers but only 1 or 2 of their
    views actually change content. The short-circuit eliminates the
    redundant message.edit() calls, cutting Discord REST traffic
    proportionally and relieving per-channel rate-limit pressure.
    """

    def _make_view_with_build(self, build_fn):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        return _V(interaction=_make_interaction())

    def test_digest_is_deterministic_for_identical_tree(self):
        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire", custom_id="a"),
                    StatefulButton(label="Close", custom_id="b"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()
        d1 = view._compute_tree_digest()
        view.build_ui()
        d2 = view._compute_tree_digest()
        assert d1 == d2

    def test_digest_changes_when_label_mutates(self):
        marks = ["Fire"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=marks[0], custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        marks[0] = "Armed"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_changes_when_disabled_toggles(self):
        enabled = [True]

        def build(view):
            view.clear_items()
            btn = StatefulButton(label="Fire", custom_id="a")
            btn.disabled = not enabled[0]
            view.add_item(ActionRow(btn))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        enabled[0] = False
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_captures_textdisplay_content(self):
        text = ["Hello"]

        def build(view):
            view.clear_items()
            view.add_item(TextDisplay(text[0]))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        text[0] = "World"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_detects_in_place_content_mutation(self):
        """EmojiGrid and similar live TextDisplay subclasses are mutated
        in place and dropped back into a rebuilt tree. The digest must
        detect the content change even when the Python object identity
        is preserved across rebuilds -- content-based hashing is the
        whole point, and any shortcut that compared identity instead
        would silently miss every in-place mutation.
        """
        shared_text = TextDisplay("red")

        def build(view):
            view.clear_items()
            view.add_item(shared_text)

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        # Mutate the same object in place -- no rebind, no new instance.
        shared_text.content = "blue"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_reflects_select_selection(self):
        """A select's rendered selection lives in opt.default, which the
        scalar wire attributes (placeholder/disabled/...) do not capture.
        A selection-only rebuild must change the digest, or refresh()
        short-circuits and the re-render is silently dropped -- the client
        keeps the stale selection and the next interaction submits stale
        values.
        """
        selected = ["a"]

        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulSelect(
                        placeholder="Pick",
                        options=[
                            discord.SelectOption(
                                label="A", value="a", default=(selected[0] == "a")
                            ),
                            discord.SelectOption(
                                label="B", value="b", default=(selected[0] == "b")
                            ),
                        ],
                        min_values=0,
                        max_values=1,
                    )
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        selected[0] = "b"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_reflects_set_selected(self):
        """set_selected() is the canonical selection-mutation API; it
        rewrites opt.default in place without rebuilding the component.
        The digest must reflect the change so the same select object
        flipped to a new selection re-renders.
        """
        select = StatefulSelect(
            placeholder="Pick",
            options=[
                discord.SelectOption(label="A", value="a"),
                discord.SelectOption(label="B", value="b"),
            ],
            min_values=0,
            max_values=1,
        )

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(select))

        view = self._make_view_with_build(build)
        select.set_selected("a")
        view.build_ui()
        before = view._compute_tree_digest()

        select.set_selected("b")
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    async def test_refresh_skips_edit_when_digest_matches(self):
        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        # Simulate a prior successful send that recorded the baseline.
        view._last_tree_digest = view._compute_tree_digest()

        await view.refresh()

        view._message.edit.assert_not_called()

    async def test_refresh_edits_when_digest_differs(self):
        label = ["Fire"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=label[0], custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        # Mutate and rebuild -- tree now differs.
        label[0] = "Armed"
        view.build_ui()
        await view.refresh()

        view._message.edit.assert_awaited_once()

    async def test_refresh_with_kwargs_never_skips(self):
        """V1 views pass embed= / content= kwargs; those are outside the
        digest, so the short-circuit must not apply."""

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        await view.refresh(content="new content")

        view._message.edit.assert_awaited_once()

    async def test_first_refresh_always_runs(self):
        """A view with no baseline digest has nothing to compare against."""

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        # _last_tree_digest remains None (no send yet).
        assert view._last_tree_digest is None

        await view.refresh()

        view._message.edit.assert_awaited_once()

    async def test_refresh_updates_baseline_after_successful_edit(self):
        label = ["A"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=label[0], custom_id="x")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        label[0] = "B"
        view.build_ui()
        first_new_digest = view._compute_tree_digest()
        await view.refresh()
        assert view._last_tree_digest == first_new_digest

        # Second refresh with no changes should now skip.
        await view.refresh()
        assert view._message.edit.await_count == 1  # still 1

    async def test_skip_recorded_in_perf_sample(self):
        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        store = get_store()
        store.clear_perf()
        store.enable_perf()
        try:
            await view.refresh()
        finally:
            store.disable_perf()

        assert len(store._refresh_samples) == 1
        sample = store._refresh_samples[-1]
        assert sample["skipped"] is True

    async def test_refresh_increments_edit_counter_only_when_editing(self):
        """End-to-end: a real edit bumps the current dispatch's counter,
        a short-circuited refresh does not. ``refresh()``'s internal
        wiring feeds the store's ``_perf_edit_stack`` on every edit.
        """
        label = ["A"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=label[0], custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        store = view.state_store
        store.clear_perf()
        store.enable_perf()
        try:
            # Push a fresh counter frame (simulating being inside a dispatch).
            store._perf_edit_stack.append(0)

            # First refresh: tree unchanged, short-circuit fires, no edit.
            await view.refresh()
            assert store._perf_edit_stack[-1] == 0

            # Mutate the tree and refresh -- real edit, counter bumps.
            label[0] = "B"
            view.build_ui()
            await view.refresh()
            assert store._perf_edit_stack[-1] == 1

            # Second real edit stacks on top.
            label[0] = "C"
            view.build_ui()
            await view.refresh()
            assert store._perf_edit_stack[-1] == 2
        finally:
            store._perf_edit_stack.clear()
            store.disable_perf()


class _FakeRateLimit(discord.HTTPException):
    """Minimal 429 stand-in for throttling tests.

    ``discord.HTTPException.__init__`` requires a live ``aiohttp`` response
    object; the real path is unreachable from a unit test. Bypassing the
    parent init and setting the two attributes ``_handle_rate_limit``
    actually reads (``status`` and ``retry_after``) keeps the subclass
    check in ``refresh()`` honest.
    """

    def __init__(self, retry_after: float = 0.5):
        Exception.__init__(self, "429 rate limit")
        self.status = 429
        self.retry_after = retry_after


class TestRefreshThrottling:
    """Reactive 429 backoff (always on) + proactive cooldown (opt-in via
    ``refresh_cooldown_ms``) share the ``_refresh_not_before`` timestamp.
    Refreshes landing inside the window defer via a single scheduled task
    that re-enters ``on_state_changed`` once the window expires.
    """

    def _make_view(self, build_fn, **class_attrs):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        for name, value in class_attrs.items():
            setattr(_V, name, value)
        return _V(interaction=_make_interaction())

    def _prime(self, view):
        """Set up mocked message and initial digest so short-circuit is inactive."""
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = None  # force first edit through

    def _build_simple(self, view):
        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

    def test_cooldown_ms_is_none_by_default(self):
        """Zero-config views never touch throttle state on the hot path."""
        assert _StatefulMixin.refresh_cooldown_ms is None

    def test_cooldown_ms_zero_rejected_at_class_def(self):
        """Zero is not a meaningful cooldown; the ``_POSITIVE_INT_ATTRS``
        validator rejects it so users don't assume 0 means 'off'.
        """
        with pytest.raises(ValueError, match="refresh_cooldown_ms"):

            class _Bad(StatefulLayoutView):
                refresh_cooldown_ms = 0

    def test_cooldown_ms_negative_rejected_at_class_def(self):
        with pytest.raises(ValueError, match="refresh_cooldown_ms"):

            class _Bad(StatefulLayoutView):
                refresh_cooldown_ms = -100

    def test_cooldown_ms_none_accepted(self):
        class _OK(StatefulLayoutView):
            refresh_cooldown_ms = None

        assert _OK.refresh_cooldown_ms is None

    def test_cooldown_ms_positive_int_accepted(self):
        class _OK(StatefulLayoutView):
            refresh_cooldown_ms = 250

        assert _OK.refresh_cooldown_ms == 250

    async def test_cooldown_off_does_not_advance_throttle(self):
        """With ``refresh_cooldown_ms = None`` (default), successful edits
        leave ``_refresh_not_before`` at 0 -- the proactive path is dead.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        await view.refresh()
        assert view._refresh_not_before == 0.0

    async def test_proactive_cooldown_stamps_after_success(self):
        view = self._make_view(self._build_simple, refresh_cooldown_ms=200)
        self._prime(view)
        before = time.monotonic()
        await view.refresh()
        # Stamp should be at least now + cooldown (minus small scheduling slack).
        assert view._refresh_not_before >= before + 0.19

    async def test_refresh_in_cooldown_window_defers(self):
        """Second rapid refresh inside the window must not hit message.edit."""
        view = self._make_view(self._build_simple, refresh_cooldown_ms=500)
        self._prime(view)

        await view.refresh()  # first edit
        assert view._message.edit.await_count == 1

        # Force the digest to differ so short-circuit can't hide the skip.
        view._last_tree_digest = 0
        await view.refresh()  # should defer, not edit
        assert view._message.edit.await_count == 1
        assert view._deferred_refresh_task is not None

        # Cancel the deferred task to avoid leaking into the test runner.
        # Let the event loop pick up the task so its coroutine enters
        # the sleep before cancellation -- avoids a 'never awaited' warning.
        await asyncio.sleep(0)
        view._deferred_refresh_task.cancel()
        try:
            await view._deferred_refresh_task
        except asyncio.CancelledError:
            pass

    async def test_cooldown_drop_intermediate_produces_one_deferred_task(self):
        """N refreshes during the window schedule 1 deferred task, not N."""
        view = self._make_view(self._build_simple, refresh_cooldown_ms=500)
        self._prime(view)

        await view.refresh()  # enters cooldown
        view._last_tree_digest = 0  # force subsequent calls past short-circuit

        await view.refresh()
        first_task = view._deferred_refresh_task
        await view.refresh()
        await view.refresh()
        # Same task instance across all four deferred refreshes.
        assert view._deferred_refresh_task is first_task

        await asyncio.sleep(0)
        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

    async def test_deferred_refresh_reenters_on_state_changed(self):
        """Deferred task runs ``on_state_changed`` so ``build_ui`` sees
        the *latest* store state, not kwargs captured at defer time.
        """
        view = self._make_view(self._build_simple, refresh_cooldown_ms=50)
        self._prime(view)
        # Spy on on_state_changed.
        view.on_state_changed = AsyncMock()

        # Run the deferred path directly with a tiny wait.
        await view._deferred_refresh(0.01)

        view.on_state_changed.assert_awaited_once_with(view.state_store.state)

    async def test_deferred_refresh_noop_on_finished_view(self):
        view = self._make_view(self._build_simple, refresh_cooldown_ms=50)
        self._prime(view)
        view.on_state_changed = AsyncMock()
        view.stop()  # mark view as finished

        await view._deferred_refresh(0.01)

        view.on_state_changed.assert_not_awaited()

    async def test_reactive_429_stamps_backoff_window(self):
        """429 raised by ``message.edit`` → ``_refresh_not_before`` set
        from ``retry_after``, exception swallowed.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.75))

        before = time.monotonic()
        await view.refresh()  # must not raise

        assert view._refresh_not_before >= before + 0.7

    async def test_reactive_429_defers_next_refresh(self):
        view = self._make_view(self._build_simple)
        self._prime(view)
        # First call: 429.
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.5))
        await view.refresh()
        first_count = view._message.edit.await_count

        # Second call: still inside backoff window → no edit attempted.
        view._last_tree_digest = 0
        await view.refresh()
        assert view._message.edit.await_count == first_count

        if view._deferred_refresh_task is not None:
            await asyncio.sleep(0)
            view._deferred_refresh_task.cancel()
            try:
                await view._deferred_refresh_task
            except asyncio.CancelledError:
                pass

    async def test_non_429_http_exception_is_reraised(self):
        """Only 429 is swallowed by the reactive path; other HTTP errors
        must propagate to the caller.
        """

        class _OtherError(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "500")
                self.status = 500
                self.retry_after = 0

        view = self._make_view(self._build_simple)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=_OtherError())

        with pytest.raises(_OtherError):
            await view.refresh()

    async def test_render_hash_skip_does_not_stamp_cooldown(self):
        """Short-circuited refreshes didn't ship an edit -- they shouldn't
        consume the cooldown window either.
        """
        view = self._make_view(self._build_simple, refresh_cooldown_ms=200)
        self._prime(view)
        # Prime digest so the short-circuit path fires on next refresh.
        view._last_tree_digest = view._compute_tree_digest()

        await view.refresh()

        view._message.edit.assert_not_called()
        assert view._refresh_not_before == 0.0


class TestActingViewFastPath:
    """Acting-view ``interaction.response.edit_message`` fast path.

    When the currently-handled interaction targets the acting view's
    message and the response slot is still open, ``refresh()`` routes
    through ``interaction.response.edit_message(view=self, **kwargs)``
    -- one REST round-trip instead of two (ack + channel PATCH). The
    contextvar ``_CURRENT_INTERACTION`` is bound by the stateful
    callback for the duration of the callback + dispatch sequence.
    Disqualified cases (modal interactions, cross-view mismatch,
    already-deferred response, unbound contextvar) fall through to
    the existing webhook/channel paths.
    """

    def _make_view(self, build_fn, **class_attrs):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        for name, value in class_attrs.items():
            setattr(_V, name, value)
        return _V(interaction=_make_interaction())

    def _prime(self, view, message_id=555):
        view.build_ui()
        view._message = MagicMock()
        view._message.id = message_id
        view._message.edit = AsyncMock()
        view._last_tree_digest = None

    def _build_simple(self, view):
        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

    def _make_acting_interaction(self, message_id=555, is_done=False):
        """Build a component interaction whose ``message.id`` matches
        the primed view's message so the fast path engages.
        """
        interaction = _make_interaction()
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.response.is_done.return_value = is_done
        return interaction

    async def test_fast_path_edits_via_interaction_response(self):
        """Happy path: bound interaction targets the acting view's
        message, response is open -> edit ships through
        ``interaction.response.edit_message``, channel endpoint is
        never touched.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once_with(view=view)
        view._message.edit.assert_not_called()

    async def test_cross_view_message_mismatch_falls_through(self):
        """Interaction bound but its ``message.id`` does not match the
        view's message -> the subscriber is a cross-view listener, not
        the acting view. Falls through to the channel endpoint.
        """
        view = self._make_view(self._build_simple)
        self._prime(view, message_id=555)
        interaction = self._make_acting_interaction(message_id=999)

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once()

    async def test_modal_interaction_falls_through(self):
        """Modal submissions have a message, but the response cannot
        carry a component edit -- fast path refuses and defers to the
        channel endpoint.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.type = discord.InteractionType.modal_submit

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once()

    async def test_already_deferred_response_falls_through(self):
        """Callback manually called ``respond()`` or ``defer()`` before
        refreshing -> response slot already consumed, fast path cannot
        piggyback. Channel endpoint carries the edit instead.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction(is_done=True)

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once()

    async def test_ack_race_interaction_responded_falls_through(self):
        """The is_done() guard passes, but the auto-defer timer acks in the
        window before edit_message's own internal guard, so edit_message raises
        InteractionResponded -- a sibling of HTTPException, not a subclass, so
        the HTTP handler would miss it. The guard raises before any HTTP (no
        edit shipped) and the interaction is already acked, so the fast path
        must fall through to the channel endpoint to ship the edit.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(
            side_effect=discord.InteractionResponded(MagicMock())
        )

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once()
        view._message.edit.assert_awaited_once()  # fell through to channel

    async def test_no_bound_interaction_falls_through(self):
        """Programmatic dispatch (persistence rehydrate, hook-driven
        refresh) runs outside a component callback -- the contextvar
        default ``None`` disqualifies the fast path. Channel endpoint
        owns the edit.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)

        assert _CURRENT_INTERACTION.get() is None
        await view.refresh()
        view._message.edit.assert_awaited_once()

    async def test_fast_path_429_arms_backoff_and_swallows(self):
        """429 on ``interaction.response.edit_message`` routes through
        ``_handle_rate_limit`` exactly like the channel path -- sets
        the backoff window, swallows the exception, does NOT fall
        through to the channel endpoint (no retry storm).
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.75))

        token = _CURRENT_INTERACTION.set(interaction)
        before = time.monotonic()
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        assert view._refresh_not_before >= before + 0.7
        view._message.edit.assert_not_called()

    async def test_fast_path_general_http_error_falls_through(self):
        """Non-429 HTTP errors (500, 502, network blip) on the fast
        path fall through to the channel endpoint so a transient
        failure on the interaction-response route never drops the
        edit entirely.
        """

        class _OtherError(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "500")
                self.status = 500
                self.retry_after = 0

        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_OtherError())

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once()
        view._message.edit.assert_awaited_once()

    async def test_fast_path_timeout_skips_channel_fallthrough(self):
        """Slow ``edit_message`` response (Discord latency spike, ephemeral
        backend under load) would starve the interaction ack past the 3s
        deadline under the fast path's one-HTTP-call contract. The
        ``wait_for`` guard caps the fast path at
        ``max(0.5, auto_defer_delay - 1.0)`` and cancels the in-flight
        edit on stall.

        On stall, refresh returns immediately rather than falling through
        to the channel endpoint. A second edit attempt on top of the
        cancelled fast path would consume the auto-defer timer's budget
        for its own ack call, producing the very interaction-failed
        toast the ack-coupling design exists to prevent. The auto-defer
        timer fires the standalone ack at ``auto_defer_delay`` seconds
        with the full remaining budget.

        The render-hash digest is invalidated so the next refresh ships
        unconditionally; whether Discord processed the cancelled edit
        server-side is indeterminate, and a redundant edit is cheaper
        than a stuck UI.

        ``_refresh_not_before`` is NOT armed: a stall is not a
        rate-limit signal, so the next refresh should not be throttled.
        """

        async def _stall_forever(*args, **kwargs):
            await asyncio.sleep(60)

        # Compress auto_defer_delay so the derived fast-path timeout
        # (max(0.5, delay - 1.0)) floors at 0.5s and the test is fast.
        view = self._make_view(self._build_simple, auto_defer_delay=1.5)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall_forever)

        # Seed the digest to a value that does NOT match the current
        # tree.  A matching digest would short-circuit refresh before
        # the fast path engages; a non-matching one lets the fast path
        # run AND lets the post-refresh ``is None`` assertion below
        # prove the new code path invalidated it (the old fall-through
        # code path would have set it to the current digest, not None).
        view._last_tree_digest = view._compute_tree_digest() + 1

        token = _CURRENT_INTERACTION.set(interaction)
        before = time.monotonic()
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)
        elapsed = time.monotonic() - before

        # Fast path was attempted and cancelled by wait_for.
        interaction.response.edit_message.assert_awaited_once()
        # Channel-endpoint fall-through was SKIPPED.  A second edit on
        # top of the cancelled fast path would drain the auto-defer
        # timer's ack budget under genuine Discord-side latency.
        view._message.edit.assert_not_called()
        # Returned within the derived timeout window (0.5s), not the
        # 60s sleep -- proves the wait_for guard fired.
        assert elapsed < 2.0
        # Stall is not a rate-limit signal: backoff window stays at zero.
        assert view._refresh_not_before == 0.0
        # Digest invalidated so the next refresh ships unconditionally.
        assert view._last_tree_digest is None


class TestEphemeralActingRefresh:
    """Ephemeral acting views edit through the webhook without pre-deferring.

    The edit ships through ``self._message.edit()`` -- the
    ``InteractionMessage`` / ``WebhookMessage`` whose ``.edit()`` routes
    through the webhook on the original send's token, independent of the
    click's ack -- so it lands without first waiting on a deferred-update
    round-trip. ``refresh()`` does not defer; the click is acknowledged
    after the callback by the post-callback defer in ``_scheduled_task``
    (or by the auto-defer timer when the edit is slow). The edit-as-ack
    fast path stays in force for non-ephemeral views, where the edit is
    fast enough to double as the ack.
    """

    def _make_ephemeral_view(self):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = _V(interaction=_make_interaction())
        view._ephemeral = True
        view.build_ui()
        view._message = MagicMock()
        view._message.id = 555
        view._message.edit = AsyncMock()
        view._last_tree_digest = None
        return view

    def _make_acting_interaction(self, message_id=555, is_done=False):
        interaction = _make_interaction()
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.response.is_done.return_value = is_done
        return interaction

    async def test_acting_ephemeral_edits_through_webhook_without_predefer(self):
        """No pre-defer in refresh(): skip the edit-as-ack fast path and ship
        the edit straight through the webhook handle (``self._message``). The
        click's ack is delegated to _scheduled_task's post-callback defer.
        """
        view = self._make_ephemeral_view()
        interaction = self._make_acting_interaction()

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        # refresh() does not ack -- no deferred update fires here.
        interaction.response.defer.assert_not_called()
        # The edit-as-ack fast path is reserved for non-ephemeral views.
        interaction.response.edit_message.assert_not_called()
        # The edit shipped through the webhook handle.
        view._message.edit.assert_awaited_once_with(view=view)

    async def test_non_acting_ephemeral_edits_without_a_defer(self):
        """Background ephemeral refreshes (no bound interaction) edit straight
        through the webhook handle -- no deferred ack fires because there is
        nothing to acknowledge.
        """
        view = self._make_ephemeral_view()

        assert _CURRENT_INTERACTION.get() is None
        await view.refresh()

        view._message.edit.assert_awaited_once_with(view=view)


class TestEditTimeout:
    """``edit_timeout`` bounds every live-view and teardown edit through
    ``_bounded``.

    discord.py issues HTTP edits with no total timeout, so a connection
    that stalls without a response would pin the awaiting code -- and, on
    the interaction-locked refresh/navigation paths, the view itself. The
    ceiling cancels the stalled request and the view recovers on the next
    interaction. ``edit_timeout = None`` restores unbounded awaits.
    """

    def _make_view(self, **class_attrs):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        for name, value in class_attrs.items():
            setattr(_V, name, value)
        view = _V(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.id = 555
        view._message.edit = AsyncMock()
        view._last_tree_digest = None
        return view

    async def test_default_timeout_is_sixty(self):
        view = self._make_view()
        assert view.edit_timeout == 60.0

    async def test_bounded_awaits_directly_when_disabled(self):
        view = self._make_view(edit_timeout=None)

        async def quick():
            return "done"

        assert await view._bounded(quick()) == "done"

    async def test_bounded_returns_result_within_ceiling(self):
        view = self._make_view(edit_timeout=5.0)

        async def quick():
            return "done"

        assert await view._bounded(quick()) == "done"

    async def test_bounded_raises_on_stall(self):
        view = self._make_view(edit_timeout=0.05)

        async def stall():
            await asyncio.sleep(60)

        with pytest.raises(asyncio.TimeoutError):
            await view._bounded(stall())

    async def test_refresh_stall_logs_and_invalidates_digest(self, caplog):
        """A stalled channel edit is cancelled at ``edit_timeout``; refresh
        logs a warning, invalidates the digest so the next refresh re-ships,
        and returns instead of hanging on the socket.
        """
        view = self._make_view(edit_timeout=0.05)

        async def stall(*args, **kwargs):
            await asyncio.sleep(60)

        view._message.edit = AsyncMock(side_effect=stall)
        # Seed a non-matching, non-None digest so refresh skips neither the
        # short-circuit-on-match nor the edit; after the stall the timeout
        # branch should have reset it to None.
        view._last_tree_digest = view._compute_tree_digest() + 1

        assert _CURRENT_INTERACTION.get() is None  # background path, no fast path
        before = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view.refresh()
        elapsed = time.monotonic() - before

        assert elapsed < 2.0  # bounded by edit_timeout, not the 60s stall
        assert view._last_tree_digest is None
        assert any("stalled" in r.getMessage() for r in caplog.records)


class TestDisplayLayoutView:
    """``DisplayLayoutView`` renders a pre-built container without
    requiring a subclass. Used for one-shot ephemeral cards (stats,
    leaderboards, confirmations) where there's no view-local state to
    manage.
    """

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(DisplayLayoutView, StatefulLayoutView)

    def test_defaults_differ_from_stateful_layout_view(self):
        assert DisplayLayoutView.owner_only is False
        assert DisplayLayoutView.state_scope is None

    def test_container_is_rendered(self):
        interaction = _make_interaction()
        body = TextDisplay("hello")
        view = DisplayLayoutView(interaction=interaction, container=body)

        assert body in view.children

    def test_build_ui_clears_and_re_adds(self):
        interaction = _make_interaction()
        body = TextDisplay("hello")
        view = DisplayLayoutView(interaction=interaction, container=body)

        view.build_ui()

        assert list(view.children) == [body]

    def test_container_kwarg_is_required(self):
        interaction = _make_interaction()
        with pytest.raises(TypeError, match="container"):
            DisplayLayoutView(interaction=interaction)
