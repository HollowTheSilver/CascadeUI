# // ========================================( Modules )======================================== // #


import pytest
from discord import SelectOption, UnfurledMediaItem
from discord.ui import (
    ActionRow,
    Button,
    Checkbox,
    CheckboxGroup,
    Container,
    File,
    FileUpload,
    Label,
    LayoutView,
    MediaGallery,
    RadioGroup,
    Section,
    Select,
    Separator,
    TextDisplay,
    Thumbnail,
    UserSelect,
    View,
)
from helpers import make_interaction

from cascadeui import (
    StatefulButton,
    StatefulLayoutView,
    StatefulView,
    action_section,
    alert,
    button_row,
    card,
    confirm_section,
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
    toggle_section,
)
from cascadeui.views._placement import (
    validate_placement,
    validate_unique_custom_ids,
    validate_unique_ids,
)


async def _noop(interaction):
    pass


def _view_with(*items):
    """Build a bare LayoutView holding the given items at top level."""
    v = LayoutView()
    for item in items:
        v.add_item(item)
    return v


# // ========================================( Top-Level Rejections )======================================== // #


class TestTopLevelRejections:
    """LayoutView top-level children must be Container / Section / TextDisplay /
    MediaGallery / File / Separator / ActionRow."""

    def test_empty_top_level_view_rejected(self):
        # A components-v2 message needs >= 1 top-level component; the empty
        # tree is the one shape the per-child walk cannot reach.
        v = _view_with()
        with pytest.raises(ValueError, match="no top-level components"):
            validate_placement(v)

    def test_standalone_button_at_top_level_rejected(self):
        v = _view_with(Button(custom_id="b", label="L"))
        with pytest.raises(ValueError, match="Button cannot be a child of LayoutView"):
            validate_placement(v)

    def test_standalone_select_at_top_level_rejected(self):
        v = _view_with(Select(custom_id="s", placeholder="p"))
        with pytest.raises(ValueError, match="Select cannot be a child of LayoutView"):
            validate_placement(v)

    def test_standalone_user_select_at_top_level_rejected(self):
        v = _view_with(UserSelect(custom_id="us"))
        with pytest.raises(ValueError, match="UserSelect cannot be a child of LayoutView"):
            validate_placement(v)

    def test_standalone_thumbnail_at_top_level_rejected(self):
        v = _view_with(Thumbnail(media="https://e.com/a.png"))
        with pytest.raises(ValueError, match="Thumbnail cannot be a child of LayoutView"):
            validate_placement(v)


# // ========================================( Container Rejections )======================================== // #


class TestContainerRejections:
    """Container children must be ActionRow / TextDisplay / Section /
    MediaGallery / File / Separator. No nesting, no standalone interactive."""

    def test_container_nesting_rejected(self):
        outer = Container(Container(TextDisplay("inner")))
        v = _view_with(outer)
        with pytest.raises(ValueError, match="Container cannot be a child of Container"):
            validate_placement(v)

    def test_container_nesting_path_includes_indices(self):
        outer = Container(TextDisplay("first"), Container(TextDisplay("second")))
        v = _view_with(outer)
        with pytest.raises(ValueError, match=r"Container\[1\]"):
            validate_placement(v)

    def test_button_in_container_rejected(self):
        v = _view_with(Container(Button(custom_id="b", label="L")))
        with pytest.raises(ValueError, match="Button cannot be a child of Container"):
            validate_placement(v)

    def test_select_in_container_rejected(self):
        v = _view_with(Container(Select(custom_id="s", placeholder="p")))
        with pytest.raises(ValueError, match="Select cannot be a child of Container"):
            validate_placement(v)

    def test_thumbnail_in_container_rejected(self):
        v = _view_with(Container(Thumbnail(media="https://e.com/a.png")))
        with pytest.raises(ValueError, match="Thumbnail cannot be a child of Container"):
            validate_placement(v)


# // ========================================( Section Rejections )======================================== // #


class TestSectionRejections:
    """Section children must all be TextDisplay; accessory must be Button or
    Thumbnail. Section nested inside Section is also rejected."""

    def test_section_select_accessory_rejected(self):
        s = Section(TextDisplay("hi"), accessory=Select(custom_id="s", placeholder="p"))
        v = _view_with(Container(s))
        with pytest.raises(ValueError, match="Select cannot be a child of Section.accessory"):
            validate_placement(v)

    def test_section_textdisplay_accessory_rejected(self):
        s = Section(TextDisplay("hi"), accessory=TextDisplay("right"))
        v = _view_with(Container(s))
        with pytest.raises(ValueError, match="TextDisplay cannot be a child of Section.accessory"):
            validate_placement(v)

    def test_section_container_accessory_rejected(self):
        s = Section(TextDisplay("hi"), accessory=Container(TextDisplay("inner")))
        v = _view_with(Container(s))
        with pytest.raises(ValueError, match="Container cannot be a child of Section.accessory"):
            validate_placement(v)

    def test_section_button_child_rejected(self):
        # Section's left column should only hold TextDisplay.
        s = Section(
            Button(custom_id="b", label="L"),
            accessory=Thumbnail(media="https://e.com/a.png"),
        )
        v = _view_with(Container(s))
        with pytest.raises(ValueError, match="Button cannot be a child of Section.children"):
            validate_placement(v)

    def test_section_at_top_level_with_bad_accessory_rejected(self):
        # The Container wrapper is not required for Section to surface.
        s = Section(TextDisplay("hi"), accessory=Select(custom_id="s", placeholder="p"))
        v = _view_with(s)
        with pytest.raises(ValueError, match="Section.accessory"):
            validate_placement(v)

    def test_section_in_section_rejected(self):
        # Section nested inside Section -- Discord rejects at HTTP send.
        inner = Section(TextDisplay("inner"), accessory=Button(custom_id="b1", label="L1"))
        outer = Section(inner, accessory=Thumbnail(media="https://e.com/a.png"))
        v = _view_with(Container(outer))
        with pytest.raises(ValueError, match="Section cannot be a child of Section"):
            validate_placement(v)


