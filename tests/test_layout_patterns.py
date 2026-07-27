"""Tests for TabLayoutView and WizardLayoutView (V2 patterns)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ui import ActionRow, Container, LayoutView, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.patterns import TabLayoutView, WizardLayoutView

# // ========================================( TabLayoutView )======================================== // #


class TestTabLayoutViewInit:
    """Basic init and tab switching tests."""

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(TabLayoutView, StatefulLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(TabLayoutView, LayoutView)

    def test_init_with_tabs(self):
        async def builder_a():
            return [Container(TextDisplay("Tab A"))]

        async def builder_b():
            return [Container(TextDisplay("Tab B"))]

        interaction = _make_interaction()
        view = TabLayoutView(
            interaction=interaction,
            tabs={"Tab A": builder_a, "Tab B": builder_b},
        )

        assert view.active_tab == "Tab A"
        assert view._active_tab == 0

    def test_has_tab_buttons(self):
        async def builder():
            return [Container(TextDisplay("content"))]

        interaction = _make_interaction()
        view = TabLayoutView(
            interaction=interaction,
            tabs={"First": builder, "Second": builder},
        )

        # Should have an ActionRow with tab buttons
        action_rows = [c for c in view.children if isinstance(c, ActionRow)]
        assert len(action_rows) >= 1

        # Check tab button custom_ids
        tab_row = action_rows[0]
        custom_ids = [getattr(c, "custom_id", None) for c in tab_row.children]
        assert "tab_0" in custom_ids
        assert "tab_1" in custom_ids

    def test_active_tab_property(self):
        async def builder():
            return [Container(TextDisplay("x"))]

        interaction = _make_interaction()
        view = TabLayoutView(
            interaction=interaction,
            tabs={"Alpha": builder, "Beta": builder, "Gamma": builder},
        )

        assert view.active_tab == "Alpha"
        view._active_tab = 2
        assert view.active_tab == "Gamma"


class TestTabLayoutViewRefresh:
    """Tab switching refresh tests."""

    async def test_refresh_tabs_updates_content(self):
        async def builder_a():
            return [Container(TextDisplay("Content A"))]

        async def builder_b():
            return [Container(TextDisplay("Content B"))]

        interaction = _make_interaction()
        view = TabLayoutView(
            interaction=interaction,
            tabs={"A": builder_a, "B": builder_b},
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        view._active_tab = 1
        await view._refresh_tabs()

        view._message.edit.assert_called_once()


# // ========================================( WizardLayoutView )======================================== // #


class TestWizardLayoutViewInit:
    """Basic init tests."""

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(WizardLayoutView, StatefulLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(WizardLayoutView, LayoutView)

    def test_init_with_steps(self):
        async def builder():
            return [Container(TextDisplay("Step content"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[
                {"name": "Step 1", "builder": builder},
                {"name": "Step 2", "builder": builder},
            ],
        )

        assert view.current_step == 0
        assert view.step_count == 2

    def test_has_nav_buttons(self):
        async def builder():
            return [Container(TextDisplay("x"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[{"name": "S1", "builder": builder}],
        )

        # Find nav ActionRow with wizard buttons
        all_custom_ids = []
        for child in view.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    cid = getattr(item, "custom_id", None)
                    if cid:
                        all_custom_ids.append(cid)

        assert "wizard_back" in all_custom_ids
        assert "wizard_indicator" in all_custom_ids
        assert "wizard_next" in all_custom_ids

    def test_single_step_shows_finish(self):
        async def builder():
            return [Container(TextDisplay("only step"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[{"name": "Only", "builder": builder}],
        )

        # Find the next button
        for child in view.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    if getattr(item, "custom_id", None) == "wizard_next":
                        assert item.label == "Finish"


class TestWizardLayoutViewNavigation:
    """Step navigation tests."""

    async def test_go_next_advances_step(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[
                {"name": "Step 1", "builder": builder},
                {"name": "Step 2", "builder": builder},
                {"name": "Step 3", "builder": builder},
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        nav_interaction = _make_interaction()
        await view._go_next(nav_interaction)

        assert view._current_step == 1

    async def test_go_back_decrements_step(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[
                {"name": "Step 1", "builder": builder},
                {"name": "Step 2", "builder": builder},
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        # Go to step 2
        nav_interaction = _make_interaction()
        await view._go_next(nav_interaction)
        assert view._current_step == 1

        # Go back to step 1
        back_interaction = _make_interaction()
        await view._go_back(back_interaction)
        assert view._current_step == 0

    async def test_go_back_at_first_step_noop(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[{"name": "Only", "builder": builder}],
        )

        nav_interaction = _make_interaction()
        await view._go_back(nav_interaction)

        assert view._current_step == 0

    async def test_finish_calls_on_finish(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        finish_called = []

        class FinishWizard(WizardLayoutView):
            async def on_finish(self, interaction):
                finish_called.append(True)
                if not interaction.response.is_done():
                    await interaction.response.defer()

        interaction = _make_interaction()
        view = FinishWizard(
            interaction=interaction,
            steps=[{"name": "Only", "builder": builder}],
        )

        nav_interaction = _make_interaction()
        await view._go_next(nav_interaction)

        assert len(finish_called) == 1

    async def test_validator_blocks_next(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        async def failing_validator():
            return False, "Validation failed"

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[
                {"name": "Step 1", "builder": builder, "validator": failing_validator},
                {"name": "Step 2", "builder": builder},
            ],
        )

        nav_interaction = _make_interaction()
        await view._go_next(nav_interaction)

        # Should not advance
        assert view._current_step == 0
        nav_interaction.response.send_message.assert_called_once_with(
            "Validation failed", ephemeral=True
        )


class TestCompositeHostSeams:
    """The V2 composites probe host render seams by name, not by type.

    The component layer cannot import views without a cycle, so
    ``_rerender_host`` duck-types each seam. That makes the seam names a
    cross-package contract: renaming one silently stops composites from
    re-rendering inside that host.
    """

    def test_probed_host_seams_still_exist(self):
        """A rename check only. The behavior these seams drive is covered
        end to end by ``test_collapsible_toggle_rebuilds_tab`` in
        ``tests/test_tab_patterns.py``; this catches the cheaper failure of
        one of the three names disappearing, which that test would report
        as a confusing render failure rather than a missing attribute.
        """
        from cascadeui.views.layout import StatefulLayoutView
        from cascadeui.views.patterns.tabs import TabLayoutView

        assert hasattr(TabLayoutView, "_refresh_tabs")
        assert hasattr(StatefulLayoutView, "reload")
        assert hasattr(StatefulLayoutView, "refresh")

    def test_probe_order_prefers_build_ui_then_tabs_then_reload(self):
        """The seam order is load-bearing: a TabLayoutView has no build_ui
        and its bare reload() would refresh a stale tree, so _refresh_tabs
        has to be probed before reload.
        """
        import inspect

        from cascadeui.components.patterns import v2

        src = inspect.getsource(v2._rerender_host)
        positions = [src.index(name) for name in ("build_ui", "_refresh_tabs", "reload")]
        assert positions == sorted(positions)


class TestGotoModalHookIsGuarded:
    """The goto-modal fires ``on_page_changed`` after moving the cursor.

    ``_safe_defer`` has already acked by that point, so a raising override
    would leave the user looking at a page that silently never turned: the
    cursor advances and ``_update_page`` never runs. The three sibling
    call sites in the same file were already guarded.
    """

    def test_goto_modal_routes_the_hook_through_the_guard(self):
        import inspect

        from cascadeui.views.patterns.paginated import _BasePaginatedMixin

        src = inspect.getsource(_BasePaginatedMixin._open_goto_modal)
        assert "_call_hook_safe(parent.on_page_changed" in src
        assert "await parent.on_page_changed(" not in src
