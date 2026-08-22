"""Tests for component creation and callback wrapping."""

import functools
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ui import ActionRow
from helpers import RenderableLayoutView
from helpers import make_interaction as _make_interaction

from cascadeui.components.base import StatefulButton, StatefulComponent, StatefulSelect
from cascadeui.components.v1_composition import (
    CompositeComponent,
    get_component,
    register_component,
)
from cascadeui.utils.hooks import accepts_second_positional


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


class TestCallbackArityRefusal:
    """``create_stateful_callback`` refuses, at construction, a callback the
    component cannot call. A wrong signature otherwise survives until the
    first click and surfaces as a bare arity ``TypeError`` from inside the
    wrapper -- "This interaction failed" with nothing naming the mistake.
    """

    def _make_view(self):
        view = MagicMock()
        view.user_id = 1
        view.id = "view-under-test"
        view.is_finished = MagicMock(return_value=False)
        view.dispatch = AsyncMock()
        return view

    def test_button_refuses_two_param_callback(self):
        async def cb(interaction, value):
            pass

        with pytest.raises(TypeError, match=r"cannot be called with \(interaction\)"):
            StatefulButton(label="Test", callback=cb)

    def test_unbound_method_refused_with_its_signature(self):
        """The message prints the signature back, so the stray ``self`` of a
        function pulled off a class body is visible in the refusal."""

        class Cog:
            async def on_click(self, interaction):
                pass

        with pytest.raises(TypeError, match=r"on_click\(self, interaction\)"):
            StatefulButton(label="Test", callback=Cog.on_click)

    def test_bound_method_accepted(self):
        class Cog:
            async def on_click(self, interaction):
                pass

        btn = StatefulButton(label="Test", callback=Cog().on_click)
        assert btn.original_callback is not None

    def test_star_args_callback_accepted(self):
        def cb(*args):
            pass

        btn = StatefulButton(label="Test", callback=cb)
        assert btn.original_callback is cb

    async def test_star_args_callback_receives_one_argument(self):
        """Variadic callbacks keep the one-argument contract: only a declared
        second positional parameter opts into a value."""
        received = []

        def cb(*args):
            received.append(args)

        btn = StatefulButton(label="Test", callback=cb)
        btn._view = self._make_view()

        from helpers import make_interaction

        await btn.callback(make_interaction())

        assert [len(args) for args in received] == [1]

    def test_unreadable_signature_accepted(self):
        # ``min`` is a C builtin whose signature cannot be read. The premise
        # is asserted first, so an interpreter that grows one fails here at
        # the proxy rather than silently changing what this test measures.
        with pytest.raises(ValueError):
            inspect.signature(min)

        btn = StatefulButton(label="Test", callback=min)
        assert btn.original_callback is min

    def test_select_accepts_two_param_callback(self):
        async def cb(interaction, values):
            pass

        sel = StatefulSelect(options=[discord.SelectOption(label="A", value="a")], callback=cb)
        assert sel.original_callback is cb

    def test_select_refuses_zero_param_callback(self):
        with pytest.raises(TypeError, match=r"\(interaction\) or \(interaction, values\)"):
            StatefulSelect(
                options=[discord.SelectOption(label="A", value="a")],
                callback=lambda: None,
            )

    async def test_select_one_param_callback_receives_one_argument(self):
        received = []

        def cb(*args):
            received.append(args)

        sel = StatefulSelect(options=[discord.SelectOption(label="A", value="a")], callback=cb)
        sel._view = self._make_view()

        from helpers import make_interaction

        await sel.callback(make_interaction())

        assert [len(args) for args in received] == [1]

    async def test_a_narrowing_wraps_adapter_is_not_handed_a_second_argument(self):
        """The refusal and the dispatch have to read the same signature.

        ``functools.wraps`` copies the wrapped function's signature onto the
        wrapper, so an adapter that narrows two parameters to one advertises
        a second positional it cannot accept. Deciding on the advertisement
        elected the two-argument call and failed on the first click, from
        inside the library, naming the wrapped function rather than the
        wrapper that could not take the argument.
        """
        received = []

        async def real_handler(interaction, values):
            received.append(values)

        @functools.wraps(real_handler)
        async def narrowed(interaction):
            received.append("one-arg")

        view = RenderableLayoutView(user_id=1, guild_id=2)
        select = StatefulSelect(
            custom_id="narrowed",
            options=[discord.SelectOption(label="L", value="v")],
            callback=narrowed,
        )
        view.add_item(ActionRow(select))

        await select.callback(_make_interaction())

        assert received == ["one-arg"]

    def test_a_transparent_wraps_adapter_still_receives_the_value(self):
        """A wrapper that really can take both keeps the two-argument call.

        The fix must not cost the shape it was protecting: an adapter
        declared ``(*args, **kwargs)`` can honor the advertisement.
        """

        async def real_handler(interaction, values):
            pass

        @functools.wraps(real_handler)
        async def transparent(*args, **kwargs):
            await real_handler(*args, **kwargs)

        assert accepts_second_positional(transparent) is True

    async def test_a_transparent_wraps_adapter_receives_the_value_on_dispatch(self):
        """The predicate answer above has to survive to the real call."""
        received = []

        async def real_handler(interaction, values):
            received.append(values)

        @functools.wraps(real_handler)
        async def transparent(*args, **kwargs):
            await real_handler(*args, **kwargs)

        view = RenderableLayoutView(user_id=1, guild_id=2)
        select = StatefulSelect(
            custom_id="transparent",
            options=[discord.SelectOption(label="L", value="v")],
            callback=transparent,
        )
        view.add_item(ActionRow(select))

        await select.callback(_make_interaction())

        assert received == [select.values]

    def test_a_widening_wraps_adapter_is_refused(self):
        """The mirror of the narrowing adapter, and the harder direction.

        A wrapper that requires ``(interaction, values)`` while advertising
        the one-parameter function it wraps reads as one-argument to the
        dispatch and passes a refusal that asks whether EITHER arity binds.
        Refusing against the arity the dispatch actually chose is what
        catches it: the select picks the one-argument call, and the callback
        cannot take one.
        """

        async def declared(interaction):
            pass

        @functools.wraps(declared)
        async def widening(interaction, values):
            pass

        with pytest.raises(TypeError, match=r"cannot be called with"):
            StatefulSelect(
                custom_id="widening",
                options=[discord.SelectOption(label="L", value="v")],
                callback=widening,
            )

    def test_an_unbound_method_is_not_handed_a_second_argument(self):
        """Three declared positionals, and it cannot be called with two."""

        class Holder:
            async def handler(self, interaction, values):
                pass

        assert accepts_second_positional(Holder.handler) is False


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