# // ========================================( Modal-Only Component Rejections )======================================== // #


class TestModalOnlyTypeRejections:
    """Label, RadioGroup, CheckboxGroup, Checkbox, FileUpload belong inside a
    Modal. Adding them to a LayoutView or Container triggers Discord HTTP 400."""

    def test_label_at_top_level_rejected(self):
        v = _view_with(Label(text="Field", component=Checkbox(custom_id="cb")))
        with pytest.raises(ValueError, match="Label is a Modal-only component"):
            validate_placement(v)

    def test_radiogroup_at_top_level_rejected(self):
        v = _view_with(RadioGroup(options=[]))
        with pytest.raises(ValueError, match="RadioGroup is a Modal-only component"):
            validate_placement(v)

    def test_checkboxgroup_at_top_level_rejected(self):
        v = _view_with(CheckboxGroup(options=[]))
        with pytest.raises(ValueError, match="CheckboxGroup is a Modal-only component"):
            validate_placement(v)

    def test_checkbox_at_top_level_rejected(self):
        v = _view_with(Checkbox(custom_id="cb"))
        with pytest.raises(ValueError, match="Checkbox is a Modal-only component"):
            validate_placement(v)

    def test_fileupload_at_top_level_rejected(self):
        v = _view_with(FileUpload())
        with pytest.raises(ValueError, match="FileUpload is a Modal-only component"):
            validate_placement(v)

    def test_label_in_container_rejected(self):
        v = _view_with(Container(Label(text="Field", component=Checkbox(custom_id="cb2"))))
        with pytest.raises(ValueError, match="Label is a Modal-only component"):
            validate_placement(v)

    def test_radiogroup_in_container_rejected(self):
        v = _view_with(Container(RadioGroup(options=[])))
        with pytest.raises(ValueError, match="RadioGroup is a Modal-only component"):
            validate_placement(v)


# // ========================================( ActionRow Rejections )======================================== // #


class TestActionRowRejections:
    """ActionRow children must be Button or Select."""

    def test_textdisplay_in_actionrow_rejected(self):
        v = _view_with(ActionRow(TextDisplay("hi")))
        with pytest.raises(ValueError, match="TextDisplay cannot be a child of ActionRow"):
            validate_placement(v)

    def test_section_in_actionrow_rejected(self):
        s = Section(TextDisplay("hi"), accessory=Button(custom_id="b", label="L"))
        v = _view_with(ActionRow(s))
        with pytest.raises(ValueError, match="Section cannot be a child of ActionRow"):
            validate_placement(v)

    def test_thumbnail_in_actionrow_rejected(self):
        v = _view_with(ActionRow(Thumbnail(media="https://e.com/a.png")))
        with pytest.raises(ValueError, match="Thumbnail cannot be a child of ActionRow"):
            validate_placement(v)


# // ========================================( Negative Tests: Builders Pass Clean )======================================== // #


class TestTextDisplaySize:
    """TextDisplay content over Discord's 4000-char cap is caught pre-flight.

    discord.py stores content as a plain string with no length check, so an
    oversized body used to pass the validator and 400 at send. The validator
    already enforced the parallel MediaGallery cap but skipped this one.
    """

    def test_oversized_top_level_rejected(self):
        v = _view_with(TextDisplay("x" * 4001))
        with pytest.raises(ValueError, match="4000-character cap"):
            validate_placement(v)

    def test_oversized_container_child_rejected(self):
        v = _view_with(Container(TextDisplay("y" * 5000)))
        with pytest.raises(ValueError, match="TextDisplay content"):
            validate_placement(v)

    def test_oversized_section_child_rejected(self):
        v = _view_with(Container(Section(TextDisplay("z" * 4001), accessory=Thumbnail("u"))))
        with pytest.raises(ValueError, match="TextDisplay content"):
            validate_placement(v)

    def test_exactly_at_cap_passes(self):
        v = _view_with(TextDisplay("a" * 4000))
        validate_placement(v)


class TestButtonLabelSize:
    """Button label over Discord's 80-char cap is caught pre-flight.

    discord.py stores label as a plain string with no length check, so an
    oversized label used to pass the validator and 400 at send. Buttons are
    visited at two seams: ActionRow children and Section accessories.
    """

    def test_oversized_label_in_actionrow_rejected(self):
        v = _view_with(ActionRow(Button(custom_id="b", label="L" * 81)))
        with pytest.raises(ValueError, match="Button label is 81 characters"):
            validate_placement(v)

    def test_oversized_label_as_section_accessory_rejected(self):
        s = Section(TextDisplay("hi"), accessory=Button(custom_id="b", label="Z" * 90))
        v = _view_with(Container(s))
        with pytest.raises(ValueError, match="80-character cap"):
            validate_placement(v)

    def test_exactly_at_cap_passes(self):
        v = _view_with(ActionRow(Button(custom_id="b", label="L" * 80)))
        validate_placement(v)


