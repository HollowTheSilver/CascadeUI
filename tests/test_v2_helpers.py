# // ========================================( Modules )======================================== // #


import discord
import pytest
from discord.components import MediaGalleryItem
from discord.ui import Container, MediaGallery, Section, Separator, TextDisplay, Thumbnail

from cascadeui import (
    StatefulButton,
    action_section,
    alert,
    card,
    divider,
    gallery,
    gap,
    image_section,
    key_value,
    toggle_section,
)

# // ========================================( Card )======================================== // #


class TestCard:
    def test_returns_container(self):
        result = card("Title")
        assert isinstance(result, Container)

    def test_title_used_as_is(self):
        result = card("## Server Info")
        children = result.children
        assert isinstance(children[0], TextDisplay)
        assert children[0].content == "## Server Info"

    def test_title_accepts_any_heading_level(self):
        for prefix in ("# ", "## ", "### "):
            result = card(f"{prefix}Title")
            assert result.children[0].content == f"{prefix}Title"

    def test_children_included(self):
        sep = Separator()
        text = TextDisplay("body")
        result = card("## Title", sep, text)
        children = result.children
        assert len(children) == 3  # title + sep + text
        assert children[1] is sep
        assert children[2] is text

    def test_accent_colour(self):
        result = card("Title", color=discord.Color.green())
        assert result.accent_colour == discord.Color.green()

    def test_no_colour(self):
        result = card("Title")
        assert result.accent_colour is None

    def test_spoiler(self):
        result = card("Title", spoiler=True)
        assert result.spoiler is True


# // ========================================( Action Section )======================================== // #


class TestActionSection:
    def _noop(self, interaction):
        pass

    def test_returns_section(self):
        result = action_section("text", label="Click", callback=self._noop)
        assert isinstance(result, Section)

    def test_text_display(self):
        result = action_section("Some text", label="Click", callback=self._noop)
        assert isinstance(result.children[0], TextDisplay)
        assert result.children[0].content == "Some text"

    def test_accessory_is_stateful_button(self):
        result = action_section("text", label="Go", callback=self._noop)
        assert isinstance(result.accessory, StatefulButton)
        assert result.accessory.label == "Go"

    def test_custom_style(self):
        result = action_section(
            "text",
            label="Go",
            callback=self._noop,
            style=discord.ButtonStyle.primary,
        )
        assert result.accessory.style == discord.ButtonStyle.primary

    def test_default_style_is_secondary(self):
        result = action_section("text", label="Go", callback=self._noop)
        assert result.accessory.style == discord.ButtonStyle.secondary

    def test_emoji(self):
        result = action_section("text", label="Go", callback=self._noop, emoji="\U0001f504")
        assert result.accessory.emoji is not None


# // ========================================( Toggle Section )======================================== // #


class TestToggleSection:
    def _noop(self, interaction):
        pass

    def test_active_renders_success(self):
        result = toggle_section("Module", active=True, callback=self._noop)
        assert result.accessory.style == discord.ButtonStyle.success
        assert result.accessory.label == "Enabled"

    def test_inactive_renders_danger(self):
        result = toggle_section("Module", active=False, callback=self._noop)
        assert result.accessory.style == discord.ButtonStyle.danger
        assert result.accessory.label == "Disabled"

    def test_custom_labels(self):
        result = toggle_section(
            "Module",
            active=True,
            callback=self._noop,
            labels=("On", "Off"),
        )
        assert result.accessory.label == "On"

        result2 = toggle_section(
            "Module",
            active=False,
            callback=self._noop,
            labels=("On", "Off"),
        )
        assert result2.accessory.label == "Off"

    def test_text_preserved(self):
        result = toggle_section("\u2705 **Moderation**", active=True, callback=self._noop)
        assert result.children[0].content == "\u2705 **Moderation**"


# // ========================================( Image Section )======================================== // #


class TestImageSection:
    def test_returns_section_with_thumbnail(self):
        result = image_section("text", url="https://example.com/img.png")
        assert isinstance(result, Section)
        assert isinstance(result.accessory, Thumbnail)

    def test_description(self):
        result = image_section("text", url="https://example.com/img.png", description="alt text")
        assert result.accessory.description == "alt text"

    def test_spoiler(self):
        result = image_section("text", url="https://example.com/img.png", spoiler=True)
        assert result.accessory.spoiler is True


# // ========================================( Key Value )======================================== // #


class TestKeyValue:
    def test_returns_text_display(self):
        result = key_value({"A": 1})
        assert isinstance(result, TextDisplay)

    def test_formatting(self):
        result = key_value({"Members": 42, "Roles": 5})
        assert result.content == "**Members:** 42\n**Roles:** 5"

    def test_empty_dict(self):
        result = key_value({})
        assert result.content == ""

    def test_non_string_values(self):
        result = key_value({"Count": 42, "Active": True})
        assert "**Count:** 42" in result.content
        assert "**Active:** True" in result.content


# // ========================================( Alert )======================================== // #


class TestAlert:
    def test_returns_container(self):
        result = alert("message")
        assert isinstance(result, Container)

    def test_info_default(self):
        result = alert("test")
        assert result.accent_colour == discord.Color.blurple()
        assert "\u2139\ufe0f" in result.children[0].content

    def test_success(self):
        result = alert("saved", level="success")
        assert result.accent_colour == discord.Color.green()
        assert "\u2705" in result.children[0].content

    def test_warning(self):
        result = alert("careful", level="warning")
        assert result.accent_colour == discord.Color.gold()

    def test_error(self):
        result = alert("failed", level="error")
        assert result.accent_colour == discord.Color.red()
        assert "\u274c" in result.children[0].content

    def test_message_included(self):
        result = alert("Something happened")
        assert "Something happened" in result.children[0].content

    def test_invalid_level_raises(self):
        with pytest.raises(ValueError, match="Unknown alert level"):
            alert("msg", level="critical")


# // ========================================( Separators )======================================== // #


class TestSeparators:
    def test_divider_is_visible(self):
        result = divider()
        assert isinstance(result, Separator)
        assert result.visible is True

    def test_divider_large(self):
        from discord.enums import SeparatorSpacing

        result = divider(large=True)
        assert result.spacing == SeparatorSpacing.large

    def test_divider_default_small(self):
        from discord.enums import SeparatorSpacing

        result = divider()
        assert result.spacing == SeparatorSpacing.small

    def test_gap_is_invisible(self):
        result = gap()
        assert isinstance(result, Separator)
        assert result.visible is False

    def test_gap_large(self):
        from discord.enums import SeparatorSpacing

        result = gap(large=True)
        assert result.spacing == SeparatorSpacing.large

    def test_gap_default_small(self):
        from discord.enums import SeparatorSpacing

        result = gap()
        assert result.spacing == SeparatorSpacing.small


# // ========================================( Gallery )======================================== // #


class TestGallery:
    def test_returns_media_gallery(self):
        result = gallery("https://example.com/a.png")
        assert isinstance(result, MediaGallery)

    def test_multiple_urls(self):
        result = gallery(
            "https://example.com/a.png",
            "https://example.com/b.png",
            "https://example.com/c.png",
        )
        assert len(result.items) == 3

    def test_descriptions(self):
        result = gallery(
            "https://example.com/a.png",
            "https://example.com/b.png",
            descriptions=["First", None],
        )
        assert result.items[0].description == "First"
        assert result.items[1].description is None

    def test_partial_descriptions(self):
        result = gallery(
            "https://example.com/a.png",
            "https://example.com/b.png",
            descriptions=["Only first"],
        )
        assert result.items[0].description == "Only first"
        assert result.items[1].description is None
