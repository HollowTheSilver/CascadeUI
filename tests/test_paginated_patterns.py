"""Tests for PaginatedView / PaginatedLayoutView customization and parity."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ui import ActionRow, Button, Container, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.views.patterns import PaginatedLayoutView, PaginatedView

# // ========================================( Button style validation )======================================== // #


class TestPaginatedStyleValidation:
    """Invalid pagination button styles raise at class definition time."""

    def test_invalid_style_raises_at_definition(self):
        with pytest.raises(ValueError, match="must be a discord.ButtonStyle"):

            class BadPaginated(PaginatedLayoutView):
                next_button_style = "secondary"  # str, not enum

    def test_valid_styles_accepted(self):
        class GoodPaginated(PaginatedLayoutView):
            first_button_style = discord.ButtonStyle.success
            prev_button_style = discord.ButtonStyle.success
            indicator_button_style = discord.ButtonStyle.danger
            next_button_style = discord.ButtonStyle.success
            last_button_style = discord.ButtonStyle.success

        assert GoodPaginated.indicator_button_style is discord.ButtonStyle.danger


# // ========================================( Customization round-trip )======================================== // #


class TestPaginatedLayoutCustomization:
    """Custom labels and styles apply to generated pagination buttons."""

    def test_label_overrides_apply_to_buttons(self):
        class CustomPaginated(PaginatedLayoutView):
            prev_button_label = "Back"
            next_button_label = "Forward"

        view = CustomPaginated(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("A"))], [Container(TextDisplay("B"))]],
        )

        assert view._prev_btn.label == "Back"
        assert view._next_btn.label == "Forward"

    def test_style_override_applies(self):
        class CustomPaginated(PaginatedLayoutView):
            next_button_style = discord.ButtonStyle.success

        view = CustomPaginated(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("A"))], [Container(TextDisplay("B"))]],
        )

        assert view._next_btn.style is discord.ButtonStyle.success


# // ========================================( on_page_changed hook )======================================== // #


class TestOnPageChangedHook:
    """on_page_changed hook fires on navigation and defaults to no-op."""

    async def test_default_hook_is_noop(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("A"))], [Container(TextDisplay("B"))]],
        )
        result = await view.on_page_changed(0)
        assert result is None

    async def test_hook_fires_on_step_callback(self):
        calls = []

        class TrackedPaginated(PaginatedLayoutView):
            async def on_page_changed(self, page):
                calls.append(page)

        view = TrackedPaginated(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
                [Container(TextDisplay("C"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        callback = view._make_step_callback(1)
        await callback(_make_interaction())

        assert calls == [1]
        assert view.current_page == 1


# // ========================================( V2 nav identity + extra_items preservation )======================================== // #


class TestPaginatedLayoutButtonIdentity:
    """V2 variant must mutate nav buttons in place and preserve extra items."""

    async def test_nav_identity_stable_across_page_turn(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
                [Container(TextDisplay("C"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        prev_id = id(view._prev_btn)
        next_id = id(view._next_btn)
        indicator_id = id(view._indicator_btn)
        nav_row_id = id(view._nav_row)

        view.current_page = 1
        await view._update_page()

        assert id(view._prev_btn) == prev_id
        assert id(view._next_btn) == next_id
        assert id(view._indicator_btn) == indicator_id
        assert id(view._nav_row) == nav_row_id

    async def test_extra_items_preserved_across_page_turn(self):
        extra_marker = Container(TextDisplay("EXTRA"))

        class WithExtras(PaginatedLayoutView):
            def _build_extra_items(self_inner):
                self_inner.add_item(extra_marker)

        view = WithExtras(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert extra_marker in view._extra_items

        view.current_page = 1
        await view._update_page()

        # Extra marker must still be a child after page turn.
        assert extra_marker in list(view.children)

    async def test_indicator_label_updates_on_page_turn(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
                [Container(TextDisplay("C"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert view._indicator_btn.label == "Page 1/3"

        view.current_page = 2
        await view._update_page()

        assert view._indicator_btn.label == "Page 3/3"


# // ========================================( Single-page nav suppression )======================================== // #


class TestPaginatedLayoutSinglePage:
    """Single-page views must not render the nav ActionRow."""

    def test_single_page_has_no_nav_row(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("Only"))]],
        )
        assert view._nav_row not in list(view.children)

    def test_empty_pages_has_no_nav_row(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[],
        )
        assert view._nav_row not in list(view.children)

    def test_multi_page_has_nav_row(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
            ],
        )
        assert view._nav_row in list(view.children)

    async def test_rebuild_growing_past_one_page_attaches_nav(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("Only"))]],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert view._nav_row not in list(view.children)

        view.pages = [
            [Container(TextDisplay("A"))],
            [Container(TextDisplay("B"))],
        ]
        await view._update_page()

        assert view._nav_row in list(view.children)

    async def test_rebuild_shrinking_to_one_page_detaches_nav(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert view._nav_row in list(view.children)

        view.pages = [[Container(TextDisplay("Only"))]]
        view.current_page = 0
        await view._update_page()

        assert view._nav_row not in list(view.children)


# // ========================================( nav_inside_container layout )======================================== // #


class TestPaginatedLayoutNavInsideContainer:
    """``nav_inside_container=True`` wraps page content + nav row in a
    single Container so the paginator renders as one cohesive card.
    """

    def test_default_keeps_nav_as_sibling(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [Container(TextDisplay("A"))],
                [Container(TextDisplay("B"))],
            ],
        )
        # Default flag: nav row is a top-level sibling of the page content.
        children = list(view.children)
        assert view._nav_row in children
        assert view.nav_inside_container is False

    def test_flag_wraps_content_and_nav_in_single_container(self):
        class _Wrapped(PaginatedLayoutView):
            nav_inside_container = True

        view = _Wrapped(
            interaction=_make_interaction(),
            pages=[
                [TextDisplay("A")],
                [TextDisplay("B")],
            ],
        )
        children = list(view.children)
        # Exactly one top-level Container that holds both the page text
        # and the nav row -- no separate sibling nav row.
        assert view._nav_row not in children
        wrappers = [c for c in children if isinstance(c, Container)]
        assert len(wrappers) == 1
        wrapper_items = list(wrappers[0].walk_children())
        assert view._nav_row in wrapper_items

    def test_flag_no_op_on_single_page(self):
        """Single-page views never render a nav row, so wrapping has nothing
        to compose -- the page content stays as a normal top-level child.
        """

        class _Wrapped(PaginatedLayoutView):
            nav_inside_container = True

        view = _Wrapped(
            interaction=_make_interaction(),
            pages=[[Container(TextDisplay("Only"))]],
        )
        # No nav row anywhere because there's only one page.
        children = list(view.children)
        assert view._nav_row not in children
        # The single Container in children is the user's page content,
        # not a wrapper added by the flag.
        assert children[0] is view.pages[0][0]

    async def test_flag_preserved_through_page_turns(self):
        class _Wrapped(PaginatedLayoutView):
            nav_inside_container = True

        view = _Wrapped(
            interaction=_make_interaction(),
            pages=[
                [TextDisplay("A")],
                [TextDisplay("B")],
                [TextDisplay("C")],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        # Initial render: wrapped Container exists.
        wrappers = [c for c in view.children if isinstance(c, Container)]
        assert len(wrappers) == 1

        # Turn the page; the wrapping must regenerate.
        view.current_page = 1
        await view._update_page()

        wrappers = [c for c in view.children if isinstance(c, Container)]
        assert len(wrappers) == 1
        assert view._nav_row not in list(view.children)
        assert view._nav_row in list(wrappers[0].walk_children())

    def test_flag_no_container_nesting_when_formatter_returns_container(self):
        """When the formatter returns a single Container per page (e.g. the
        ``card(...)`` builder for per-page accent color), the wrapping path
        builds a fresh Container that adopts the source's children + nav row
        instead of nesting the source Container inside another Container.
        Discord rejects type-17-inside-type-17 with HTTP 400 "Invalid Form
        Body" (the field type validator only accepts (1, 9, 10, 12, 13, 14)).
        """
        import discord

        class _Wrapped(PaginatedLayoutView):
            nav_inside_container = True

        page_a = Container(TextDisplay("A"), accent_color=discord.Color.blue())
        page_b = Container(TextDisplay("B"), accent_color=discord.Color.green())

        view = _Wrapped(
            interaction=_make_interaction(),
            pages=[[page_a], [page_b]],
        )

        children = list(view.children)
        assert len(children) == 1
        wrapper = children[0]

        # The wrapper is fresh, NOT the source Container -- the source must
        # remain pristine in self.pages so revisits do not double-add nav.
        assert wrapper is not page_a

        # Wrapper inherited the source's accent color.
        assert wrapper.accent_color == page_a.accent_color

        # No Container-in-Container in the wrapper subtree.
        for child in wrapper.walk_children():
            assert not isinstance(
                child, Container
            ), f"Container-in-Container detected: {child!r} inside {wrapper!r}"

        # Nav row is inside the wrapper.
        assert view._nav_row in list(wrapper.walk_children())

    async def test_back_button_survives_page_turn(self):
        """``_update_page`` calls ``clear_items()`` to recompose the page
        tree. The auto back button added by ``push()`` (via the navigation
        mixin's ``_add_back_button``) sits as a top-level child and would
        be stripped by the clear; the rebuild path must restore it so the
        user is not stranded inside the pushed view after a page turn.
        """
        from discord.ui import ActionRow

        class _Wrapped(PaginatedLayoutView):
            nav_inside_container = True

        view = _Wrapped(
            interaction=_make_interaction(),
            pages=[
                [TextDisplay("A")],
                [TextDisplay("B")],
                [TextDisplay("C")],
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        # Simulate what _navigate_to does on push: V2 path adds an
        # ActionRow holding the back button. The seam stashes the row
        # on the view as ``_auto_back_item``.
        view._add_back_button()
        back_row = view._auto_back_item

        # Sanity: the back row is currently a top-level child.
        assert back_row in list(view.children)

        # Page turn -> clear_items + rebuild.
        view.current_page = 1
        await view._update_page()

        # The back row must still be in the tree.
        assert back_row in list(
            view.children
        ), "auto back button was stripped by _update_page rebuild"

    async def test_flag_does_not_mutate_source_container_across_page_turns(self):
        """Page turns must not accumulate state on the source Container in
        ``self.pages``. If a previous render mutated the source by appending
        the nav row, revisiting that page would re-append it, producing
        duplicate button ``custom_id``s and tripping Discord's "Component
        custom id cannot be duplicated" reject (HTTP 400).
        """
        import discord

        class _Wrapped(PaginatedLayoutView):
            nav_inside_container = True

        page_a = Container(TextDisplay("A"), accent_color=discord.Color.blue())
        page_b = Container(TextDisplay("B"), accent_color=discord.Color.green())

        view = _Wrapped(
            interaction=_make_interaction(),
            pages=[[page_a], [page_b]],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        # Source pages start with one child each (the TextDisplay).
        assert len(list(page_a.children)) == 1
        assert len(list(page_b.children)) == 1

        # Cycle: 0 -> 1 -> 0 -> 1.
        for target in (1, 0, 1):
            view.current_page = target
            await view._update_page()

        # Source pages must still have exactly one child each.
        assert (
            len(list(page_a.children)) == 1
        ), f"page_a mutated across renders: {list(page_a.children)}"
        assert (
            len(list(page_b.children)) == 1
        ), f"page_b mutated across renders: {list(page_b.children)}"


# // ========================================( V2 Send Kwargs Propagation )======================================== // #


class TestPaginatedLayoutSendKwargsPropagation:
    """V2 ``PaginatedLayoutView.send`` forwards ``file=``/``files=`` to ``super().send()``.

    Sibling of ``TestSendKwargsPropagation`` in ``test_pagination.py``;
    same shape, V2 base path. Patches target the V2 base class
    (``cascadeui.views.layout.StatefulLayoutView.send``) for the same
    reason: the override calls ``super().send()`` and MRO resolves to
    the base, so the base is the only seam where forwarded kwargs land.
    """

    def _make_page(self, label: str):
        return Container(TextDisplay(label))

    async def test_files_forwarded_to_super_send(self):
        """``files=`` reaches the V2 base ``send`` call alongside paginator kwargs."""
        view = PaginatedLayoutView(
            pages=[self._make_page("A"), self._make_page("B")],
            interaction=_make_interaction(),
        )
        photo = MagicMock(spec=discord.File)

        with patch(
            "cascadeui.views.layout.StatefulLayoutView.send",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_super_send:
            await view.send(files=[photo])

        mock_super_send.assert_called_once()
        assert mock_super_send.call_args.kwargs["files"] == [photo]

    async def test_single_file_forwarded_to_super_send(self):
        """``file=`` (singular) reaches the V2 base ``send`` call."""
        view = PaginatedLayoutView(
            pages=[self._make_page("A"), self._make_page("B")],
            interaction=_make_interaction(),
        )
        photo = MagicMock(spec=discord.File)

        with patch(
            "cascadeui.views.layout.StatefulLayoutView.send",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_super_send:
            await view.send(file=photo)

        assert mock_super_send.call_args.kwargs["file"] is photo
