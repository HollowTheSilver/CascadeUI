"""Tests for PaginatedLayoutView (V2 pagination)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ui import ActionRow, Container, LayoutView, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.patterns import PaginatedLayoutView


def _tree_text(view) -> str:
    """Every TextDisplay in the view's tree, joined.

    A page turn is only correct if the body changed, and the edit call
    reports nothing about what the edit carried.
    """
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, TextDisplay))


class TestPaginatedLayoutViewInit:
    """Basic init and inheritance tests."""

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(PaginatedLayoutView, StatefulLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(PaginatedLayoutView, LayoutView)

    def test_init_with_string_pages(self):
        interaction = _make_interaction()
        pages = ["Page 1", "Page 2", "Page 3"]
        view = PaginatedLayoutView(interaction=interaction, pages=pages)

        assert view.current_page == 0
        assert len(view.pages) == 3

    def test_init_with_component_pages(self):
        interaction = _make_interaction()
        pages = [
            [Container(TextDisplay("Page 1"))],
            [Container(TextDisplay("Page 2"))],
        ]
        view = PaginatedLayoutView(interaction=interaction, pages=pages)

        assert len(view.pages) == 2

    def test_has_navigation_buttons(self):
        """View should contain at least one ActionRow with nav buttons."""
        interaction = _make_interaction()
        pages = ["Page 1", "Page 2"]
        view = PaginatedLayoutView(interaction=interaction, pages=pages)

        action_rows = [c for c in view.children if isinstance(c, ActionRow)]
        assert len(action_rows) >= 1

    def test_empty_pages(self):
        interaction = _make_interaction()
        view = PaginatedLayoutView(interaction=interaction, pages=[])

        assert view.current_page == 0


class TestPaginatedLayoutViewNavigation:
    """Page navigation tests."""

    async def test_update_page_changes_content(self):
        interaction = _make_interaction()
        pages = ["Page 1", "Page 2", "Page 3"]
        view = PaginatedLayoutView(interaction=interaction, pages=pages)
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert "Page 1" in _tree_text(view)

        view.current_page = 1
        await view._update_page()

        view._message.edit.assert_called_once()
        # The name promises content. Asserting the edit fired would pass on
        # an _update_page that shipped the same tree it already had.
        rendered = _tree_text(view)
        assert "Page 2" in rendered
        assert "Page 1" not in rendered

    async def test_update_page_rebuilds_view(self):
        interaction = _make_interaction()
        pages = ["Page A", "Page B"]
        view = PaginatedLayoutView(interaction=interaction, pages=pages)
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        initial_children = list(view.children)

        view.current_page = 1
        await view._update_page()

        assert view._message.edit.called
        # The tree is recomposed from scratch, so the old children are gone
        # rather than mutated in place.
        assert view.children != initial_children
        assert "Page B" in _tree_text(view)


class TestPaginatedLayoutViewFromData:
    """from_data classmethod tests."""

    async def test_from_data_creates_view(self):
        items = list(range(25))

        def formatter(chunk):
            text = "\n".join(str(x) for x in chunk)
            return [Container(TextDisplay(text))]

        interaction = _make_interaction()
        view = await PaginatedLayoutView.from_data(
            items=items,
            per_page=10,
            formatter=formatter,
            interaction=interaction,
        )

        assert isinstance(view, PaginatedLayoutView)
        assert len(view.pages) == 3  # 10 + 10 + 5

    async def test_from_data_async_formatter(self):
        items = ["a", "b", "c"]

        async def formatter(chunk):
            return [Container(TextDisplay(", ".join(chunk)))]

        interaction = _make_interaction()
        view = await PaginatedLayoutView.from_data(
            items=items,
            per_page=2,
            formatter=formatter,
            interaction=interaction,
        )

        assert len(view.pages) == 2  # ["a","b"] and ["c"]

    async def test_refresh_data(self):
        items = list(range(20))

        def formatter(chunk):
            return [Container(TextDisplay(str(len(chunk))))]

        interaction = _make_interaction()
        view = await PaginatedLayoutView.from_data(
            items=items, per_page=10, formatter=formatter, interaction=interaction
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        assert len(view.pages) == 2

        await view.refresh_data(list(range(30)))
        assert len(view.pages) == 3

    async def test_refresh_data_clamps_page(self):
        items = list(range(30))

        def formatter(chunk):
            return [Container(TextDisplay("x"))]

        interaction = _make_interaction()
        view = await PaginatedLayoutView.from_data(
            items=items, per_page=10, formatter=formatter, interaction=interaction
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view.current_page = 2  # Last page

        # Shrink data so page 2 no longer exists
        await view.refresh_data(list(range(5)))
        assert view.current_page == 0

    async def test_refresh_data_requires_from_data(self):
        interaction = _make_interaction()
        view = PaginatedLayoutView(interaction=interaction, pages=["A", "B"])

        with pytest.raises(RuntimeError, match="from_data"):
            await view.refresh_data([1, 2, 3])


class TestPaginatedLayoutViewJump:
    """Jump buttons and go-to-page threshold tests."""

    def test_jump_buttons_appear_above_threshold(self):
        interaction = _make_interaction()
        pages = [f"Page {i}" for i in range(10)]
        view = PaginatedLayoutView(interaction=interaction, pages=pages)

        # Find the navigation ActionRow (last one usually)
        nav_row = None
        for child in view.children:
            if isinstance(child, ActionRow):
                # Check if it has paginated buttons
                for item in child.children:
                    if getattr(item, "custom_id", None) == "paginated_prev":
                        nav_row = child
                        break

        assert nav_row is not None
        custom_ids = [getattr(c, "custom_id", None) for c in nav_row.children]
        assert "paginated_first" in custom_ids
        assert "paginated_last" in custom_ids
        assert "paginated_goto" in custom_ids

    def test_no_jump_buttons_below_threshold(self):
        interaction = _make_interaction()
        pages = ["A", "B", "C"]
        view = PaginatedLayoutView(interaction=interaction, pages=pages)

        all_custom_ids = []
        for child in view.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    cid = getattr(item, "custom_id", None)
                    if cid:
                        all_custom_ids.append(cid)

        assert "paginated_first" not in all_custom_ids
        assert "paginated_last" not in all_custom_ids
        assert "paginated_indicator" in all_custom_ids


class TestPaginatedLayoutViewCursorRestore:
    """Cursor mode preloads the current page through ``on_load``.

    The V2 half of the contract ``TestFromCursor`` covers for V1: both
    versions restore a page index across a ``pop`` through the shared mixin,
    so both owe a fetch for the slot that index lands on.
    """

    @staticmethod
    def _make_fetch(total_items, track_calls=None):
        async def fetch(offset: int, limit: int):
            if track_calls is not None:
                track_calls.append((offset, limit))
            return list(range(offset, min(offset + limit, total_items)))

        return fetch

    @staticmethod
    def _formatter(chunk):
        return TextDisplay(f"Items: {chunk}")

    async def test_on_load_fetches_the_page_restored_across_a_pop(self):
        """The restored slot is fetched and the tree recomposed around it."""
        calls = []
        view = PaginatedLayoutView.from_cursor(
            self._make_fetch(40, track_calls=calls),
            total=40,
            per_page=4,
            formatter=self._formatter,
            interaction=_make_interaction(),
        )
        assert view.pages[7] is None

        view.restore_nav_state({"current_page": 7})
        await view.on_load()

        assert calls == [(28, 4)]
        assert isinstance(view.pages[7], TextDisplay)

    async def test_on_load_is_a_no_op_for_an_already_loaded_page(self):
        """No refetch, and no recompose, for a slot the cache already holds."""
        calls = []
        view = PaginatedLayoutView.from_cursor(
            self._make_fetch(40, track_calls=calls),
            total=40,
            per_page=4,
            formatter=self._formatter,
            interaction=_make_interaction(),
        )
        await view._ensure_page_loaded(0)
        calls.clear()

        await view.on_load()

        assert calls == []
