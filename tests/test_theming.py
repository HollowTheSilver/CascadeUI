"""Tests for the theming system."""

import pytest
import discord

from cascadeui.theming.core import (
    Theme,
    register_theme,
    get_theme,
    set_default_theme,
    get_default_theme,
)


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
