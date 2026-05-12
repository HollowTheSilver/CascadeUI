# // ========================================( Modules )======================================== // #


from io import BytesIO

import discord
import pytest
from discord.components import MediaGalleryItem
from discord.ui import (
    ActionRow,
    Container,
    File,
    MediaGallery,
    Section,
    Separator,
    TextDisplay,
    Thumbnail,
)

from cascadeui import (
    StatefulButton,
    action_section,
    alert,
    button_row,
    card,
    confirm_section,
    cycle_button,
    divider,
    file_attachment,
    gallery,
    gap,
    image_section,
    key_value,
    link_section,
    progress_bar,
    stats_card,
    tab_nav,
    toggle_button,
    toggle_section,
)

# // ========================================( Re-Export Symmetry )======================================== // #


class TestComponentReExports:
    """``cascadeui.components`` re-exports the same V2 surface as the
    package root. The two `MediaInput` / `EmojiInput` aliases ship as a
    matched pair; the same applies to `gallery` / `file_attachment`.
    """

    def test_emoji_input_importable_from_components(self):
        from cascadeui.components import EmojiInput  # noqa: F401

    def test_media_input_importable_from_components(self):
        from cascadeui.components import MediaInput  # noqa: F401

    def test_gallery_importable_from_components(self):
        from cascadeui.components import gallery  # noqa: F401

    def test_file_attachment_importable_from_components(self):
        from cascadeui.components import file_attachment  # noqa: F401


# // ========================================( Card )======================================== // #


class TestCard:
    """card() wraps content in a Container with accent color and auto-wrapped strings."""

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
    """action_section() creates a Section with a StatefulButton accessory."""

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
    """toggle_section() creates a Section with a green/red toggle button."""

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
    """image_section() creates a Section with a Thumbnail accessory."""

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

    def test_accepts_discord_file(self):
        """``url=`` accepts a ``discord.File`` and resolves through ``.uri``."""
        photo = discord.File(BytesIO(b"fake bytes"), filename="avatar.png")
        result = image_section("text", url=photo)
        assert isinstance(result.accessory, Thumbnail)
        assert result.accessory.media.url == "attachment://avatar.png"


# // ========================================( Key Value )======================================== // #


class TestKeyValue:
    """key_value() renders a dict as bold-key: value TextDisplay lines."""

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
    """alert() creates a colored Container with status-themed accent."""

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
    """divider() and gap() produce Separator components with correct spacing."""

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
    """gallery() creates a MediaGallery from one or more URLs."""

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

    def test_length_mismatch_raises(self):
        """Descriptions length must match URLs exactly — fail loud like emoji_grid."""
        with pytest.raises(ValueError, match="descriptions length"):
            gallery(
                "https://example.com/a.png",
                "https://example.com/b.png",
                descriptions=["Only first"],
            )

    def test_accepts_discord_file(self):
        """``*media`` accepts a ``discord.File`` and resolves through ``.uri``."""
        photo = discord.File(BytesIO(b"fake bytes"), filename="a.png")
        result = gallery(photo)
        assert len(result.items) == 1
        assert result.items[0].media.url == "attachment://a.png"

    def test_accepts_mixed_string_and_file(self):
        """A mix of URL strings and ``discord.File`` instances coexists in one call."""
        photo = discord.File(BytesIO(b"local bytes"), filename="local.png")
        result = gallery(
            "https://example.com/remote.png",
            photo,
            descriptions=["Remote", "Local"],
        )
        assert result.items[0].media.url == "https://example.com/remote.png"
        assert result.items[1].media.url == "attachment://local.png"
        assert result.items[0].description == "Remote"
        assert result.items[1].description == "Local"

    def test_zero_items_raises(self):
        """Empty gallery() rejected at construction (Discord requires 1-10)."""
        with pytest.raises(ValueError, match="at least one media reference"):
            gallery()

    def test_too_many_items_raises(self):
        """Gallery with 11+ items rejected at construction."""
        urls = [f"https://example.com/img{i}.png" for i in range(11)]
        with pytest.raises(ValueError, match="too many media references"):
            gallery(*urls)