class TestButtonUrlSize:
    """Link-button url over Discord's 512-char cap is caught pre-flight.

    discord.py stores ``url`` unchecked, same as the label, so an
    oversized link URL constructs cleanly and 400s at send.
    """

    def test_oversized_url_rejected(self):
        v = _view_with(ActionRow(Button(label="go", url="https://e.com/" + "x" * 500)))
        with pytest.raises(ValueError, match="Button url is 514 characters"):
            validate_placement(v)

    def test_oversized_url_as_section_accessory_rejected(self):
        s = Section(
            TextDisplay("hi"), accessory=Button(label="go", url="https://e.com/" + "x" * 500)
        )
        v = _view_with(Container(s))
        with pytest.raises(ValueError, match="512-character cap"):
            validate_placement(v)

    def test_exactly_at_cap_passes(self):
        base = "https://e.com/"
        v = _view_with(ActionRow(Button(label="go", url=base + "x" * (512 - len(base)))))
        validate_placement(v)


class TestEmptyButtonUrl:
    """An empty link-button url is rejected on the media-reference reasoning.

    A URL is machine-consumed, so a blank one cannot resolve and ships to
    be refused as a form error naming no component. Whitespace-only counts
    as empty, one notch stricter than the text checks.
    """

    def test_empty_url_rejected(self):
        v = _view_with(ActionRow(Button(label="go", url="")))
        with pytest.raises(ValueError, match="Button url is empty"):
            validate_placement(v)

    def test_whitespace_url_rejected_as_section_accessory(self):
        s = Section(TextDisplay("hi"), accessory=Button(label="go", url="   "))
        v = _view_with(s)
        with pytest.raises(ValueError, match="Button url is empty"):
            validate_placement(v)

    def test_urlless_button_passes(self):
        # An ordinary button stores url=None; only a present-but-blank
        # string is the mistake this check exists for.
        v = _view_with(ActionRow(Button(label="go", custom_id="ok")))
        validate_placement(v)


class TestCustomIdLengthCap:
    """An oversized custom_id is caught at both seams that read one.

    The per-node check rides the structural walk (V2 only); the
    uniqueness walk carries its own length check because it is the only
    walk that runs for V1 views and the only one that visits DynamicItem.
    """

    def test_pre_flight_walk_rejects_oversized_custom_id(self):
        v = _view_with(ActionRow(Button(label="go", custom_id="x" * 101)))
        with pytest.raises(ValueError, match="custom_id is 101 characters"):
            validate_placement(v)

    def test_uniqueness_walk_rejects_oversized_custom_id_on_v1(self):
        view = View()
        view.add_item(Button(label="go", custom_id="x" * 101))
        with pytest.raises(ValueError, match="over Discord's 100-character cap"):
            validate_unique_custom_ids(view)

    def test_exactly_at_cap_passes_both_walks(self):
        v = _view_with(ActionRow(Button(label="go", custom_id="x" * 100)))
        validate_placement(v)
        validate_unique_custom_ids(v)


class TestSelectPlaceholderSize:
    """Select placeholder over Discord's 150-char cap is caught pre-flight."""

    def test_oversized_placeholder_rejected(self):
        v = _view_with(ActionRow(Select(custom_id="s", placeholder="p" * 151)))
        with pytest.raises(ValueError, match="Select placeholder is 151 characters"):
            validate_placement(v)

    def test_exactly_at_cap_passes(self):
        v = _view_with(ActionRow(Select(custom_id="s", placeholder="p" * 150)))
        validate_placement(v)

    def test_auto_populated_select_without_options_passes(self):
        # UserSelect exposes no ``.options``; the option walk must not crash.
        v = _view_with(ActionRow(UserSelect(custom_id="us", placeholder="pick")))
        validate_placement(v)


class TestSelectOptionTextSize:
    """SelectOption label / value / description over Discord's 100-char cap are
    each caught pre-flight; discord.py stores all three unchecked."""

    def test_oversized_option_label_rejected(self):
        select = Select(custom_id="s", options=[SelectOption(label="A" * 101, value="v")])
        v = _view_with(ActionRow(select))
        with pytest.raises(ValueError, match="SelectOption label is 101 characters"):
            validate_placement(v)

    def test_oversized_option_value_rejected(self):
        select = Select(custom_id="s", options=[SelectOption(label="A", value="v" * 101)])
        v = _view_with(ActionRow(select))
        with pytest.raises(ValueError, match="SelectOption value is 101 characters"):
            validate_placement(v)

    def test_oversized_option_description_rejected(self):
        select = Select(
            custom_id="s",
            options=[SelectOption(label="A", value="v", description="d" * 101)],
        )
        v = _view_with(ActionRow(select))
        with pytest.raises(ValueError, match="SelectOption description is 101 characters"):
            validate_placement(v)

    def test_oversized_option_path_names_option_index(self):
        select = Select(
            custom_id="s",
            options=[
                SelectOption(label="ok", value="v0"),
                SelectOption(label="B" * 101, value="v1"),
            ],
        )
        v = _view_with(ActionRow(select))
        with pytest.raises(ValueError, match=r"options\[1\]"):
            validate_placement(v)

    def test_all_fields_exactly_at_cap_pass(self):
        select = Select(
            custom_id="s",
            options=[SelectOption(label="A" * 100, value="v" * 100, description="d" * 100)],
        )
        v = _view_with(ActionRow(select))
        validate_placement(v)


