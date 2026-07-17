"""Tests for MenuView / MenuLayoutView pattern."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ui import Container, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.views import StatefulLayoutView, StatefulView
from cascadeui.views.patterns import FormLayoutView, FormView, MenuLayoutView, MenuView


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
            # _DummySubView subclasses MenuView, so it inherits nav_rebuild
            # and keeps it. Routing for destinations that name none lives in
            # test_destination_naming_no_rebuild_takes_the_menu_default,
            # which this fixture cannot reach.
            assert kwargs["rebuild"] is None

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

    async def test_destination_naming_its_own_rebuild_keeps_it(self):
        """A pattern destination renders its own way; the menu must not overrule it.

        Every V1 pattern names a ``nav_rebuild`` and none of them defines
        ``build_embed``, so a menu that forced its own embed shape onto the
        push raised ``AttributeError`` on the click.
        """
        view = MenuView(
            interaction=_make_interaction(),
            categories=[{"label": "Form", "view": FormView}],
        )

        callback = view._category_buttons[0].callback
        with patch.object(view, "push", new_callable=AsyncMock) as mock_push:
            await callback(_make_interaction())

            _, kwargs = mock_push.call_args
            # None lets _apply_navigation_edit fall back to FormView.nav_rebuild.
            assert kwargs["rebuild"] is None

    async def test_destination_naming_no_rebuild_takes_the_menu_default(self):
        """A plain view names no rebuild, so nothing else would supply its embed."""

        class _PlainPage(StatefulView):
            def build_embed(self):
                return discord.Embed(title="Plain")

        view = MenuView(
            interaction=_make_interaction(),
            categories=[{"label": "Plain", "view": _PlainPage}],
        )

        callback = view._category_buttons[0].callback
        with patch.object(view, "push", new_callable=AsyncMock) as mock_push:
            await callback(_make_interaction())

            _, kwargs = mock_push.call_args
            assert kwargs["rebuild"] is MenuView.nav_rebuild
            assert kwargs["rebuild"](_PlainPage())["embed"].title == "Plain"


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
    """V2 MenuLayoutView build_header/build_footer hooks and the deprecation
    shim for the underscore-prefixed pair."""

    def test_build_header_default_empty(self):
        view = MenuLayoutView(interaction=_make_interaction(), categories=[])
        assert view.build_header() == []

    def test_build_footer_default_empty(self):
        view = MenuLayoutView(interaction=_make_interaction(), categories=[])
        assert view.build_footer() == []

    def test_build_header_override(self):
        class HeaderMenu(MenuLayoutView):
            def build_header(self):
                return [TextDisplay("Header")]

        view = HeaderMenu(interaction=_make_interaction(), categories=[])
        header = view.build_header()
        assert len(header) == 1
        assert isinstance(header[0], TextDisplay)

    def test_build_footer_override(self):
        class FooterMenu(MenuLayoutView):
            def build_footer(self):
                return TextDisplay("Footer note")

        view = FooterMenu(interaction=_make_interaction(), categories=[])
        footer = view.build_footer()
        assert isinstance(footer, TextDisplay)

    def test_legacy_underscore_override_still_renders(self):
        """The deprecated ``_build_header`` / ``_build_footer`` names warn at
        class definition and keep rendering through the public hooks'
        delegation, so pre-rename subclasses survive the migration intact.
        """
        with pytest.warns(DeprecationWarning, match="_build_header"):

            class LegacyMenu(MenuLayoutView):
                auto_exit_button = False

                def _build_header(self):
                    return [TextDisplay("LEGACY HEADER")]

        view = LegacyMenu(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubLayoutView}],
        )
        assert isinstance(view.children[0], TextDisplay)
        assert view.children[0].content == "LEGACY HEADER"

    def test_legacy_underscore_footer_warns_and_renders(self):
        with pytest.warns(DeprecationWarning, match="_build_footer"):

            class LegacyFooterMenu(MenuLayoutView):
                auto_exit_button = False

                def _build_footer(self):
                    return TextDisplay("LEGACY FOOTER")

        view = LegacyFooterMenu(
            interaction=_make_interaction(),
            categories=[{"label": "A", "view": _DummySubLayoutView}],
        )
        assert view.children[-1].content == "LEGACY FOOTER"

    def test_public_names_define_without_warning(self):
        """Overriding the public hooks must not trip the deprecation check."""
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("error", DeprecationWarning)

            class CleanMenu(MenuLayoutView):
                def build_header(self):
                    return [TextDisplay("Clean")]

                def build_footer(self):
                    return [TextDisplay("Clean")]

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

            def build_header(self):
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

            def build_footer(self):
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

    @staticmethod
    def _callback_for(view, label):
        for child in view.walk_children():
            if (
                getattr(child, "callback", None) is not None
                and getattr(child, "label", None) == label
            ):
                return child.callback
        raise AssertionError(f"no category button labelled {label!r}")

    async def test_v2_pattern_destination_gets_no_menu_rebuild(self):
        """A V2 pattern renders through on_load and defines no build_ui.

        The menu's fallback calls build_ui, so forcing it onto such a
        destination raised AttributeError on the click.
        """
        view = MenuLayoutView(
            interaction=_make_interaction(),
            categories=[{"label": "Form", "view": FormLayoutView}],
        )

        with patch.object(view, "push", new_callable=AsyncMock) as mock_push:
            await self._callback_for(view, "Form")(_make_interaction())

            _, kwargs = mock_push.call_args
            assert kwargs["rebuild"] is None

    async def test_plain_v2_destination_takes_the_menu_default(self):
        """A plain V2 view defines build_ui, so the menu's fallback fits it."""

        class _PlainPage(StatefulLayoutView):
            def build_ui(self):
                return None

        view = MenuLayoutView(
            interaction=_make_interaction(),
            categories=[{"label": "Plain", "view": _PlainPage}],
        )

        with patch.object(view, "push", new_callable=AsyncMock) as mock_push:
            await self._callback_for(view, "Plain")(_make_interaction())

            _, kwargs = mock_push.call_args
            assert kwargs["rebuild"] is MenuLayoutView.nav_rebuild

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
            # Inherits nav_rebuild from MenuLayoutView, as the V1 sibling does.
            assert kwargs["rebuild"] is None

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


class TestMenuViewInitialRender:
    """The hub card ships without the caller passing it explicitly."""

    async def test_send_ships_build_embed_by_default(self):
        """build_embed() is the menu's render; send() supplies it.

        Callers previously wrote send(embed=view.build_embed()) at every call
        site. The library owns that now; an explicit embed still wins.
        """
        interaction = _make_interaction()
        view = MenuView(
            interaction=interaction,
            categories=[{"label": "One", "view": _DummySubView}],
        )

        await view.send()

        embed = interaction.response.send_message.call_args.kwargs.get("embed")
        assert embed is not None

    async def test_explicit_embed_wins(self):
        """The pre-existing send(embed=...) call shape is unchanged."""
        interaction = _make_interaction()
        view = MenuView(
            interaction=interaction,
            categories=[{"label": "One", "view": _DummySubView}],
        )

        await view.send(embed=discord.Embed(title="Caller"))

        embed = interaction.response.send_message.call_args.kwargs.get("embed")
        assert embed.title == "Caller"
