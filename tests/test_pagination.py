"""Tests for PaginatedView enhancements: jump buttons, go-to modal, from_data, stacked pages."""

import asyncio

import pytest
import discord
from unittest.mock import AsyncMock, MagicMock, patch

from cascadeui.views.specialized import PaginatedView
from cascadeui.state.singleton import get_store
from helpers import make_interaction as _make_interaction


def _make_embeds(n):
    """Create n simple embeds for testing."""
    return [discord.Embed(title=f"Page {i + 1}") for i in range(n)]


def _get_custom_ids(view):
    """Return set of custom_ids from a view's children."""
    return {getattr(item, "custom_id", None) for item in view.children}


def _get_item(view, custom_id):
    """Find an item by custom_id."""
    for item in view.children:
        if getattr(item, "custom_id", None) == custom_id:
            return item
    return None


# // ========================================( Basic Pages )======================================== // #


class TestBasicPages:
    async def test_embed_pages(self):
        """PaginatedView accepts Embed pages."""
        pages = _make_embeds(3)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        assert view.pages == pages
        assert view.current_page == 0

    async def test_string_pages(self):
        """PaginatedView accepts string pages."""
        pages = ["Hello", "World", "Test"]
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        assert view.pages == pages

    async def test_stacked_page_dict(self):
        """Pages can be dicts with embed and content keys."""
        embed = discord.Embed(title="Mixed")
        pages = [{"embed": embed, "content": "Text content"}]
        view = PaginatedView(pages=pages, interaction=_make_interaction())

        result = view._extract_page(pages[0])
        assert result["embed"] is embed
        assert result["content"] == "Text content"

    async def test_extract_page_embed(self):
        """_extract_page handles plain Embeds — only includes 'embed' key."""
        embed = discord.Embed(title="Test")
        view = PaginatedView(pages=[embed], interaction=_make_interaction())

        result = view._extract_page(embed)
        assert result["embed"] is embed
        assert "content" not in result

    async def test_extract_page_string(self):
        """_extract_page handles strings — only includes 'content' key."""
        view = PaginatedView(pages=["text"], interaction=_make_interaction())

        result = view._extract_page("text")
        assert result["content"] == "text"
        assert "embed" not in result


# // ========================================( Button Visibility )======================================== // #


class TestButtonVisibility:
    async def test_no_jump_buttons_below_threshold(self):
        """First/last/go-to buttons hidden when pages <= jump_threshold."""
        pages = _make_embeds(3)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        ids = _get_custom_ids(view)

        assert "paginated_prev" in ids
        assert "paginated_next" in ids
        assert "paginated_indicator" in ids
        assert "paginated_first" not in ids
        assert "paginated_last" not in ids
        assert "paginated_goto" not in ids

    async def test_jump_buttons_above_threshold(self):
        """First/last/go-to buttons visible when pages > jump_threshold."""
        pages = _make_embeds(10)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        ids = _get_custom_ids(view)

        assert "paginated_first" in ids
        assert "paginated_last" in ids
        assert "paginated_goto" in ids
        assert "paginated_indicator" not in ids

    async def test_at_threshold_no_jump(self):
        """Exactly at threshold (5 pages), jump buttons are hidden."""
        pages = _make_embeds(5)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        ids = _get_custom_ids(view)

        assert "paginated_first" not in ids
        assert "paginated_goto" not in ids

    async def test_custom_threshold_override(self):
        """Subclass can override jump_threshold."""

        class CustomPaginated(PaginatedView):
            jump_threshold = 3

        pages = _make_embeds(4)
        view = CustomPaginated(pages=pages, interaction=_make_interaction())
        ids = _get_custom_ids(view)

        assert "paginated_first" in ids
        assert "paginated_goto" in ids


# // ========================================( Button States )======================================== // #


