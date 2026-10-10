# // ========================================( Modules )======================================== // #


import asyncio
import logging
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import aiohttp
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
from helpers import RenderableLayoutView, make_interaction, until

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
    render_progress,
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


class TestCardColour:
    """``color=`` takes the int form its signature has always documented.

    discord.py coerces an int in ``Embed.colour``'s setter but stores one
    verbatim on ``Container.accent_colour``, so the builders coerce to keep
    a single type in the tree.
    """

    def test_int_is_coerced_to_colour(self):
        assert card("x", color=0xD4AF37).accent_colour == discord.Colour(0xD4AF37)

    def test_colour_passes_through_untouched(self):
        colour = discord.Colour(0xD4AF37)

        assert card("x", color=colour).accent_colour is colour

    def test_stats_card_coerces_the_same_way(self):
        assert stats_card("T", {"a": 1}, color=0xD4AF37).accent_colour == discord.Colour(0xD4AF37)

    def test_a_string_colour_is_rejected(self):
        with pytest.raises(TypeError, match="card color must be a discord.Colour or an int"):
            card("x", color="#D4AF37")

    def test_a_bool_is_rejected(self):
        # bool is an int subclass, so True would otherwise become Colour(1).
        with pytest.raises(TypeError, match="got bool"):
            card("x", color=True)

    def test_an_out_of_range_int_is_rejected(self):
        with pytest.raises(ValueError, match="between 0x000000 and 0xFFFFFF"):
            card("x", color=0x1000000)


class TestCardContainerChild:
    """card() rejects a Container child at construction.

    A Container is never a legal Container child, so the mistake is
    decidable at the composing call. Before the guard it constructed
    silently and failed at whichever seam shipped the view, as a placement
    rejection naming Container indexes rather than the call -- on a pushed
    screen, a failed navigation.
    """

    def test_alert_child_rejected_naming_position(self):
        with pytest.raises(ValueError, match="card: child 1 is a Container"):
            card("## Display", alert("none yet", level="info"))

    def test_nested_card_rejected(self):
        with pytest.raises(ValueError, match="Container inside a Container"):
            card(card("inner"))

    def test_stats_card_child_rejected(self):
        with pytest.raises(ValueError, match="card: child 0 is a Container"):
            card(stats_card("T", {"a": 1}))

    def test_raw_container_child_rejected(self):
        with pytest.raises(ValueError, match="card: child 0 is a Container"):
            card(Container(TextDisplay("x")))

    def test_container_subclass_rejected_naming_subclass(self):
        class BrandedContainer(Container):
            pass

        with pytest.raises(ValueError, match="BrandedContainer"):
            card("heading", BrandedContainer(TextDisplay("x")))

    def test_message_names_sibling_placement_as_the_fix(self):
        with pytest.raises(ValueError, match="sibling"):
            card("x", alert("y"))

    def test_container_legal_children_still_accepted(self):
        result = card("## T", TextDisplay("body"), Separator(), ActionRow())
        assert isinstance(result, Container)
        assert len(result.children) == 4


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

    def test_two_param_callback_refused(self):
        # The button carries no value, so a handler shaped for one is a
        # mistake caught at the builder line, not on the first click.
        async def cb(interaction, value):
            pass

        with pytest.raises(TypeError, match=r"cannot be called with \(interaction\)"):
            action_section("text", label="Go", callback=cb)


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

    async def _click(self, section):
        """Drive the accessory button's real (wrapped) callback."""
        button = section.accessory
        view = MagicMock()
        view.user_id = 1
        view.id = "view-under-test"
        view.is_finished = MagicMock(return_value=False)
        view.dispatch = AsyncMock()
        button._view = view
        await button.callback(make_interaction())

    async def test_two_param_callback_receives_the_flip_of_active(self):
        # The builder is immediate-mode: the click asks for the flip of the
        # rendered state, the value toggle_button reports post-flip.
        received = []

        async def cb(interaction, active):
            received.append(active)

        await self._click(toggle_section("Module", active=True, callback=cb))
        await self._click(toggle_section("Module", active=False, callback=cb))

        assert received == [False, True]

    async def test_one_param_callback_receives_one_argument(self):
        received = []

        async def cb(*args):
            received.append(args)

        await self._click(toggle_section("Module", active=True, callback=cb))

        assert [len(args) for args in received] == [1]

    def test_callback_accepting_neither_shape_refused(self):
        with pytest.raises(TypeError, match=r"toggle_section: .*\(interaction, active\)"):
            toggle_section("Module", active=True, callback=lambda: None)


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

    def test_unfurled_media_item_passes_through_unchanged(self):
        item = discord.UnfurledMediaItem("https://example.com/img.png")
        result = image_section("text", url=item)
        assert result.accessory.media is item

    def test_empty_url_rejected(self):
        """An empty reference shipped silently and 400ed at send otherwise."""
        with pytest.raises(ValueError, match="image_section: url is empty"):
            image_section("text", url="")

    def test_whitespace_url_rejected(self):
        # One notch stricter than the text builders: a URL is machine-consumed,
        # so whitespace-only is decidable garbage where whitespace text is not.
        with pytest.raises(ValueError, match="image_section: url is empty"):
            image_section("text", url="   ")

    def test_empty_unfurled_media_item_rejected(self):
        # The passthrough branch never read the wrapped url, so
        # UnfurledMediaItem("") sailed through where "" is now caught.
        with pytest.raises(ValueError, match="image_section: url is empty"):
            image_section("text", url=discord.UnfurledMediaItem(""))


# // ========================================( Key Value )======================================== // #


class TestKeyValue:
    """key_value() renders a dict as bold-key: value TextDisplay lines."""

    def test_returns_text_display(self):
        result = key_value({"A": 1})
        assert isinstance(result, TextDisplay)

    def test_formatting(self):
        result = key_value({"Members": 42, "Roles": 5})
        assert result.content == "**Members:** 42\n**Roles:** 5"

    def test_empty_dict_rejected(self):
        # An empty dict produced a TextDisplay with no content, which Discord
        # rejects along with every component beside it. The old assertion here
        # (content == "") documented the shape that fails at send.
        with pytest.raises(ValueError, match="key_value"):
            key_value({})

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
        """Descriptions length must match URLs exactly: fail loud like emoji_grid."""
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

    def test_empty_item_rejected_naming_index(self):
        """The per-item ``media[i]`` param name points at the blank reference."""
        with pytest.raises(ValueError, match=r"gallery: media\[1\] is empty"):
            gallery("https://example.com/a.png", "")


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

    def test_empty_url_rejected(self):
        with pytest.raises(ValueError, match="file_attachment: url is empty"):
            file_attachment("")


# // ========================================( Additive helpers )======================================== // #


async def _noop(interaction):
    pass


async def _noop_with_value(interaction, value):
    pass


class TestToggleSectionEmoji:
    """toggle_section gains emoji kwarg for parity with action_section."""

    def test_emoji_kwarg_passthrough(self):
        result = toggle_section("Lights", active=True, callback=_noop, emoji="\U0001f4a1")
        assert isinstance(result, Section)
        # Emoji lives on the accessory button.
        button = result.accessory
        assert button.emoji is not None


class TestLinkSection:
    """Section with link-style Button accessory."""

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
    """returns [TextDisplay, ActionRow] for splat-into-card composition."""

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

    def test_two_param_callback_refused(self):
        with pytest.raises(TypeError, match=r"cannot be called with \(interaction\)"):
            confirm_section("Sure?", on_confirm=_noop_with_value, on_cancel=_noop)


class TestButtonRow:
    """dict shorthand for an ActionRow of same-style buttons."""

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

    def test_two_param_callback_refused(self):
        with pytest.raises(TypeError, match=r"cannot be called with \(interaction\)"):
            button_row({"Save": _noop_with_value})


