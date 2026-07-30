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


class TestThemeResolutionSeams:
    """theme_context() provider seams + the late-resolution backstop."""

    def test_theme_context_sets_and_resets(self):
        from cascadeui.theming.context import theme_context

        red = Theme("red", {"accent_colour": discord.Color.red()})
        assert get_current_theme() is None
        with theme_context(red):
            assert get_current_theme() is red
        assert get_current_theme() is None

    def test_theme_context_resets_on_exception(self):
        from cascadeui.theming.context import theme_context

        red = Theme("red")
        with pytest.raises(RuntimeError):
            with theme_context(red):
                raise RuntimeError("boom")
        assert get_current_theme() is None

    async def test_on_load_runs_inside_theme_context(self):
        """on_load builds get the ambient theme, matching build_ui."""
        from cascadeui.components.patterns.v2 import card

        red = Theme("red", {"accent_colour": discord.Color.red()})
        captured = {}

        class PreloadView(StatefulLayoutView):
            theme = red

            async def on_load(self):
                captured["container"] = card("test")

        view = PreloadView(user_id=1)
        await view.on_load()
        assert captured["container"].accent_colour == discord.Color.red()

    def test_backstop_resolves_marked_container_built_anywhere(self):
        """A card() built with no context and no color renders themed once
        the view's resolution seam runs -- the tree's build site no longer
        matters.
        """
        from cascadeui.components.patterns.v2 import card

        orphan = card("built at module level")
        assert orphan.accent_colour is None

        red = Theme("red", {"accent_colour": discord.Color.red()})

        class Host(StatefulLayoutView):
            theme = red

        view = Host(user_id=1)
        view.add_item(orphan)
        view._apply_theme_defaults()
        assert orphan.accent_color == discord.Color.red()

    def test_backstop_never_touches_explicit_color(self):
        from cascadeui.components.patterns.v2 import card

        pinned = card("explicit", color=discord.Color.green())

        red = Theme("red", {"accent_colour": discord.Color.red()})

        class Host(StatefulLayoutView):
            theme = red

        view = Host(user_id=1)
        view.add_item(pinned)
        view._apply_theme_defaults()
        assert pinned.accent_color == discord.Color.green()

    def test_backstop_follows_runtime_theme_switch(self):
        """Marked containers re-resolve against the live theme, so a theme
        change lands on the next resolution pass instead of staying baked.
        """
        from cascadeui.components.patterns.v2 import card

        red = Theme("red", {"accent_colour": discord.Color.red()})
        blue = Theme("blue", {"accent_colour": discord.Color.blue()})

        class Host(StatefulLayoutView):
            theme = red

        view = Host(user_id=1)
        managed = card("managed")
        view.add_item(managed)
        view._apply_theme_defaults()
        assert managed.accent_color == discord.Color.red()

        view.theme = blue
        view._apply_theme_defaults()
        assert managed.accent_color == discord.Color.blue()

    def test_divider_and_gap_follow_theme_spacing(self):
        from discord import SeparatorSpacing

        from cascadeui.components.patterns.v2 import divider, gap
        from cascadeui.theming.context import theme_context

        roomy = Theme("roomy", {"separator_spacing": "large"})
        with theme_context(roomy):
            assert divider().spacing == SeparatorSpacing.large
            assert gap().spacing == SeparatorSpacing.large
            # Explicit values win over the theme.
            assert divider(large=False).spacing == SeparatorSpacing.small
        # Outside a theme context the default stays small.
        assert divider().spacing == SeparatorSpacing.small


# // ========================================( Selector theme key )======================================== // #


class TestSelectorCarriesTheme:
    """A view's theme rides its state selector, so a theme change is never
    filtered out by a selector that only tracks the view's own content."""

    @staticmethod
    def _view(selector_value, theme_name):
        """A view whose selector ignores the theme and whose theme is dynamic.

        Mirrors the shape that produced the bug: a settings sub-page tracking
        only its own toggles, painting itself with a theme resolved from state.
        """
        register_theme(Theme("dusk", {"accent_colour": discord.Color.purple()}))
        register_theme(Theme("dawn", {"accent_colour": discord.Color.gold()}))

        class _V(StatefulView):
            def state_selector(self, state):
                return selector_value

            def get_theme(self):
                return get_theme(theme_name[0]) or super().get_theme()

        return _V()

    def test_theme_key_reflects_the_resolved_theme(self):
        name = ["dusk"]
        view = self._view("static", name)
        assert view._theme_key() == "dusk"

        name[0] = "dawn"
        assert view._theme_key() == "dawn"

    def test_selector_value_changes_when_only_the_theme_changes(self):
        """The content half is constant; the theme half carries the change."""
        name = ["dusk"]
        view = self._view("static", name)
        selector = view._build_selector()

        before = selector({})
        name[0] = "dawn"
        after = selector({})

        assert before != after
        assert before[0] == after[0]  # content half unchanged
        assert (before[1], after[1]) == ("dusk", "dawn")

    def test_fixed_theme_view_selector_is_stable(self):
        """A view with no dynamic theme selects the same value as before."""

        class _Fixed(StatefulView):
            def state_selector(self, state):
                return "static"

        view = _Fixed()
        selector = view._build_selector()
        assert selector({}) == selector({})

    def test_no_selector_override_still_returns_none(self):
        """Views without a selector stay unfiltered; the theme adds no gate."""
        assert StatefulView()._build_selector() is None

    def test_raising_get_theme_degrades_to_none(self):
        """A broken override must not poison the comparison."""

        class _Broken(StatefulView):
            def state_selector(self, state):
                return "static"

            def get_theme(self):
                raise RuntimeError("boom")

        assert _Broken()._theme_key() is None


class TestThemeDoesNotAdoptTheCallersMapping:
    """``Theme`` copies the styles it is given before seeding defaults.

    ``normalize_mapping`` returns a mapping untouched by contract, which
    suits its read-only callers and not the one caller that writes into
    the result. Holding the caller's own dict made construction add six
    keys to it as a side effect, and made two Themes built from one base
    share a single live mapping.
    """

    def test_construction_leaves_the_caller_dict_alone(self):
        base = {"primary_color": discord.Color.blue()}

        Theme("custom", base)

        assert list(base) == ["primary_color"]

    def test_two_themes_from_one_base_do_not_share_styles(self):
        base = {"primary_color": discord.Color.blue()}

        first = Theme("first", base)
        second = Theme("second", base)
        first.styles["accent_colour"] = discord.Color.red()

        assert first.styles is not second.styles
        assert second.styles["accent_colour"] != discord.Color.red()

    def test_pair_sequence_still_converts(self):
        theme = Theme("pairs", [("primary_color", discord.Color.gold())])

        assert theme.get_style("primary_color") == discord.Color.gold()
        assert theme.get_style("separator_spacing") == "small"
