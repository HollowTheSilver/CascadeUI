# // ========================================( Modules )======================================== // #


import pytest
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
from cascadeui.views._placement import validate_placement, validate_unique_custom_ids


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