class TestStatefulSelectOptionShape:
    """Entries are checked at construction, not by discord.py at send.

    ``Dropdown`` is the seam that accepts dict shorthand. Its base class
    accepted the same shapes without converting them, so a dict, a bare
    mapping, or a plain string reached Discord as an option carrying
    neither a label nor a value.
    """

    def test_bare_mapping_rejected(self):
        # len() of a mapping is its key count, so a bare mapping cleared
        # both the empty check and the 25-option cap before this guard.
        with pytest.raises(TypeError, match="not a single mapping"):
            StatefulSelect(options={"label": "A", "value": "a"})

    def test_dict_entry_rejected_naming_the_index(self):
        with pytest.raises(TypeError, match=r"options\[0\] must be a SelectOption"):
            StatefulSelect(options=[{"label": "A", "value": "a"}])

    def test_string_entry_rejected_naming_the_index(self):
        with pytest.raises(TypeError, match=r"options\[1\] must be a SelectOption"):
            StatefulSelect(options=[discord.SelectOption(label="A", value="a"), "b"])

    def test_rejection_points_at_the_sibling_that_accepts_dicts(self):
        with pytest.raises(TypeError, match="Dropdown"):
            StatefulSelect(options=[{"label": "A", "value": "a"}])

    def test_dropdown_still_converts_dict_shorthand(self):
        # The guard must not collapse the distinction it points users at.
        from cascadeui.components.selects import Dropdown

        drop = Dropdown(options=[{"label": "A", "value": "a"}])
        assert isinstance(drop.options[0], discord.SelectOption)
        assert drop.options[0].value == "a"


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


