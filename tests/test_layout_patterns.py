"""Tests for TabLayoutView and WizardLayoutView (V2 patterns)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from discord.ui import ActionRow, Container, LayoutView, TextDisplay

from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.layout_patterns import TabLayoutView, WizardLayoutView
from helpers import make_interaction as _make_interaction


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

        async def on_finish(interaction):
            finish_called.append(True)
            await interaction.response.defer()

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[{"name": "Only", "builder": builder}],
            on_finish=on_finish,
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
