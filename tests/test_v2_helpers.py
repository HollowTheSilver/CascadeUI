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
from helpers import make_interaction

from cascadeui import (
    Choice,
    Collapsible,
    PaginatedRegion,
    StatefulButton,
    StatefulLayoutView,
    StatefulSelect,
    action_section,
    alert,
    button_row,
    card,
    choice_row,
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

    def test_disabled_default_false(self):
        result = action_section("text", label="Go", callback=self._noop)
        assert result.accessory.disabled is False

    def test_disabled_true(self):
        result = action_section("text", label="Go", callback=self._noop, disabled=True)
        assert result.accessory.disabled is True


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

    def test_disabled_default_false(self):
        result = toggle_section("Module", active=True, callback=self._noop)
        assert result.accessory.disabled is False

    def test_disabled_true(self):
        result = toggle_section("Module", active=True, callback=self._noop, disabled=True)
        assert result.accessory.disabled is True


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


# // ========================================( Paginated Region )======================================== // #


class _FakeHost:
    """Minimal stand-in for the StatefulLayoutView a region captures.

    Records build_ui / refresh / defer / respond / open_modal calls so the
    region's callbacks can be exercised without the full view machinery.
    """

    def __init__(self, *, finished=False, async_build=False):
        self.build_calls = 0
        self.refresh_calls = 0
        self._finished = finished
        self._async_build = async_build
        self.deferred = []
        self.responded = []
        self.opened_modal = None

    def is_finished(self):
        return self._finished

    def build_ui(self):
        self.build_calls += 1
        if self._async_build:

            async def _done():
                return None

            return _done()

    async def refresh(self, **kwargs):
        self.refresh_calls += 1

    async def _safe_defer(self, interaction):
        self.deferred.append(interaction)

    async def respond(self, interaction, content, **kwargs):
        self.responded.append(content)

    async def open_modal(self, interaction, modal):
        self.opened_modal = modal


class _OnLoadHost:
    """Host that builds in on_load (no build_ui) -- exercises reload() fallback.

    The modern preload-based view shape: a view defines ``on_load`` rather
    than ``build_ui``, so the region's ``_rerender`` must route through
    ``reload`` (on_load + refresh) instead of the build_ui seam.
    """

    def __init__(self):
        self.reload_calls = 0
        self.refresh_calls = 0
        self._finished = False

    def is_finished(self):
        return self._finished

    async def reload(self):
        self.reload_calls += 1

    async def refresh(self, **kwargs):
        self.refresh_calls += 1


class _TabHost:
    """Host that rebuilds via _refresh_tabs (the TabLayoutView seam).

    A composite placed inside a tab must route its re-render through
    ``_refresh_tabs`` -- the tab view has no build_ui and its bare reload()
    would refresh a stale tree.
    """

    def __init__(self):
        self.tab_refreshes = 0
        self._finished = False

    def is_finished(self):
        return self._finished

    async def _refresh_tabs(self):
        self.tab_refreshes += 1

    # A real TabLayoutView also inherits reload(); _refresh_tabs must win.
    async def reload(self):
        raise AssertionError("reload() should not be called for a tab host")

    async def refresh(self, **kwargs):
        raise AssertionError("bare refresh() should not be called for a tab host")


def _ids(row):
    return [b.custom_id for b in row.children]


class TestPaginatedRegionConstruction:
    """PaginatedRegion validates its construction arguments at __init__."""

    def test_per_page_zero_raises(self):
        with pytest.raises(ValueError, match="per_page must be a positive int"):
            PaginatedRegion(per_page=0)

    def test_per_page_negative_raises(self):
        with pytest.raises(ValueError, match="per_page must be a positive int"):
            PaginatedRegion(per_page=-1)

    def test_per_page_bool_raises(self):
        # bool is an int subclass; True must not slip through as per_page=1.
        with pytest.raises(ValueError, match="per_page must be a positive int"):
            PaginatedRegion(per_page=True)

    def test_jump_threshold_zero_raises(self):
        # jump_threshold is a class attribute (mirrors PaginatedLayoutView);
        # a bad override fails at class-definition time, not construction.
        with pytest.raises(ValueError, match="jump_threshold must be a positive int"):

            class _R(PaginatedRegion):
                jump_threshold = 0

    def test_jump_threshold_bool_raises(self):
        with pytest.raises(ValueError, match="jump_threshold must be a positive int"):

            class _R(PaginatedRegion):
                jump_threshold = True

    def test_bad_button_style_raises(self):
        # Mirrors _BasePaginatedMixin: a bad nav-button style fails at
        # class-definition time, not when the button is built.
        with pytest.raises(TypeError, match="must be a discord.ButtonStyle"):

            class _R(PaginatedRegion):
                prev_button_style = "green"

    def test_bad_button_label_raises(self):
        # A non-str label fails at class-definition time, not at render.
        with pytest.raises(TypeError, match="must be a str or None"):

            class _R(PaginatedRegion):
                next_button_label = 42

    def test_bad_button_emoji_raises(self):
        with pytest.raises(TypeError, match="must be a str, discord.Emoji"):

            class _R(PaginatedRegion):
                next_button_emoji = 123

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="key must be a non-empty str"):
            PaginatedRegion(key="")

    def test_defaults(self):
        region = PaginatedRegion()
        assert region.page == 0
        assert region.items == []
        assert region.page_count == 1