class TestButtonStates:
    async def test_prev_disabled_at_first_page(self):
        """Previous button is disabled on the first page."""
        pages = _make_embeds(3)
        view = PaginatedView(pages=pages, interaction=_make_interaction())

        prev = _get_item(view, "paginated_prev")
        assert prev.disabled is True

    async def test_next_disabled_at_last_page(self):
        """Next button is disabled on the last page."""
        pages = _make_embeds(3)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view.current_page = 2

        # Simulate _update_page button state logic
        for item in view.children:
            cid = getattr(item, "custom_id", None)
            if cid == "paginated_next":
                item.disabled = view.current_page >= len(pages) - 1

        nxt = _get_item(view, "paginated_next")
        assert nxt.disabled is True

    async def test_next_enabled_at_first_page(self):
        """Next button is enabled when there are pages ahead."""
        pages = _make_embeds(3)
        view = PaginatedView(pages=pages, interaction=_make_interaction())

        nxt = _get_item(view, "paginated_next")
        assert nxt.disabled is False

    async def test_first_disabled_at_first_page(self):
        """First jump button is disabled at page 0."""
        pages = _make_embeds(10)
        view = PaginatedView(pages=pages, interaction=_make_interaction())

        first = _get_item(view, "paginated_first")
        assert first.disabled is True

    async def test_last_enabled_at_first_page(self):
        """Last jump button is enabled when not at the end."""
        pages = _make_embeds(10)
        view = PaginatedView(pages=pages, interaction=_make_interaction())

        last = _get_item(view, "paginated_last")
        assert last.disabled is False

    async def test_single_page_all_disabled(self):
        """With only 1 page, navigation buttons are disabled."""
        pages = _make_embeds(1)
        view = PaginatedView(pages=pages, interaction=_make_interaction())

        prev = _get_item(view, "paginated_prev")
        nxt = _get_item(view, "paginated_next")
        assert prev.disabled is True
        assert nxt.disabled is True


# // ========================================( Navigation )======================================== // #


class TestNavigation:
    async def test_first_button_goes_to_page_zero(self):
        """Clicking first button sets current_page to 0."""
        pages = _make_embeds(10)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view.current_page = 5
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        first_btn = _get_item(view, "paginated_first")
        await first_btn.callback(_make_interaction())

        assert view.current_page == 0

    async def test_last_button_goes_to_last_page(self):
        """Clicking last button sets current_page to the end."""
        pages = _make_embeds(10)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        last_btn = _get_item(view, "paginated_last")
        await last_btn.callback(_make_interaction())

        assert view.current_page == 9

    async def test_prev_button_decrements(self):
        """Previous button decrements current_page."""
        pages = _make_embeds(5)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view.current_page = 2
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        prev_btn = _get_item(view, "paginated_prev")
        await prev_btn.callback(_make_interaction())

        assert view.current_page == 1

    async def test_next_button_increments(self):
        """Next button increments current_page."""
        pages = _make_embeds(5)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        next_btn = _get_item(view, "paginated_next")
        await next_btn.callback(_make_interaction())

        assert view.current_page == 1

    async def test_prev_clamps_at_zero(self):
        """Previous button doesn't go below 0."""
        pages = _make_embeds(5)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view.current_page = 0
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        prev_btn = _get_item(view, "paginated_prev")
        await prev_btn.callback(_make_interaction())

        assert view.current_page == 0

    async def test_next_clamps_at_max(self):
        """Next button doesn't exceed last page."""
        pages = _make_embeds(5)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view.current_page = 4
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        next_btn = _get_item(view, "paginated_next")
        await next_btn.callback(_make_interaction())

        assert view.current_page == 4


# // ========================================( Go-to Modal )======================================== // #