class TestV1CompositeCallbackArity:
    """The V1 composites hold the same callback contract as the builders:
    value-carrying controls refuse a callback that cannot receive the value,
    value-less buttons refuse one that demands a second argument."""

    def _make_view(self):
        view = MagicMock()
        view.user_id = 1
        view.id = "view-under-test"
        view.is_finished = MagicMock(return_value=False)
        view.dispatch = AsyncMock()
        return view

    def test_toggle_group_refuses_one_param_on_select(self):
        from cascadeui.components.patterns.v1 import ToggleGroup

        async def cb(interaction):
            pass

        with pytest.raises(
            TypeError, match=r"ToggleGroup: on_select callback .* \(interaction, value\)"
        ):
            ToggleGroup(options=["A", "B"], on_select=cb)

    def test_pagination_controls_refuses_one_param_on_page_change(self):
        from cascadeui.components.patterns.v1 import PaginationControls

        async def cb(interaction):
            pass

        with pytest.raises(
            TypeError,
            match=r"PaginationControls: on_page_change callback .* \(interaction, page\)",
        ):
            PaginationControls(page_count=3, on_page_change=cb)

    def test_confirmation_buttons_refuse_two_param_callback(self):
        from cascadeui.components.patterns.v1 import ConfirmationButtons

        async def cb(interaction, value):
            pass

        with pytest.raises(TypeError, match=r"cannot be called with \(interaction\)"):
            ConfirmationButtons(on_confirm=cb)

    async def test_toggle_group_on_select_receives_the_value(self):
        from helpers import make_interaction

        from cascadeui.components.patterns.v1 import ToggleGroup

        received = []

        async def cb(interaction, value):
            received.append(value)

        group = ToggleGroup(options=["A", "B"], on_select=cb)
        button = group._buttons[1]
        button._view = self._make_view()

        await button.callback(make_interaction())

        assert received == ["B"]

    async def test_pagination_controls_report_the_new_page(self):
        from helpers import make_interaction

        from cascadeui.components.patterns.v1 import PaginationControls

        received = []

        async def cb(interaction, page):
            received.append(page)

        controls = PaginationControls(page_count=3, on_page_change=cb)
        button = controls.next_button
        button._view = self._make_view()

        await button.callback(make_interaction())

        assert received == [1]


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

    The gate compares ``_button_owner_only is True``, which is what lets
    any mock component reach a callback at all: ``getattr`` returns
    another MagicMock for an unset attribute, truthy under ``bool()`` and
    not ``True`` under identity. This class sets the attribute on every
    component it builds, so the dependency is not visible here -- it is
    the classes that build a mock component without setting it that the
    identity check protects.
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


