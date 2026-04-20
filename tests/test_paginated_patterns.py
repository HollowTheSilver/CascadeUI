"""Tests for PaginatedView / PaginatedLayoutView customization and parity (Phase 2B Move 5)."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import ActionRow, Button, Container, TextDisplay

from cascadeui.views.patterns import PaginatedLayoutView, PaginatedView
from helpers import make_interaction as _make_interaction


# // ========================================( Button style validation )======================================== // #


class TestPaginatedStyleValidation:
    """Invalid pagination button styles raise at class definition time."""
    def test_invalid_style_raises_at_definition(self):
        with pytest.raises(ValueError, match="must be a discord.ButtonStyle"):

            class BadPaginated(PaginatedLayoutView):
                next_button_style = "secondary"  # str, not enum

    def test_valid_styles_accepted(self):
        class GoodPaginated(PaginatedLayoutView):
            first_button_style = discord.ButtonStyle.success
            prev_button_style = discord.ButtonStyle.success
            indicator_button_style = discord.ButtonStyle.danger
            next_button_style = discord.ButtonStyle.success
            last_button_style = discord.ButtonStyle.success

        assert GoodPaginated.indicator_button_style is discord.ButtonStyle.danger


# // ========================================( Customization round-trip )======================================== // #


class TestPaginatedLayoutCustomization:
    """Custom labels and styles apply to generated pagination buttons."""
    def test_label_overrides_apply_to_buttons(self):
        class CustomPaginated(PaginatedLayoutView):
            prev_button_label = "Back"
            next_button_label = "Forward"

        view = CustomPaginated(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("A"))], [Container(TextDisplay("B"))]],
        )

        assert view._prev_btn.label == "Back"
        assert view._next_btn.label == "Forward"

    def test_style_override_applies(self):
        class CustomPaginated(PaginatedLayoutView):
            next_button_style = discord.ButtonStyle.success

        view = CustomPaginated(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("A"))], [Container(TextDisplay("B"))]],
        )

        assert view._next_btn.style is discord.ButtonStyle.success


# // ========================================( on_page_changed hook )======================================== // #


class TestOnPageChangedHook:
    """on_page_changed hook fires on navigation and defaults to no-op."""
    async def test_default_hook_is_noop(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("A"))], [Container(TextDisplay("B"))]],
        )
        result = await view.on_page_changed(0)
        assert result is None

    async def test_hook_fires_on_step_callback(self):
        calls = []

        class TrackedPaginated(PaginatedLayoutView):
            async def on_page_changed(self, page):
                calls.append(page)

        view = TrackedPaginated(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
                [Container(TextDisplay("C"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        callback = view._make_step_callback(1)
        await callback(_make_interaction())

        assert calls == [1]
        assert view.current_page == 1


# // ========================================( V2 nav identity + extra_items preservation )======================================== // #


class TestPaginatedLayoutButtonIdentity:
    """V2 variant must mutate nav buttons in place and preserve extra items."""

    async def test_nav_identity_stable_across_page_turn(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
                [Container(TextDisplay("C"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        prev_id = id(view._prev_btn)
        next_id = id(view._next_btn)
        indicator_id = id(view._indicator_btn)
        nav_row_id = id(view._nav_row)

        view.current_page = 1
        await view._update_page()

        assert id(view._prev_btn) == prev_id
        assert id(view._next_btn) == next_id
        assert id(view._indicator_btn) == indicator_id
        assert id(view._nav_row) == nav_row_id

    async def test_extra_items_preserved_across_page_turn(self):
        extra_marker = Container(TextDisplay("EXTRA"))

        class WithExtras(PaginatedLayoutView):
            def _build_extra_items(self_inner):
                self_inner.add_item(extra_marker)

        view = WithExtras(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert extra_marker in view._extra_items

        view.current_page = 1
        await view._update_page()

        # Extra marker must still be a child after page turn.
        assert extra_marker in list(view.children)

    async def test_indicator_label_updates_on_page_turn(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
                [Container(TextDisplay("C"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert view._indicator_btn.label == "Page 1/3"

        view.current_page = 2
        await view._update_page()

        assert view._indicator_btn.label == "Page 3/3"


# // ========================================( Single-page nav suppression )======================================== // #


class TestPaginatedLayoutSinglePage:
    """Single-page views must not render the nav ActionRow."""

    def test_single_page_has_no_nav_row(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("Only"))]],
        )
        assert view._nav_row not in list(view.children)

    def test_empty_pages_has_no_nav_row(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[],
        )
        assert view._nav_row not in list(view.children)

    def test_multi_page_has_nav_row(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
            ],
        )
        assert view._nav_row in list(view.children)

    async def test_rebuild_growing_past_one_page_attaches_nav(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("Only"))]],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert view._nav_row not in list(view.children)

        view.pages = [
            [Container(TextDisplay("A"))],
            [Container(TextDisplay("B"))],
        ]
        await view._update_page()

        assert view._nav_row in list(view.children)

    async def test_rebuild_shrinking_to_one_page_detaches_nav(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert view._nav_row in list(view.children)

        view.pages = [[Container(TextDisplay("Only"))]]
        view.current_page = 0
        await view._update_page()

        assert view._nav_row not in list(view.children)