class TestBuildersPassClean:
    """Every cascadeui v2 builder produces a tree the validator accepts."""

    def test_card_with_strings(self):
        v = _view_with(card("## Title", "Body line"))
        validate_placement(v)

    def test_card_with_action_section(self):
        v = _view_with(card(action_section("Click", label="Go", callback=_noop)))
        validate_placement(v)

    def test_card_with_image_section(self):
        v = _view_with(card(image_section("Profile", url="https://e.com/a.png")))
        validate_placement(v)

    def test_card_with_link_section(self):
        v = _view_with(card(link_section("Docs", label="Open", url="https://e.com")))
        validate_placement(v)

    def test_card_with_toggle_section(self):
        v = _view_with(card(toggle_section("Module", active=True, callback=_noop)))
        validate_placement(v)

    def test_card_with_confirm_section(self):
        v = _view_with(card(*confirm_section("Sure?", on_confirm=_noop, on_cancel=_noop)))
        validate_placement(v)

    def test_card_with_button_row(self):
        v = _view_with(card(button_row({"A": _noop, "B": _noop})))
        validate_placement(v)

    def test_card_with_tab_nav(self):
        v = _view_with(card(tab_nav({"One": _noop, "Two": _noop})))
        validate_placement(v)

    def test_card_with_gallery(self):
        v = _view_with(card(gallery("https://e.com/a.png", "https://e.com/b.png")))
        validate_placement(v)

    def test_card_with_file_attachment(self):
        v = _view_with(card(file_attachment("attachment://report.pdf")))
        validate_placement(v)

    def test_card_with_separators(self):
        v = _view_with(card(divider(), gap(), divider(large=True)))
        validate_placement(v)

    def test_card_with_key_value_and_progress(self):
        v = _view_with(card(key_value({"Score": 42}), progress_bar(7, 10)))
        validate_placement(v)

    def test_alert(self):
        v = _view_with(alert("Settings saved", level="success"))
        validate_placement(v)

    def test_stats_card(self):
        v = _view_with(stats_card("Server", {"Members": 42}))
        validate_placement(v)

    def test_stateful_button_in_actionrow(self):
        """StatefulButton is a Button subclass; the validator accepts it."""
        row = ActionRow(StatefulButton(label="Click", callback=_noop))
        v = _view_with(Container(row))
        validate_placement(v)


# // ========================================( Path String Format )======================================== // #


class TestPathStrings:
    """Error messages include a readable path through the tree."""

    def test_path_starts_at_view_class(self):
        v = _view_with(Container(Container(TextDisplay("inner"))))
        with pytest.raises(ValueError, match="LayoutView ->"):
            validate_placement(v)

    def test_path_includes_violation_class(self):
        v = _view_with(Container(Button(custom_id="b", label="L")))
        with pytest.raises(ValueError, match=r"Container\[0\] -> Button\[0\]"):
            validate_placement(v)

    def test_path_carries_fix_text(self):
        v = _view_with(Container(Container(TextDisplay("inner"))))
        with pytest.raises(ValueError, match="Containers cannot nest"):
            validate_placement(v)

    def test_error_mentions_discord_400(self):
        v = _view_with(Container(Button(custom_id="b", label="L")))
        with pytest.raises(ValueError, match="HTTP 400"):
            validate_placement(v)

    def test_path_uses_actual_top_level_index(self):
        """Top-level index reflects the offending child's position, not [0]."""
        # Two valid top-level items, then a violation at index 2.
        v = LayoutView()
        v.add_item(TextDisplay("first"))
        v.add_item(TextDisplay("second"))
        v.add_item(Button(custom_id="b", label="L"))
        with pytest.raises(ValueError, match=r"Button\[2\]"):
            validate_placement(v)

    def test_path_uses_actual_container_child_index(self):
        """Container child index reflects the offending child's position."""
        outer = Container(
            TextDisplay("first"),
            TextDisplay("second"),
            Container(TextDisplay("nested at index 2")),
        )
        v = _view_with(outer)
        with pytest.raises(ValueError, match=r"Container\[0\] -> Container\[2\]"):
            validate_placement(v)


# // ========================================( MediaGallery Size Cap )======================================== // #


class TestMediaGallerySizeCap:
    """MediaGallery rejects more than 10 items; discord.py does not enforce."""

    def _gallery_with(self, count: int) -> MediaGallery:
        from discord.components import MediaGalleryItem

        items = [MediaGalleryItem(media=f"https://e.com/{i}.png") for i in range(count)]
        return MediaGallery(*items)

    def test_ten_items_accepted(self):
        v = _view_with(self._gallery_with(10))
        validate_placement(v)

    def test_eleven_items_rejected_at_top_level(self):
        v = _view_with(self._gallery_with(11))
        with pytest.raises(ValueError, match="MediaGallery exceeds Discord's 10-item cap"):
            validate_placement(v)

    def test_eleven_items_rejected_in_container(self):
        v = _view_with(Container(self._gallery_with(11)))
        with pytest.raises(ValueError, match="MediaGallery exceeds Discord's 10-item cap"):
            validate_placement(v)

    def test_oversized_gallery_path_includes_index(self):
        v = _view_with(Container(TextDisplay("first"), self._gallery_with(15)))
        with pytest.raises(ValueError, match=r"MediaGallery\[1\]"):
            validate_placement(v)

    def test_count_in_error_matches_actual(self):
        v = _view_with(self._gallery_with(15))
        with pytest.raises(ValueError, match=r"got 15"):
            validate_placement(v)

    def test_zero_items_rejected_at_top_level(self):
        """Empty MediaGallery (1-10 floor violation) is caught by the validator."""
        v = _view_with(self._gallery_with(0))
        with pytest.raises(ValueError, match="MediaGallery has no items"):
            validate_placement(v)

    def test_zero_items_rejected_in_container(self):
        """Empty MediaGallery nested inside a Container is also caught."""
        v = _view_with(Container(self._gallery_with(0)))
        with pytest.raises(ValueError, match="MediaGallery has no items"):
            validate_placement(v)


