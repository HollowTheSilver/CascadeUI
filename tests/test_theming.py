"""Tests for the theming system."""

import discord
import pytest

from cascadeui.theming.context import get_current_theme
from cascadeui.theming.core import (
    Theme,
    get_default_theme,
    get_theme,
    register_theme,
    set_default_theme,
)
from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.view import StatefulView


@pytest.fixture(autouse=True)
def reset_themes():
    """Reset the global theme registry between tests."""
    from cascadeui.theming import core

    core._themes.clear()
    core._default_theme = None
    yield
    core._themes.clear()
    core._default_theme = None


class TestTheme:
    """Theme stores styles, applies them to embeds, and falls back for missing keys."""

    def test_theme_has_default_colors(self):
        t = Theme("test")
        assert t.get_style("primary_color") is not None
        assert t.get_style("success_color") is not None

    def test_theme_custom_styles_override_defaults(self):
        t = Theme("custom", {"primary_color": discord.Color.purple()})
        assert t.get_style("primary_color") == discord.Color.purple()

    def test_apply_to_embed_sets_color(self):
        t = Theme("test", {"primary_color": discord.Color.red()})
        embed = discord.Embed(title="Hello")
        t.apply_to_embed(embed)
        assert embed.color == discord.Color.red()

    def test_get_style_returns_default_for_missing_key(self):
        t = Theme("test")
        assert t.get_style("nonexistent", "fallback") == "fallback"


class TestThemeRegistry:
    """Global theme registry: register, retrieve, and set default theme."""

    def test_register_and_retrieve(self):
        t = Theme("my_theme")
        register_theme(t)
        assert get_theme("my_theme") is t

    def test_get_unknown_theme_returns_none(self):
        assert get_theme("unknown") is None

    def test_set_default_theme(self):
        t = Theme("default")
        register_theme(t)
        assert set_default_theme("default") is True
        assert get_default_theme() is t

    def test_set_default_unknown_returns_false(self):
        assert set_default_theme("nonexistent") is False
        assert get_default_theme() is None


class TestThemeContext:
    """Theme context propagation via build_ui wrapping."""

    def test_sync_build_ui_sets_context(self):
        """build_ui can read the theme context during execution."""
        red = Theme("red", {"accent_colour": discord.Color.red()})
        captured = {}

        class MyView(StatefulLayoutView):
            theme = red

            def build_ui(self):
                captured["theme"] = get_current_theme()

        view = MyView(user_id=1)
        view.build_ui()
        assert captured["theme"] is red

    def test_context_resets_after_build_ui(self):
        """Theme context is None outside build_ui execution."""
        red = Theme("red", {"accent_colour": discord.Color.red()})

        class MyView(StatefulLayoutView):
            theme = red

            def build_ui(self):
                pass

        view = MyView(user_id=1)
        view.build_ui()
        assert get_current_theme() is None

    async def test_async_build_ui_sets_context(self):
        """Async build_ui can read the theme context during execution."""
        red = Theme("red", {"accent_colour": discord.Color.red()})
        captured = {}

        class MyView(StatefulLayoutView):
            theme = red

            async def build_ui(self):
                captured["theme"] = get_current_theme()

        view = MyView(user_id=1)
        await view.build_ui()
        assert captured["theme"] is red

    async def test_async_context_resets_after_build_ui(self):
        """Theme context resets after async build_ui completes."""
        red = Theme("red", {"accent_colour": discord.Color.red()})

        class MyView(StatefulLayoutView):
            theme = red

            async def build_ui(self):
                pass

        view = MyView(user_id=1)
        await view.build_ui()
        assert get_current_theme() is None

    def test_context_none_outside_build_ui(self):
        """get_current_theme returns None when no view is building."""
        assert get_current_theme() is None