class TestPaginatedRegionSlicing:
    """The region owns the slice math: page_count, page_items, clamping."""

    def test_page_count_ceils(self):
        region = PaginatedRegion(per_page=3, items=list(range(10)))
        assert region.page_count == 4  # ceil(10 / 3)

    def test_page_count_minimum_one(self):
        region = PaginatedRegion(per_page=5, items=[])
        assert region.page_count == 1

    def test_page_items_first_page(self):
        region = PaginatedRegion(per_page=3, items=list(range(10)))
        assert region.page_items == [0, 1, 2]

    def test_page_items_tracks_page(self):
        region = PaginatedRegion(per_page=3, items=list(range(10)))
        region.set_page(1)
        assert region.page_items == [3, 4, 5]

    def test_set_page_clamps_high(self):
        region = PaginatedRegion(per_page=3, items=list(range(10)))
        region.set_page(99)
        assert region.page == 3  # last page
        assert region.page_items == [9]

    def test_set_page_clamps_low(self):
        region = PaginatedRegion(per_page=3, items=list(range(10)))
        region.set_page(-5)
        assert region.page == 0

    def test_items_setter_reclamps(self):
        region = PaginatedRegion(per_page=3, items=list(range(10)))
        region.set_page(3)
        # Data shrinks under the cursor -- the page must clamp back in range.
        region.items = [1, 2]
        assert region.page == 0

    def test_carousel_per_page_one(self):
        region = PaginatedRegion(per_page=1, items=["a", "b", "c"])
        assert region.page_count == 3
        assert region.page_items == ["a"]