class TestMediaDescriptionSize:
    """Thumbnail / MediaGalleryItem description over Discord's 1024-char cap.

    Discord's component reference documents ``description`` on both
    structures as alt text capped at 1024 characters; discord.py stores
    it unchecked, so oversized alt text constructs cleanly and 400s at
    send.
    """

    def test_oversized_thumbnail_description_rejected(self):
        s = Section(
            TextDisplay("hi"),
            accessory=Thumbnail("https://e.com/a.png", description="d" * 1025),
        )
        v = _view_with(Container(s))
        with pytest.raises(ValueError, match="Thumbnail description is 1025 characters"):
            validate_placement(v)

    def test_oversized_gallery_item_description_rejected(self):
        from discord.components import MediaGalleryItem

        g = MediaGallery(MediaGalleryItem("https://e.com/a.png", description="d" * 1025))
        v = _view_with(g)
        with pytest.raises(ValueError, match="MediaGalleryItem description is 1025 characters"):
            validate_placement(v)

    def test_exactly_at_cap_passes(self):
        from discord.components import MediaGalleryItem

        s = Section(
            TextDisplay("hi"),
            accessory=Thumbnail("https://e.com/a.png", description="d" * 1024),
        )
        g = MediaGallery(MediaGalleryItem("https://e.com/a.png", description="d" * 1024))
        validate_placement(_view_with(Container(s), g))


# // ========================================( Container Size Bounds )======================================== // #


class TestContainerSizeBounds:
    """Container must hold at least one child; Discord does not document
    a per-Container child cap (the only documented cap is the message-
    level 40-component recursive total). The library enforces ``min=1``
    conservatively because an empty Container has no content to render.
    """

    def test_empty_container_at_top_level_rejected(self):
        v = _view_with(Container())
        with pytest.raises(ValueError, match="Container has no children"):
            validate_placement(v)

    def test_container_with_one_child_accepted(self):
        v = _view_with(Container(TextDisplay("only")))
        validate_placement(v)

    def test_container_with_ten_children_accepted(self):
        children = [TextDisplay(f"item {i}") for i in range(10)]
        v = _view_with(Container(*children))
        validate_placement(v)

    def test_container_with_twenty_children_accepted(self):
        # Discord does not document a per-Container cap; a 20-child
        # Container ships cleanly as long as the recursive message-level
        # 40-component total is not exceeded (1 Container + 20 leaves = 21).
        children = [TextDisplay(f"item {i}") for i in range(20)]
        v = _view_with(Container(*children))
        validate_placement(v)


# // ========================================( Section Size Bounds )======================================== // #


class TestSectionSizeBounds:
    """Section must hold at least 1 child per Discord's documented contract.

    The ``components`` field is documented as "One to three child
    components". discord.py enforces the upper bound (max 3) at
    construction via ``Section.add_item``, so the validator only
    catches the lower-bound case discord.py allows but Discord
    rejects: a ``Section(accessory=...)`` with zero children.
    """

    def _section_with(self, count: int) -> Section:
        children = [TextDisplay(f"line {i}") for i in range(count)]
        return Section(*children, accessory=Thumbnail(media="https://e.com/x.png"))

    def test_section_with_one_child_accepted(self):
        v = _view_with(self._section_with(1))
        validate_placement(v)

    def test_section_with_three_children_accepted(self):
        v = _view_with(self._section_with(3))
        validate_placement(v)

    def test_empty_section_rejected(self):
        section = Section(accessory=Thumbnail(media="https://e.com/x.png"))
        v = _view_with(section)
        with pytest.raises(ValueError, match="Section has no children"):
            validate_placement(v)


# // ========================================( ActionRow Size Bounds )======================================== // #


class TestActionRowSizeBounds:
    """ActionRow must hold at least one child per Discord's documented contract."""

    def test_empty_actionrow_at_top_level_rejected(self):
        v = _view_with(ActionRow())
        with pytest.raises(ValueError, match="ActionRow has no children"):
            validate_placement(v)

    def test_empty_actionrow_in_container_rejected(self):
        # Wrap in Container so the Container size check passes; the ActionRow
        # size check is the failure under test.
        v = _view_with(Container(TextDisplay("filler"), ActionRow()))
        with pytest.raises(ValueError, match="ActionRow has no children"):
            validate_placement(v)

    def test_actionrow_with_one_button_accepted(self):
        row = ActionRow(Button(custom_id="b", label="L"))
        v = _view_with(Container(row))
        validate_placement(v)

    def test_dynamic_item_in_actionrow_accepted(self):
        """DynamicItem-wrapped components serialize as Button/Select at send.

        DynamicItem is an Item subclass that is NOT a Button or Select
        subclass at the Python level. The validator must let unknown
        Item subclasses pass through ActionRow children so that
        DynamicPersistentButton (CascadeUI's DynamicItem-based persistent
        button) and similar wrappers do not falsely trip rejection.
        """
        from discord.ui import DynamicItem

        class _PassThroughDynamic(DynamicItem[Button], template=r"pt:(?P<x>\d+)"):
            def __init__(self, x: int):
                super().__init__(Button(custom_id=f"pt:{x}", label=f"L{x}"))

        row = ActionRow(_PassThroughDynamic(1))
        v = _view_with(Container(row))
        validate_placement(v)


# // ========================================( ClassVar Validation ) ======================================== // #


class TestValidatePlacementAttribute:
    """``validate_placement`` is validated through ``_BOOL_ATTRS`` at class
    definition time, so typos fail loud."""

    def test_non_bool_value_rejected_at_class_definition(self):
        from cascadeui import StatefulLayoutView

        with pytest.raises(ValueError, match="validate_placement must be a bool"):

            class _Bad(StatefulLayoutView):
                validate_placement = "yes"  # type: ignore[assignment]

    def test_bool_false_accepted_at_class_definition(self):
        from cascadeui import StatefulLayoutView

        class _OK(StatefulLayoutView):
            validate_placement = False

        assert _OK.validate_placement is False


