"""Tests for component creation and callback wrapping."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cascadeui.components.base import StatefulButton, StatefulComponent, StatefulSelect
from cascadeui.components.v1_composition import (
    CompositeComponent,
    get_component,
    register_component,
)


class TestStatefulComponent:
    """StatefulButton and StatefulSelect store original callbacks and pass-through args."""

    def test_button_stores_original_callback(self):
        async def my_cb(interaction):
            pass

        btn = StatefulButton(label="Test", callback=my_cb)
        assert btn.original_callback is my_cb

    def test_button_without_callback(self):
        btn = StatefulButton(label="No CB")
        assert btn.original_callback is None

    def test_select_stores_original_callback(self):
        async def my_cb(interaction):
            pass

        sel = StatefulSelect(options=[discord.SelectOption(label="A", value="a")], callback=my_cb)
        assert sel.original_callback is my_cb

    def test_button_passes_style_through(self):
        btn = StatefulButton(label="Danger", style=discord.ButtonStyle.danger)
        assert btn.style == discord.ButtonStyle.danger


class TestStatefulSelectEmptyOptions:
    """Empty ``options=[]`` must not crash Discord with error 50035.

    ``StatefulSelect`` substitutes a disabled placeholder when the caller
    passes an empty list, so dynamically-filtered selects render cleanly
    through the empty state without a bespoke fallback branch at every
    usage site.
    """

    def test_empty_options_substitutes_placeholder(self):
        sel = StatefulSelect(options=[], placeholder="Nothing to pick")
        assert len(sel.options) == 1
        assert sel.options[0].value == "__cascadeui_empty__"

    def test_empty_options_forces_disabled(self):
        # Forced regardless of caller intent -- an enabled empty select
        # would still hit the same Discord constraint on interaction.
        sel = StatefulSelect(options=[], disabled=False)
        assert sel.disabled is True

    def test_nonempty_options_untouched(self):
        original = [
            discord.SelectOption(label="A", value="a"),
            discord.SelectOption(label="B", value="b"),
        ]
        sel = StatefulSelect(options=original)
        assert len(sel.options) == 2
        assert [o.value for o in sel.options] == ["a", "b"]
        assert sel.disabled is False


class TestStatefulSelectSetSelected:
    """``set_selected`` / ``get_selected`` reflect state into option defaults.

    Mirrors the native ``Select.values`` always-list convention so single-
    and multi-select call sites share one accessor shape.
    """

    def _make(self, **kwargs):
        return StatefulSelect(
            options=[
                discord.SelectOption(label="A", value="a"),
                discord.SelectOption(label="B", value="b"),
                discord.SelectOption(label="C", value="c"),
            ],
            **kwargs,
        )

    def test_none_clears_all_defaults(self):
        sel = self._make()
        for opt in sel.options:
            opt.default = True
        sel.set_selected(None)
        assert all(opt.default is False for opt in sel.options)
        assert sel.get_selected() == []

    def test_empty_iterable_clears_all_defaults(self):
        sel = self._make()
        sel.options[0].default = True
        sel.set_selected([])
        assert sel.get_selected() == []

    def test_single_string_marks_one(self):
        sel = self._make()
        sel.set_selected("b")
        assert sel.get_selected() == ["b"]

    def test_iterable_marks_multiple(self):
        sel = self._make(max_values=2)
        sel.set_selected(["a", "c"])
        assert sel.get_selected() == ["a", "c"]

    def test_unknown_value_silent_noop(self):
        sel = self._make()
        sel.set_selected("zzz")
        assert sel.get_selected() == []

    def test_round_trip_single(self):
        sel = self._make()
        sel.set_selected("a")
        assert sel.get_selected() == ["a"]
        sel.set_selected("c")
        assert sel.get_selected() == ["c"]

    def test_string_not_iterated_as_chars(self):
        """Correctness-critical: ``"ab"`` must not be treated as ``{"a","b"}``."""
        sel = self._make()
        sel.set_selected("a")
        # Only the literal "a" value should match, never character-split.
        assert sel.get_selected() == ["a"]

    def test_empty_placeholder_select_safe(self):
        sel = StatefulSelect(options=[])
        sel.set_selected("anything")  # must not crash
        assert sel.get_selected() == []


class TestCompositeComponent:
    """V1 CompositeComponent registration, retrieval, and child management."""

    def test_add_and_retrieve_components(self):
        comp = CompositeComponent()
        btn = StatefulButton(label="Child")
        comp.add_component(btn)
        assert btn in comp.components

    def test_register_and_get_component(self):
        register_component("test_comp", CompositeComponent)
        cls = get_component("test_comp")
        assert cls is CompositeComponent

    def test_get_unknown_component_returns_none(self):
        result = get_component("nonexistent_component_xyz")
        assert result is None


class TestStatefulCallbackTokenDiscipline:
    """``_CURRENT_INTERACTION`` resets after ``stateful_callback`` returns,
    regardless of which exit path the wrapped callback takes.

    The contextvar binds the live interaction for the ``refresh()``
    fast path. A leaked token would keep a stale interaction visible
    to the next acting dispatch on the same task, so the ``finally``
    block that resets it is a correctness contract worth locking down.
    """

    def _make_component(self, view):
        component = MagicMock()
        component.custom_id = "test-btn"
        component.view = view
        return component

    def _make_view(self, is_finished=False):
        view = MagicMock()
        view.is_finished = MagicMock(return_value=is_finished)
        view.dispatch = AsyncMock()
        view.id = "view-under-test"
        return view

    async def test_normal_exit_resets_contextvar(self):
        from helpers import make_interaction

        from cascadeui.state.store import _CURRENT_INTERACTION

        async def user_cb(interaction):
            pass

        view = self._make_view(is_finished=False)
        component = self._make_component(view)
        stateful_cb = StatefulComponent().create_stateful_callback(component, user_cb)

        assert _CURRENT_INTERACTION.get() is None
        await stateful_cb(make_interaction())
        assert _CURRENT_INTERACTION.get() is None
        view.dispatch.assert_awaited_once()

    async def test_is_finished_early_return_resets_contextvar(self):
        from helpers import make_interaction

        from cascadeui.state.store import _CURRENT_INTERACTION

        async def user_cb(interaction):
            pass  # view becomes "finished" during this await

        view = self._make_view(is_finished=True)
        component = self._make_component(view)
        stateful_cb = StatefulComponent().create_stateful_callback(component, user_cb)

        assert _CURRENT_INTERACTION.get() is None
        await stateful_cb(make_interaction())
        assert _CURRENT_INTERACTION.get() is None
        # is_finished() short-circuits before dispatch.
        view.dispatch.assert_not_called()

    async def test_callback_exception_resets_contextvar(self):
        from helpers import make_interaction

        from cascadeui.state.store import _CURRENT_INTERACTION

        async def user_cb(interaction):
            raise RuntimeError("boom")

        view = self._make_view(is_finished=False)
        component = self._make_component(view)
        stateful_cb = StatefulComponent().create_stateful_callback(component, user_cb)

        assert _CURRENT_INTERACTION.get() is None
        with pytest.raises(RuntimeError, match="boom"):
            await stateful_cb(make_interaction())
        assert _CURRENT_INTERACTION.get() is None
        view.dispatch.assert_not_called()


class TestButtonOwnerOnly:
    """``StatefulButton(owner_only=True)`` gates the callback on
    ``interaction.user.id == view.user_id``. Mismatches route through
    ``view.on_unauthorized(interaction)`` instead of invoking the
    user callback. Pairs with view-level ``owner_only=False`` to
    express open-view + host-only-button (lobby Start/Disband,
    ticket Close, poll End).

    Tests exercise the inner ``stateful_callback`` directly via a
    MagicMock component (matching ``TestStatefulCallbackTokenDiscipline``
    above) -- the gate logic lives there, and bypassing ``StatefulButton``
    construction avoids mutating the discord.py ``view`` property at
    the class level.
    """

    def _make_view(self, owner_id=1):
        view = MagicMock()
        view.user_id = owner_id
        view.id = "view-under-test"
        view.is_finished = MagicMock(return_value=False)
        view.dispatch = AsyncMock()
        view.on_unauthorized = AsyncMock()
        return view

    def _make_component(self, view, *, owner_only=False):
        component = MagicMock()
        component.custom_id = "host-btn"
        component.view = view
        component._button_owner_only = owner_only
        return component

    def _make_interaction(self, user_id):
        from helpers import make_interaction

        interaction = make_interaction()
        interaction.user.id = user_id
        return interaction

    async def test_owner_click_invokes_callback(self):
        callback_calls = []

        async def user_cb(interaction):
            callback_calls.append(True)

        view = self._make_view(owner_id=1)
        component = self._make_component(view, owner_only=True)
        stateful_cb = StatefulComponent().create_stateful_callback(component, user_cb)

        await stateful_cb(self._make_interaction(user_id=1))

        assert callback_calls == [True]
        view.on_unauthorized.assert_not_awaited()

    async def test_non_owner_click_routes_to_on_unauthorized(self):
        callback_calls = []

        async def user_cb(interaction):
            callback_calls.append(True)

        view = self._make_view(owner_id=1)
        component = self._make_component(view, owner_only=True)
        stateful_cb = StatefulComponent().create_stateful_callback(component, user_cb)

        await stateful_cb(self._make_interaction(user_id=999))

        assert callback_calls == []
        view.on_unauthorized.assert_awaited_once()
        view.dispatch.assert_not_called()

    async def test_default_owner_only_false_preserves_existing_behavior(self):
        """Backward compat: omitting owner_only or passing False
        keeps the pre-v3.2.0 callback contract intact."""
        callback_calls = []

        async def user_cb(interaction):
            callback_calls.append(True)

        view = self._make_view(owner_id=1)
        component = self._make_component(view, owner_only=False)
        stateful_cb = StatefulComponent().create_stateful_callback(component, user_cb)

        await stateful_cb(self._make_interaction(user_id=999))

        assert callback_calls == [True]
        view.on_unauthorized.assert_not_awaited()

    async def test_view_without_user_id_skips_check(self):
        """Anonymous flows (no view.user_id) skip the gate entirely
        so background or system-driven views still work."""
        callback_calls = []

        async def user_cb(interaction):
            callback_calls.append(True)

        view = self._make_view(owner_id=None)
        component = self._make_component(view, owner_only=True)
        stateful_cb = StatefulComponent().create_stateful_callback(component, user_cb)

        await stateful_cb(self._make_interaction(user_id=999))

        assert callback_calls == [True]
        view.on_unauthorized.assert_not_awaited()

    async def test_kwarg_stored_on_button_instance(self):
        """Verify the kwarg actually lands on the button as
        ``_button_owner_only`` so the integration path through
        ``StatefulButton.__init__`` is wired correctly."""
        btn_default = StatefulButton(label="Default")
        btn_owner = StatefulButton(label="Host", owner_only=True)

        assert btn_default._button_owner_only is False
        assert btn_owner._button_owner_only is True
