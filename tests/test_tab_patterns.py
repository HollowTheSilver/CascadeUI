"""Tests for TabView / TabLayoutView customization and parity."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from discord.ui import Container, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.state.singleton import get_store
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

    async def test_builder_derives_active_from_cursor(self):
        """The builder styles the active tab from _active_tab, not position 0,
        so a rebuild off a non-zero tab highlights the right button.
        """
        view = TabLayoutView(
            interaction=_make_interaction(),
            tabs={"A": _builder, "B": _builder, "C": _builder},
        )
        view._active_tab = 2
        view._tab_buttons = []
        view.clear_items()
        view._build_tab_buttons()

        assert view._tab_buttons[2].style is discord.ButtonStyle.primary
        assert view._tab_buttons[0].style is discord.ButtonStyle.secondary


class TestTabButtonDerivationV1:
    """V1 TabView's builder styles the active tab from _active_tab, mirroring
    the V2 fix. The two builders are independent implementations, so the V1
    path needs its own coverage.
    """

    def test_v1_builder_derives_active_from_cursor(self):
        view = TabView(
            interaction=_make_interaction(),
            tabs={"A": _builder, "B": _builder, "C": _builder},
        )
        view._active_tab = 2
        view._tab_buttons = []
        view.clear_items()
        view._build_tab_buttons()

        assert view._tab_buttons[2].style is view.active_tab_style
        assert view._tab_buttons[0].style is view.inactive_tab_style


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

    async def test_raising_hook_does_not_abort_switch(self):
        """A raising on_tab_switched override is swallowed so the tab switch
        and its refresh still complete."""

        class RaisingTabs(TabLayoutView):
            async def on_tab_switched(self, index):
                raise RuntimeError("boom")

        view = RaisingTabs(
            interaction=_make_interaction(),
            tabs={"A": _builder, "B": _builder},
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        callback = view._make_switch_callback(1)
        await callback(_make_interaction())  # must not raise

        assert view._active_tab == 1
        assert view._message.edit.called

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

    async def test_switch_tab_without_notify_renders_without_the_hook(self):
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

        await view.switch_tab("B", notify=False)

        assert calls == []
        assert view._active_tab == 1
        assert view._message.edit.called


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


class TestTabViewInitialRender:
    """V1 tab content reaches the first message, not just the pop edit."""

    async def test_send_ships_the_active_tab_embed(self):
        """Tab builders are async, so only send() can put them on the message.

        The tab row is added in __init__ and always ships; the embed is the
        content. Without it the first message is a row of tab buttons over an
        empty body, while a later pop (which routes through
        nav_rebuild) renders correctly.
        """

        async def tab_a():
            return discord.Embed(title="Tab A")

        interaction = _make_interaction()
        view = TabView(interaction=interaction, tabs={"A": tab_a, "B": tab_a})

        await view.send()

        embed = interaction.response.send_message.call_args.kwargs.get("embed")
        assert embed is not None
        assert embed.title == "Tab A"

    async def test_explicit_embed_wins(self):
        """A caller-supplied embed is not replaced by the tab builder."""

        async def tab_a():
            return discord.Embed(title="Tab A")

        interaction = _make_interaction()
        view = TabView(interaction=interaction, tabs={"A": tab_a})

        await view.send(embed=discord.Embed(title="Caller"))

        embed = interaction.response.send_message.call_args.kwargs.get("embed")
        assert embed.title == "Caller"


# // ========================================( refresh_content (public re-render) )======================================== // #


class TestRefreshContent:
    """refresh_content() re-renders in place: V1 ships the embed, V2 the tree."""

    async def test_v1_ships_the_embed(self):
        async def embed_builder():
            return discord.Embed(title="V1 content")

        view = TabView(
            interaction=_make_interaction(),
            tabs={"A": embed_builder, "B": embed_builder},
        )
        view.refresh = AsyncMock()
        await view.refresh_content()
        embed = view.refresh.call_args.kwargs.get("embed")
        assert embed is not None
        assert embed.title == "V1 content"

    async def test_v2_recomposes_the_active_tab(self):
        state = {"text": "before"}

        async def builder():
            return [Container(TextDisplay(state["text"]))]

        view = TabLayoutView(
            interaction=_make_interaction(),
            tabs={"A": builder, "B": builder},
        )
        view.refresh = AsyncMock()
        state["text"] = "after"
        await view.refresh_content()
        tree = " ".join(
            getattr(t, "content", "") for t in view.walk_children() if isinstance(t, TextDisplay)
        )
        assert "after" in tree
        view.refresh.assert_awaited()

    async def test_v1_reload_ships_the_embed(self):
        async def eb():
            return discord.Embed(title="reloaded")

        view = TabView(interaction=_make_interaction(), tabs={"A": eb, "B": eb})
        view.refresh = AsyncMock()
        await view.reload()
        embed = view.refresh.call_args.kwargs.get("embed")
        assert embed is not None  # was the empty-kwargs no-op before the fix
        assert embed.title == "reloaded"

    async def test_a_tab_render_waits_for_a_reload_already_rebuilding(self):
        """Both clear and refill the tree around an awaited builder, so running
        side by side they interleaved and shipped duplicated components."""
        builds = {"n": 0}

        async def tab_a():
            builds["n"] += 1
            await asyncio.sleep(0.05 if builds["n"] % 2 else 0.01)
            return TextDisplay(f"A{builds['n']}")

        async def tab_b():
            return TextDisplay("B")

        class Tabs(TabLayoutView):
            def _build_extra_items(self):
                self.add_item(
                    discord.ui.ActionRow(discord.ui.Button(label="extra", custom_id="extra_btn"))
                )

        view = Tabs(interaction=_make_interaction(), tabs={"A": tab_a, "B": tab_b})
        await view.on_load()
        message = MagicMock(id=1)
        message.edit = AsyncMock(return_value=message)
        view._message = message

        reloading = asyncio.create_task(view.reload())
        await asyncio.sleep(0)  # the reload has cleared and awaits the builder
        rendering = asyncio.create_task(view.refresh_content())
        await asyncio.wait_for(asyncio.gather(reloading, rendering), timeout=3)

        ids = [c.custom_id for c in view.walk_children() if getattr(c, "custom_id", None)]
        assert ids.count("extra_btn") == 1
        assert sum(isinstance(c, TextDisplay) for c in view.children) == 1

    @pytest.mark.parametrize(
        "cls, content",
        [
            (TabView, lambda text: discord.Embed(title=text)),
            (TabLayoutView, TextDisplay),
        ],
        ids=["v1", "v2"],
    )
    async def test_tab_renders_that_outlive_an_exit_leave_the_frozen_panel_alone(
        self, cls, content
    ):
        """A render whose builder was running when exit() froze the panel, and
        one queued behind it, both shipped live controls onto it afterwards."""
        gate = asyncio.Event()
        builds = {"n": 0}

        async def tab_a():
            builds["n"] += 1
            if builds["n"] > 1:
                await gate.wait()
            return content(f"A{builds['n']}")

        async def tab_b():
            return content("B")

        view = cls(interaction=_make_interaction(), tabs={"A": tab_a, "B": tab_b})
        await view.send()
        message = view._message
        in_flight = asyncio.create_task(view.refresh_content())
        await asyncio.sleep(0)
        queued = asyncio.create_task(view.refresh_content())
        await asyncio.sleep(0)
        await view.exit()
        edits = message.edit.await_count

        gate.set()
        await asyncio.wait_for(asyncio.gather(in_flight, queued), timeout=2)

        assert message.edit.await_count == edits
        assert builds["n"] == 2  # the queued render never built on the dead view

    async def test_a_tab_render_leaves_an_armed_view_on_its_refresh_button(self):
        """A background update that re-rendered the active tab rebuilt the tree
        over the refresh button, and an armed view drops every notification
        that could put it back."""

        async def tab():
            return TextDisplay("content")

        class Tabs(TabLayoutView):
            auto_refresh_ephemeral = True

        view = Tabs(interaction=_make_interaction(), tabs={"A": tab, "B": tab}, timeout=None)
        await view.send(ephemeral=True)
        await view._arm_refresh_button()
        armed_tree = [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]

        await view.refresh_content()

        assert [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)] == (
            armed_tree
        )

    async def test_the_first_tab_embed_reads_the_seeded_state(self):
        """The first embed was built before seed_initial_state ran, so a tab
        reading seeded state sent without it."""
        from cascadeui import access_slot, read_slot

        class Tabs(TabView):
            async def seed_initial_state(self, state):
                access_slot(state, "greetings", self.id)["text"] = "seeded"

        holder = {}

        async def overview():
            view = holder["view"]
            text = read_slot(view.state_store.state, "greetings", view.id, "text", default="none")
            return discord.Embed(title=text)

        interaction = _make_interaction()
        view = Tabs(interaction=interaction, tabs={"Overview": overview})
        holder["view"] = view
        await view.send()

        assert interaction.response.send_message.call_args.kwargs["embed"].title == "seeded"

    async def test_explicit_embeds_are_sent_in_place_of_the_active_tab(self):
        """The active tab's embed was added beside them, and discord.py refuses
        ``embed`` and ``embeds`` together."""

        async def tab_a():
            return discord.Embed(title="tab")

        interaction = _make_interaction()
        view = TabView(interaction=interaction, tabs={"A": tab_a})
        mine = [discord.Embed(title="mine")]

        await view.send(embeds=mine)

        sent = interaction.response.send_message.call_args.kwargs
        assert sent["embeds"] is mine
        assert "embed" not in sent

    async def test_a_state_change_during_the_first_tab_build_reaches_the_message(self):
        """A V1 tab view builds its first embed before the message exists. A
        change arriving meanwhile was rendered with nowhere to go, and the
        send then shipped the embed built before it."""
        store = get_store()

        async def bump(action, state):
            return {**state, "application": {**state["application"], "count": 1}}

        store._register_reducer("FIRST_TAB_BUMPED", bump)
        in_build = asyncio.Event()
        gate = asyncio.Event()

        class Tabs(TabView):
            subscribed_actions = {"FIRST_TAB_BUMPED"}

            def state_selector(self, state):
                return state["application"].get("count")

            async def on_state_changed(self, state):
                await self.refresh_content()

            async def on_pre_send(self, interaction):
                await asyncio.sleep(0.01)  # a permission check against a database
                return True

        async def tab_a():
            count = store.state["application"].get("count", 0)
            if not in_build.is_set():
                in_build.set()
                await gate.wait()
            return discord.Embed(title=f"count={count}")

        view = Tabs(interaction=_make_interaction(), tabs={"A": tab_a})
        send = asyncio.create_task(view.send())
        await asyncio.wait_for(in_build.wait(), timeout=2)
        await store.dispatch("FIRST_TAB_BUMPED", {})
        await store._flush_notifications()
        gate.set()
        await asyncio.wait_for(send, timeout=2)
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)

        titles = [
            call.kwargs["embed"].title
            for call in view._message.edit.await_args_list
            if "embed" in call.kwargs
        ]
        assert titles[-1:] == ["count=1"]


class TestTabCursorRewindsWhenTheEditNeverLanded:
    """The active tab moves before the repaint, so a dropped edit desyncs them."""

    @staticmethod
    def _wire(view):
        view.interaction = _make_interaction()
        view.user_id = 1
        view.guild_id = 2
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock()
        view._message = message
        return message

    @pytest.mark.parametrize(
        "cls,tabs",
        [
            (
                TabLayoutView,
                {
                    "x": lambda: [TextDisplay("x")],
                    "y": lambda: [TextDisplay("y")],
                    "z": lambda: [TextDisplay("z")],
                },
            ),
            (
                TabView,
                {
                    "x": lambda: discord.Embed(title="x"),
                    "y": lambda: discord.Embed(title="y"),
                    "z": lambda: discord.Embed(title="z"),
                },
            ),
        ],
        ids=["v2", "v1"],
    )
    async def test_a_dropped_switch_leaves_the_cursor_on_the_visible_tab(self, cls, tabs):
        view = cls(tabs=tabs)
        message = self._wire(view)

        message.edit = AsyncMock(side_effect=aiohttp.ClientOSError(104, "reset"))
        await view._make_switch_callback(2)(_make_interaction())
        assert view._active_tab == 0

        message.edit = AsyncMock()
        await view._make_switch_callback(2)(_make_interaction())
        assert view._active_tab == 2

    @pytest.mark.parametrize(
        "cls,tabs",
        [
            (
                TabLayoutView,
                {
                    "x": lambda: [TextDisplay("x")],
                    "y": lambda: [TextDisplay("y")],
                    "z": lambda: [TextDisplay("z")],
                },
            ),
            (
                TabView,
                {
                    "x": lambda: discord.Embed(title="x"),
                    "y": lambda: discord.Embed(title="y"),
                    "z": lambda: discord.Embed(title="z"),
                },
            ),
        ],
        ids=["v2", "v1"],
    )
    async def test_a_refused_switch_leaves_the_cursor_on_the_visible_tab(self, cls, tabs):
        view = cls(tabs=tabs)
        message = self._wire(view)

        message.edit = AsyncMock(
            side_effect=discord.HTTPException(
                MagicMock(status=400, reason="Bad Request"), "Invalid Form Body"
            )
        )
        with pytest.raises(discord.HTTPException):
            await view._make_switch_callback(2)(_make_interaction())
        assert view._active_tab == 0
        assert view._tab_buttons[0].style == view.active_tab_style

        message.edit = AsyncMock()
        await view._make_switch_callback(2)(_make_interaction())
        assert view._active_tab == 2

    async def test_a_tab_whose_builder_raises_leaves_the_cursor_on_the_visible_tab(self):
        def broken():
            raise RuntimeError("database unavailable")

        view = TabLayoutView(tabs={"x": lambda: [TextDisplay("x")], "z": broken})
        message = self._wire(view)

        with pytest.raises(RuntimeError, match="database unavailable"):
            await view._make_switch_callback(1)(_make_interaction())

        assert view._active_tab == 0
        message.edit.assert_not_awaited()
        shown = [c.content for c in view.walk_children() if isinstance(c, TextDisplay)]
        assert "x" in shown