class TestPaginatedRegionControls:
    """controls() captures the host and returns the nav row when warranted."""

    def test_single_page_returns_empty(self):
        region = PaginatedRegion(per_page=10, items=[1, 2, 3])
        assert region.controls(_FakeHost()) == []

    def test_becomes_single_page_after_shrink(self):
        # A region that was multi-page drops its nav row once the data
        # shrinks to one page -- controls() re-evaluates page_count per call.
        region = PaginatedRegion(per_page=5, items=list(range(10)))
        region.controls(_FakeHost())  # multi-page
        region.items = [1, 2]
        assert region.controls(_FakeHost()) == []

    def test_multi_page_returns_one_row(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        rows = region.controls(_FakeHost())
        assert len(rows) == 1
        assert isinstance(rows[0], ActionRow)

    def test_controls_captures_view(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _FakeHost()
        region.controls(host)
        assert region._view is host

    def test_below_threshold_three_buttons(self):
        # 3 pages, threshold 5 -> prev / indicator / next only.
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        rows = region.controls(_FakeHost())
        assert _ids(rows[0]) == [
            "region_page_prev",
            "region_page_indicator",
            "region_page_next",
        ]

    def test_at_threshold_five_buttons(self):
        # 5 pages, threshold 5 -> first / prev / goto / next / last.
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        rows = region.controls(_FakeHost())
        assert _ids(rows[0]) == [
            "region_page_first",
            "region_page_prev",
            "region_page_goto",
            "region_page_next",
            "region_page_last",
        ]

    def test_distinct_keys_avoid_collision(self):
        left = PaginatedRegion(per_page=2, items=list(range(6)), key="left")
        right = PaginatedRegion(per_page=2, items=list(range(6)), key="right")
        lids = set(_ids(left.controls(_FakeHost())[0]))
        rids = set(_ids(right.controls(_FakeHost())[0]))
        assert lids.isdisjoint(rids)

    def test_disabled_at_first_page(self):
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        row = region.controls(_FakeHost())[0]
        state = {b.custom_id: b.disabled for b in row.children}
        assert state["region_page_first"] is True
        assert state["region_page_prev"] is True
        assert state["region_page_next"] is False
        assert state["region_page_last"] is False

    def test_disabled_at_last_page(self):
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        region.set_page(4)
        row = region.controls(_FakeHost())[0]
        state = {b.custom_id: b.disabled for b in row.children}
        assert state["region_page_first"] is False
        assert state["region_page_prev"] is False
        assert state["region_page_next"] is True
        assert state["region_page_last"] is True


class TestPaginatedRegionControlButtons:
    """control_buttons() returns the nav buttons unwrapped for host composition."""

    def test_single_page_returns_empty(self):
        region = PaginatedRegion(per_page=10, items=[1, 2, 3])
        assert region.control_buttons(_FakeHost()) == []

    def test_returns_bare_button_list_not_row(self):
        # Unlike controls(), control_buttons() returns the buttons directly.
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        buttons = region.control_buttons(_FakeHost())
        assert not isinstance(buttons, ActionRow)
        assert all(not isinstance(b, ActionRow) for b in buttons)
        assert [b.custom_id for b in buttons] == [
            "region_page_first",
            "region_page_prev",
            "region_page_goto",
            "region_page_next",
            "region_page_last",
        ]

    def test_captures_view(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _FakeHost()
        region.control_buttons(host)
        assert region._view is host

    def test_compact_returns_three_buttons(self):
        # compact drops first/last and forces the clickable go-to middle.
        region = PaginatedRegion(per_page=1, items=list(range(20)))
        buttons = region.control_buttons(_FakeHost(), compact=True)
        assert [b.custom_id for b in buttons] == [
            "region_page_prev",
            "region_page_goto",
            "region_page_next",
        ]

    def test_compact_goto_is_clickable_below_threshold(self):
        # Even with few pages, compact's middle is the go-to button, not
        # the non-interactive indicator -- the jump is compact's whole point.
        region = PaginatedRegion(per_page=2, items=list(range(6)))  # 3 pages < threshold
        buttons = region.control_buttons(_FakeHost(), compact=True)
        goto = buttons[1]
        assert goto.custom_id == "region_page_goto"
        assert goto.disabled is False

    def test_compact_disabled_states(self):
        region = PaginatedRegion(per_page=1, items=list(range(20)))
        first = {b.custom_id: b.disabled for b in region.control_buttons(_FakeHost(), compact=True)}
        assert first["region_page_prev"] is True
        assert first["region_page_next"] is False
        region.set_page(19)
        last = {b.custom_id: b.disabled for b in region.control_buttons(_FakeHost(), compact=True)}
        assert last["region_page_prev"] is False
        assert last["region_page_next"] is True

    def test_full_set_matches_controls_row(self):
        # control_buttons() and controls() build the same buttons; controls()
        # just wraps them in an ActionRow.
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        bare = region.control_buttons(_FakeHost())
        wrapped = region.controls(_FakeHost())[0]
        assert [b.custom_id for b in bare] == _ids(wrapped)

    def test_controls_compact_three_button_row(self):
        region = PaginatedRegion(per_page=1, items=list(range(20)))
        rows = region.controls(_FakeHost(), compact=True)
        assert len(rows) == 1
        assert _ids(rows[0]) == ["region_page_prev", "region_page_goto", "region_page_next"]

    def test_compact_fuses_with_back_exit_in_one_row(self):
        # The carousel use case: three compact pager buttons plus Back and
        # Exit pack into a single five-button ActionRow without overflowing.
        view = StatefulLayoutView()
        region = PaginatedRegion(per_page=1, items=list(range(20)), key="car")
        row = ActionRow(
            *region.control_buttons(view, compact=True),
            view.make_back_button(),
            view.make_exit_button(),
        )
        assert len(row.children) == 5

    def test_full_set_overflows_when_fused(self):
        # The full five-button set is meant for a row of its own; fusing it
        # with other buttons overflows discord.py's five-unit ActionRow cap.
        view = StatefulLayoutView()
        region = PaginatedRegion(per_page=2, items=list(range(10)), key="big")
        with pytest.raises(ValueError):
            ActionRow(*region.control_buttons(view), view.make_back_button())


class TestPaginatedRegionLabels:
    """Indicator and goto labels follow PaginatedLayoutView's conventions."""

    def test_indicator_label_default(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        assert region._resolve_indicator_label() == "Page 1/3"

    def test_goto_label_default(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        assert region._resolve_goto_label() == "1/3"

    def test_indicator_label_override(self):
        # indicator_button_label is a class attribute (mirrors the view pattern).
        class _Labeled(PaginatedRegion):
            indicator_button_label = "Items"

        region = _Labeled(per_page=2, items=list(range(6)))
        assert region._resolve_indicator_label() == "Items"
        assert region._resolve_goto_label() == "Items"


class TestPaginatedRegionNavigation:
    """Click callbacks mutate the index and rebuild + refresh the host."""

    async def test_step_next_advances_and_rerenders(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _FakeHost()
        region.controls(host)
        await region._make_step(1)(make_interaction())
        assert region.page == 1
        assert host.build_calls == 1
        assert host.refresh_calls == 1

    async def test_step_prev_clamps_at_zero(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        region.controls(_FakeHost())
        await region._make_step(-1)(make_interaction())
        assert region.page == 0

    async def test_jump_last_tracks_live_count(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        region.controls(_FakeHost())
        await region._make_jump(lambda: region.page_count - 1)(make_interaction())
        assert region.page == 2

    async def test_jump_first_returns_to_zero(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        region.set_page(2)
        region.controls(_FakeHost())
        await region._make_jump(lambda: 0)(make_interaction())
        assert region.page == 0

    async def test_on_page_changed_fires(self):
        seen = []

        class _Tracked(PaginatedRegion):
            async def on_page_changed(self, page):
                seen.append(page)

        region = _Tracked(per_page=2, items=list(range(6)))
        region.controls(_FakeHost())
        await region._make_step(1)(make_interaction())
        assert seen == [1]

    async def test_rerender_skips_finished_view(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _FakeHost(finished=True)
        region.controls(host)
        await region._make_step(1)(make_interaction())
        # Index still advances, but no edit is shipped to a dead view.
        assert region.page == 1
        assert host.refresh_calls == 0

    async def test_rerender_awaits_async_build_ui(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _FakeHost(async_build=True)
        region.controls(host)
        await region._make_step(1)(make_interaction())
        assert host.build_calls == 1
        assert host.refresh_calls == 1

    async def test_rerender_uses_reload_for_on_load_host(self):
        # A host that builds in on_load (no build_ui) re-renders via reload().
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _OnLoadHost()
        region.controls(host)
        await region._make_step(1)(make_interaction())
        assert region.page == 1
        assert host.reload_calls == 1

    async def test_rerender_uses_refresh_tabs_for_tab_host(self):
        # Inside a TabLayoutView the region re-renders via _refresh_tabs.
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _TabHost()
        region.controls(host)
        await region._make_step(1)(make_interaction())
        assert region.page == 1
        assert host.tab_refreshes == 1


class TestPaginatedRegionGoto:
    """The goto button opens a modal that jumps to a typed page number."""

    async def test_open_goto_sends_modal(self):
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        host = _FakeHost()
        region.controls(host)
        await region._open_goto_modal(make_interaction())
        assert host.opened_modal is not None

    async def test_goto_submit_jumps(self):
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        host = _FakeHost()
        region.controls(host)
        await region._open_goto_modal(make_interaction())
        modal = host.opened_modal
        modal.page_input._value = "3"
        await modal.on_submit(make_interaction())
        assert region.page == 2  # page 3 (1-based) -> index 2
        assert host.refresh_calls == 1
        assert len(host.deferred) == 1

    async def test_goto_submit_invalid_responds(self):
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        host = _FakeHost()
        region.controls(host)
        await region._open_goto_modal(make_interaction())
        modal = host.opened_modal
        modal.page_input._value = "abc"
        await modal.on_submit(make_interaction())
        assert region.page == 0  # unchanged
        assert host.responded  # error message sent
        assert host.refresh_calls == 0

    async def test_goto_submit_clamps_overshoot(self):
        region = PaginatedRegion(per_page=2, items=list(range(10)))  # 5 pages
        host = _FakeHost()
        region.controls(host)
        await region._open_goto_modal(make_interaction())
        modal = host.opened_modal
        modal.page_input._value = "999"
        await modal.on_submit(make_interaction())
        assert region.page == 4  # clamped to last page
        assert host.refresh_calls == 1
        assert len(host.deferred) == 1

    async def test_goto_submit_clamps_undershoot(self):
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        host = _FakeHost()
        region.controls(host)
        await region._open_goto_modal(make_interaction())
        modal = host.opened_modal
        modal.page_input._value = "0"  # below the 1-based minimum
        await modal.on_submit(make_interaction())
        assert region.page == 0
        assert host.refresh_calls == 1


# // ========================================( Choice Row )======================================== // #


async def _noop_select(interaction, value):
    pass


class TestChoiceRowConstruction:
    """choice_row validates its inputs and rejects bad shapes."""

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            choice_row({}, on_select=_noop_select)

    def test_over_25_raises(self):
        with pytest.raises(ValueError, match="25-option limit"):
            choice_row({str(i): i for i in range(26)}, on_select=_noop_select)

    def test_max_select_options_constant_exported(self):
        from cascadeui import MAX_SELECT_OPTIONS

        assert MAX_SELECT_OPTIONS == 25

    def test_at_limit_is_accepted(self):
        # Exactly MAX_SELECT_OPTIONS is the boundary and must be accepted.
        from cascadeui import MAX_SELECT_OPTIONS

        row = choice_row({str(i): i for i in range(MAX_SELECT_OPTIONS)}, on_select=_noop_select)
        assert row is not None

    def test_bad_threshold_raises(self):
        with pytest.raises(ValueError, match="button_threshold"):
            choice_row({"a": 1}, on_select=_noop_select, button_threshold=9)

    def test_threshold_bool_raises(self):
        with pytest.raises(ValueError, match="button_threshold"):
            choice_row({"a": 1}, on_select=_noop_select, button_threshold=True)

    def test_on_select_not_callable_raises(self):
        with pytest.raises(TypeError, match="on_select must be callable"):
            choice_row({"a": 1}, on_select=None)

    def test_non_choice_option_raises(self):
        with pytest.raises(TypeError, match="dict or a sequence of Choice"):
            choice_row([("a", 1)], on_select=_noop_select)

    def test_multi_selected_non_collection_raises(self):
        with pytest.raises(TypeError, match="must be a collection"):
            choice_row({"a": 1, "b": 2}, on_select=_noop_select, selected=1, multi=True)

    def test_multi_selected_str_raises(self):
        # A bare string is the likeliest mistake -- it is iterable, but a
        # single value is not a collection of one-character choices.
        with pytest.raises(TypeError, match="must be a collection"):
            choice_row({"a": 1, "b": 2}, on_select=_noop_select, selected="a", multi=True)

    def test_single_selected_collection_raises(self):
        # The inverse mistake: a collection passed to single-select would hit
        # an unhashable-set TypeError deep in the builder. Raise a directed
        # error at the entry point instead, naming multi=True as the fix.
        with pytest.raises(TypeError, match="must be a single value"):
            choice_row({"a": 1, "b": 2}, on_select=_noop_select, selected=["a"], multi=False)


class TestChoiceRowSingleButtons:
    """Single-select small sets render as a segmented button row."""

    def test_returns_action_row(self):
        row = choice_row({"A": 1, "B": 2}, selected=1, on_select=_noop_select)
        assert isinstance(row, ActionRow)
        assert all(isinstance(b, StatefulButton) for b in row.children)

    def test_active_is_highlighted_and_disabled(self):
        row = choice_row({"A": 1, "B": 2, "C": 3}, selected=2, on_select=_noop_select)
        a, b, c = list(row.children)
        assert b.style == discord.ButtonStyle.primary and b.disabled is True
        assert a.style == discord.ButtonStyle.secondary and a.disabled is False
        assert c.style == discord.ButtonStyle.secondary and c.disabled is False

    def test_none_selected_no_active(self):
        row = choice_row({"A": 1, "B": 2}, on_select=_noop_select)
        assert all(b.style == discord.ButtonStyle.secondary for b in row.children)
        assert all(b.disabled is False for b in row.children)

    def test_custom_styles(self):
        row = choice_row(
            {"A": 1, "B": 2},
            selected=1,
            on_select=_noop_select,
            active_style=discord.ButtonStyle.success,
            inactive_style=discord.ButtonStyle.danger,
        )
        a, b = list(row.children)
        assert a.style == discord.ButtonStyle.success
        assert b.style == discord.ButtonStyle.danger

    def test_choice_input_with_emoji(self):
        row = choice_row(
            [Choice("Goals", 1, emoji="⚽"), Choice("Cards", 2)],
            on_select=_noop_select,
        )
        assert row.children[0].emoji is not None

    def test_custom_id_disambiguates(self):
        left = choice_row({"A": 1}, on_select=_noop_select, custom_id="left")
        right = choice_row({"A": 1}, on_select=_noop_select, custom_id="right")
        lids = {b.custom_id for b in left.children}
        rids = {b.custom_id for b in right.children}
        assert lids.isdisjoint(rids)

    async def test_click_passes_real_value(self):
        seen = {}

        async def on_sel(interaction, value):
            seen["v"] = value

        row = choice_row({"A": "alpha", "B": "beta"}, selected="alpha", on_select=on_sel)
        await list(row.children)[1].original_callback(make_interaction())
        assert seen["v"] == "beta"


class TestChoiceRowDisabled:
    """disabled=True greys out the whole control (buttons or dropdown)."""

    def test_button_form_all_disabled(self):
        row = choice_row(
            {"A": 1, "B": 2, "C": 3}, selected=1, on_select=_noop_select, disabled=True
        )
        assert all(b.disabled is True for b in row.children)

    def test_multi_button_form_all_disabled(self):
        # Multi toggles are never self-disabled, but disabled=True overrides.
        row = choice_row(
            {"A": 1, "B": 2}, selected={1}, on_select=_noop_select, multi=True, disabled=True
        )
        assert all(b.disabled is True for b in row.children)

    def test_dropdown_form_disabled(self):
        opts = {chr(65 + i): i for i in range(8)}  # 8 options -> dropdown
        row = choice_row(opts, on_select=_noop_select, disabled=True)
        select = list(row.children)[0]
        assert select.disabled is True

    def test_default_not_disabled(self):
        # Without disabled=, only the active single-select option is disabled.
        row = choice_row({"A": 1, "B": 2}, selected=1, on_select=_noop_select)
        a, b = list(row.children)
        assert a.disabled is True and b.disabled is False


class TestChoiceRowMultiButtons:
    """Multi-select buttons are toggles, never disabled."""

    def test_active_set_highlighted_none_disabled(self):
        row = choice_row(
            {"A": 1, "B": 2, "C": 3}, selected={1, 3}, on_select=_noop_select, multi=True
        )
        a, b, c = list(row.children)
        assert a.style == discord.ButtonStyle.primary
        assert b.style == discord.ButtonStyle.secondary
        assert c.style == discord.ButtonStyle.primary
        assert all(btn.disabled is False for btn in (a, b, c))

    async def test_click_active_toggles_off(self):
        seen = {}

        async def on_sel(interaction, values):
            seen["v"] = sorted(values)

        row = choice_row({"A": 1, "B": 2, "C": 3}, selected={1, 3}, on_select=on_sel, multi=True)
        await list(row.children)[0].original_callback(make_interaction())  # click A (active)
        assert seen["v"] == [3]

    async def test_click_inactive_toggles_on(self):
        seen = {}

        async def on_sel(interaction, values):
            seen["v"] = sorted(values)

        row = choice_row({"A": 1, "B": 2, "C": 3}, selected={1, 3}, on_select=on_sel, multi=True)
        await list(row.children)[1].original_callback(make_interaction())  # click B (inactive)
        assert seen["v"] == [1, 2, 3]


class TestChoiceRowDropdown:
    """Larger sets render as a dropdown that round-trips real values."""

    def test_over_threshold_is_select(self):
        row = choice_row({f"O{i}": i for i in range(10)}, selected=3, on_select=_noop_select)
        child = list(row.children)[0]
        assert isinstance(child, StatefulSelect)
        assert len(child.options) == 10

    def test_threshold_boundary(self):
        five = choice_row({f"O{i}": i for i in range(5)}, on_select=_noop_select)
        six = choice_row({f"O{i}": i for i in range(6)}, on_select=_noop_select)
        assert all(isinstance(b, StatefulButton) for b in five.children)
        assert isinstance(list(six.children)[0], StatefulSelect)

    def test_threshold_zero_forces_select(self):
        row = choice_row({"a": 1, "b": 2}, on_select=_noop_select, button_threshold=0)
        assert isinstance(list(row.children)[0], StatefulSelect)

    def test_selected_option_defaulted(self):
        row = choice_row({f"O{i}": i for i in range(10)}, selected=3, on_select=_noop_select)
        select = list(row.children)[0]
        defaults = [o.value for o in select.options if o.default]
        assert defaults == ["3"]  # the 4th option (index 3)

    def test_option_values_are_string_indices(self):
        row = choice_row({f"O{i}": i * 100 for i in range(8)}, on_select=_noop_select)
        select = list(row.children)[0]
        assert [o.value for o in select.options] == [str(i) for i in range(8)]

    async def test_single_select_round_trips_value(self):
        seen = {}

        async def on_sel(interaction, value):
            seen["v"] = value

        # value 700 lives at index 7; the dropdown reports option value "7"
        row = choice_row({f"O{i}": i * 100 for i in range(8)}, on_select=on_sel)
        select = list(row.children)[0]
        await select.original_callback(make_interaction(), ["7"])
        assert seen["v"] == 700

    async def test_single_select_empty_values_passes_none(self):
        seen = {"v": "unset"}

        async def on_sel(interaction, value):
            seen["v"] = value

        row = choice_row({f"O{i}": i for i in range(8)}, on_select=on_sel)
        select = list(row.children)[0]
        await select.original_callback(make_interaction(), [])
        assert seen["v"] is None

    def test_select_callback_is_two_param(self):
        # The dropdown callback must accept (interaction, values) so
        # StatefulSelect's create_stateful_callback passes component.values
        # through. A regression to one param would silently drop the values.
        import inspect

        row = choice_row({f"O{i}": i for i in range(8)}, on_select=_noop_select)
        select = list(row.children)[0]
        params = [
            p
            for p in inspect.signature(select.original_callback).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        assert len(params) >= 2

    def test_dropdown_custom_id_disambiguates(self):
        left = choice_row({f"O{i}": i for i in range(8)}, on_select=_noop_select, custom_id="left")
        right = choice_row(
            {f"O{i}": i for i in range(8)}, on_select=_noop_select, custom_id="right"
        )
        assert list(left.children)[0].custom_id != list(right.children)[0].custom_id

    def test_descriptions_on_options(self):
        row = choice_row(
            [Choice(f"C{i}", i, description=f"desc {i}") for i in range(8)],
            on_select=_noop_select,
        )
        select = list(row.children)[0]
        assert select.options[0].description == "desc 0"


class TestChoiceRowMultiDropdown:
    """Multi-select dropdowns set max_values and round-trip a value list."""

    def test_min_max_values(self):
        row = choice_row(
            {f"O{i}": i for i in range(8)}, selected={2, 5}, on_select=_noop_select, multi=True
        )
        select = list(row.children)[0]
        assert select.min_values == 0
        assert select.max_values == 8

    def test_selected_set_defaulted(self):
        row = choice_row(
            {f"O{i}": i for i in range(8)}, selected={2, 5}, on_select=_noop_select, multi=True
        )
        select = list(row.children)[0]
        defaults = sorted(o.value for o in select.options if o.default)
        assert defaults == ["2", "5"]

    async def test_round_trips_value_list(self):
        seen = {}

        async def on_sel(interaction, values):
            seen["v"] = sorted(values)

        row = choice_row({f"O{i}": i for i in range(8)}, on_select=on_sel, multi=True)
        select = list(row.children)[0]
        await select.original_callback(make_interaction(), ["2", "5"])
        assert seen["v"] == [2, 5]


# // ========================================( Collapsible )======================================== // #


def _reveal_one():
    return TextDisplay("revealed")


def _reveal_many():
    return [TextDisplay("a"), TextDisplay("b")]


class TestCollapsibleConstruction:
    """Collapsible validates its construction arguments."""

    def test_empty_label_raises(self):
        with pytest.raises(ValueError, match="label must be a non-empty str"):
            Collapsible(label="", reveal=_reveal_one)

    def test_reveal_not_callable_raises(self):
        with pytest.raises(TypeError, match="reveal must be callable"):
            Collapsible(label="Edit", reveal=None)

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="key must be a non-empty str"):
            Collapsible(label="Edit", reveal=_reveal_one, key="")

    def test_non_bool_expanded_raises(self):
        with pytest.raises(TypeError, match="expanded must be a bool"):
            Collapsible(label="Edit", reveal=_reveal_one, expanded="yes")

    def test_empty_expanded_label_raises(self):
        with pytest.raises(ValueError, match="expanded_label must be a non-empty str"):
            Collapsible(label="Edit", reveal=_reveal_one, expanded_label="")

    def test_async_reveal_raises(self):
        async def _async_reveal():
            return TextDisplay("x")

        with pytest.raises(TypeError, match="reveal must be synchronous"):
            Collapsible(label="Edit", reveal=_async_reveal)

    def test_bad_style_raises(self):
        with pytest.raises(TypeError, match="style must be a discord.ButtonStyle"):
            Collapsible(label="Edit", reveal=_reveal_one, style="primary")

    def test_defaults(self):
        c = Collapsible(label="Edit", reveal=_reveal_one)
        assert c.expanded is False
        # expanded_label defaults to label
        assert c._expanded_label == "Edit"

    def test_initial_expanded_state(self):
        c = Collapsible(label="Edit", reveal=_reveal_one, expanded=True)
        assert c.expanded is True


class TestCollapsibleRender:
    """render() returns the trigger collapsed, trigger + reveal expanded."""

    def test_collapsed_one_trigger_row(self):
        c = Collapsible(label="Edit", reveal=_reveal_one)
        items = c.render(_FakeHost())
        assert len(items) == 1
        assert isinstance(items[0], ActionRow)
        assert items[0].children[0].label == "Edit"

    def test_trigger_custom_id_uses_key(self):
        c = Collapsible(label="Edit", reveal=_reveal_one, key="leagues")
        items = c.render(_FakeHost())
        assert items[0].children[0].custom_id == "leagues_trigger"

    def test_expanded_shows_reveal_and_trigger(self):
        c = Collapsible(label="Edit", reveal=_reveal_one, expanded=True)
        items = c.render(_FakeHost())
        assert len(items) == 2  # trigger row + one revealed component
        assert isinstance(items[0], ActionRow)  # trigger first (default)
        assert isinstance(items[1], TextDisplay)

    def test_expanded_relabels_trigger(self):
        c = Collapsible(label="Edit", expanded_label="Done", reveal=_reveal_one, expanded=True)
        items = c.render(_FakeHost())
        assert items[0].children[0].label == "Done"

    def test_expanded_restyles_trigger(self):
        c = Collapsible(
            label="Edit",
            reveal=_reveal_one,
            style=discord.ButtonStyle.primary,
            expanded_style=discord.ButtonStyle.success,
            expanded=True,
        )
        assert c.render(_FakeHost())[0].children[0].style == discord.ButtonStyle.success
        # collapsed render uses the base style
        c.collapse()
        assert c.render(_FakeHost())[0].children[0].style == discord.ButtonStyle.primary

    def test_expanded_reemojis_trigger(self):
        c = Collapsible(
            label="Edit",
            reveal=_reveal_one,
            emoji="\U0001f512",
            expanded_emoji="\U0001f513",
            expanded=True,
        )
        # PartialEmoji.name distinguishes the two glyphs
        assert str(c.render(_FakeHost())[0].children[0].emoji) == "\U0001f513"

    def test_reveal_list_flattened(self):
        c = Collapsible(label="Edit", reveal=_reveal_many, expanded=True)
        items = c.render(_FakeHost())
        assert len(items) == 3  # trigger + two revealed components

    def test_trigger_first_false_puts_trigger_last(self):
        c = Collapsible(label="Edit", reveal=_reveal_one, expanded=True, trigger_first=False)
        items = c.render(_FakeHost())
        assert isinstance(items[-1], ActionRow)
        assert items[-1].children[0].custom_id.endswith("_trigger")

    def test_distinct_keys_avoid_collision(self):
        left = Collapsible(label="x", reveal=_reveal_one, key="left").render(_FakeHost())
        right = Collapsible(label="x", reveal=_reveal_one, key="right").render(_FakeHost())
        assert left[0].children[0].custom_id != right[0].children[0].custom_id

    def test_render_captures_view(self):
        c = Collapsible(label="Edit", reveal=_reveal_one)
        host = _FakeHost()
        c.render(host)
        assert c._view is host


class TestCollapsibleSummary:
    """A summary callable renders the trigger as an in-card action_section."""

    def test_summary_renders_action_section(self):
        c = Collapsible(label="Edit", reveal=_reveal_one, summary=lambda: "Flagged: Bob", key="rep")
        trigger = c.render(_FakeHost())[0]
        assert isinstance(trigger, Section)
        assert trigger.children[0].content == "Flagged: Bob"
        assert isinstance(trigger.accessory, StatefulButton)
        assert trigger.accessory.label == "Edit"
        assert trigger.accessory.custom_id == "rep_trigger"

    def test_no_summary_stays_bare_button(self):
        c = Collapsible(label="Edit", reveal=_reveal_one)
        assert isinstance(c.render(_FakeHost())[0], ActionRow)

    def test_empty_summary_falls_back_to_bare_button(self):
        # A summary that yields nothing (e.g. data not loaded yet) degrades to
        # the bare button rather than emitting an empty Section.
        c = Collapsible(label="Edit", reveal=_reveal_one, summary=lambda: "")
        assert isinstance(c.render(_FakeHost())[0], ActionRow)

    def test_summary_section_relabels_on_expand(self):
        c = Collapsible(
            label="Edit",
            expanded_label="Done",
            reveal=_reveal_one,
            summary=lambda: "text",
            expanded=True,
        )
        trigger = c.render(_FakeHost())[0]
        assert isinstance(trigger, Section)
        assert trigger.accessory.label == "Done"

    def test_summary_section_restyles_on_expand(self):
        # Style parity with the bare-button path: the Section accessory carries
        # the expanded style when open and the base style when collapsed.
        c = Collapsible(
            label="Edit",
            reveal=_reveal_one,
            summary=lambda: "text",
            style=discord.ButtonStyle.primary,
            expanded_style=discord.ButtonStyle.success,
            expanded=True,
        )
        assert c.render(_FakeHost())[0].accessory.style == discord.ButtonStyle.success
        c.collapse()
        assert c.render(_FakeHost())[0].accessory.style == discord.ButtonStyle.primary

    def test_summary_section_reemojis_on_expand(self):
        c = Collapsible(
            label="Edit",
            reveal=_reveal_one,
            summary=lambda: "text",
            emoji="\U0001f512",
            expanded_emoji="\U0001f513",
            expanded=True,
        )
        assert str(c.render(_FakeHost())[0].accessory.emoji) == "\U0001f513"

    def test_summary_trigger_first_false_puts_section_last(self):
        c = Collapsible(
            label="Edit",
            reveal=_reveal_one,
            summary=lambda: "text",
            expanded=True,
            trigger_first=False,
        )
        items = c.render(_FakeHost())
        assert isinstance(items[-1], Section)
        assert items[-1].accessory.custom_id.endswith("_trigger")

    def test_none_summary_falls_back_to_bare_button(self):
        # The fallback fires on None as well as empty string.
        c = Collapsible(label="Edit", reveal=_reveal_one, summary=lambda: None)
        assert isinstance(c.render(_FakeHost())[0], ActionRow)

    def test_summary_trigger_fuses_into_card(self):
        c = Collapsible(label="Edit", reveal=_reveal_one, summary=lambda: "summary", expanded=True)
        result = card("### Title", *c.render(_FakeHost()))
        assert isinstance(result, Container)

    def test_summary_not_callable_raises(self):
        with pytest.raises(TypeError, match="summary must be callable"):
            Collapsible(label="Edit", reveal=_reveal_one, summary="text")

    def test_async_summary_raises(self):
        async def _async_summary():
            return "x"

        with pytest.raises(TypeError, match="summary must be synchronous"):
            Collapsible(label="Edit", reveal=_reveal_one, summary=_async_summary)


class TestCollapsibleToggle:
    """The trigger toggles state and re-renders the host."""

    def test_collapse_expand_methods(self):
        c = Collapsible(label="Edit", reveal=_reveal_one)
        c.expand()
        assert c.expanded is True
        c.collapse()
        assert c.expanded is False

    async def test_toggle_expands_and_rerenders(self):
        c = Collapsible(label="Edit", reveal=_reveal_one)
        host = _FakeHost()
        c.render(host)
        await c._toggle(make_interaction())
        assert c.expanded is True
        assert host.build_calls == 1
        assert host.refresh_calls == 1

    async def test_toggle_collapses_when_open(self):
        c = Collapsible(label="Edit", reveal=_reveal_one, expanded=True)
        host = _FakeHost()
        c.render(host)
        await c._toggle(make_interaction())
        assert c.expanded is False

    async def test_toggle_uses_reload_for_on_load_host(self):
        c = Collapsible(label="Edit", reveal=_reveal_one)
        host = _OnLoadHost()
        c.render(host)
        await c._toggle(make_interaction())
        assert c.expanded is True
        assert host.reload_calls == 1

    async def test_toggle_uses_refresh_tabs_for_tab_host(self):
        # Inside a TabLayoutView the composite re-renders via _refresh_tabs.
        c = Collapsible(label="Edit", reveal=_reveal_one)
        host = _TabHost()
        c.render(host)
        await c._toggle(make_interaction())
        assert c.expanded is True
        assert host.tab_refreshes == 1

    async def test_toggle_skips_finished_view(self):
        c = Collapsible(label="Edit", reveal=_reveal_one)
        host = _FakeHost(finished=True)
        c.render(host)
        await c._toggle(make_interaction())
        # State still flips, but no edit ships to a dead view (the finished
        # guard, not a missing view: build_ui is not called either).
        assert c.expanded is True
        assert host.build_calls == 0
        assert host.refresh_calls == 0

    async def test_toggle_fires_on_toggle_hook(self):
        seen = []

        class _Tracked(Collapsible):
            async def on_toggle(self, expanded):
                seen.append(expanded)

        c = _Tracked(label="Edit", reveal=_reveal_one)
        c.render(_FakeHost())
        await c._toggle(make_interaction())
        await c._toggle(make_interaction())
        assert seen == [True, False]