class TestClassLevelTheme:
    """Class-level theme attribute configuration."""

    def test_class_theme_preserved_without_kwarg(self):
        """Class-level theme attribute is not overridden by missing kwarg."""
        red = Theme("red", {"accent_colour": discord.Color.red()})

        class MyView(StatefulLayoutView):
            theme = red

        view = MyView(user_id=1)
        assert view.theme is red

    def test_kwarg_theme_overrides_class_level(self):
        """Explicit theme= kwarg takes precedence over class attribute."""
        red = Theme("red", {"accent_colour": discord.Color.red()})
        blue = Theme("blue", {"accent_colour": discord.Color.blue()})

        class MyView(StatefulLayoutView):
            theme = red

        view = MyView(user_id=1, theme=blue)
        assert view.theme is blue

    def test_no_class_no_kwarg_gives_none(self):
        """Views with no class-level theme and no kwarg get theme=None."""

        class MyView(StatefulLayoutView):
            pass

        view = MyView(user_id=1)
        assert view.theme is None

    def test_get_theme_falls_back_to_default(self):
        """get_theme() returns default theme when no per-view theme."""
        default = Theme("default")
        register_theme(default)
        set_default_theme("default")

        class MyView(StatefulLayoutView):
            pass

        view = MyView(user_id=1)
        assert view.get_theme() is default


class TestThemeValidation:
    """__init_subclass__ validation of theme attribute."""

    def test_string_theme_raises_type_error(self):
        with pytest.raises(TypeError, match="must be a Theme instance"):

            class BadView(StatefulLayoutView):
                theme = "dark"

    def test_int_theme_raises_type_error(self):
        with pytest.raises(TypeError, match="must be a Theme instance"):

            class BadView(StatefulLayoutView):
                theme = 42

    def test_none_theme_allowed(self):
        class GoodView(StatefulLayoutView):
            theme = None

    def test_theme_instance_allowed(self):
        t = Theme("valid")

        class GoodView(StatefulLayoutView):
            theme = t


class TestBuilderThemeFallback:
    """card() and stats_card() read theme context as color fallback."""

    def test_card_uses_theme_accent_when_no_color(self):
        """card() picks up accent_colour from theme context."""
        from cascadeui.components.patterns.v2 import card
        from cascadeui.theming.context import _current_theme, set_current_theme

        red = Theme("red", {"accent_colour": discord.Color.red()})
        token = set_current_theme(red)
        try:
            c = card("test")
            assert c.accent_colour == discord.Color.red()
        finally:
            _current_theme.reset(token)

    def test_card_explicit_color_overrides_theme(self):
        """Explicit color= on card() takes precedence over theme context."""
        from cascadeui.components.patterns.v2 import card
        from cascadeui.theming.context import _current_theme, set_current_theme

        red = Theme("red", {"accent_colour": discord.Color.red()})
        token = set_current_theme(red)
        try:
            c = card("test", color=discord.Color.green())
            assert c.accent_colour == discord.Color.green()
        finally:
            _current_theme.reset(token)

    def test_card_no_theme_no_color_gives_none(self):
        """card() with no theme context and no color= gives None accent."""
        from cascadeui.components.patterns.v2 import card

        c = card("test")
        assert c.accent_colour is None

    def test_stats_card_uses_theme_accent(self):
        """stats_card() picks up accent_colour from theme context."""
        from cascadeui.components.patterns.v2 import stats_card
        from cascadeui.theming.context import _current_theme, set_current_theme

        red = Theme("red", {"accent_colour": discord.Color.red()})
        token = set_current_theme(red)
        try:
            c = stats_card("Title", {"A": 1})
            assert c.accent_colour == discord.Color.red()
        finally:
            _current_theme.reset(token)

    def test_build_ui_propagates_to_card(self):
        """card() called inside build_ui() automatically gets theme color."""
        from cascadeui.components.patterns.v2 import card

        red = Theme("red", {"accent_colour": discord.Color.red()})
        captured = {}

        class MyView(StatefulLayoutView):
            theme = red

            def build_ui(self):
                captured["container"] = card("test")

        view = MyView(user_id=1)
        view.build_ui()
        assert captured["container"].accent_colour == discord.Color.red()