# // ========================================( custom_id Uniqueness )======================================== // #


class TestUniqueCustomIds:
    """validate_unique_custom_ids rejects duplicate custom_ids in one tree."""

    def test_duplicate_buttons_raise(self):
        row = ActionRow()
        row.add_item(Button(label="a", custom_id="dup"))
        row.add_item(Button(label="b", custom_id="dup"))
        with pytest.raises(ValueError, match="Duplicate component custom_id"):
            validate_unique_custom_ids(_view_with(row))

    def test_error_names_the_repeated_id(self):
        row = ActionRow()
        row.add_item(Button(label="a", custom_id="choice"))
        row.add_item(Button(label="b", custom_id="choice"))
        with pytest.raises(ValueError, match="'choice'"):
            validate_unique_custom_ids(_view_with(row))

    def test_distinct_buttons_pass(self):
        row = ActionRow()
        row.add_item(Button(label="a", custom_id="x"))
        row.add_item(Button(label="b", custom_id="y"))
        validate_unique_custom_ids(_view_with(row))

    def test_duplicate_selects_across_rows_raise(self):
        row1 = ActionRow()
        row1.add_item(UserSelect(custom_id="s"))
        row2 = ActionRow()
        row2.add_item(UserSelect(custom_id="s"))
        with pytest.raises(ValueError, match="Duplicate component custom_id"):
            validate_unique_custom_ids(_view_with(row1, row2))

    def test_non_interactive_items_ignored(self):
        # Display items get an injected custom_id once attached, so the
        # detector discriminates by type: two of them never collide.
        validate_unique_custom_ids(_view_with(TextDisplay("a"), TextDisplay("b")))

    def test_v1_flat_view_duplicate_raises(self):
        # V1 View is a flat tree (no ActionRow nesting); walk_children still
        # yields the buttons, so one check covers both versions.
        v = View()
        v.add_item(Button(label="a", custom_id="dup"))
        v.add_item(Button(label="b", custom_id="dup"))
        with pytest.raises(ValueError, match="Duplicate component custom_id"):
            validate_unique_custom_ids(v)

    def test_dynamic_item_duplicate_raises(self):
        # DynamicItem wraps a Button but is not a Button subclass; the
        # detector includes it so two DynamicPersistentButton-style items
        # sharing an inner custom_id are caught, not just native buttons.
        from discord.ui import DynamicItem

        class _Dyn(DynamicItem[Button], template=r"dyn:(?P<x>\d+)"):
            def __init__(self, x: int):
                super().__init__(Button(custom_id=f"dyn:{x}", label=f"L{x}"))

        row1 = ActionRow()
        row1.add_item(_Dyn(1))
        row2 = ActionRow()
        row2.add_item(_Dyn(1))
        with pytest.raises(ValueError, match="Duplicate component custom_id"):
            validate_unique_custom_ids(_view_with(row1, row2))


class TestUniqueComponentIds:
    """validate_unique_ids rejects a duplicate or malformed component ``id``.

    ``id`` is a per-component integer distinct from ``custom_id``: every
    component carries one, not just interactive leaves, and discord.py
    stores whatever it is handed. The walk runs ungated from
    ``_check_placement`` for both V1 and V2 views.
    """

    def test_duplicate_int_id_raises(self):
        v = _view_with(TextDisplay("a", id=5), TextDisplay("b", id=5))
        with pytest.raises(ValueError, match="Duplicate component id: 5"):
            validate_unique_ids(v)

    def test_string_id_raises(self):
        # discord.py's setter stores the string as-is; it would reach
        # Discord unserializable and return an opaque 400.
        t = TextDisplay("a")
        v = _view_with(t)
        t.id = "x7"
        with pytest.raises(ValueError, match="Component id must be an int, got str"):
            validate_unique_ids(v)

    def test_bool_id_raises(self):
        # bool is an int subclass, so it passes a bare isinstance check
        # while serializing as true/false.
        t = TextDisplay("a")
        v = _view_with(t)
        t.id = True
        with pytest.raises(ValueError, match="Component id must be an int, got bool"):
            validate_unique_ids(v)

    def test_zero_id_raises(self):
        # Discord treats 0 as absent, so a caller-supplied 0 never sticks.
        v = _view_with(TextDisplay("a", id=0))
        with pytest.raises(ValueError, match="outside Discord's range"):
            validate_unique_ids(v)

    def test_over_range_id_raises(self):
        v = _view_with(TextDisplay("a", id=2**31))
        with pytest.raises(ValueError, match="outside Discord's range"):
            validate_unique_ids(v)

    def test_distinct_ids_and_unset_ids_pass(self):
        # A Separator with no id contributes nothing to the walk; distinct
        # ints on the rest pass.
        v = _view_with(TextDisplay("a", id=1), Separator(), TextDisplay("b", id=2))
        validate_unique_ids(v)


# // ========================================( custom_id Wiring )======================================== // #