class TestToggleButton:
    """``ToggleButton`` flips inside the shared stateful callback.

    The class used to build its own callback and assign it over
    ``self.callback``, which replaced the wrapper every guard rides:
    ``owner_only`` was accepted and never read, the finished-view skip
    never ran, the dispatch preceded the user callback, and the acting
    interaction was never bound. These drive the real class rather than
    a mock component, so a future callback assignment that bypasses
    the wrapper fails here.
    """

    def _make_button(self, **kwargs):
        from cascadeui.components.buttons import ToggleButton

        view = MagicMock()
        view.user_id = 1
        view.id = "view-under-test"
        view.is_finished = MagicMock(return_value=False)
        view.dispatch = AsyncMock()
        view.on_unauthorized = AsyncMock()

        button = ToggleButton(label="Mode", **kwargs)
        button._view = view
        return button, view

    def _interaction(self, user_id):
        from helpers import make_interaction

        interaction = make_interaction()
        interaction.user.id = user_id
        return interaction

    async def test_non_owner_cannot_toggle(self):
        calls = []

        async def user_cb(interaction):
            calls.append(True)

        button, view = self._make_button(callback=user_cb, owner_only=True)

        await button.callback(self._interaction(user_id=999))

        assert calls == []
        assert button.is_toggled is False
        view.on_unauthorized.assert_awaited_once()
        view.dispatch.assert_not_awaited()

    async def test_owner_toggles_and_relabels(self):
        button, view = self._make_button(toggled_label="Mode on", owner_only=True)

        await button.callback(self._interaction(user_id=1))

        assert button.is_toggled is True
        assert button.label == "Mode on"
        assert button.style is discord.ButtonStyle.success

        await button.callback(self._interaction(user_id=1))

        assert button.is_toggled is False
        assert button.label == "Mode"
        assert button.style is discord.ButtonStyle.secondary

    async def test_dispatched_value_is_the_state_it_ended_on(self):
        button, view = self._make_button()

        await button.callback(self._interaction(user_id=1))

        payload = view.dispatch.await_args.args[1]
        assert payload["value"] is True

    async def test_sync_callback_is_accepted(self):
        calls = []
        button, _ = self._make_button(callback=lambda interaction: calls.append(True))

        await button.callback(self._interaction(user_id=1))

        assert calls == [True]
        assert button.is_toggled is True

    async def test_finished_view_skips_dispatch(self):
        button, view = self._make_button()
        view.is_finished = MagicMock(return_value=True)

        await button.callback(self._interaction(user_id=1))

        view.dispatch.assert_not_awaited()

    async def test_two_param_callback_receives_post_flip_state(self):
        received = []

        async def cb(interaction, toggled):
            received.append(toggled)

        button, _ = self._make_button(callback=cb)

        await button.callback(self._interaction(user_id=1))
        await button.callback(self._interaction(user_id=1))

        assert received == [True, False]

    async def test_two_param_callback_from_toggled_start(self):
        received = []

        async def cb(interaction, toggled):
            received.append(toggled)

        button, _ = self._make_button(callback=cb, toggled=True)

        await button.callback(self._interaction(user_id=1))

        assert received == [False]

    async def test_one_param_callback_receives_one_argument(self):
        received = []

        async def cb(*args):
            received.append(args)

        button, _ = self._make_button(callback=cb)

        await button.callback(self._interaction(user_id=1))

        assert [len(args) for args in received] == [1]

    def test_zero_param_callback_refused(self):
        from cascadeui.components.buttons import ToggleButton

        with pytest.raises(TypeError) as caught:
            ToggleButton(label="Mode", callback=lambda: None)
        message = str(caught.value)

        assert "ToggleButton callback" in message
        # Both supported shapes are named, and the second one especially:
        # a message that omitted it would read as though this control
        # passes no value, which is what the generic button message says
        # and what this class does not do.
        assert "(interaction, toggled)" in message
        assert "passes no second value" not in message

    async def test_callback_is_the_shared_wrapper(self):
        """The toggle rides ``create_stateful_callback``; the raw
        ``_toggle`` coroutine is never installed as the callback."""
        button, _ = self._make_button()

        assert button.callback.__name__ == "stateful_callback"
        assert button.original_callback == button._toggle


class TestBuilderCustomIdAndDisabled:
    """Interactive builders expose custom_id so persistent panels can use them.

    Without it, every builder-produced button carries an auto id anchored
    on the view instance, which changes on every restart.
    """

    @staticmethod
    async def _cb(interaction):
        pass

    @staticmethod
    async def _cb_value(interaction, value):
        pass

    def test_button_row_suffixes_per_button(self):
        from cascadeui.components.patterns.v2 import button_row

        row = button_row({"Yes": self._cb, "No": self._cb}, custom_id="vote")
        assert [b.custom_id for b in row.children] == ["vote_0", "vote_1"]

    def test_button_row_without_custom_id_is_unchanged(self):
        from cascadeui.components.patterns.v2 import button_row

        row = button_row({"Yes": self._cb})
        assert row.children[0]._provided_custom_id is False

    def test_confirm_section_names_each_half(self):
        from cascadeui.components.patterns.v2 import confirm_section

        _, row = confirm_section("Sure?", on_confirm=self._cb, on_cancel=self._cb, custom_id="wipe")
        assert [b.custom_id for b in row.children] == ["wipe_confirm", "wipe_cancel"]

    def test_tab_nav_suffixes_per_tab(self):
        from cascadeui.components.patterns.v2 import tab_nav

        row = tab_nav({"A": self._cb, "B": self._cb}, custom_id="tab")
        assert [b.custom_id for b in row.children] == ["tab_0", "tab_1"]

    def test_cycle_and_toggle_buttons_take_custom_id(self):
        from cascadeui.components.patterns.v2 import cycle_button, toggle_button

        cycler = cycle_button(values=["a", "b"], on_change=self._cb_value, custom_id="preset")
        toggler = toggle_button(active=True, on_toggle=self._cb_value, custom_id="dark")
        assert cycler.custom_id == "preset"
        assert toggler.custom_id == "dark"

    def test_link_section_takes_disabled_like_its_siblings(self):
        from cascadeui.components.patterns.v2 import link_section

        section = link_section("Docs", label="Open", url="https://x.dev", disabled=True)
        assert section.accessory.disabled is True

    def test_image_section_renders_multiple_text_lines(self):
        from cascadeui.components.patterns.v2 import image_section

        section = image_section("Ada", "12W / 20G", url="https://x.dev/a.png")
        assert [c.content for c in section.children] == ["Ada", "12W / 20G"]

    def test_image_section_rejects_a_fourth_line(self):
        from cascadeui.components.patterns.v2 import image_section

        with pytest.raises(ValueError, match="3-children-per-Section"):
            image_section("a", "b", "c", "d", url="https://x.dev/a.png")

    def test_gallery_takes_per_item_spoilers(self):
        from cascadeui.components.patterns.v2 import gallery

        g = gallery("https://x.dev/1.png", "https://x.dev/2.png", spoilers=[True, False])
        assert [i.spoiler for i in g.items] == [True, False]

    def test_gallery_spoilers_length_must_match(self):
        from cascadeui.components.patterns.v2 import gallery

        with pytest.raises(ValueError, match="spoilers length"):
            gallery("https://x.dev/1.png", spoilers=[True, False])

    def test_stats_card_takes_spoiler_like_card(self):
        from cascadeui.components.patterns.v2 import stats_card

        assert stats_card("Title", {"a": 1}, spoiler=True).spoiler is True