class TestCycleButton:
    """first stateful v2 helper; index tracked on instance."""

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

    def test_one_param_on_change_refused(self):
        # The value rides every click; a callback that cannot take it would
        # otherwise die with a bare arity TypeError on the first advance.
        with pytest.raises(
            TypeError, match=r"cycle_button: on_change callback .*\(interaction, value\)"
        ):
            cycle_button(values=[1, 2], on_change=_noop)


class TestToggleButton:
    """standalone boolean toggle, distinct from toggle_section."""

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

    def test_one_param_on_toggle_refused(self):
        with pytest.raises(
            TypeError, match=r"toggle_button: on_toggle callback .*\(interaction, active\)"
        ):
            toggle_button(active=True, on_toggle=_noop)


class TestDoubleClickOnAStatefulControl:
    """Both clicks of a double-click come from one render and ask for the
    same state, so the second must not flip the toggle back.

    Each click is numbered as it reaches the view, before the lock; one that
    arrived before the same control's previous click finished repeats it.
    """

    @staticmethod
    def _host():
        view = RenderableLayoutView(interaction=make_interaction())
        return view

    @staticmethod
    async def _double(view, button):
        await asyncio.gather(
            view._scheduled_task(button, make_interaction()),
            view._scheduled_task(button, make_interaction()),
        )

    async def test_toggle_section_with_a_one_parameter_callback(self, caplog):
        """The callback flips its own state, as the settings examples did."""
        caplog.set_level(logging.DEBUG, logger="cascadeui")
        view = self._host()
        state = {"on": False}

        async def flip(interaction):
            state["on"] = not state["on"]
            await asyncio.sleep(0)

        section = toggle_section("Alerts", active=False, callback=flip)
        view.add_item(section)

        await self._double(view, section.accessory)

        assert state["on"] is True
        assert any(
            "Dropped a click" in r.getMessage()
            and "the second click of a double-click" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    @pytest.mark.parametrize("half", [0, 1], ids=["confirm", "cancel"])
    async def test_confirm_section_runs_a_double_clicked_answer_once(self, half):
        """A confirm is one decision; the second click of a double-click made
        it again, so a delete or a payment ran twice."""
        view = self._host()
        calls = []

        async def answer(interaction):
            calls.append(interaction)
            await asyncio.sleep(0)

        _, row = confirm_section("Sure?", on_confirm=answer, on_cancel=answer)
        view.add_item(row)

        await self._double(view, row.children[half])

        assert len(calls) == 1

    async def test_confirm_section_runs_a_later_click(self):
        """Only the repeat is dropped: a click sent after the answer settled
        is a new decision on a prompt that is still up."""
        view = self._host()
        calls = []

        async def confirm(interaction):
            calls.append(interaction)

        _, row = confirm_section("Sure?", on_confirm=confirm, on_cancel=_noop)
        view.add_item(row)

        await view._scheduled_task(row.children[0], make_interaction())
        await view._scheduled_task(row.children[0], make_interaction())

        assert len(calls) == 2

    @pytest.mark.parametrize("first", [0, 1], ids=["confirm_first", "cancel_first"])
    async def test_confirm_section_takes_one_answer(self, first):
        """Each button guarded its own clicks, so a Cancel sent from the
        same render as a Confirm ran after the Confirm had already acted."""
        view = self._host()
        calls = []

        async def confirm(interaction):
            calls.append("confirm")
            await asyncio.sleep(0)

        async def cancel(interaction):
            calls.append("cancel")
            await asyncio.sleep(0)

        _, row = confirm_section("Sure?", on_confirm=confirm, on_cancel=cancel)
        view.add_item(row)
        second = 1 - first

        await asyncio.gather(
            view._scheduled_task(row.children[first], make_interaction()),
            view._scheduled_task(row.children[second], make_interaction()),
        )

        assert calls == [["confirm", "cancel"][first]]

    async def test_a_dropped_answer_is_acknowledged(self):
        """The dropped click runs none of the caller's code, so with
        ``auto_defer`` off nothing else answers it."""
        view = self._host()
        view.auto_defer = False

        async def confirm(interaction):
            await interaction.response.defer()
            await asyncio.sleep(0)

        _, row = confirm_section("Sure?", on_confirm=confirm, on_cancel=_noop)
        view.add_item(row)
        dropped = make_interaction()

        await asyncio.gather(
            view._scheduled_task(row.children[0], make_interaction()),
            view._scheduled_task(row.children[1], dropped),
        )

        dropped.response.defer.assert_awaited_once()

    async def test_confirm_section_cooldowns_are_per_answer(self):
        """``with_cooldown`` keys an unnamed control on its callback's name,
        and both answers carried the library's wrapper name, so a cooldown on
        Confirm refused the Cancel that followed."""
        from cascadeui import with_cooldown

        view = self._host()
        calls = []

        async def confirm(interaction):
            calls.append("confirm")

        async def cancel(interaction):
            calls.append("cancel")

        _, row = confirm_section("Sure?", on_confirm=confirm, on_cancel=cancel)
        view.add_item(row)
        for button in row.children:
            with_cooldown(button, seconds=30)

        await view._scheduled_task(row.children[0], make_interaction())
        await view._scheduled_task(row.children[1], make_interaction())

        assert calls == ["confirm", "cancel"]

    async def test_toggle_button_reports_the_new_state_once(self):
        view = self._host()
        seen = []

        async def on_toggle(interaction, active):
            seen.append(active)
            await asyncio.sleep(0)

        button = toggle_button(active=False, on_toggle=on_toggle)
        view.add_item(ActionRow(button))

        await self._double(view, button)

        assert seen == [True]
        assert button._toggle_active is True

    async def test_the_dropped_click_is_answered_without_auto_defer(self):
        # No callback of the user's ran for it, so nothing else could answer it.
        view = self._host()
        view.auto_defer = False
        seen = []

        async def on_toggle(interaction, active):
            seen.append(active)
            await asyncio.sleep(0)

        button = toggle_button(active=False, on_toggle=on_toggle)
        view.add_item(ActionRow(button))
        first, second = make_interaction(), make_interaction()

        await asyncio.gather(
            view._scheduled_task(button, first),
            view._scheduled_task(button, second),
        )

        assert seen == [True]
        second.response.defer.assert_awaited()

    async def test_toggle_button_component_flips_once(self):
        from cascadeui import ToggleButton

        view = self._host()
        calls = []

        async def cb(interaction):
            calls.append(1)
            await asyncio.sleep(0)

        button = ToggleButton(label="Mute", callback=cb)
        view.add_item(ActionRow(button))

        await self._double(view, button)

        assert button.is_toggled is True
        assert calls == [1]

    async def test_cycle_button_advances_once(self):
        view = self._host()
        seen = []

        async def on_change(interaction, value):
            seen.append(value)
            await asyncio.sleep(0)

        button = cycle_button(values=["a", "b", "c"], on_change=on_change)
        view.add_item(ActionRow(button))

        await self._double(view, button)

        assert seen == ["b"]

    async def test_collapsible_opens_once(self):
        class _Slow(Collapsible):
            async def on_toggle(self, expanded):
                # Stands in for the edit, which is what lets the second
                # click arrive while the first is still being handled.
                await asyncio.sleep(0)

        view = self._host()
        box = _Slow(label="More", reveal=lambda: [TextDisplay("hidden")], key="more")
        row = box.render(view)[0]
        view.add_item(row)
        trigger = row.children[0]

        await self._double(view, trigger)

        assert box.expanded is True

    async def test_a_click_sent_after_the_result_flips_again(self):
        view = self._host()
        seen = []

        async def on_toggle(interaction, active):
            seen.append(active)

        button = toggle_button(active=False, on_toggle=on_toggle)
        view.add_item(ActionRow(button))
        await view._scheduled_task(button, make_interaction())

        await view._scheduled_task(button, make_interaction())

        assert seen == [True, False]

    async def test_a_multi_choice_row_rebuilt_under_the_same_ids_toggles_once(self):
        """A persistent view keeps a choice_row's ids across renders, so the
        second click of a double-click reaches the rebuilt option, whose
        snapshot already holds the first click's result, and toggled it back
        out."""
        view = self._host()
        picks = []
        rebuilt = asyncio.Event()
        release = asyncio.Event()

        def build(selected):
            async def on_select(interaction, values):
                picks.append(values)
                view.clear_items()
                view.add_item(build(values))
                rebuilt.set()
                await release.wait()

            return choice_row(
                {"Red": "r", "Blue": "b"},
                on_select=on_select,
                selected=selected,
                multi=True,
                custom_id="tags",
            )

        view.clear_items()
        view.add_item(build([]))
        old = view.children[0].children[0]
        first = asyncio.create_task(view._scheduled_task(old, make_interaction()))
        await rebuilt.wait()
        new = view.children[0].children[0]
        assert new is not old and new.custom_id == old.custom_id
        second = asyncio.create_task(view._scheduled_task(new, make_interaction()))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

        assert picks == [["r"]]

    async def test_a_control_rebuilt_under_the_same_id_keeps_its_mark(self):
        """Discord routes a click by custom_id, so the second click of a
        double-click reaches the rebuilt control, a new object with no mark
        of its own; it flipped the toggle back."""
        view = self._host()
        saves = []
        rebuilt = asyncio.Event()
        release = asyncio.Event()

        def build(active):
            async def on_toggle(interaction, new):
                saves.append(new)
                view.clear_items()
                view.add_item(ActionRow(build(new)))
                rebuilt.set()
                await release.wait()

            return toggle_button(active=active, on_toggle=on_toggle, custom_id="dark")

        old = build(False)
        view.add_item(ActionRow(old))
        first = asyncio.create_task(view._scheduled_task(old, make_interaction()))
        await rebuilt.wait()
        new = view.children[0].children[0]
        assert new is not old and new.custom_id == old.custom_id
        second = asyncio.create_task(view._scheduled_task(new, make_interaction()))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

        assert saves == [True]

        await view._scheduled_task(view.children[0].children[0], make_interaction())

        assert saves == [True, False]


class TestStatsCard:
    """Container composition of heading + divider + key_value."""

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


class TestRenderProgress:
    """The bar as a string, for callers inlining it into a row."""

    def test_returns_a_string_not_a_component(self):
        assert isinstance(render_progress(5, 10), str)

    def test_the_builder_composes_the_same_string(self):
        """One renderer, so the two can never drift apart."""
        assert progress_bar(7, 10, width=10).content == render_progress(7, 10, width=10)

    def test_width_bounds_the_bar_however_large_the_value(self):
        """A hand-rolled glyph loop grows a cell per unit; this does not."""
        bar = render_progress(400, 5, width=5, show_percent=False)
        assert len(bar) == 7  # five cells plus the two brackets

    def test_rejects_a_non_numeric_value(self):
        with pytest.raises(TypeError, match="render_progress: value must be a number"):
            render_progress("x", 10)

    def test_rejects_a_non_positive_max(self):
        with pytest.raises(ValueError, match="render_progress: max_value must be positive"):
            render_progress(1, 0)


class TestProgressBar:
    """text-based progress bar as TextDisplay."""

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
    """ActionRow of tab-styled buttons for manual-control views."""

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

    def test_two_param_callback_refused(self):
        with pytest.raises(TypeError, match=r"cannot be called with \(interaction\)"):
            tab_nav({"Stats": _noop_with_value})


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
        self.answered = []
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

    async def _render_as_answer(self, interaction, render):
        self.answered.append(interaction)
        await render()

    async def respond(self, interaction, content, **kwargs):
        self.responded.append(content)

    async def open_modal(self, interaction, modal):
        self.opened_modal = modal

    async def _answer_if_closed(self, interaction):
        return self._finished


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

    async def _refresh_tabs(self, *, lineage=False):
        assert lineage, "a region's tab render joins the turn of the task that started it"
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

    def test_bad_indicator_format_placeholder_raises(self):
        # PaginatedRegion validates its own copy of this check -- it does
        # not inherit _StatefulMixin, so _FORMAT_ATTRS never runs here.
        with pytest.raises(ValueError, match="indicator_button_format is not a valid"):

            class _R(PaginatedRegion):
                indicator_button_format = "{pagee}/{total}"

    def test_non_str_indicator_format_raises(self):
        with pytest.raises(TypeError, match="indicator_button_format must be a str or None"):

            class _R(PaginatedRegion):
                indicator_button_format = 5

    def test_valid_indicator_format_accepted(self):
        class _R(PaginatedRegion):
            indicator_button_format = "{page} of {total}"

        assert _R.indicator_button_format == "{page} of {total}"


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

    def test_set_page_defers_clamping_until_items_arrive(self):
        """A host that loads items in on_load sets the page before they land.

        restore_nav_state runs ahead of on_load, so a page carried across a
        pop reaches an empty region. Clamping there would rewrite it to zero
        and lose the user's place for a list that is about to exist.
        """
        region = PaginatedRegion(per_page=3, items=[])
        region.set_page(3)
        assert region.page == 3  # held, not clamped against an empty list

        region.items = list(range(10))  # on_load lands
        assert region.page == 3
        assert region.page_items == [9]

    def test_a_from_end_index_resolves_against_the_items_that_arrive(self):
        """``set_page(-1)`` names the last page of the next render, not this one.

        ``show_page`` seeks and then re-renders, and the host's ``on_load``
        assigns fresh items during that render. Resolved against the list in
        hand, a negative index leaves the cursor on the old last page whenever
        the new list crossed a ``per_page`` boundary, so the row that prompted
        the jump renders off-screen.
        """
        region = PaginatedRegion(per_page=4, items=list(range(8)))
        region.set_page(-1)
        assert region.page == 1  # last page of the list in hand

        region.items = list(range(9))  # the render loads one more, crossing 4
        assert region.page == 2
        assert region.page_items == [8]

    def test_an_explicit_move_drops_a_pending_from_end_index(self):
        """Only an unconsumed seek re-resolves; a later move is the user's."""
        region = PaginatedRegion(per_page=4, items=list(range(8)))
        region.set_page(-1)
        region.set_page(0)

        region.items = list(range(9))
        assert region.page == 0

    def test_deferred_page_still_clamps_once_the_list_is_known(self):
        """Holding the index is not the same as trusting it."""
        region = PaginatedRegion(per_page=3, items=[])
        region.set_page(99)

        region.items = list(range(10))

        assert region.page == 3  # last real page

    def test_page_items_clamps_an_out_of_range_index_on_read(self):
        """Nothing out of range can render, even while the clamp is deferred."""
        region = PaginatedRegion(per_page=3, items=[])
        region.set_page(9)

        assert region.page_items == []
        assert region.page == 0  # the read corrected it

    def test_a_negative_index_on_an_empty_region_resolves_to_zero_and_waits(self):
        """With no items there is one page, so the immediate resolution is
        zero. The request is still held: an index counted from an end the
        region does not have yet is answered when the items arrive."""
        region = PaginatedRegion(per_page=3, items=[])
        region.set_page(-5)
        assert region.page == 0

        region.items = list(range(30))  # ten pages; -5 counts back from ten
        assert region.page == 5

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


class TestCompositeHostAccessor:
    """Both stateful composites expose the captured host as read-only ``host``.

    ``on_page_changed`` / ``on_toggle`` overrides that prefetch or respond
    need the host, and the documented public surface previously offered no
    path to it -- consumers reached into ``_view``. Mirrors the
    ``_BaseLeaderboardMixin.bot`` property: a read-only accessor over the
    private capture.
    """

    def test_region_host_none_before_attach(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        assert region.host is None

    def test_region_host_set_by_controls(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _FakeHost()
        region.controls(host)
        assert region.host is host

    def test_region_host_set_by_control_buttons(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _FakeHost()
        region.control_buttons(host)
        assert region.host is host

    def test_region_host_is_read_only(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        with pytest.raises(AttributeError):
            region.host = _FakeHost()

    async def test_region_hook_reads_host_on_page_turn(self):
        seen = []

        class Prefetching(PaginatedRegion):
            async def on_page_changed(self, page):
                seen.append(self.host)

        region = Prefetching(per_page=2, items=list(range(6)))
        host = _FakeHost()
        region.controls(host)
        await region._make_step(1)(make_interaction())
        assert seen == [host]

    def test_collapsible_host_none_before_render(self):
        collapsible = Collapsible(label="More", reveal=lambda: TextDisplay("body"))
        assert collapsible.host is None

    def test_collapsible_host_set_by_render(self):
        collapsible = Collapsible(label="More", reveal=lambda: TextDisplay("body"))
        host = _FakeHost()
        collapsible.render(host)
        assert collapsible.host is host

    def test_collapsible_host_is_read_only(self):
        collapsible = Collapsible(label="More", reveal=lambda: TextDisplay("body"))
        with pytest.raises(AttributeError):
            collapsible.host = _FakeHost()

    async def test_collapsible_hook_reads_host_on_toggle(self):
        seen = []

        class Loading(Collapsible):
            async def on_toggle(self, expanded):
                seen.append(self.host)

        collapsible = Loading(label="More", reveal=lambda: TextDisplay("body"))
        host = _FakeHost()
        collapsible.render(host)
        await collapsible._toggle(make_interaction())
        assert seen == [host]


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


class _LoadingHost(RenderableLayoutView):
    """A host whose load writes on both sides of an await; each build records what it saw."""

    def __init__(self, *args, **kwargs):
        self.a = self.b = 0
        self.loading, self.release = asyncio.Event(), asyncio.Event()
        self.composed = []
        super().__init__(*args, **kwargs)
        self._message = AsyncMock()

    async def on_load(self):
        self.a += 1
        self.loading.set()
        await self.release.wait()
        self.b += 1

    def build_ui(self):
        self.composed.append((self.a, self.b))


async def _render_during_a_load(host, render):
    """Start ``render()`` while ``host.load()`` is halfway; return what was built before it ended."""
    load = asyncio.create_task(host.load())
    await asyncio.wait_for(host.loading.wait(), 2)
    rendering = asyncio.create_task(render())
    for _ in range(10):
        await asyncio.sleep(0)
    during = list(host.composed)
    host.release.set()
    await asyncio.wait_for(asyncio.gather(load, rendering), 2)
    return during


class TestPaginatedRegionNavigation:
    """Click callbacks mutate the index and rebuild + refresh the host."""

    async def test_a_page_turn_waits_for_a_load_in_progress(self):
        """A page turn built the host's tree beside its load, from half-loaded data."""
        host = _LoadingHost(interaction=make_interaction(), user_id=1, guild_id=2)
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        region.controls(host)
        host.composed.clear()

        during = await _render_during_a_load(host, lambda: region.show_page(1))

        assert during == []
        assert host.composed == [(1, 1)]

    async def test_a_page_turn_gathered_from_the_hosts_own_load_does_not_wait_for_it(self):
        """The load held the turn and awaited the page turn, which waited for
        the turn, so neither finished."""

        class _GatheringHost(RenderableLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.region = PaginatedRegion(
                    items=[TextDisplay(f"row {i}") for i in range(9)], per_page=3
                )
                self.jump = False

            def build_ui(self):
                self.clear_items()
                for item in self.region.page_items:
                    self.add_item(item)
                for item in self.region.controls(self):
                    self.add_item(item)

            async def on_load(self):
                self.build_ui()
                if self.jump:
                    await asyncio.gather(self.region.show_page(2))

        host = _GatheringHost(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()
        host.jump = True

        await asyncio.wait_for(host.reload(), 2)

        assert host.region.page == 2

    @pytest.mark.parametrize("spawn", ["gather", "task"])
    async def test_a_page_turn_from_a_state_renders_child_task_does_not_wait_for_it(self, spawn):
        """The page turn waited for the state render to finish, and the render
        was awaiting the page turn, so neither finished and the view stopped
        rendering state changes."""

        class _NotifiedHost(RenderableLayoutView):
            subscribed_actions = {"JUMP"}

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.region = PaginatedRegion(
                    items=[TextDisplay(f"row {i}") for i in range(9)], per_page=3
                )

            def build_ui(self):
                self.clear_items()
                for item in self.region.page_items:
                    self.add_item(item)
                for item in self.region.controls(self):
                    self.add_item(item)

            async def on_state_changed(self, state):
                if spawn == "gather":
                    await asyncio.gather(self.region.show_page(2))
                else:
                    await asyncio.create_task(self.region.show_page(2))

        host = _NotifiedHost(interaction=make_interaction(), user_id=1, guild_id=2)
        host.build_ui()
        await host.send()

        await asyncio.wait_for(host.dispatch("JUMP"), 2)

        assert host.region.page == 2

    @pytest.mark.parametrize("where", ["on_load", "on_state_changed"])
    async def test_a_page_turn_gathered_in_a_tab_host_does_not_wait_for_it(self, where):
        """A region in a tab, turned from the host's own load or state render
        through gather(), waited for the turn that render held, so neither
        finished."""
        from cascadeui import TabLayoutView

        class _TabHost(TabLayoutView):
            subscribed_actions = {"JUMP"}

            def __init__(self, **kwargs):
                self.region = PaginatedRegion(
                    items=[f"row {i}" for i in range(9)], per_page=3, key="r"
                )
                self.jump = False
                super().__init__(tabs={"Main": self._main, "Other": self._main}, **kwargs)

            async def _main(self):
                rows = [TextDisplay(row) for row in self.region.page_items]
                return [Container(*rows), *self.region.controls(self)]

            async def on_load(self):
                await super().on_load()
                if where == "on_load" and self.jump:
                    self.jump = False
                    await asyncio.gather(self.region.show_page(2))

            async def on_state_changed(self, state):
                if where == "on_state_changed":
                    await asyncio.gather(self.region.show_page(2))
                await self.refresh()

        host = _TabHost(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()
        host.jump = True

        if where == "on_load":
            await asyncio.wait_for(host.reload(), 2)
        else:
            await asyncio.wait_for(host.dispatch("JUMP"), 2)

        shown = [i.content for i in host.walk_children() if isinstance(i, TextDisplay)]
        assert host.region.page == 2 and "row 6" in shown

    async def test_a_page_turn_gathered_in_a_load_hosts_state_render_does_not_wait_for_it(self):
        """A region on a host that renders in on_load, turned from its state
        render through gather(), waited for that render to finish."""

        class _LoadHost(RenderableLayoutView):
            subscribed_actions = {"JUMP"}

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.region = PaginatedRegion(
                    items=[f"row {i}" for i in range(9)], per_page=3, key="r"
                )

            async def on_load(self):
                self.clear_items()
                for row in self.region.page_items:
                    self.add_item(TextDisplay(row))
                for item in self.region.controls(self):
                    self.add_item(item)

            async def on_state_changed(self, state):
                await asyncio.gather(self.region.show_page(2))

        host = _LoadHost(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()

        await asyncio.wait_for(host.dispatch("JUMP"), 2)

        shown = [i.content for i in host.walk_children() if isinstance(i, TextDisplay)]
        assert host.region.page == 2 and "row 6" in shown

    @pytest.mark.parametrize("where", ["on_load", "on_state_changed"])
    async def test_two_page_turns_gathered_from_one_render_build_one_at_a_time(self, where):
        """Both turns joined the render and ran the host's async build_ui at
        once on one tree, and one failed on a duplicate custom_id."""

        class _TwoRegions(RenderableLayoutView):
            subscribed_actions = {"JUMP"}

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.a = PaginatedRegion(items=list(range(9)), per_page=3, key="a")
                self.b = PaginatedRegion(items=list(range(9)), per_page=3, key="b")
                self.building = self.most = 0
                self.jump = False
                self.results = None
                self._fill()

            def _fill(self):
                for region in (self.a, self.b):
                    for i in region.page_items:
                        self.add_item(TextDisplay(f"{region._key} {i}"))
                    for item in region.controls(self):
                        self.add_item(item)

            async def build_ui(self):
                self.building += 1
                self.most = max(self.most, self.building)
                try:
                    self.clear_items()
                    await asyncio.sleep(0)
                    self._fill()
                finally:
                    self.building -= 1

            async def _turn_both(self):
                self.results = await asyncio.gather(
                    self.a.show_page(1), self.b.show_page(1), return_exceptions=True
                )

            async def on_load(self):
                if where == "on_load" and self.jump:
                    self.jump = False
                    await self._turn_both()

            async def on_state_changed(self, state):
                if where == "on_state_changed":
                    await self._turn_both()

        host = _TwoRegions(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()
        host.jump = True

        if where == "on_load":
            await asyncio.wait_for(host.reload(), 2)
        else:
            await asyncio.wait_for(host.dispatch("JUMP"), 2)

        assert host.results == [None, None]
        assert host.most == 1
        assert (host.a.page, host.b.page) == (1, 1)

    async def test_a_page_turn_the_render_did_not_await_keeps_a_reload_out(self):
        """A turn the state render started without awaiting kept building
        after the render ended, beside a reload queued meanwhile."""
        parked, release = asyncio.Event(), asyncio.Event()

        class _Spawning(RenderableLayoutView):
            subscribed_actions = {"JUMP"}

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.region = PaginatedRegion(items=list(range(9)), per_page=3, key="r")
                self.building = self.most = 0
                self.loading = False
                self._fill()

            def _fill(self):
                for i in self.region.page_items:
                    self.add_item(TextDisplay(f"row {i}"))
                for item in self.region.controls(self):
                    self.add_item(item)

            def _enter(self):
                self.building += 1
                self.most = max(self.most, self.building)

            async def build_ui(self):
                self._enter()
                try:
                    self.clear_items()
                    if not parked.is_set():
                        parked.set()
                        await release.wait()
                    self._fill()
                finally:
                    self.building -= 1

            async def on_load(self):
                if not self.loading:
                    return
                self._enter()
                try:
                    self.clear_items()
                    await asyncio.sleep(0)
                    self._fill()
                finally:
                    self.building -= 1

            async def on_state_changed(self, state):
                self.spawned = asyncio.create_task(self.region.show_page(1))
                await asyncio.wait_for(parked.wait(), 2)

        host = _Spawning(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()
        await asyncio.wait_for(host.dispatch("JUMP"), 2)
        host.loading = True
        reloading = asyncio.create_task(host.reload())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not reloading.done(), "the reload did not wait for the page turn"
        release.set()
        await asyncio.wait_for(asyncio.gather(host.spawned, reloading), 2)

        assert host.most == 1

    @pytest.mark.parametrize("nested", ["reload", "gathered_reload", "gathered_turn"])
    async def test_a_page_turn_that_needs_the_turn_while_a_reload_waits_finishes(self, nested):
        """A turn the state render started without awaiting outlived it, a
        reload began waiting for it, and the turn's build then needed the
        turn itself: by its own reload(), a reload() it gathered, or a second
        region's page turn it gathered. The reload held the lock while it
        waited, so neither ever finished and the panel stopped changing."""
        parked, release = asyncio.Event(), asyncio.Event()

        class _Host(RenderableLayoutView):
            subscribed_actions = {"JUMP"}

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.a = PaginatedRegion(items=list(range(9)), per_page=3, key="a")
                self.b = PaginatedRegion(items=list(range(9)), per_page=3, key="b")
                self.spawned = None
                self.follow = False
                self.events = []
                self._fill()

            def _fill(self):
                for region in (self.a, self.b):
                    for i in region.page_items:
                        self.add_item(TextDisplay(f"row {i}"))
                    for item in region.controls(self):
                        self.add_item(item)

            async def on_load(self):
                self.events.append(("load", asyncio.current_task().get_name()))

            async def build_ui(self):
                self.clear_items()
                self._fill()
                if self.follow:
                    self.follow = False
                    parked.set()
                    await release.wait()
                    if nested == "reload":
                        await self.reload()
                    elif nested == "gathered_reload":
                        await asyncio.gather(self.reload())
                    else:
                        await asyncio.gather(self.b.show_page(2))
                    self.events.append("turn built")

            async def on_state_changed(self, state):
                self.follow = True
                self.spawned = asyncio.create_task(self.a.show_page(1))
                await asyncio.wait_for(parked.wait(), 2)

        host = _Host(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()
        await asyncio.wait_for(host.dispatch("JUMP"), 2)
        reloading = asyncio.create_task(host.reload(), name="taker")
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        _, pending = await asyncio.wait({host.spawned, reloading}, timeout=3)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=2)

        assert not pending, "the page turn and the reload waited on each other"
        # The waiting reload loads only once the page turn's build is done.
        assert host.events.index(("load", "taker")) > host.events.index("turn built")

    async def test_a_page_turn_queued_behind_a_reload_builds_before_it(self):
        """A load started a page turn without awaiting it, a reload queued
        behind that load, and the page turn's build then queued behind the
        reload for a reload of its own. The waiting reload took the turn as
        though the page turn were idle and loaded while that build sat half
        done."""
        parked, release, finish_load = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class _Host(RenderableLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.a = PaginatedRegion(items=list(range(9)), per_page=3, key="a")
                self.spawned = None
                self.spawn = self.follow = False
                self.events = []
                self._fill()

            def _fill(self):
                for i in self.a.page_items:
                    self.add_item(TextDisplay(f"row {i}"))
                for item in self.a.controls(self):
                    self.add_item(item)

            async def on_load(self):
                self.events.append(("load", asyncio.current_task().get_name()))
                if self.spawn:
                    self.spawn = False
                    self.follow = True
                    self.spawned = asyncio.create_task(self.a.show_page(1))
                    await asyncio.wait_for(parked.wait(), 2)
                    await finish_load.wait()

            async def build_ui(self):
                self.clear_items()
                self._fill()
                if self.follow:
                    self.follow = False
                    parked.set()
                    await release.wait()
                    await self.reload()
                    self.events.append("turn built")

        host = _Host(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()
        host.spawn = True
        first = asyncio.create_task(host.reload(), name="first")
        await asyncio.wait_for(parked.wait(), 2)
        taker = asyncio.create_task(host.reload(), name="taker")
        for _ in range(10):
            await asyncio.sleep(0)
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        finish_load.set()
        _, pending = await asyncio.wait({first, taker, host.spawned}, timeout=3)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=2)

        assert not pending, "the page turn and the reload waited on each other"
        assert host.events.index(("load", "taker")) > host.events.index("turn built")

    async def test_two_page_turns_that_each_need_the_turn_both_finish(self):
        """A load started two regions' page turns without awaiting them, and
        each turn's build called reload() while the load still held the turn.
        Each waiting turn counted the other as still building, so both waited
        for the other to leave and the panel never changed again."""
        from cascadeui.views import base as base_module

        class _Host(RenderableLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.regions = {
                    key: PaginatedRegion(items=list(range(9)), per_page=3, key=key) for key in "ab"
                }
                self.spawned = []
                self.spawn = False
                self.reloads_owed = 0
                self._fill()

            def _fill(self):
                for key, region in self.regions.items():
                    for i in region.page_items:
                        self.add_item(TextDisplay(f"{key} row {i}"))
                    for item in region.controls(self):
                        self.add_item(item)

            async def build_ui(self):
                self.clear_items()
                self._fill()
                if self.reloads_owed:
                    self.reloads_owed -= 1
                    await self.reload()

            async def on_load(self):
                if not self.spawn:
                    return
                self.spawn = False
                self.reloads_owed = len(self.regions)
                self.spawned = [asyncio.create_task(r.show_page(1)) for r in self.regions.values()]
                # Hold the turn until both builds have queued for it.
                await until(
                    lambda: all(
                        base_module._RELOAD_WAITING.get(task) is self for task in self.spawned
                    ),
                    timeout=2,
                )

        host = _Host(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()
        host.spawn = True
        await asyncio.wait_for(host.reload(), 2)
        _, pending = await asyncio.wait(set(host.spawned), timeout=3)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=2)

        assert not pending, "the two page turns waited on each other"
        assert [region.page for region in host.regions.values()] == [1, 1]

    async def test_a_page_turn_the_render_did_not_await_holds_the_next_render(self):
        """A state render arriving while that turn built ran beside it, and
        ran again only once the turn ended."""
        parked, release = asyncio.Event(), asyncio.Event()

        class _Spawning(RenderableLayoutView):
            subscribed_actions = {"JUMP", "PING"}

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.region = PaginatedRegion(items=list(range(9)), per_page=3, key="r")
                self.building = self.most = self.renders = 0
                self.spawned = None
                self._fill()

            def _fill(self):
                for i in self.region.page_items:
                    self.add_item(TextDisplay(f"row {i}"))
                for item in self.region.controls(self):
                    self.add_item(item)

            async def build_ui(self):
                self.building += 1
                self.most = max(self.most, self.building)
                try:
                    self.clear_items()
                    if self.spawned is not None and not parked.is_set():
                        parked.set()
                        await release.wait()
                    self._fill()
                finally:
                    self.building -= 1

            async def on_state_changed(self, state):
                self.renders += 1
                if self.spawned is None:
                    self.spawned = asyncio.create_task(self.region.show_page(1))
                    await asyncio.wait_for(parked.wait(), 2)
                    return
                await self.build_ui()
                await self.refresh()

        host = _Spawning(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()
        await asyncio.wait_for(host.dispatch("JUMP"), 2)
        await asyncio.wait_for(host.dispatch("PING"), 2)
        for _ in range(20):
            await asyncio.sleep(0)
        assert host.renders == 1, "a state render ran while the page turn built"
        release.set()
        await asyncio.wait_for(host.spawned, 2)
        await until(lambda: host.renders == 2)

        assert host.most == 1

    async def test_a_page_turn_awaited_inside_another_ones_build_does_not_wait_for_it(self):
        """Joined turns take turns, and one a joined build itself awaits runs
        inside that build rather than waiting for it."""

        class _Nested(RenderableLayoutView):
            subscribed_actions = {"JUMP"}

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.a = PaginatedRegion(items=list(range(9)), per_page=3, key="a")
                self.b = PaginatedRegion(items=list(range(9)), per_page=3, key="b")
                self.follow = False

            def build_ui(self):
                self.clear_items()
                for region in (self.a, self.b):
                    for item in region.controls(self):
                        self.add_item(item)

            async def on_state_changed(self, state):
                self.follow = True
                await asyncio.gather(self.a.show_page(1))

            async def on_page_changed_a(self):
                if self.follow:
                    self.follow = False
                    await asyncio.gather(self.b.show_page(2))

        host = _Nested(interaction=make_interaction(), user_id=1, guild_id=2)
        host.build_ui()
        await host.send()
        original = host.build_ui

        async def build_then_follow():
            original()
            await host.on_page_changed_a()

        host.build_ui = build_then_follow

        await asyncio.wait_for(host.dispatch("JUMP"), 2)

        assert (host.a.page, host.b.page) == (1, 2)

    @pytest.mark.parametrize("spawn", ["await", "gather"])
    async def test_a_page_turn_inside_a_load_hosts_own_load_raises(self, spawn):
        """A host that renders in on_load would have to load again to show the
        page: a direct call raised reload()'s message naming neither the
        region nor the fix, and one through gather() waited forever."""

        class _LoadHost(RenderableLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.region = PaginatedRegion(
                    items=[f"row {i}" for i in range(9)], per_page=3, key="r"
                )
                self.jump = False

            async def on_load(self):
                self.clear_items()
                for row in self.region.page_items:
                    self.add_item(TextDisplay(row))
                for item in self.region.controls(self):
                    self.add_item(item)
                if self.jump:
                    self.jump = False
                    turn = self.region.show_page(2)
                    await (turn if spawn == "await" else asyncio.gather(turn))

        host = _LoadHost(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.send()
        host.jump = True

        with pytest.raises(RuntimeError, match=r"inside the on_load\(\).*set_page\(\)"):
            await asyncio.wait_for(host.reload(), 2)
        assert host.region.page == 0

    async def test_a_page_turn_queued_behind_a_load_skips_a_host_that_closed(self):
        host = _LoadingHost(interaction=make_interaction(), user_id=1, guild_id=2)
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        region.controls(host)
        host.composed.clear()
        load = asyncio.create_task(host.load())
        await asyncio.wait_for(host.loading.wait(), 2)
        turning = asyncio.create_task(region.show_page(1))
        for _ in range(10):
            await asyncio.sleep(0)

        await host.exit(delete_message=False)
        host.release.set()
        await asyncio.wait_for(asyncio.gather(load, turning), 2)

        assert host.composed == []

    async def test_a_toggle_whose_build_outlasts_a_close_ships_it_disabled(self):
        """The close froze the half-built tree while an async build awaited.
        The finished tree goes out after it with the controls disabled, as a
        tab or step render's does."""
        building, release = asyncio.Event(), asyncio.Event()

        class _Host(RenderableLayoutView):
            slow = False

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.more = Collapsible(label="More", reveal=lambda: TextDisplay("details"))

            async def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("panel"))
                if self.slow:
                    building.set()
                    await release.wait()
                for item in self.more.render(self):
                    self.add_item(item)

        host = _Host(interaction=make_interaction(), user_id=1, guild_id=2)
        await host.build_ui()
        host._message = MagicMock(id=42)
        host._message.edit = AsyncMock(return_value=host._message)
        host.slow = True
        toggling = asyncio.create_task(host.more._toggle(make_interaction()))
        await asyncio.wait_for(building.wait(), 2)

        await host.exit(delete_message=False)
        shipped = []

        def edit(**kwargs):
            tree = list(kwargs["view"].walk_children())
            shipped.append(
                (
                    [c.content for c in tree if isinstance(c, TextDisplay)],
                    [b.disabled for b in tree if isinstance(b, discord.ui.Button)],
                )
            )
            return host._message

        host._message.edit.side_effect = edit
        release.set()
        await asyncio.wait_for(toggling, 2)

        assert shipped == [(["panel", "details"], [True])]

    async def test_step_next_advances_and_rerenders(self):
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        host = _FakeHost()
        region.controls(host)
        await region._make_step(1)(make_interaction())
        assert region.page == 1
        assert host.build_calls == 1
        assert host.refresh_calls == 1

    async def test_show_page_still_renders_for_a_host_in_its_timeout_window(self):
        """discord.py stops a view before calling ``on_timeout``.

        ``show_page`` is the programmatic jump a host reaches for from
        ``on_timeout`` ("land on the last page as this closes"), so the
        render probe has to read teardown rather than the stopped future.
        """
        builds = []

        class _Host(RenderableLayoutView):
            def build_ui(self):
                builds.append(1)

        host = _Host(interaction=make_interaction(), user_id=1, guild_id=2)
        host._message = AsyncMock()
        region = PaginatedRegion(per_page=2, items=list(range(6)))
        region.controls(host)
        host.stop()
        builds.clear()

        await region.show_page(2)

        assert region.page == 2
        assert builds == [1]

    async def test_show_page_before_the_region_has_a_host_raises_and_moves_nothing(self):
        calls = []

        class Tracked(PaginatedRegion):
            async def on_page_changed(self, page):
                calls.append(page)

        region = Tracked(per_page=2, items=list(range(6)))

        with pytest.raises(RuntimeError, match=r"set_page\(index\)"):
            await region.show_page(2)
        assert region.page == 0
        assert calls == []

    async def test_show_page_without_notify_renders_without_the_hook(self):
        calls = []

        class Tracked(PaginatedRegion):
            async def on_page_changed(self, page):
                calls.append(page)

        region = Tracked(per_page=2, items=list(range(6)))
        host = _FakeHost()
        region.controls(host)

        await region.show_page(2, notify=False)
        assert calls == []
        assert region.page == 2
        assert host.refresh_calls == 1

        await region.show_page(1)
        assert calls == [1]

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

    async def test_the_last_button_lands_on_the_page_the_render_loads(self):
        """Resolving at click time is one render too early on a reloading host.

        The button names the last page, then the re-render reloads the host,
        which is where a grown list arrives. Resolved against the count in
        hand, the click lands one page short of the row that prompted it and
        the nav row reports there is a page after this one.
        """

        class _GrowingHost:
            def __init__(self, region, source):
                self.region, self.source = region, source

            def build_ui(self):
                self.region.items = list(self.source)

            async def refresh(self, **kwargs):
                return None

            def is_finished(self):
                return False

        source = list(range(25))
        region = PaginatedRegion(per_page=5, items=list(source))
        rows = region.controls(_GrowingHost(region, source))
        last_button = next(
            child
            for row in rows
            for child in row.children
            if (getattr(child, "custom_id", "") or "").endswith("_last")
        )

        source[:] = list(range(26))  # crosses the per_page boundary
        await last_button.callback(make_interaction())

        assert region.page == region.page_count - 1 == 5
        assert 25 in region.page_items  # the row that prompted the jump

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
        assert len(host.answered) == 1

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
        assert len(host.answered) == 1

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

    async def test_goto_answers_the_submission_with_the_page(self):
        """The region's go-to answered the submission, then edited the host's
        message through the channel endpoint, which is not ordered with the
        next click's edit."""
        region = PaginatedRegion(per_page=2, items=list(range(10)))

        class _Host(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay(f"rows {region.page_items}"))
                for row in region.controls(self):
                    self.add_item(row)

        host = _Host(interaction=make_interaction())
        host.build_ui()
        host._message = MagicMock()
        host._message.edit = AsyncMock()
        opening = make_interaction()
        await region._open_goto_modal(opening)
        modal = opening.response.send_modal.call_args.args[0]
        modal.page_input._value = "3"
        submit = make_interaction(message=host._message)
        submit.type = discord.InteractionType.modal_submit

        await modal.on_submit(submit)

        assert region.page == 2
        submit.response.edit_message.assert_awaited_once()
        assert "rows [4, 5]" in [
            c.content for c in host.walk_children() if isinstance(c, TextDisplay)
        ]
        submit.response.defer.assert_not_awaited()
        host._message.edit.assert_not_awaited()

    async def test_goto_submit_after_the_host_closed_says_so_and_stays(self):
        """The modal outlives the host; a page typed after it closed turned
        nothing and said nothing."""
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        host = RenderableLayoutView(interaction=make_interaction())
        region.controls(host)
        opening = make_interaction()
        await region._open_goto_modal(opening)
        modal = opening.response.send_modal.call_args.args[0]
        host.stop()
        modal.page_input._value = "3"
        submit = make_interaction()

        await modal.on_submit(submit)

        assert region.page == 0
        submit.response.send_message.assert_awaited_once()
        assert submit.response.send_message.call_args.args[0] == "This session has ended."

    async def test_goto_submit_after_the_host_closed_is_answered_when_silent(self):
        """The go-to modal is a raw discord.py Modal with no trailing ack, so
        a notice set to None left the submission unanswered."""
        region = PaginatedRegion(per_page=2, items=list(range(10)))
        host = RenderableLayoutView(interaction=make_interaction())
        host.set_class_attribute("session_ended_message", None)
        region.controls(host)
        opening = make_interaction()
        await region._open_goto_modal(opening)
        modal = opening.response.send_modal.call_args.args[0]
        host.stop()
        modal.page_input._value = "3"
        submit = make_interaction()

        await modal.on_submit(submit)

        assert region.page == 0
        submit.response.send_message.assert_not_awaited()
        submit.response.defer.assert_awaited_once()


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

    def test_one_param_on_select_refused(self):
        with pytest.raises(
            TypeError, match=r"choice_row: on_select callback .*\(interaction, value\)"
        ):
            choice_row({"a": 1}, on_select=_noop)

    def test_star_args_on_select_accepted(self):
        row = choice_row({"a": 1}, on_select=lambda *args: None)
        assert row is not None

    def test_unreadable_signature_on_select_accepted(self):
        # ``min`` has no readable signature; the refusal fails open rather
        # than rejecting what it cannot inspect.
        row = choice_row({"a": 1}, on_select=min)
        assert row is not None

    def test_non_choice_option_raises(self):
        """A two-item pair is now converted, so the rejection needs an entry
        that carries no label and value at all."""
        with pytest.raises(TypeError, match="mapping, a sequence of Choice"):
            choice_row(["a"], on_select=_noop_select)

    def test_three_item_sequence_entry_raises(self):
        with pytest.raises(TypeError, match="mapping, a sequence of Choice"):
            choice_row([("a", 1, "extra")], on_select=_noop_select)

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


class TestChoiceRowReselect:
    """allow_reselect keeps the active single-select option clickable.

    Default (allow_reselect=False): the active button is disabled and a
    dropdown re-pick of the active value is a no-op, so re-picking never fires
    on_select. allow_reselect=True keeps the active option live in both forms,
    for a control whose callback has a side effect beyond selection.
    """

    def test_default_active_button_disabled(self):
        row = choice_row({"A": 1, "B": 2, "C": 3}, selected=2, on_select=_noop_select)
        _, b, _ = list(row.children)
        assert b.disabled is True

    def test_allow_reselect_active_button_enabled(self):
        row = choice_row(
            {"A": 1, "B": 2, "C": 3}, selected=2, on_select=_noop_select, allow_reselect=True
        )
        a, b, c = list(row.children)
        assert b.disabled is False  # active option stays clickable
        assert a.disabled is False and c.disabled is False
        assert b.style == discord.ButtonStyle.primary  # still highlighted

    def test_allow_reselect_still_honors_whole_control_disable(self):
        # disabled=True overrides allow_reselect: the whole control locks.
        row = choice_row(
            {"A": 1, "B": 2},
            selected=1,
            on_select=_noop_select,
            allow_reselect=True,
            disabled=True,
        )
        assert all(btn.disabled is True for btn in row.children)

    async def test_allow_reselect_active_button_fires_with_active_value(self):
        seen = {}

        async def on_sel(interaction, value):
            seen["v"] = value

        row = choice_row(
            {"A": "alpha", "B": "beta"}, selected="alpha", on_select=on_sel, allow_reselect=True
        )
        active_button = list(row.children)[0]
        # For buttons the reselect contract IS the enabled state -- Discord only
        # delivers clicks to an enabled component, and the callback has no
        # internal active-value guard. Assert it stays clickable (this is what
        # allow_reselect controls), then confirm the active button carries its
        # own value through the closure.
        assert active_button.disabled is False
        await active_button.original_callback(make_interaction())
        assert seen["v"] == "alpha"

    async def test_dropdown_repick_active_is_noop_by_default(self):
        seen = {"fired": False}

        async def on_sel(interaction, value):
            seen["fired"] = True

        # 8 options -> dropdown; value 3 is active, option value "3" is a re-pick.
        row = choice_row({f"O{i}": i for i in range(8)}, selected=3, on_select=on_sel)
        select = list(row.children)[0]
        await select.original_callback(make_interaction(), ["3"])
        assert seen["fired"] is False  # re-pick of active value swallowed

    async def test_dropdown_pick_nonactive_fires_by_default(self):
        seen = {}

        async def on_sel(interaction, value):
            seen["v"] = value

        row = choice_row({f"O{i}": i for i in range(8)}, selected=3, on_select=on_sel)
        select = list(row.children)[0]
        await select.original_callback(make_interaction(), ["5"])
        assert seen["v"] == 5  # a real change still fires

    async def test_dropdown_repick_active_fires_with_allow_reselect(self):
        seen = {}

        async def on_sel(interaction, value):
            seen["v"] = value

        row = choice_row(
            {f"O{i}": i for i in range(8)}, selected=3, on_select=on_sel, allow_reselect=True
        )
        select = list(row.children)[0]
        await select.original_callback(make_interaction(), ["3"])
        assert seen["v"] == 3  # re-pick now fires


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

    def test_reveal_requiring_arguments_refused(self):
        # Unchecked, this constructs and renders collapsed cleanly, then dies
        # on the first expand click with a bare arity TypeError naming
        # neither the kwarg nor the class.
        with pytest.raises(
            TypeError, match=r"reveal callable .* cannot be called with no arguments"
        ):
            Collapsible(label="Edit", reveal=lambda x: [])

    def test_summary_requiring_arguments_refused(self):
        with pytest.raises(
            TypeError, match=r"summary callable .* cannot be called with no arguments"
        ):
            Collapsible(label="Edit", reveal=_reveal_one, summary=lambda x: "s")

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

    def test_summary_given_as_text_renders_that_text(self):
        c = Collapsible(label="Edit", reveal=_reveal_one, summary="Advanced options")
        trigger = c.render(_FakeHost())[0]
        assert isinstance(trigger, Section)
        assert trigger.children[0].content == "Advanced options"

    def test_summary_neither_text_nor_callable_raises(self):
        with pytest.raises(TypeError, match="summary must be a str, a callable, or None"):
            Collapsible(label="Edit", reveal=_reveal_one, summary=5)

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

    async def test_a_toggle_waits_for_a_load_in_progress(self):
        """A toggle built the host's tree beside its load, from half-loaded data."""
        host = _LoadingHost(interaction=make_interaction(), user_id=1, guild_id=2)
        c = Collapsible(label="Edit", reveal=_reveal_one)
        c.render(host)
        host.composed.clear()

        during = await _render_during_a_load(host, lambda: c._toggle(make_interaction()))

        assert during == []
        assert host.composed == [(1, 1)]

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


class TestPairSequenceAcceptance:
    """The {key: value} builders take the same data written as ordered pairs.

    tab_nav resolved ``active`` against the raw argument before normalizing,
    so a pair sequence bound it to a (label, callback) tuple: no tab matched,
    none rendered active, and an explicit active= was rejected for not being
    a key. choice_row advertised the pair form in its annotation and refused
    it in its body.
    """

    @staticmethod
    async def _cb(interaction):
        pass

    @staticmethod
    async def _cb2(interaction, value):
        pass

    def test_tab_nav_pairs_mark_the_first_tab_active(self):
        row = tab_nav([("Overview", self._cb), ("Stats", self._cb)])
        assert [b.style for b in row.children] == [
            discord.ButtonStyle.primary,
            discord.ButtonStyle.secondary,
        ]

    def test_tab_nav_pairs_honour_an_explicit_active(self):
        row = tab_nav([("Overview", self._cb), ("Stats", self._cb)], active="Stats")
        assert [b.style for b in row.children] == [
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.primary,
        ]

    def test_tab_nav_mapping_form_is_unchanged(self):
        row = tab_nav({"Overview": self._cb, "Stats": self._cb}, active="Stats")
        assert [b.style for b in row.children] == [
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.primary,
        ]

    def test_choice_row_accepts_pairs(self):
        row = choice_row([("Easy", 1), ("Hard", 2)], on_select=self._cb2)
        assert [b.label for b in row.children] == ["Easy", "Hard"]

    def test_choice_row_still_accepts_choice_and_mapping(self):
        assert [
            b.label for b in choice_row([Choice(label="A", value=1)], on_select=self._cb2).children
        ] == ["A"]
        assert [b.label for b in choice_row({"X": 1}, on_select=self._cb2).children] == ["X"]

    def test_choice_row_rejects_a_non_pair_entry(self):
        with pytest.raises(TypeError, match="mapping, a sequence of Choice"):
            choice_row(["Easy"], on_select=self._cb2)

    def test_button_row_and_key_value_accept_pairs(self):
        assert [b.label for b in button_row([("Save", self._cb)]).children] == ["Save"]
        assert key_value([("k", "v")]).content == "**k:** v"


class TestCompositeCursorRewindsWhenTheEditNeverLanded:
    """The V2 composites hold their own cursor and repaint through the host.

    Same shape as the paginated/wizard/tab patterns: state moves before the
    host's edit ships, so a dropped edit leaves it pointing somewhere the
    screen never went.
    """

    @staticmethod
    def _host(child, *, kind):
        class Host(RenderableLayoutView):
            def __init__(self, *, child=None, **kw):
                self.child = child
                super().__init__(**kw)

            def build_ui(self):
                self.clear_items()
                items = child.controls(self) if kind == "region" else child.render(self)
                for item in items:
                    self.add_item(item)

        host = Host(child=child, interaction=make_interaction(), user_id=1, guild_id=2)
        host.build_ui()
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock(side_effect=aiohttp.ClientOSError(104, "reset"))
        host._message = message
        return host, message

    async def test_paginated_region_rewinds_its_page(self):
        region = PaginatedRegion(items=list(range(30)), per_page=5)
        _host, message = self._host(region, kind="region")

        await region._make_step(1)(make_interaction())
        assert region.page == 0, "the cursor must stay on the slice still shown"

        message.edit = AsyncMock()
        await region._make_step(1)(make_interaction())
        assert region.page == 1, "the recovery press advances one page, not two"

    async def test_collapsible_rewinds_its_toggle(self):
        collapsible = Collapsible(label="More", reveal=lambda: [TextDisplay("body")], key="c")
        _host, message = self._host(collapsible, kind="collapsible")

        await collapsible._toggle(make_interaction())
        assert collapsible.expanded is False, "the trigger on screen never opened"

        message.edit = AsyncMock()
        await collapsible._toggle(make_interaction())
        assert collapsible.expanded is True

    async def test_paginated_region_rewinds_when_the_edit_is_refused(self):
        region = PaginatedRegion(items=list(range(30)), per_page=5)
        _host, message = self._host(region, kind="region")
        message.edit = AsyncMock(
            side_effect=discord.HTTPException(
                MagicMock(status=400, reason="Bad Request"), "Invalid Form Body"
            )
        )

        with pytest.raises(discord.HTTPException):
            await region._make_step(1)(make_interaction())
        assert region.page == 0, "the cursor must stay on the slice still shown"

        message.edit = AsyncMock()
        await region._make_step(1)(make_interaction())
        assert region.page == 1, "the recovery press advances one page, not two"

    async def test_collapsible_rewinds_when_its_reveal_raises(self):
        def broken():
            raise RuntimeError("database unavailable")

        collapsible = Collapsible(label="More", reveal=broken, key="c")
        _host, message = self._host(collapsible, kind="collapsible")
        message.edit = AsyncMock()

        with pytest.raises(RuntimeError, match="database unavailable"):
            await collapsible._toggle(make_interaction())

        assert collapsible.expanded is False, "the trigger on screen never opened"
        message.edit.assert_not_awaited()