class TestCheckPlacementUniqueIdsWiring:
    """_check_placement runs the uniqueness detector ungated, including V1."""

    async def test_v1_duplicate_blocked_by_check_placement(self):
        from cascadeui import StatefulView

        view = StatefulView(interaction=make_interaction())
        view.add_item(StatefulButton(label="a", custom_id="dup"))
        view.add_item(StatefulButton(label="b", custom_id="dup"))
        with pytest.raises(ValueError, match="Duplicate component custom_id"):
            view._check_placement()

    async def test_send_pipeline_raise_rolls_back_subscriber(self):
        # A dup-id raise at Stage 0b must undo the __init__ subscriber, so a
        # rejected send leaves no leaked subscription (the rollback contract).
        from cascadeui import StatefulView
        from cascadeui.state.singleton import get_store

        store = get_store()
        view = StatefulView(interaction=make_interaction())
        view.add_item(StatefulButton(label="a", custom_id="dup"))
        view.add_item(StatefulButton(label="b", custom_id="dup"))
        assert view.id in store.subscribers
        with pytest.raises(ValueError, match="Duplicate component custom_id"):
            await view.send()
        assert view.id not in store.subscribers


class TestEmptyTextDisplay:
    """An empty TextDisplay fails the whole message, so it is caught pre-flight.

    Discord's text display requires non-empty ``content``. discord.py stores
    it unchecked, so a formatter hook returning ``""`` for one entry sends a
    tree that Discord rejects entirely, every component beside it included.
    """

    def test_empty_content_rejected(self):
        v = _view_with(Container(TextDisplay("")))
        with pytest.raises(ValueError, match="TextDisplay content is empty"):
            validate_placement(v)

    def test_whitespace_content_allowed(self):
        """Only truly empty content is rejected; Discord accepts whitespace."""
        validate_placement(_view_with(Container(TextDisplay(" "))))

    def test_error_names_the_path(self):
        v = _view_with(Container(TextDisplay("fine"), TextDisplay("")))
        with pytest.raises(ValueError, match="Path:"):
            validate_placement(v)


class TestEmptySelectOptionText:
    """SelectOption label and value are required and displayed.

    Same shape as the empty-TextDisplay case: an option built from a
    record whose name field happens to be blank fails the whole select.
    """

    def test_empty_label_rejected(self):
        v = _view_with(
            ActionRow(Select(custom_id="s", options=[SelectOption(label="", value="x")]))
        )
        with pytest.raises(ValueError, match="SelectOption label is empty"):
            validate_placement(v)

    def test_empty_value_rejected(self):
        v = _view_with(
            ActionRow(Select(custom_id="s", options=[SelectOption(label="L", value="")]))
        )
        with pytest.raises(ValueError, match="SelectOption value is empty"):
            validate_placement(v)

    def test_populated_option_passes(self):
        validate_placement(
            _view_with(
                ActionRow(Select(custom_id="s", options=[SelectOption(label="L", value="v")]))
            )
        )


class TestEmptyMediaUrl:
    """An empty media URL fails the whole message, so it is caught pre-flight.

    discord.py normalizes every media assignment to an ``UnfurledMediaItem``
    and stores its ``url`` unvalidated, so an empty reference constructs
    cleanly from a raw primitive or a post-construction mutation. The
    builders reject the same input at construction; the validator is what
    covers trees the builders never saw.
    """

    def test_empty_thumbnail_accessory_rejected(self):
        s = Section(TextDisplay("hi"), accessory=Thumbnail(""))
        v = _view_with(Container(s))
        with pytest.raises(ValueError, match="Thumbnail media URL is empty"):
            validate_placement(v)

    def test_whitespace_thumbnail_accessory_rejected(self):
        # Whitespace-only is rejected along with empty: a URL is
        # machine-consumed, unlike TextDisplay content, where whitespace
        # renders and is allowed.
        s = Section(TextDisplay("hi"), accessory=Thumbnail("   "))
        v = _view_with(s)
        with pytest.raises(ValueError, match="Thumbnail media URL is empty"):
            validate_placement(v)

    def test_thumbnail_error_names_accessory_path(self):
        s = Section(TextDisplay("hi"), accessory=Thumbnail(""))
        v = _view_with(s)
        with pytest.raises(ValueError, match=r"accessory\(Thumbnail\)"):
            validate_placement(v)

    def test_empty_gallery_item_rejected_naming_index(self):
        from discord.components import MediaGalleryItem

        g = MediaGallery(MediaGalleryItem("https://e.com/a.png"), MediaGalleryItem(""))
        v = _view_with(g)
        with pytest.raises(ValueError, match=r"MediaGalleryItem media URL is empty"):
            validate_placement(v)

    def test_empty_gallery_item_path_names_item_index(self):
        from discord.components import MediaGalleryItem

        g = MediaGallery(MediaGalleryItem("https://e.com/a.png"), MediaGalleryItem(""))
        v = _view_with(Container(g))
        with pytest.raises(ValueError, match=r"items\[1\]"):
            validate_placement(v)

    def test_empty_file_media_rejected_at_top_level(self):
        v = _view_with(File(media=""))
        with pytest.raises(ValueError, match="File media URL is empty"):
            validate_placement(v)

    def test_empty_file_media_rejected_in_container(self):
        v = _view_with(Container(TextDisplay("t"), File(media="")))
        with pytest.raises(ValueError, match="File media URL is empty"):
            validate_placement(v)

    def test_mutated_thumbnail_media_rejected(self):
        """A ``media = ""`` mutation lands past every builder guard.

        The setter re-normalizes the string to ``UnfurledMediaItem("")``,
        so a validly built section carries an empty reference by the time
        the ship-seam walk runs; the walk is the only check positioned to
        catch it.
        """
        section = image_section("text", url="https://example.com/img.png")
        section.accessory.media = ""
        v = _view_with(section)
        with pytest.raises(ValueError, match="Thumbnail media URL is empty"):
            validate_placement(v)

    def test_empty_text_child_reported_before_empty_accessory(self):
        # The accessory checks run after the children loop, so a Section
        # carrying both defects names the text first -- fixing violations
        # in reading order instead of ping-ponging.
        s = Section(TextDisplay(""), accessory=Thumbnail(""))
        v = _view_with(s)
        with pytest.raises(ValueError, match="TextDisplay content is empty"):
            validate_placement(v)

    def test_non_string_url_skipped(self):
        # The type is established before the value: a non-string url is not
        # this check's domain, and raising TypeError from a pre-flight check
        # would be worse than the 400 it prevents.
        s = Section(TextDisplay("hi"), accessory=Thumbnail(UnfurledMediaItem(None)))
        validate_placement(_view_with(s))

    def test_populated_media_passes(self):
        from discord.components import MediaGalleryItem

        s = Section(TextDisplay("hi"), accessory=Thumbnail("attachment://a.png"))
        g = MediaGallery(MediaGalleryItem("https://e.com/a.png"))
        validate_placement(_view_with(s, g, File(media="attachment://f.pdf")))