class TestFileAttachment:
    """file_attachment() wraps the V2 File primitive for inline attachment cards."""

    def test_returns_file(self):
        result = file_attachment("attachment://report.pdf")
        assert isinstance(result, File)

    def test_url_passthrough(self):
        result = file_attachment("attachment://report.pdf")
        # File stores media as an UnfurledMediaItem with the original URL.
        assert result.media.url == "attachment://report.pdf"

    def test_remote_url_accepted(self):
        result = file_attachment("https://example.com/report.pdf")
        assert result.media.url == "https://example.com/report.pdf"

    def test_spoiler_default_false(self):
        result = file_attachment("attachment://report.pdf")
        assert result.spoiler is False

    def test_spoiler_flag(self):
        result = file_attachment("attachment://report.pdf", spoiler=True)
        assert result.spoiler is True

    def test_composes_into_card(self):
        """file_attachment integrates with card() the same way gallery does."""
        c = card(
            "## Report",
            file_attachment("attachment://report.pdf"),
            "Released April 15.",
        )
        assert isinstance(c, Container)
        assert len(c.children) == 3
        assert isinstance(c.children[1], File)

    def test_accepts_discord_file(self):
        """``url=`` accepts a ``discord.File`` and resolves through ``.uri``."""
        report = discord.File(BytesIO(b"pdf bytes"), filename="report.pdf")
        result = file_attachment(report)
        assert isinstance(result, File)
        assert result.media.url == "attachment://report.pdf"


# // ========================================( 7.8b — additive helpers )======================================== // #


async def _noop(interaction):
    pass


async def _noop_with_value(interaction, value):
    pass


class TestToggleSectionEmoji:
    """L4 — toggle_section gains emoji kwarg for parity with action_section."""

    def test_emoji_kwarg_passthrough(self):
        result = toggle_section("Lights", active=True, callback=_noop, emoji="\U0001f4a1")
        assert isinstance(result, Section)
        # Emoji lives on the accessory button.
        button = result.accessory
        assert button.emoji is not None


class TestLinkSection:
    """C1 — Section with link-style Button accessory."""

    def test_returns_section(self):
        result = link_section("Docs", label="Open", url="https://example.com")
        assert isinstance(result, Section)

    def test_accessory_is_link_button(self):
        result = link_section("Docs", label="Open", url="https://example.com")
        assert result.accessory.style == discord.ButtonStyle.link
        assert result.accessory.url == "https://example.com"

    def test_emoji_passthrough(self):
        result = link_section("Docs", label="Open", url="https://example.com", emoji="\U0001f4d6")
        assert result.accessory.emoji is not None


class TestConfirmSection:
    """C3 — returns [TextDisplay, ActionRow] for splat-into-card composition."""

    def test_returns_list_of_two(self):
        result = confirm_section("Sure?", on_confirm=_noop, on_cancel=_noop)
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], TextDisplay)
        assert isinstance(result[1], ActionRow)

    def test_confirm_button_is_success(self):
        result = confirm_section("Sure?", on_confirm=_noop, on_cancel=_noop)
        buttons = list(result[1].children)
        assert buttons[0].style == discord.ButtonStyle.success
        assert buttons[1].style == discord.ButtonStyle.danger

    def test_custom_labels(self):
        result = confirm_section(
            "Delete server?",
            on_confirm=_noop,
            on_cancel=_noop,
            confirm_label="Delete",
            cancel_label="Keep",
        )
        buttons = list(result[1].children)
        assert buttons[0].label == "Delete"
        assert buttons[1].label == "Keep"


class TestButtonRow:
    """C2 — dict shorthand for an ActionRow of same-style buttons."""

    def test_returns_action_row(self):
        result = button_row({"Save": _noop, "Cancel": _noop})
        assert isinstance(result, ActionRow)
        assert len(list(result.children)) == 2

    def test_preserves_dict_order(self):
        result = button_row({"A": _noop, "B": _noop, "C": _noop})
        labels = [b.label for b in result.children]
        assert labels == ["A", "B", "C"]

    def test_shared_style(self):
        result = button_row({"Go": _noop, "Stop": _noop}, style=discord.ButtonStyle.primary)
        for b in result.children:
            assert b.style == discord.ButtonStyle.primary

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            button_row({})

    def test_overflow_raises(self):
        with pytest.raises(ValueError, match="5-per-ActionRow"):
            button_row({str(i): _noop for i in range(6)})


