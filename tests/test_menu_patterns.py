"""Tests for MenuView / MenuLayoutView pattern."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ui import Container, TextDisplay

from cascadeui.views.patterns import MenuLayoutView, MenuView
from helpers import make_interaction as _make_interaction


class _DummySubView(MenuView):
    """Minimal target view for push testing."""

    pass


class _DummySubLayoutView(MenuLayoutView):
    """Minimal target view for V2 push testing."""

    pass


# // ========================================( V1: MenuView )======================================== // #


class TestMenuViewInit:
    """V1 MenuView initialization, validation, and category storage."""
    def test_creates_buttons_from_categories(self):
        categories = [
            {"label": "Alpha", "view": _DummySubView},
            {"label": "Beta", "view": _DummySubView, "emoji": "\N{BELL}"},
        ]
        view = MenuView(interaction=_make_interaction(), categories=categories)

        assert len(view._category_buttons) == 2
        assert view._category_buttons[0].label == "Alpha"
        assert view._category_buttons[1].label == "Beta"
        assert view._category_buttons[1].emoji.name == "\N{BELL}"

    def test_empty_categories_produces_no_buttons(self):
        view = MenuView(interaction=_make_interaction(), categories=[])

        assert view._category_buttons == []

    def test_auto_exit_button_added_by_default(self):
        view = MenuView(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubView}],
        )
        # Exit button is the last child
        labels = [c.label for c in view.children if hasattr(c, "label")]
        assert "Exit" in labels

    def test_auto_exit_button_disabled(self):
        class NoExit(MenuView):
            auto_exit_button = False

        view = NoExit(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubView}],
        )
        labels = [c.label for c in view.children if hasattr(c, "label")]
        assert "Exit" not in labels

    def test_categories_property(self):
        cats = [{"label": "X", "view": _DummySubView}]
        view = MenuView(interaction=_make_interaction(), categories=cats)
        assert view.categories is cats


class TestMenuViewStyles:
    """V1 MenuView button style validation and customization."""
    def test_default_style_applied(self):
        view = MenuView(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubView}],
        )
        assert view._category_buttons[0].style is discord.ButtonStyle.primary

    def test_custom_menu_style(self):
        class DangerMenu(MenuView):
            menu_style = discord.ButtonStyle.danger

        view = DangerMenu(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubView}],
        )
        assert view._category_buttons[0].style is discord.ButtonStyle.danger

    def test_per_category_style_override(self):
        view = MenuView(
            interaction=_make_interaction(),
            categories=[
                {"label": "A", "view": _DummySubView, "style": discord.ButtonStyle.success},
            ],
        )
        assert view._category_buttons[0].style is discord.ButtonStyle.success

    def test_invalid_menu_style_raises(self):
        with pytest.raises(ValueError, match="must be a discord.ButtonStyle"):

            class BadMenu(MenuView):
                menu_style = "primary"


class TestMenuViewHooks:
    """V1 MenuView hook overrides fire on category selection."""
    async def test_on_category_selected_fires_before_push(self):
        hook_calls = []

        class TrackedMenu(MenuView):
            async def on_category_selected(self, category, index, interaction):
                hook_calls.append((category["label"], index))

        view = TrackedMenu(
            interaction=_make_interaction(),
            categories=[{"label": "Alpha", "view": _DummySubView}],
        )

        callback = view._category_buttons[0].callback
        with patch.object(view, "push", new_callable=AsyncMock):
            await callback(_make_interaction())

        assert hook_calls == [("Alpha", 0)]

    async def test_build_extra_items_hook(self):
        class ExtraMenu(MenuView):
            def _build_extra_items(self):
                self.add_item(
                    discord.ui.Button(label="Extra", style=discord.ButtonStyle.secondary, row=3)
                )

        view = ExtraMenu(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubView}],
        )
        labels = [c.label for c in view.children if hasattr(c, "label")]
        assert "Extra" in labels

    async def test_build_category_button_override(self):
        class CustomButton(MenuView):
            def _build_category_button(self, category, index):
                from cascadeui import StatefulButton

                return StatefulButton(
                    label=f"Custom: {category['label']}",
                    style=discord.ButtonStyle.success,
                    row=index // 5,
                    callback=self._make_push_callback(category, index),
                )

        view = CustomButton(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubView}],
        )
        assert view._category_buttons[0].label == "Custom: A"
        assert view._category_buttons[0].style is discord.ButtonStyle.success


class TestMenuViewBuildUi:
    """V1 MenuView embed builder and extra-items hook."""
    def test_build_ui_rebuilds_categories(self):
        view = MenuView(
            interaction=_make_interaction(),
            categories=[
                {"label": "A", "view": _DummySubView},
                {"label": "B", "view": _DummySubView},
            ],
        )
        # Simulate a rebuild
        result = view.build_ui()

        assert len(view._category_buttons) == 2
        assert "embed" in result

    def test_build_embed_default(self):
        view = MenuView(interaction=_make_interaction(), categories=[])
        embed = view.build_embed()
        assert embed.title == "Menu"

    def test_build_embed_override(self):
        class CustomEmbed(MenuView):
            def build_embed(self):
                return discord.Embed(title="My Settings")

        view = CustomEmbed(interaction=_make_interaction(), categories=[])
        embed = view.build_embed()
        assert embed.title == "My Settings"


class TestMenuViewPush:
    """V1 MenuView category push callback wiring."""
    async def test_push_callback_calls_push_with_view_class(self):
        view = MenuView(
            interaction=_make_interaction(),
            categories=[{"label": "Target", "view": _DummySubView}],
        )

        callback = view._category_buttons[0].callback
        with patch.object(view, "push", new_callable=AsyncMock) as mock_push:
            interaction = _make_interaction()
            await callback(interaction)

            mock_push.assert_called_once()
            args, kwargs = mock_push.call_args
            assert args[0] is _DummySubView
            assert args[1] is interaction
            assert "rebuild" in kwargs

    async def test_custom_rebuild_passed_through(self):
        custom_rebuild = MagicMock()
        view = MenuView(
            interaction=_make_interaction(),
            categories=[
                {"label": "A", "view": _DummySubView, "rebuild": custom_rebuild},
            ],
        )

        callback = view._category_buttons[0].callback
        with patch.object(view, "push", new_callable=AsyncMock) as mock_push:
            await callback(_make_interaction())

            _, kwargs = mock_push.call_args
            assert kwargs["rebuild"] is custom_rebuild


# // ========================================( V2: MenuLayoutView )======================================== // #


class TestMenuLayoutViewInit:
    """V2 MenuLayoutView initialization, validation, and category storage."""
    def test_creates_action_sections_from_categories(self):
        categories = [
            {
                "label": "Alpha",
                "description": "First category",
                "view": _DummySubLayoutView,
            },
            {
                "label": "Beta",
                "emoji": "\N{BELL}",
                "description": "Second category",
                "view": _DummySubLayoutView,
            },
        ]
        view = MenuLayoutView(interaction=_make_interaction(), categories=categories)

        # Should have header (empty) + 2 category items + exit button ActionRow
        assert len(view.children) >= 2

    def test_empty_categories_still_renders(self):
        view = MenuLayoutView(interaction=_make_interaction(), categories=[])
        # Just the exit button ActionRow
        assert len(view.children) >= 1

    def test_auto_exit_button_added(self):
        view = MenuLayoutView(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubLayoutView}],
        )
        # Walk all children to find the exit button
        found_exit = False
        for child in view.walk_children():
            if hasattr(child, "label") and child.label == "Exit":
                found_exit = True
                break
        assert found_exit

    def test_auto_exit_button_disabled(self):
        class NoExit(MenuLayoutView):
            auto_exit_button = False

        view = NoExit(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubLayoutView}],
        )
        found_exit = False
        for child in view.walk_children():
            if hasattr(child, "label") and child.label == "Exit":
                found_exit = True
                break
        assert not found_exit

    def test_categories_property(self):
        cats = [{"label": "X", "view": _DummySubLayoutView}]
        view = MenuLayoutView(interaction=_make_interaction(), categories=cats)
        assert view.categories is cats


class TestMenuLayoutViewStyles:
    """V2 MenuLayoutView button style validation and customization."""
    def test_default_style(self):
        assert MenuLayoutView.menu_style is discord.ButtonStyle.primary

    def test_custom_menu_style(self):
        class DangerMenu(MenuLayoutView):
            menu_style = discord.ButtonStyle.danger

        assert DangerMenu.menu_style is discord.ButtonStyle.danger

    def test_invalid_menu_style_raises(self):
        with pytest.raises(ValueError, match="must be a discord.ButtonStyle"):

            class BadMenu(MenuLayoutView):
                menu_style = "primary"


class TestMenuLayoutViewHooks:
    """V2 MenuLayoutView hook overrides fire on category selection."""
    def test_build_header_default_empty(self):
        view = MenuLayoutView(interaction=_make_interaction(), categories=[])
        assert view._build_header() == []

    def test_build_footer_default_empty(self):
        view = MenuLayoutView(interaction=_make_interaction(), categories=[])
        assert view._build_footer() == []

    def test_build_header_override(self):
        class HeaderMenu(MenuLayoutView):
            def _build_header(self):
                return [TextDisplay("Header")]

        view = HeaderMenu(interaction=_make_interaction(), categories=[])
        header = view._build_header()
        assert len(header) == 1
        assert isinstance(header[0], TextDisplay)

    def test_build_footer_override(self):
        class FooterMenu(MenuLayoutView):
            def _build_footer(self):
                return TextDisplay("Footer note")

        view = FooterMenu(interaction=_make_interaction(), categories=[])
        footer = view._build_footer()
        assert isinstance(footer, TextDisplay)

    async def test_on_category_selected_fires(self):
        hook_calls = []

        class TrackedMenu(MenuLayoutView):
            async def on_category_selected(self, category, index, interaction):
                hook_calls.append((category["label"], index))

        view = TrackedMenu(
            interaction=_make_interaction(),
            categories=[{"label": "Alpha", "view": _DummySubLayoutView}],
        )

        # Find the callback in the action_section's button
        callback = None
        for child in view.walk_children():
            if hasattr(child, "callback") and child.callback is not None:
                if hasattr(child, "label") and child.label == "Alpha":
                    callback = child.callback
                    break

        assert callback is not None, "Category button not found in component tree"

        with patch.object(view, "push", new_callable=AsyncMock):
            await callback(_make_interaction())

        assert hook_calls == [("Alpha", 0)]


class TestMenuLayoutViewBuildUi:
    """V2 MenuLayoutView card builder and header/footer hooks."""
    def test_build_ui_rebuilds_full_tree(self):
        view = MenuLayoutView(
            interaction=_make_interaction(),
            categories=[
                {"label": "A", "view": _DummySubLayoutView, "description": "First"},
                {"label": "B", "view": _DummySubLayoutView, "description": "Second"},
            ],
        )
        initial_count = len(view.children)

        # Rebuild
        view.build_ui()
        assert len(view.children) == initial_count

    def test_header_renders_above_categories(self):
        class OrderedMenu(MenuLayoutView):
            auto_exit_button = False

            def _build_header(self):
                return [TextDisplay("HEADER")]

        view = OrderedMenu(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubLayoutView}],
        )
        # First child should be the header TextDisplay
        assert isinstance(view.children[0], TextDisplay)
        assert view.children[0].content == "HEADER"

    def test_footer_renders_below_categories(self):
        class OrderedMenu(MenuLayoutView):
            auto_exit_button = False

            def _build_footer(self):
                return [TextDisplay("FOOTER")]

        view = OrderedMenu(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubLayoutView}],
        )
        # Last child should be the footer TextDisplay
        assert isinstance(view.children[-1], TextDisplay)
        assert view.children[-1].content == "FOOTER"


class TestMenuLayoutViewPush:
    """V2 MenuLayoutView category push callback wiring."""
    async def test_push_callback_calls_push(self):
        view = MenuLayoutView(
            interaction=_make_interaction(),
            categories=[{"label": "Target", "view": _DummySubLayoutView}],
        )

        # Find the category button
        callback = None
        for child in view.walk_children():
            if hasattr(child, "callback") and child.callback is not None:
                if hasattr(child, "label") and child.label == "Target":
                    callback = child.callback
                    break

        assert callback is not None

        with patch.object(view, "push", new_callable=AsyncMock) as mock_push:
            interaction = _make_interaction()
            await callback(interaction)

            mock_push.assert_called_once()
            args, kwargs = mock_push.call_args
            assert args[0] is _DummySubLayoutView
            assert args[1] is interaction
            assert "rebuild" in kwargs

    async def test_custom_rebuild_per_category(self):
        custom_rebuild = MagicMock()
        view = MenuLayoutView(
            interaction=_make_interaction(),
            categories=[
                {"label": "A", "view": _DummySubLayoutView, "rebuild": custom_rebuild},
            ],
        )

        callback = None
        for child in view.walk_children():
            if hasattr(child, "callback") and child.callback is not None:
                if hasattr(child, "label") and child.label == "A":
                    callback = child.callback
                    break

        with patch.object(view, "push", new_callable=AsyncMock) as mock_push:
            await callback(_make_interaction())

            _, kwargs = mock_push.call_args
            assert kwargs["rebuild"] is custom_rebuild
