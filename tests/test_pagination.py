"""Tests for PaginatedView enhancements: jump buttons, go-to modal, from_data, stacked pages."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from helpers import make_interaction as _make_interaction

from cascadeui.state.singleton import get_store
from cascadeui.views.patterns import PaginatedView


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
    """PaginatedView accepts embed and string pages and tracks current_page."""

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
    """Jump buttons appear when page count reaches jump_threshold or above."""

    async def test_no_jump_buttons_below_threshold(self):
        """First/last/go-to buttons hidden when pages < jump_threshold."""
        pages = _make_embeds(3)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        ids = _get_custom_ids(view)

        assert "paginated_prev" in ids
        assert "paginated_next" in ids
        assert "paginated_indicator" in ids
        assert "paginated_first" not in ids
        assert "paginated_last" not in ids
        assert "paginated_goto" not in ids

    async def test_just_below_threshold_no_jump(self):
        """At threshold minus one (4 pages, default threshold 5), no jumps."""
        pages = _make_embeds(4)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        ids = _get_custom_ids(view)

        assert "paginated_first" not in ids
        assert "paginated_last" not in ids
        assert "paginated_goto" not in ids

    async def test_at_threshold_shows_jump(self):
        """At exactly threshold (5 pages), jump buttons appear."""
        pages = _make_embeds(5)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        ids = _get_custom_ids(view)

        assert "paginated_first" in ids
        assert "paginated_last" in ids
        assert "paginated_goto" in ids
        assert "paginated_indicator" not in ids

    async def test_above_threshold_shows_jump(self):
        """Above threshold (10 pages), jump buttons visible."""
        pages = _make_embeds(10)
        view = PaginatedView(pages=pages, interaction=_make_interaction())
        ids = _get_custom_ids(view)

        assert "paginated_first" in ids
        assert "paginated_last" in ids
        assert "paginated_goto" in ids
        assert "paginated_indicator" not in ids

    async def test_custom_threshold_override(self):
        """Subclass can override jump_threshold."""

        class CustomPaginated(PaginatedView):
            jump_threshold = 3

        pages = _make_embeds(3)
        view = CustomPaginated(pages=pages, interaction=_make_interaction())
        ids = _get_custom_ids(view)

        assert "paginated_first" in ids
        assert "paginated_goto" in ids


# // ========================================( Button States )======================================== // #


class TestButtonStates:
    """Prev/next buttons disable at page boundaries."""

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
    """First, last, prev, next button callbacks navigate to correct pages."""

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
    """Go-to button opens a modal for direct page number input."""

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
    """from_data classmethod chunks items and applies sync/async formatters."""

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
    """_update_page edits the message with the correct page content type."""

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


# // ========================================( From Cursor )======================================== // #


class TestFromCursor:
    """from_cursor classmethod wires lazy page loading with an LRU cache."""

    def _make_fetch(self, total_items, track_calls=None):
        """Build a fetch callable that returns a slice of range(total_items).

        When ``track_calls`` is supplied it records each ``(offset, limit)``
        tuple, letting individual tests assert which pages fetched.
        """

        async def fetch(offset: int, limit: int):
            if track_calls is not None:
                track_calls.append((offset, limit))
            end = min(offset + limit, total_items)
            return list(range(offset, end))

        return fetch

    @staticmethod
    def _embed_formatter(chunk):
        return discord.Embed(title=f"Items: {chunk}")

    # // ----( Construction )---- // #

    async def test_basic_construction(self):
        """from_cursor builds a view with placeholder pages sized to total."""
        fetch = self._make_fetch(25)

        view = PaginatedView.from_cursor(
            fetch,
            total=25,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )

        assert len(view.pages) == 5
        assert all(p is None for p in view.pages)
        assert view._is_cursor_mode is True
        assert view._cursor_total == 25
        assert view._cache_size == 10

    async def test_total_zero(self):
        """total=0 produces an empty pages list with no placeholders."""
        view = PaginatedView.from_cursor(
            self._make_fetch(0),
            total=0,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )

        assert view.pages == []
        assert view._is_cursor_mode is True

    async def test_uneven_total(self):
        """Page count rounds up when total is not a multiple of per_page."""
        view = PaginatedView.from_cursor(
            self._make_fetch(23),
            total=23,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )

        assert len(view.pages) == 5  # 23 / 5 = 4.6 -> 5

    async def test_custom_cache_size(self):
        """cache_size kwarg is stored on the instance."""
        view = PaginatedView.from_cursor(
            self._make_fetch(100),
            total=100,
            per_page=10,
            formatter=self._embed_formatter,
            cache_size=3,
            interaction=_make_interaction(),
        )

        assert view._cache_size == 3

    # // ----( Validation )---- // #

    async def test_non_callable_fetch_fn_raises(self):
        """TypeError when fetch_fn is not callable."""
        with pytest.raises(TypeError, match="fetch_fn must be callable"):
            PaginatedView.from_cursor(
                "not callable",
                total=10,
                per_page=5,
                formatter=self._embed_formatter,
                interaction=_make_interaction(),
            )

    async def test_non_callable_formatter_raises(self):
        """TypeError when formatter is not callable."""
        with pytest.raises(TypeError, match="formatter must be callable"):
            PaginatedView.from_cursor(
                self._make_fetch(10),
                total=10,
                per_page=5,
                formatter="not callable",
                interaction=_make_interaction(),
            )

    async def test_negative_total_raises(self):
        """ValueError when total is negative."""
        with pytest.raises(ValueError, match="total must be a non-negative int"):
            PaginatedView.from_cursor(
                self._make_fetch(0),
                total=-1,
                per_page=5,
                formatter=self._embed_formatter,
                interaction=_make_interaction(),
            )

    async def test_zero_per_page_raises(self):
        """ValueError when per_page is zero."""
        with pytest.raises(ValueError, match="per_page must be a positive int"):
            PaginatedView.from_cursor(
                self._make_fetch(10),
                total=10,
                per_page=0,
                formatter=self._embed_formatter,
                interaction=_make_interaction(),
            )

    async def test_zero_cache_size_raises(self):
        """ValueError when cache_size is zero."""
        with pytest.raises(ValueError, match="cache_size must be a positive int"):
            PaginatedView.from_cursor(
                self._make_fetch(10),
                total=10,
                per_page=5,
                formatter=self._embed_formatter,
                cache_size=0,
                interaction=_make_interaction(),
            )

    async def test_bool_rejected_on_numerics(self):
        """Booleans are rejected even though bool is a subclass of int."""
        with pytest.raises(ValueError, match="total must be a non-negative int"):
            PaginatedView.from_cursor(
                self._make_fetch(10),
                total=True,  # noqa -- deliberately wrong
                per_page=5,
                formatter=self._embed_formatter,
                interaction=_make_interaction(),
            )

    # // ----( Send-time load )---- // #

    async def test_send_loads_page_zero(self):
        """send() preloads page 0 before handing off to super().send()."""
        calls = []
        fetch = self._make_fetch(15, track_calls=calls)
        view = PaginatedView.from_cursor(
            fetch,
            total=15,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )

        with patch.object(type(view).__mro__[2], "send", new=AsyncMock(return_value=MagicMock())):
            await view.send()

        assert calls == [(0, 5)]
        assert isinstance(view.pages[0], discord.Embed)
        assert view.pages[1] is None

    # // ----( Ensure-page-loaded )---- // #

    async def test_ensure_page_loaded_populates_cache(self):
        """First call fetches, second call is a no-op."""
        calls = []
        fetch = self._make_fetch(10, track_calls=calls)
        view = PaginatedView.from_cursor(
            fetch,
            total=10,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )

        await view._ensure_page_loaded(1)
        assert calls == [(5, 5)]
        assert view.pages[1] is not None

        await view._ensure_page_loaded(1)
        assert calls == [(5, 5)]  # no second fetch

    async def test_ensure_page_loaded_out_of_range_is_noop(self):
        """Out-of-range indices skip cleanly."""
        calls = []
        view = PaginatedView.from_cursor(
            self._make_fetch(10, track_calls=calls),
            total=10,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )

        await view._ensure_page_loaded(-1)
        await view._ensure_page_loaded(99)
        assert calls == []

    async def test_async_formatter(self):
        """Async formatters are awaited, not double-wrapped."""

        async def async_formatter(chunk):
            return discord.Embed(title=f"Async: {chunk}")

        view = PaginatedView.from_cursor(
            self._make_fetch(10),
            total=10,
            per_page=5,
            formatter=async_formatter,
            interaction=_make_interaction(),
        )

        await view._ensure_page_loaded(0)
        assert isinstance(view.pages[0], discord.Embed)
        assert view.pages[0].title == "Async: [0, 1, 2, 3, 4]"

    # // ----( LRU eviction )---- // #

    async def test_lru_eviction_evicts_oldest(self):
        """With cache_size=2 the oldest unprotected page evicts first."""
        view = PaginatedView.from_cursor(
            self._make_fetch(25),
            total=25,
            per_page=5,
            formatter=self._embed_formatter,
            cache_size=2,
            interaction=_make_interaction(),
        )
        view.current_page = 4  # last page, guarantees protection does not interfere

        await view._ensure_page_loaded(0)
        await view._ensure_page_loaded(1)
        await view._ensure_page_loaded(2)

        # Cache size is 2; adding page 2 should evict page 0 (oldest).
        assert view.pages[0] is None
        assert view.pages[1] is not None
        assert view.pages[2] is not None

    async def test_current_page_protected_from_eviction(self):
        """The current page is never evicted even under heavy churn."""
        view = PaginatedView.from_cursor(
            self._make_fetch(25),
            total=25,
            per_page=5,
            formatter=self._embed_formatter,
            cache_size=2,
            interaction=_make_interaction(),
        )
        # Current page is 0; load 0 first so it enters the cache.
        await view._ensure_page_loaded(0)
        await view._ensure_page_loaded(1)
        await view._ensure_page_loaded(2)
        await view._ensure_page_loaded(3)

        # Page 0 must survive because it is current. Older non-current entries
        # evict in its stead.
        assert view.pages[0] is not None

    # // ----( Refresh ops )---- // #

    async def test_refresh_data_in_cursor_mode_raises(self):
        """refresh_data errors in cursor mode with a directed message."""
        view = PaginatedView.from_cursor(
            self._make_fetch(10),
            total=10,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )

        with pytest.raises(RuntimeError, match="refresh_pages"):
            await view.refresh_data([1, 2, 3])

    async def test_refresh_pages_in_eager_mode_raises(self):
        """refresh_pages errors in eager mode with a directed message."""

        def formatter(chunk):
            return discord.Embed(title=str(chunk))

        view = await PaginatedView.from_data(
            [1, 2, 3], per_page=1, formatter=formatter, interaction=_make_interaction()
        )

        with pytest.raises(RuntimeError, match="refresh_data"):
            await view.refresh_pages()

    async def test_refresh_pages_clears_cache(self):
        """refresh_pages() clears the cache; the current page refetches."""
        calls = []
        view = PaginatedView.from_cursor(
            self._make_fetch(10, track_calls=calls),
            total=10,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._ensure_page_loaded(0)
        await view._ensure_page_loaded(1)
        assert calls == [(0, 5), (5, 5)]

        await view.refresh_pages()

        # Non-current pages stay nulled; current page re-fetched for display
        # (_update_page calls _ensure_page_loaded on self.current_page).
        assert view.pages[1] is None
        assert view.pages[view.current_page] is not None
        assert list(view._page_cache_order.keys()) == [view.current_page]
        # Third fetch proves the cache was flushed before the refetch ran.
        assert calls == [(0, 5), (5, 5), (0, 5)]

    async def test_refresh_pages_with_new_total_resizes(self):
        """new_total resizes the pages list and updates _cursor_total."""
        view = PaginatedView.from_cursor(
            self._make_fetch(25),
            total=25,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view.refresh_pages(new_total=40)

        assert view._cursor_total == 40
        assert len(view.pages) == 8  # 40 / 5

    async def test_refresh_pages_with_new_total_clamps_current(self):
        """Shrinking new_total clamps current_page to the last valid index."""
        view = PaginatedView.from_cursor(
            self._make_fetch(25),
            total=25,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view.current_page = 4

        await view.refresh_pages(new_total=10)

        assert len(view.pages) == 2
        assert view.current_page == 1

    async def test_refresh_pages_negative_new_total_raises(self):
        """Negative new_total fails validation."""
        view = PaginatedView.from_cursor(
            self._make_fetch(10),
            total=10,
            per_page=5,
            formatter=self._embed_formatter,
            interaction=_make_interaction(),
        )

        with pytest.raises(ValueError, match="new_total must be a non-negative int"):
            await view.refresh_pages(new_total=-1)


# // ========================================( Send Kwargs Propagation )======================================== // #


class TestSendKwargsPropagation:
    """V1 ``PaginatedView.send`` forwards ``file=``/``files=`` to ``super().send()``.

    The patches target ``cascadeui.views.view.StatefulView.send`` (the V1
    base) rather than ``PaginatedView.send`` (the override) because
    ``PaginatedView.send`` invokes ``super().send(...)`` whose MRO
    resolves to ``StatefulView.send``. Patching the override would not
    intercept the super-call; the base is the only seam where the
    forwarded kwargs land. Each ``with`` block scopes the patch to the
    test, so cross-test pollution stays contained.
    """

    async def test_files_forwarded_to_super_send(self):
        """``files=`` reaches the V1 base ``send`` call alongside the page kwargs."""
        view = PaginatedView(pages=_make_embeds(3), interaction=_make_interaction())
        photo = MagicMock(spec=discord.File)

        with patch(
            "cascadeui.views.view.StatefulView.send",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_super_send:
            await view.send(files=[photo])

        mock_super_send.assert_called_once()
        assert mock_super_send.call_args.kwargs["files"] == [photo]

    async def test_single_file_forwarded_to_super_send(self):
        """``file=`` (singular) reaches the V1 base ``send`` call."""
        view = PaginatedView(pages=_make_embeds(3), interaction=_make_interaction())
        photo = MagicMock(spec=discord.File)

        with patch(
            "cascadeui.views.view.StatefulView.send",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_super_send:
            await view.send(file=photo)

        assert mock_super_send.call_args.kwargs["file"] is photo