class TestCycleButton:
    """C4 — first stateful v2 helper; index tracked on instance."""

    def test_returns_stateful_button(self):
        btn = cycle_button(values=["Low", "Med", "High"], on_change=_noop_with_value)
        assert isinstance(btn, StatefulButton)

    def test_initial_label_matches_start(self):
        btn = cycle_button(values=["Low", "Med", "High"], on_change=_noop_with_value, start=1)
        assert btn.label == "Med"
        assert btn._cycle_index == 1

    def test_custom_labels(self):
        btn = cycle_button(
            values=[1, 2, 3],
            labels=["One", "Two", "Three"],
            on_change=_noop_with_value,
        )
        assert btn.label == "One"

    def test_labels_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="labels length"):
            cycle_button(values=[1, 2, 3], labels=["A", "B"], on_change=_noop_with_value)

    def test_empty_values_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            cycle_button(values=[], on_change=_noop_with_value)

    def test_out_of_range_start_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            cycle_button(values=[1, 2], on_change=_noop_with_value, start=5)


class TestToggleButton:
    """C5 — standalone boolean toggle, distinct from toggle_section."""

    def test_active_initial_state(self):
        btn = toggle_button(active=True, on_toggle=_noop_with_value)
        assert btn._toggle_active is True
        assert btn.style == discord.ButtonStyle.success
        assert btn.label == "Enabled"

    def test_inactive_initial_state(self):
        btn = toggle_button(active=False, on_toggle=_noop_with_value)
        assert btn._toggle_active is False
        assert btn.style == discord.ButtonStyle.danger
        assert btn.label == "Disabled"

    def test_custom_labels(self):
        btn = toggle_button(active=True, on_toggle=_noop_with_value, labels=("Dark", "Light"))
        assert btn.label == "Dark"


class TestStatsCard:
    """C6 — Container composition of heading + divider + key_value."""

    def test_returns_container(self):
        result = stats_card("Stats", {"Members": 5})
        assert isinstance(result, Container)

    def test_auto_heading_prefix(self):
        result = stats_card("Server Info", {"Members": 5})
        # First child should be a TextDisplay containing "## Server Info"
        first = list(result.children)[0]
        assert isinstance(first, TextDisplay)
        assert "## Server Info" in first.content

    def test_pre_formatted_heading_preserved(self):
        result = stats_card("### Small Heading", {"Members": 5})
        first = list(result.children)[0]
        assert first.content == "### Small Heading"

    def test_footer_appended(self):
        result = stats_card("Stats", {"K": 1}, footer="Updated now")
        last = list(result.children)[-1]
        assert isinstance(last, TextDisplay)
        assert "-# Updated now" in last.content

    def test_no_footer_by_default(self):
        result = stats_card("Stats", {"K": 1})
        children = list(result.children)
        # heading, divider, key_value → 3 children
        assert len(children) == 3


class TestProgressBar:
    """C7 — text-based progress bar as TextDisplay."""

    def test_returns_text_display(self):
        result = progress_bar(5, 10)
        assert isinstance(result, TextDisplay)

    def test_percent_default(self):
        result = progress_bar(7, 10, width=10)
        assert "70%" in result.content

    def test_hide_percent(self):
        result = progress_bar(7, 10, show_percent=False)
        assert "%" not in result.content

    def test_clamp_overshoot(self):
        result = progress_bar(15, 10, width=5)
        assert "100%" in result.content

    def test_clamp_undershoot(self):
        result = progress_bar(-5, 10, width=5)
        assert "0%" in result.content

    def test_zero_max_raises(self):
        with pytest.raises(ValueError, match="max_value must be positive"):
            progress_bar(5, 0)

    def test_zero_width_raises(self):
        with pytest.raises(ValueError, match="width must be positive"):
            progress_bar(5, 10, width=0)


class TestTabNav:
    """C8 — ActionRow of tab-styled buttons for manual-control views."""

    def test_returns_action_row(self):
        result = tab_nav({"A": _noop, "B": _noop})
        assert isinstance(result, ActionRow)

    def test_first_tab_active_by_default(self):
        result = tab_nav({"A": _noop, "B": _noop})
        buttons = list(result.children)
        assert buttons[0].style == discord.ButtonStyle.primary
        assert buttons[1].style == discord.ButtonStyle.secondary

    def test_explicit_active(self):
        result = tab_nav({"A": _noop, "B": _noop, "C": _noop}, active="B")
        buttons = list(result.children)
        assert buttons[0].style == discord.ButtonStyle.secondary
        assert buttons[1].style == discord.ButtonStyle.primary
        assert buttons[2].style == discord.ButtonStyle.secondary

    def test_unknown_active_raises(self):
        with pytest.raises(ValueError, match="not a key"):
            tab_nav({"A": _noop}, active="X")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            tab_nav({})

    def test_overflow_raises(self):
        with pytest.raises(ValueError, match="5-per-ActionRow"):
            tab_nav({str(i): _noop for i in range(6)})
