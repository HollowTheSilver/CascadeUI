"""Tests for TabView / TabLayoutView customization and parity."""

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import Container, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.views.patterns import TabLayoutView
from cascadeui.views.patterns.tabs import TabView


async def _builder():
    return [Container(TextDisplay("content"))]


# // ========================================( Button style validation )======================================== // #


class TestTabStyleValidation:
    """Invalid tab button styles raise at class definition time."""

    def test_invalid_style_raises_at_definition(self):
        with pytest.raises(ValueError, match="must be a discord.ButtonStyle"):

            class BadTabs(TabLayoutView):
                active_tab_style = "primary"  # str, not enum

    def test_valid_styles_accepted(self):
        class GoodTabs(TabLayoutView):
            active_tab_style = discord.ButtonStyle.success
            inactive_tab_style = discord.ButtonStyle.danger

        assert GoodTabs.active_tab_style is discord.ButtonStyle.success


# // ========================================( Style application )======================================== // #


class TestTabStyleApplication:
    """Custom active/inactive tab styles apply to generated buttons."""

    async def test_initial_styles_follow_customization(self):
        class ThemedTabs(TabLayoutView):
            active_tab_style = discord.ButtonStyle.success
            inactive_tab_style = discord.ButtonStyle.secondary

        view = ThemedTabs(
            interaction=_make_interaction(),
            tabs={"A": _builder, "B": _builder, "C": _builder},
        )

        assert view._tab_buttons[0].style is discord.ButtonStyle.success
        assert view._tab_buttons[1].style is discord.ButtonStyle.secondary
        assert view._tab_buttons[2].style is discord.ButtonStyle.secondary

    async def test_styles_mutate_on_switch(self):
        view = TabLayoutView(
            interaction=_make_interaction(),
            tabs={"A": _builder, "B": _builder},
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        view._active_tab = 1
        await view._refresh_tabs()

        assert view._tab_buttons[0].style is discord.ButtonStyle.secondary
        assert view._tab_buttons[1].style is discord.ButtonStyle.primary


# // ========================================( on_tab_switched hook )======================================== // #


class TestOnTabSwitchedHook:
    """on_tab_switched hook fires on tab change and defaults to no-op."""

    async def test_default_hook_is_noop(self):
        """Default hook must not raise or mutate state."""
        view = TabLayoutView(
            interaction=_make_interaction(),
            tabs={"A": _builder, "B": _builder},
        )
        result = await view.on_tab_switched(0)
        assert result is None

    async def test_hook_fires_on_switch_tab(self):
        calls = []

        class TrackedTabs(TabLayoutView):
            async def on_tab_switched(self, index):
                calls.append(index)

        view = TrackedTabs(
            interaction=_make_interaction(),
            tabs={"A": _builder, "B": _builder},
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view.switch_tab("B")

        assert calls == [1]


# // ========================================( V2 button identity parity )======================================== // #


class TestTabLayoutButtonIdentity:
    """V2 variant must mutate buttons in place, not rebuild them."""

    async def test_button_identity_stable_across_refresh(self):
        view = TabLayoutView(
            interaction=_make_interaction(),
            tabs={"A": _builder, "B": _builder, "C": _builder},
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        ids_before = [id(b) for b in view._tab_buttons]
        row_ids = [id(r) for r in view._tab_rows]

        view._active_tab = 2
        await view._refresh_tabs()

        ids_after = [id(b) for b in view._tab_buttons]
        assert ids_before == ids_after
        assert [id(r) for r in view._tab_rows] == row_ids


# // ========================================( Multi-row spill )======================================== // #


class TestTabLayoutRowSpill:
    """Tab buttons must chunk into multiple ActionRows past five tabs.

    Discord caps ``ActionRow`` at five interactive children, so
    a six-tab ``TabLayoutView`` has to spill into a second row.
    """

    def test_three_tabs_one_row(self):
        view = TabLayoutView(
            interaction=_make_interaction(),
            tabs={"A": _builder, "B": _builder, "C": _builder},
        )
        assert len(view._tab_rows) == 1
        assert len(view._tab_rows[0].children) == 3

    def test_five_tabs_one_row(self):
        tabs = {name: _builder for name in "ABCDE"}
        view = TabLayoutView(interaction=_make_interaction(), tabs=tabs)
        assert len(view._tab_rows) == 1
        assert len(view._tab_rows[0].children) == 5

    def test_six_tabs_two_rows(self):
        tabs = {name: _builder for name in "ABCDEF"}
        view = TabLayoutView(interaction=_make_interaction(), tabs=tabs)
        assert len(view._tab_rows) == 2
        assert len(view._tab_rows[0].children) == 5
        assert len(view._tab_rows[1].children) == 1

    def test_ten_tabs_two_full_rows(self):
        tabs = {f"tab{i}": _builder for i in range(10)}
        view = TabLayoutView(interaction=_make_interaction(), tabs=tabs)
        assert len(view._tab_rows) == 2
        assert len(view._tab_rows[0].children) == 5
        assert len(view._tab_rows[1].children) == 5

    async def test_callback_routes_correctly_across_rows(self):
        """Button index 7 (second row) must still flip ``_active_tab`` to 7."""
        tabs = {f"tab{i}": _builder for i in range(8)}
        view = TabLayoutView(interaction=_make_interaction(), tabs=tabs)
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        # The 8th button lives on the second row at position 2.
        target = view._tab_buttons[7]
        mock_interaction = _make_interaction()
        await target.callback(mock_interaction)

        assert view._active_tab == 7


# // ========================================( Tab overflow validation )======================================== // #


class TestTabOverflowValidation:
    """Static validation of ``tab_overflow_policy`` at class-definition time."""

    def test_unknown_preset_rejected(self):
        with pytest.raises(ValueError, match="not a valid preset"):

            class BadPreset(TabLayoutView):
                tab_overflow_policy = "squish"

    def test_empty_tuple_rejected(self):
        with pytest.raises(ValueError, match="at least one row width"):

            class EmptyTuple(TabLayoutView):
                tab_overflow_policy = ()

    def test_tuple_with_zero_rejected(self):
        with pytest.raises(ValueError, match="row widths must be >= 1"):

            class ZeroTuple(TabLayoutView):
                tab_overflow_policy = (3, 0, 2)

    def test_tuple_with_negative_rejected(self):
        with pytest.raises(ValueError, match="row widths must be >= 1"):

            class NegTuple(TabLayoutView):
                tab_overflow_policy = (3, -1)

    def test_tuple_width_over_five_rejected(self):
        with pytest.raises(ValueError, match="at most 5 buttons"):

            class OverWide(TabLayoutView):
                tab_overflow_policy = (6, 1)

    def test_tuple_length_over_five_rejected(self):
        with pytest.raises(ValueError, match="at most 5 component rows"):

            class OverTall(TabLayoutView):
                tab_overflow_policy = (1, 1, 1, 1, 1, 1)

    def test_non_int_tuple_entry_rejected(self):
        with pytest.raises(ValueError, match="must be integers"):

            class FloatTuple(TabLayoutView):
                tab_overflow_policy = (3.0, 2)

    def test_bool_tuple_entry_rejected(self):
        with pytest.raises(ValueError, match="must be integers"):

            class BoolTuple(TabLayoutView):
                tab_overflow_policy = (True, 2)

    def test_wrong_type_rejected(self):
        with pytest.raises(ValueError, match="must be a preset string"):

            class ListPolicy(TabLayoutView):
                tab_overflow_policy = [3, 3]

    def test_valid_preset_accepted(self):
        class Balanced(TabLayoutView):
            tab_overflow_policy = "balance"

        assert Balanced.tab_overflow_policy == "balance"

    def test_valid_tuple_accepted(self):
        class Fixed(TabLayoutView):
            tab_overflow_policy = (2, 3)

        assert Fixed.tab_overflow_policy == (2, 3)


# // ========================================( Tab overflow policy behavior )======================================== // #


class TestTabOverflowPolicy:
    """Row splitting honors the declared policy at build time."""

    def _tabs(self, n: int):
        return {f"tab{i}": _builder for i in range(n)}

    def test_fill_preset_six_tabs_five_one(self):
        view = TabLayoutView(interaction=_make_interaction(), tabs=self._tabs(6))
        widths = [len(r.children) for r in view._tab_rows]
        assert widths == [5, 1]

    def test_balance_preset_six_tabs_three_three(self):
        class Balanced(TabLayoutView):
            tab_overflow_policy = "balance"

        view = Balanced(interaction=_make_interaction(), tabs=self._tabs(6))
        widths = [len(r.children) for r in view._tab_rows]
        assert widths == [3, 3]

    def test_balance_preset_seven_tabs_four_three(self):
        class Balanced(TabLayoutView):
            tab_overflow_policy = "balance"

        view = Balanced(interaction=_make_interaction(), tabs=self._tabs(7))
        widths = [len(r.children) for r in view._tab_rows]
        assert widths == [4, 3]

    def test_pin_first_six_tabs_one_five(self):
        class PinFirst(TabLayoutView):
            tab_overflow_policy = "pin_first"

        view = PinFirst(interaction=_make_interaction(), tabs=self._tabs(6))
        widths = [len(r.children) for r in view._tab_rows]
        assert widths == [1, 5]

    def test_pin_last_six_tabs_five_one(self):
        class PinLast(TabLayoutView):
            tab_overflow_policy = "pin_last"

        view = PinLast(interaction=_make_interaction(), tabs=self._tabs(6))
        widths = [len(r.children) for r in view._tab_rows]
        assert widths == [5, 1]

    def test_pin_last_seven_tabs_differs_from_fill(self):
        """At N=7, pin_last = [5,1,1] while fill = [5,2]."""

        class PinLast(TabLayoutView):
            tab_overflow_policy = "pin_last"

        pin_view = PinLast(interaction=_make_interaction(), tabs=self._tabs(7))
        fill_view = TabLayoutView(interaction=_make_interaction(), tabs=self._tabs(7))
        assert [len(r.children) for r in pin_view._tab_rows] == [5, 1, 1]
        assert [len(r.children) for r in fill_view._tab_rows] == [5, 2]

    def test_tuple_exact_match(self):
        class Fixed(TabLayoutView):
            tab_overflow_policy = (3, 3)

        view = Fixed(interaction=_make_interaction(), tabs=self._tabs(6))
        widths = [len(r.children) for r in view._tab_rows]
        assert widths == [3, 3]

    def test_tuple_asymmetric(self):
        class Fixed(TabLayoutView):
            tab_overflow_policy = (1, 5)

        view = Fixed(interaction=_make_interaction(), tabs=self._tabs(6))
        widths = [len(r.children) for r in view._tab_rows]
        assert widths == [1, 5]

    def test_tuple_short_declaration_greedy_fills(self, caplog):
        class ShortTuple(TabLayoutView):
            tab_overflow_policy = (2, 2)

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.patterns.tabs"):
            view = ShortTuple(interaction=_make_interaction(), tabs=self._tabs(7))

        widths = [len(r.children) for r in view._tab_rows]
        assert widths == [2, 2, 3]
        assert any("packed via fill" in rec.message for rec in caplog.records)

    def test_tuple_long_declaration_drops_trailing(self, caplog):
        class LongTuple(TabLayoutView):
            tab_overflow_policy = (3, 3, 3)

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.patterns.tabs"):
            view = LongTuple(interaction=_make_interaction(), tabs=self._tabs(5))

        widths = [len(r.children) for r in view._tab_rows]
        assert widths == [3, 2]
        assert any("trailing row widths dropped" in rec.message for rec in caplog.records)


# // ========================================( V1 / V2 parity ) ======================================== // #


class TestTabOverflowParity:
    """V1 and V2 produce the same row shape for the same tab_overflow_policy."""

    def test_v1_balance_six_tabs_three_three(self):
        class V1Balanced(TabView):
            tab_overflow_policy = "balance"

        tabs = {f"tab{i}": _builder for i in range(6)}
        view = V1Balanced(interaction=_make_interaction(), tabs=tabs)
        rows_by_index: dict[int, int] = {}
        for button in view._tab_buttons:
            rows_by_index[button.row] = rows_by_index.get(button.row, 0) + 1
        widths = [rows_by_index[i] for i in sorted(rows_by_index)]
        assert widths == [3, 3]

    def test_v1_six_tabs_default_fill_spans_two_rows(self):
        """V1 previously hardcoded row=0 for every tab button; with
        ``_build_tab_rows`` it now respects the ActionRow cap."""
        tabs = {f"tab{i}": _builder for i in range(6)}
        view = TabView(interaction=_make_interaction(), tabs=tabs)
        rows_by_index: dict[int, int] = {}
        for button in view._tab_buttons:
            rows_by_index[button.row] = rows_by_index.get(button.row, 0) + 1
        widths = [rows_by_index[i] for i in sorted(rows_by_index)]
        assert widths == [5, 1]


# // ========================================( Composites inside tabs )======================================== // #


class TestCompositesInsideTabs:
    """V2 stateful composites re-render correctly when placed inside a tab.

    A ``TabLayoutView`` rebuilds its active tab through ``_refresh_tabs``,
    not ``build_ui``/``on_load``. The shared ``_rerender_host`` probe must
    recognize that seam so a ``Collapsible`` (or ``PaginatedRegion``) click
    inside a tab rebuilds the tab instead of refreshing a stale tree.
    """

    async def test_collapsible_toggle_rebuilds_tab(self):
        from cascadeui import Collapsible, card

        class DemoTabs(TabLayoutView):
            owner_only = False

            def __init__(self, **kwargs):
                self.box = Collapsible(
                    label="More",
                    expanded_label="Less",
                    reveal=lambda: TextDisplay("REVEALED"),
                )
                super().__init__(tabs={"T": self.build_t}, **kwargs)

            async def build_t(self):
                return [card("## Tab", *self.box.render(self))]

        view = DemoTabs(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        await view._refresh_tabs()  # initial build, collapsed

        def has_revealed():
            return any(
                isinstance(c, TextDisplay) and "REVEALED" in (c.content or "")
                for c in view.walk_children()
            )

        assert has_revealed() is False
        await view.box._toggle(_make_interaction())  # internal composite toggle
        assert has_revealed() is True  # the tab was rebuilt via _refresh_tabs