class TestComposedCustomIdLength:
    """The builders concatenate, so the composed length is theirs to answer for.

    A base well inside Discord's 100-char cap can be pushed past it by a
    suffix the caller never sees, and discord.py stores custom_id
    unchecked, so it would surface as an HTTP 400 naming no component.
    """

    @staticmethod
    async def _cb(interaction):
        pass

    def test_confirm_section_rejects_an_overlong_composition(self):
        from cascadeui.components.patterns.v2 import confirm_section

        with pytest.raises(ValueError, match="over Discord's 100-character cap"):
            confirm_section("Sure?", on_confirm=self._cb, on_cancel=self._cb, custom_id="x" * 95)

    def test_button_row_boundary_is_exactly_100(self):
        from cascadeui.components.patterns.v2 import button_row

        row = button_row({"A": self._cb}, custom_id="x" * 98)
        assert len(row.children[0].custom_id) == 100

        with pytest.raises(ValueError, match="100-character cap"):
            button_row({"A": self._cb}, custom_id="x" * 99)

    def test_error_names_the_shortening_target(self):
        from cascadeui.components.patterns.v2 import tab_nav

        with pytest.raises(ValueError, match="at most 98 characters"):
            tab_nav({"A": self._cb}, custom_id="x" * 99)


class TestImageSectionEmptyLines:
    """An empty line is dropped rather than rendered.

    Discord rejects a text display with empty content and fails the whole
    message, so a formatter returning "" for one entry would otherwise
    take down every component beside it.
    """

    def test_empty_secondary_is_dropped(self):
        from cascadeui.components.patterns.v2 import image_section

        section = image_section("Ada", "", url="https://x.dev/a.png")
        assert [c.content for c in section.children] == ["Ada"]

    def test_empty_middle_line_is_dropped(self):
        from cascadeui.components.patterns.v2 import image_section

        section = image_section("Ada", "", "12W", url="https://x.dev/a.png")
        assert [c.content for c in section.children] == ["Ada", "12W"]

    def test_all_lines_empty_raises(self):
        from cascadeui.components.patterns.v2 import image_section

        with pytest.raises(ValueError, match="at least one non-empty text line"):
            image_section("", "", url="https://x.dev/a.png")

    def test_a_dropped_line_still_leaves_a_valid_section(self):
        """A Section with one text child and an accessory is legal, which is
        why dropping beats raising here.
        """
        from cascadeui.components.patterns.v2 import image_section
        from cascadeui.views._placement import validate_placement

        view = RenderableLayoutView()
        view.add_item(image_section("Ada", "", url="https://x.dev/a.png"))
        validate_placement(view)