class TestValidationTableMroUnion:
    """Class-attribute tables resolve across the whole MRO.

    Each pattern mixin extends a table by naming ``_StatefulMixin``
    directly, and the mixin precedes the concrete V2 class in the MRO, so
    a plain attribute read returns the mixin's copy and silently drops
    whatever the concrete class added. ``validate_placement`` is declared
    on ``StatefulLayoutView``, which is exactly the position that loses.

    Asserting against ``StatefulLayoutView`` alone cannot see this: that
    class declares the entry itself, so it passes either way. These tests
    target the classes where the bug actually lived.
    """

    @pytest.mark.parametrize(
        "cls_name",
        [
            "LeaderboardLayoutView",
            "MenuLayoutView",
            "PaginatedLayoutView",
            "WizardLayoutView",
            "TabLayoutView",
        ],
    )
    def test_validate_placement_rejected_on_every_pattern_class(self, cls_name):
        import cascadeui

        cls = getattr(cascadeui, cls_name)
        with pytest.raises(ValueError, match="validate_placement must be a bool"):
            type("_Bad", (cls,), {"validate_placement": "yes"})

    def test_falsy_value_cannot_silently_disable_the_validator(self):
        """``validate_placement = 0`` is the shape that shipped: a bool-ish
        value that turns the pre-flight validator off with no error.
        """
        from cascadeui import LeaderboardLayoutView

        with pytest.raises(ValueError, match="validate_placement must be a bool"):
            type("_Off", (LeaderboardLayoutView,), {"validate_placement": 0})

    def test_effective_table_unions_tuple_declarations(self):
        from cascadeui import PaginatedLayoutView

        table = PaginatedLayoutView._effective_table("_BOOL_ATTRS")
        # Contributed by StatefulLayoutView, which the mixin's splat omits.
        assert "validate_placement" in table
        # Contributed by _StatefulMixin, reached through the mixin's splat.
        assert "owner_only" in table

    def test_effective_table_merges_dict_declarations(self):
        """``_ENUM_ATTRS`` maps name -> allowed values, so the merge has to
        carry the values through, not just the keys; otherwise membership
        succeeds and the lookup raises KeyError.
        """
        from cascadeui import LeaderboardLayoutView

        table = LeaderboardLayoutView._effective_table("_ENUM_ATTRS")
        assert table["instance_policy"] == {"reject", "replace"}
        assert "entry_layout" in table

    def test_non_splatting_mixin_still_gets_the_directed_error(self):
        """A mixin that declares its own table without splatting the base is
        the shape that turned a typo into a KeyError.
        """
        from cascadeui import StatefulLayoutView

        class _Mixin:
            _ENUM_ATTRS = {"conc_policy": {"a", "b"}}

        class _View(_Mixin, StatefulLayoutView):
            pass

        with pytest.raises(ValueError, match="instance_policy must be one of"):
            type("_Bad", (_View,), {"instance_policy": "rejct"})


# // ========================================( Class )======================================== // #


class TestPublicValidate:
    """``view.validate()`` is the public entry to the pre-flight checks.

    A consumer testing a view offline needs to build the tree, count it, and
    validate it without a Discord connection. Composing and counting were
    already public (``on_load`` / ``build_ui`` and ``walk_children``);
    validation was not, so a test had to reach ``_check_placement``.
    """

    @staticmethod
    def _cascade_view(*items):
        """A CascadeUI view: ``validate()`` lives on the mixin, not LayoutView."""
        v = StatefulLayoutView(user_id=1, guild_id=2)
        for item in items:
            v.add_item(item)
        return v

    async def test_validate_passes_a_composed_view(self):
        view = self._cascade_view(Container(TextDisplay("Ready")))
        view.validate()

    async def test_validate_rejects_an_empty_text_display(self):
        # Discriminating rather than inert: the same tree with one blank node.
        view = self._cascade_view(Container(TextDisplay("")))
        with pytest.raises(ValueError, match="content is empty"):
            view.validate()

    async def test_validate_covers_v1_uniqueness(self):
        # V1 views never run the structural walk, so this proves validate()
        # is not merely a validate_placement alias.
        async def _cb(interaction):
            pass

        view = StatefulView(user_id=1, guild_id=2)
        view.add_item(StatefulButton(label="A", custom_id="dup", callback=_cb))
        view.add_item(StatefulButton(label="B", custom_id="dup", callback=_cb))
        with pytest.raises(ValueError, match="Duplicate component custom_id"):
            view.validate()

    async def test_validate_delegates_to_check_placement(self):
        # The public entry must stay a thin wrapper; a divergent second
        # implementation is how the two would drift apart.
        calls = []
        view = self._cascade_view(Container(TextDisplay("Ready")))
        view._check_placement = lambda: calls.append(1)
        view.validate()
        assert calls == [1]