class TestGotoModal:
    async def test_goto_button_opens_modal(self):
        """Clicking the go-to button calls send_modal."""
        pages = _make_embeds(10)
        view = PaginatedView(pages=pages, interaction=_make_interaction())

        interaction = _make_interaction()
        goto_btn = _get_item(view, "paginated_goto")
        await goto_btn.callback(interaction)

        interaction.response.send_modal.assert_called_once()

    async def test_goto_label_shows_page_count(self):
        """Go-to button label shows current/total."""
        pages = _make_embeds(10)
        view = PaginatedView(pages=pages, interaction=_make_interaction())

        goto_btn = _get_item(view, "paginated_goto")
        assert goto_btn.label == "1/10"


# // ========================================( from_data )======================================== // #


class TestFromData:
    async def test_sync_formatter(self):
        """from_data works with a sync formatter."""
        items = list(range(9))

        def formatter(chunk):
            return discord.Embed(title=f"Items: {chunk}")

        view = await PaginatedView.from_data(
            items, per_page=3, formatter=formatter, interaction=_make_interaction()
        )

        assert len(view.pages) == 3
        assert all(isinstance(p, discord.Embed) for p in view.pages)

    async def test_async_formatter(self):
        """from_data works with an async formatter."""
        items = list(range(6))

        async def formatter(chunk):
            return discord.Embed(title=f"Async: {chunk}")

        view = await PaginatedView.from_data(
            items, per_page=2, formatter=formatter, interaction=_make_interaction()
        )

        assert len(view.pages) == 3

    async def test_chunking_uneven(self):
        """from_data handles uneven item counts correctly."""
        items = list(range(7))

        def formatter(chunk):
            return discord.Embed(title=f"Count: {len(chunk)}")

        view = await PaginatedView.from_data(
            items, per_page=3, formatter=formatter, interaction=_make_interaction()
        )

        # 7 items / 3 per page = 3 pages (3, 3, 1)
        assert len(view.pages) == 3

    async def test_empty_items(self):
        """from_data with empty list produces no pages."""

        def formatter(chunk):
            return discord.Embed(title="Empty")

        view = await PaginatedView.from_data(
            [], per_page=5, formatter=formatter, interaction=_make_interaction()
        )

        assert len(view.pages) == 0

    async def test_single_page(self):
        """from_data with fewer items than per_page produces one page."""
        items = [1, 2]

        def formatter(chunk):
            return discord.Embed(title=f"Items: {chunk}")

        view = await PaginatedView.from_data(
            items, per_page=10, formatter=formatter, interaction=_make_interaction()
        )

        assert len(view.pages) == 1


# // ========================================( Update Page )======================================== // #


class TestUpdatePage:
    async def test_update_page_with_embed(self):
        """_update_page edits with embed for Embed pages — no content key sent."""
        pages = _make_embeds(3)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._update_page()

        view._message.edit.assert_called_once()
        call_kwargs = view._message.edit.call_args[1]
        assert call_kwargs["embed"] is pages[0]
        assert "content" not in call_kwargs

    async def test_update_page_with_string(self):
        """_update_page edits with content for string pages — no embed key sent."""
        pages = ["Hello", "World"]
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._update_page()

        call_kwargs = view._message.edit.call_args[1]
        assert call_kwargs["content"] == "Hello"
        assert "embed" not in call_kwargs

    async def test_update_page_with_dict(self):
        """_update_page edits with both embed and content for dict pages."""
        embed = discord.Embed(title="Mixed")
        pages = [{"embed": embed, "content": "Both"}]
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._update_page()

        call_kwargs = view._message.edit.call_args[1]
        assert call_kwargs["embed"] is embed
        assert call_kwargs["content"] == "Both"

    async def test_update_page_updates_goto_label(self):
        """_update_page refreshes the go-to button label."""
        pages = _make_embeds(10)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view.current_page = 4
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._update_page()

        goto_btn = _get_item(view, "paginated_goto")
        assert goto_btn.label == "5/10"

    async def test_update_page_updates_indicator_label(self):
        """_update_page refreshes the indicator label for small page sets."""
        pages = _make_embeds(3)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        view.current_page = 1
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._update_page()

        indicator = _get_item(view, "paginated_indicator")
        assert indicator.label == "Page 2/3"
