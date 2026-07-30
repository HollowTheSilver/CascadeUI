# // ========================================( Modules )======================================== // #


import inspect
import logging
from collections import OrderedDict
from typing import Any, Callable, ClassVar, Iterable, List, Optional

import discord
from discord import Interaction
from discord.ui import ActionRow, Button, Container, TextDisplay

from ...components.base import StatefulButton
from ...components.types import EmojiInput
from ...utils.hooks import await_maybe
from ..base import _StatefulMixin
from ..layout import StatefulLayoutView
from ..view import StatefulView

logger = logging.getLogger(__name__)

# // ========================================( Shared Mixin )======================================== // #


class _BasePaginatedMixin:
    """Version-agnostic paginated logic shared by ``PaginatedView`` and
    ``PaginatedLayoutView``.

    Holds the customization triples, the ``on_page_changed`` hook, label
    resolvers, callback factories, the goto modal, and the ``from_data``
    / ``refresh_data`` / ``on_state_changed`` surface. V1 and V2 subclasses
    supply only the button construction and content rendering paths that
    genuinely differ between component systems.

    Internal. Not exported. The public hierarchy
    (``PaginatedView`` / ``PaginatedLayoutView``) is unchanged.
    """

    # Minimum page count at which first/last and go-to-page buttons appear
    jump_threshold: int = 5
    _POSITIVE_INT_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._POSITIVE_INT_ATTRS,
        "jump_threshold",
    )

    # // ----( Customization triples - nav buttons )---- // #

    first_button_label: ClassVar[Optional[str]] = "\u23ee"
    first_button_emoji: ClassVar[EmojiInput] = None
    first_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    prev_button_label: ClassVar[Optional[str]] = "\u25c0"
    prev_button_emoji: ClassVar[EmojiInput] = None
    prev_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    indicator_button_label: ClassVar[Optional[str]] = None  # default uses "Page {n}/{t}"
    indicator_button_emoji: ClassVar[EmojiInput] = None
    indicator_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.primary

    next_button_label: ClassVar[Optional[str]] = "\u25b6"
    next_button_emoji: ClassVar[EmojiInput] = None
    next_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    last_button_label: ClassVar[Optional[str]] = "\u23ed"
    last_button_emoji: ClassVar[EmojiInput] = None
    last_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    _BUTTON_STYLE_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BUTTON_STYLE_ATTRS,
        "first_button_style",
        "prev_button_style",
        "indicator_button_style",
        "next_button_style",
        "last_button_style",
    )
    _STR_OR_NONE_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._STR_OR_NONE_ATTRS,
        "first_button_label",
        "prev_button_label",
        "indicator_button_label",
        "next_button_label",
        "last_button_label",
    )
    _EMOJI_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._EMOJI_ATTRS,
        "first_button_emoji",
        "prev_button_emoji",
        "indicator_button_emoji",
        "next_button_emoji",
        "last_button_emoji",
    )

    _BOOL_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BOOL_ATTRS,
        "nav_inside_container",
        "nav_divider",
    )

    # // ----( Override hook )---- // #

    async def on_page_changed(self, page: int) -> None:
        """Called after ``self.current_page`` updates, before the refresh.

        ``page`` is the zero-based index of the new current page. Default
        is a no-op. Override for analytics, async prefetch, or per-page
        validation that should fire on every page turn.
        """
        return None

    # // ----( Label resolvers )---- // #

    def _resolve_indicator_label(self) -> str:
        total = max(len(self.pages), 1)
        current = self.current_page + 1
        if self.indicator_button_label is not None:
            return self.indicator_button_label
        return f"Page {current}/{total}"

    def _resolve_goto_label(self) -> str:
        total = max(len(self.pages), 1)
        current = self.current_page + 1
        if self.indicator_button_label is not None:
            return self.indicator_button_label
        return f"{current}/{total}"

    # // ----( Nav state )---- // #

    def _sync_nav_state(self) -> None:
        """Derive every nav button's disabled state and the indicator label
        from ``current_page`` / ``len(pages)``.

        The single source of truth for nav state, shared by the builders
        (``_build_navigation_buttons`` / ``_build_nav_buttons``) and the
        page-turn handlers (``_update_page``). Every render path that rebuilds
        the row runs the same computation, so a rebuild triggered off the
        page-turn path (a reload, an ``on_load``) can no longer leave the
        buttons at page-one defaults while the content shows a later page.
        Buttons are already constructed by the time any caller runs this, so
        the only guards are the ``_first_btn`` / ``_last_btn`` None-checks for
        views below the jump threshold.
        """
        self._clamp_page()
        total = len(self.pages)
        at_first = self.current_page == 0
        at_last = self.current_page >= total - 1
        self._prev_btn.disabled = at_first
        self._next_btn.disabled = at_last
        if self._first_btn is not None:
            self._first_btn.disabled = at_first
        if self._last_btn is not None:
            self._last_btn.disabled = at_last
        if self._show_jump:
            self._indicator_btn.label = self._resolve_goto_label()
        else:
            self._indicator_btn.label = self._resolve_indicator_label()

    # // ----( Navigation state )---- // #

    def get_nav_state(self) -> dict:
        """Carry the user's page across a ``pop``.

        ``current_page`` is assigned in ``__init__`` and is not a constructor
        kwarg, so a reconstruction reverts it to zero: page through to four,
        open a detail view, press Back, and land on page one. The pages
        themselves survive (they ride the kwargs snapshot); only the cursor
        into them is lost.
        """
        return {"current_page": self.current_page}

    def restore_nav_state(self, state: dict) -> None:
        """Restore the page index, clamping once the page list is known.

        The list can be shorter than it was at push time -- a reconstruction
        re-runs ``from_data``'s formatter over whatever the source holds now,
        and rows may have been deleted meanwhile -- so a stale index must
        never reach an indexing read.

        Clamping waits while ``pages`` is empty. A subclass that fetches its
        pages in ``on_load`` (the leaderboard does) has none yet: this runs
        first. The index is held rather than discarded, and ``_clamp_page``
        re-runs from the render seams once the real list exists.
        """
        page = state.get("current_page")
        if page is None:
            return
        self.current_page = max(0, int(page))
        if self.pages:
            self._clamp_page()
            self._sync_nav_state()

    async def on_load(self) -> None:
        """Fetch the current page before the view renders.

        Only cursor mode has anything to do here: an eager view holds every
        page already, while a cursor view holds ``None`` in any slot it has
        not fetched or has since evicted. ``restore_nav_state`` can land the
        cursor on such a slot across a ``pop``, which would otherwise render
        the "Loading..." placeholder with nothing left to replace it.
        """
        if self._is_cursor_mode and self.pages and self.pages[self.current_page] is None:
            await self._ensure_page_loaded(self.current_page)

    def _clamp_page(self) -> None:
        """Pull ``current_page`` back into range for the current page list.

        Called from the render seams rather than only from the writers,
        because the page can be set before the list it indexes exists.
        """
        if not self.pages:
            return
        self.current_page = max(0, min(self.current_page, len(self.pages) - 1))

    # // ----( Callback factories )---- // #

    async def set_page(self, page: int) -> None:
        """Jump to a zero-based page index and re-render.

        Clamps to the valid range, fires the ``on_page_changed`` hook, and
        edits the message. The public counterpart to the nav buttons, for a
        subclass that needs a programmatic "jump to page N" control (a search
        hit, a "find me" button). Setting ``current_page`` directly does not
        re-render; this is the supported way to move the cursor.

        Not to be confused with ``PaginatedRegion.set_page``, which is
        synchronous and moves the cursor without rendering; the region's
        rendering equivalent is ``PaginatedRegion.show_page``.
        """
        if not self.pages:
            return
        target = max(0, min(len(self.pages) - 1, page))
        self.current_page = target
        await self._call_hook_safe(self.on_page_changed, target)
        await self._update_page()

    def _make_step_callback(self, delta: int):
        async def callback(interaction: Interaction):
            new_page = max(0, min(len(self.pages) - 1, self.current_page + delta))
            self.current_page = new_page
            await self._call_hook_safe(self.on_page_changed, new_page)
            await self._update_page()

        return callback

    def _make_jump_callback(self, target_resolver):
        # target_resolver is either an int (set at build time) or a callable
        # (re-resolved at click time). Callables let `_last_btn` track index
        # drift when `pages` changes via refresh_data().
        async def callback(interaction: Interaction):
            if callable(target_resolver):
                target = target_resolver()
            else:
                target = target_resolver
            target = max(0, min(len(self.pages) - 1, target))
            self.current_page = target
            await self._call_hook_safe(self.on_page_changed, target)
            await self._update_page()

        return callback

    async def _open_goto_modal(self, interaction: Interaction):
        """Open a modal for direct page number input."""
        total = len(self.pages)
        parent = self

        class _GotoModal(discord.ui.Modal, title="Go to Page"):
            page_input = discord.ui.TextInput(
                label=f"Page number (1\u2013{total})",
                placeholder=str(parent.current_page + 1),
                min_length=1,
                max_length=len(str(total)),
                required=True,
            )

            async def on_submit(modal_self, modal_interaction: Interaction):
                value = modal_self.page_input.value.strip()
                try:
                    page_num = int(value)
                except ValueError:
                    await parent.respond(
                        modal_interaction,
                        f"'{value}' is not a valid page number.",
                        ephemeral=True,
                    )
                    return

                page_num = max(1, min(page_num, total))
                parent.current_page = page_num - 1
                await parent._safe_defer(modal_interaction)
                await parent._call_hook_safe(parent.on_page_changed, parent.current_page)
                await parent._update_page()

        await self.open_modal(interaction, _GotoModal())

    # // ----( Data-driven construction )---- // #

    @classmethod
    async def from_data(
        cls,
        items: Iterable[Any],
        per_page: int,
        formatter: Callable,
        **kwargs,
    ):
        """Create a paginated view by chunking items and applying a formatter.

        A coroutine, because the formatter runs here -- once per chunk, up
        front -- and it may itself be async. Its sibling ``from_cursor`` is
        *not* a coroutine: it formats nothing at construction, so it has
        nothing to await. The await-ness of the two is the eager/lazy
        difference, not an inconsistency::

            view = await PaginatedView.from_data(items, 10, fmt)   # awaited
            view = PaginatedView.from_cursor(fetch, total=n, ...)  # not
        """
        if not callable(formatter):
            raise TypeError(f"formatter must be callable, got {type(formatter).__name__}")
        if not isinstance(per_page, int) or isinstance(per_page, bool) or per_page < 1:
            raise ValueError(f"per_page must be a positive int, got {per_page!r}")
        # Anything sliceable with a length is paged as it arrives: the annotation
        # says list, but tuples, ranges and strings have always worked and stay
        # working. A one-shot iterable is consumed instead, since chunking reads
        # it repeatedly and a generator would silently yield nothing the second
        # time. Neither shape is ambiguous, so both are absorbed rather than
        # refused, and only a value that is not a sequence at all raises.
        if not hasattr(items, "__len__") or not hasattr(items, "__getitem__"):
            try:
                items = list(items)
            except TypeError:
                raise TypeError(
                    f"items must be a sequence or an iterable of them, got "
                    f"{type(items).__name__}"
                ) from None

        chunks = [items[i : i + per_page] for i in range(0, len(items), per_page)]
        pages = []
        for chunk in chunks:
            pages.append(await await_maybe(formatter(chunk)))
        return cls(pages=pages, _per_page=per_page, _formatter=formatter, **kwargs)

    @classmethod
    def from_cursor(
        cls,
        fetch_fn: Callable,
        *,
        total: int,
        per_page: int,
        formatter: Callable,
        cache_size: int = 10,
        **kwargs,
    ):
        """Create a paginated view that loads pages lazily through a cursor.

        Not a coroutine, unlike ``from_data``: nothing is fetched or
        formatted here, so there is nothing to await. Call it without
        ``await`` and await ``send()`` as usual (the example below shows the
        shape). ``from_data`` awaits because it applies the formatter to
        every chunk up front; this one defers both the fetch and the format
        to the first render.

        Instead of pre-chunking an in-memory list (``from_data``), cursor
        mode calls ``fetch_fn(offset, limit)`` on demand as the caller
        navigates. Pages are cached up to ``cache_size`` entries and
        evicted in LRU order -- the page currently displayed is never
        evicted, so revisiting it always avoids a refetch.

        Appropriate for database-backed pagination where loading the
        full dataset into memory is wasteful or impossible. The fetch
        signature matches SQL / REST / Firestore idioms so typical
        backends drop in unchanged::

            async def fetch_users(offset: int, limit: int) -> list[dict]:
                return await db.fetch(
                    "SELECT id, name FROM users ORDER BY name "
                    "LIMIT $1 OFFSET $2",
                    limit, offset,
                )

            total = await db.fetchval("SELECT count(*) FROM users")
            view = PaginatedView.from_cursor(
                fetch_users,
                total=total,
                per_page=10,
                formatter=format_users_page,
            )
            await view.send()

        ``total`` is required because ``jump_threshold``, the goto modal,
        the ``Page N/M`` indicator, and the first/last buttons all need
        the total page count at construction time. Query alongside the
        first page fetch; it is cheap for most backends.

        When data changes at runtime, call ``refresh_pages(new_total=N)``
        to invalidate the cache and resize the pages list. Use
        ``refresh_pages()`` (no ``new_total``) when only the contents
        changed, not the row count.

        Args:
            fetch_fn: Async callable ``(offset, limit) -> list[T]`` that
                returns the items for one page slice. ``offset`` is the
                zero-based item offset; ``limit`` equals ``per_page``.
            total: Total item count across all pages. Used to size the
                pages list and enable jump UX. Must be non-negative.
            per_page: Items per page. Must be positive.
            formatter: Sync or async callable ``chunk -> page_content``
                matching the ``from_data`` formatter contract.
            cache_size: Maximum cached pages. Defaults to 10. Older
                entries evict LRU; the current page is protected.
            **kwargs: Forwarded to the constructor (``timeout``,
                ``allowed_users``, subclass kwargs, etc.).

        Raises:
            TypeError: ``fetch_fn`` or ``formatter`` is not callable.
            ValueError: ``total``, ``per_page``, or ``cache_size`` fails
                range validation.

        Returns:
            A paginated view instance with lazy page loading. Page 0
            loads during ``send()`` before the first Discord message
            is shipped.
        """
        if not callable(fetch_fn):
            raise TypeError(f"fetch_fn must be callable, got {type(fetch_fn).__name__}")
        if not callable(formatter):
            raise TypeError(f"formatter must be callable, got {type(formatter).__name__}")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ValueError(f"total must be a non-negative int, got {total!r}")
        if not isinstance(per_page, int) or isinstance(per_page, bool) or per_page < 1:
            raise ValueError(f"per_page must be a positive int, got {per_page!r}")
        if not isinstance(cache_size, int) or isinstance(cache_size, bool) or cache_size < 1:
            raise ValueError(f"cache_size must be a positive int, got {cache_size!r}")

        total_pages = (total + per_page - 1) // per_page
        pages = [None] * total_pages

        return cls(
            pages=pages,
            _per_page=per_page,
            _formatter=formatter,
            _fetch_fn=fetch_fn,
            _cursor_total=total,
            _cache_size=cache_size,
            **kwargs,
        )

    # // ----( Cursor-mode cache + fetch )---- // #

    @property
    def _is_cursor_mode(self) -> bool:
        """True when the view was constructed via ``from_cursor``."""
        return getattr(self, "_fetch_fn", None) is not None

    async def _ensure_page_loaded(self, page_idx: int) -> None:
        """Fetch and cache the page at ``page_idx`` if not already resident.

        No-op for eager mode and for cached cursor pages. On cache miss,
        awaits ``fetch_fn(offset, limit)``, runs the formatter, stores
        the content in ``self.pages[page_idx]``, touches the LRU order,
        and evicts older entries when the cache exceeds ``_cache_size``.
        """
        if not self._is_cursor_mode:
            return
        if page_idx < 0 or page_idx >= len(self.pages):
            return
        if self.pages[page_idx] is not None:
            self._touch_cache(page_idx)
            return

        offset = page_idx * self._per_page
        chunk = await await_maybe(self._fetch_fn(offset, self._per_page))
        content = await await_maybe(self._formatter(chunk))

        self.pages[page_idx] = content
        self._touch_cache(page_idx)
        self._evict_if_needed()

    def _touch_cache(self, page_idx: int) -> None:
        """Mark ``page_idx`` as most-recently-used in the LRU tracker."""
        self._page_cache_order.pop(page_idx, None)
        self._page_cache_order[page_idx] = None

    def _evict_if_needed(self) -> None:
        """Evict LRU pages until the cache fits ``_cache_size``.

        The current page is never evicted -- the user is looking at it
        and re-loading on refresh would produce a visible flicker. When
        only the current page remains cached, the loop exits even if
        the cache still exceeds the nominal size (one-over is the
        unavoidable floor).
        """
        while len(self._page_cache_order) > self._cache_size:
            evicted = None
            for page_idx in self._page_cache_order:
                if page_idx != self.current_page:
                    evicted = page_idx
                    break
            if evicted is None:
                break
            self._page_cache_order.pop(evicted)
            self.pages[evicted] = None

    async def refresh_data(self, items: Iterable[Any]):
        """Re-paginate with new data using the original per_page and formatter.

        Eager mode only (views created via ``from_data``). Cursor-mode
        views raise ``RuntimeError`` -- use ``refresh_pages`` instead
        because the data lives behind ``fetch_fn``, not in a caller-owned
        list.
        """
        if self._is_cursor_mode:
            raise RuntimeError(
                "refresh_data() requires a view created via from_data(); "
                "this view uses from_cursor() -- call refresh_pages() instead."
            )
        if self._per_page is None or self._formatter is None:
            raise RuntimeError("refresh_data() requires a view created via from_data()")

        chunks = [items[i : i + self._per_page] for i in range(0, len(items), self._per_page)]
        pages = []
        for chunk in chunks:
            pages.append(await await_maybe(self._formatter(chunk)))

        self.pages = pages or []
        if self.current_page >= len(self.pages):
            self.current_page = max(0, len(self.pages) - 1)

        await self._update_page()

    async def refresh_pages(self, *, new_total: Optional[int] = None) -> None:
        """Invalidate the cursor-mode page cache and re-render.

        Cursor mode only (views created via ``from_cursor``). Eager-mode
        views raise ``RuntimeError`` -- use ``refresh_data(items)``
        instead because the data lives in a caller-owned list, not
        behind a fetch callable.

        Pass ``new_total`` when the underlying row count has changed
        (rows inserted, rows deleted). The pages list resizes to match,
        jump-button state rebuilds against the new total, and the
        indicator label reflects the new ``N/M``. The current page is
        preserved when it still exists in the resized list, otherwise
        clamps to the last available page.

        Omit ``new_total`` when only page contents changed. The cache
        invalidates and the next navigation re-fetches.
        """
        if not self._is_cursor_mode:
            raise RuntimeError(
                "refresh_pages() requires a view created via from_cursor(); "
                "this view uses from_data() -- call refresh_data(items) instead."
            )

        if new_total is not None:
            if not isinstance(new_total, int) or isinstance(new_total, bool) or new_total < 0:
                raise ValueError(f"new_total must be a non-negative int, got {new_total!r}")
            new_page_count = (new_total + self._per_page - 1) // self._per_page
            self._cursor_total = new_total
            self.pages = [None] * new_page_count
        else:
            self.pages = [None] * len(self.pages)

        self._page_cache_order.clear()
        if self.current_page >= len(self.pages):
            self.current_page = max(0, len(self.pages) - 1)

        await self._update_page()

    async def on_state_changed(self, state):
        """Update pagination when state changes."""
        await self._update_page()


# // ========================================( V1: PaginatedView )======================================== // #


class PaginatedView(_BasePaginatedMixin, StatefulView):
    """A view for paginated content.

    Pages can be:
    - ``discord.Embed`` objects
    - ``str`` for plain text content
    - ``dict`` with keys ``"embed"`` and/or ``"content"`` for mixed content

    When the page count reaches ``jump_threshold`` or above, first/last jump
    buttons and a go-to-page modal button are shown automatically.

    Customization:
        Each of the five navigation buttons (``first``, ``prev``, ``indicator``,
        ``next``, ``last``) exposes a ``{label,emoji,style}`` class-attribute
        triple, matching the ``refresh_button_*`` / ``text_edit_button_*``
        grammar elsewhere in the library.

    Override hook:
        ``async def on_page_changed(self, page: int)`` runs after the
        ``current_page`` cursor is updated, before the refresh. Default
        is a no-op. Use for analytics, prefetch, or async validation.

    Use the ``from_data`` classmethod to auto-paginate a list of items.
    """

    def __init__(
        self,
        *args,
        pages=None,
        _per_page=None,
        _formatter=None,
        _fetch_fn=None,
        _cursor_total=None,
        _cache_size=10,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.pages = pages if pages is not None else []
        self.current_page = 0

        # Restored from _init_kwargs on pop, or set by from_data() / from_cursor()
        self._per_page: Optional[int] = _per_page
        self._formatter: Optional[Callable] = _formatter
        self._fetch_fn: Optional[Callable] = _fetch_fn
        self._cursor_total: Optional[int] = _cursor_total
        self._cache_size: int = _cache_size
        self._page_cache_order: OrderedDict = OrderedDict()

        self._build_navigation_buttons()
        for btn in self._nav_buttons:
            self.add_item(btn)
        self._build_extra_items()

    def _build_extra_items(self):
        """Hook for subclasses to add components below the navigation buttons.

        Called once during init. Nav buttons are mutated in place on page
        turns so items added here survive without needing to be rebuilt.
        The default implementation does nothing.
        """
        pass

    def _build_navigation_buttons(self):
        """Build navigation buttons into instance attributes and ``self._nav_buttons``.

        Pure builder: assigns ``self._first_btn`` / ``self._prev_btn`` /
        ``self._indicator_btn`` / ``self._next_btn`` / ``self._last_btn``
        and populates ``self._nav_buttons`` with the ordered attach list,
        but does NOT call ``self.add_item()``. Callers are responsible for
        iterating ``self._nav_buttons`` and attaching each entry.

        Separating build from attach lets subclasses that finalize
        ``self.pages`` after ``__init__`` (e.g. async page builders) re-invoke
        the builder against the real page count before shipping the first
        render. Mirrors the V2 ``_build_nav_buttons`` contract.
        """
        total = len(self.pages)
        show_jump = total >= self.jump_threshold
        self._show_jump = show_jump

        self._first_btn: Optional[StatefulButton] = None
        self._last_btn: Optional[StatefulButton] = None
        self._nav_buttons: list = []

        if show_jump:
            self._first_btn = StatefulButton(
                label=self.first_button_label or "\u23ee",
                emoji=self.first_button_emoji,
                style=self.first_button_style,
                custom_id="paginated_first",
                disabled=True,
                row=0,
                callback=self._make_jump_callback(0),
            )
            self._nav_buttons.append(self._first_btn)

        self._prev_btn = StatefulButton(
            label=self.prev_button_label or "\u25c0",
            emoji=self.prev_button_emoji,
            style=self.prev_button_style,
            custom_id="paginated_prev",
            disabled=True,
            row=0,
            callback=self._make_step_callback(-1),
        )
        self._nav_buttons.append(self._prev_btn)

        if show_jump:
            self._indicator_btn = StatefulButton(
                label=self._resolve_goto_label(),
                emoji=self.indicator_button_emoji,
                style=self.indicator_button_style,
                custom_id="paginated_goto",
                row=0,
                callback=self._open_goto_modal,
            )
        else:
            self._indicator_btn = discord.ui.Button(
                label=self._resolve_indicator_label(),
                style=discord.ButtonStyle.secondary,
                custom_id="paginated_indicator",
                disabled=True,
                row=0,
            )
        self._nav_buttons.append(self._indicator_btn)

        self._next_btn = StatefulButton(
            label=self.next_button_label or "\u25b6",
            emoji=self.next_button_emoji,
            style=self.next_button_style,
            custom_id="paginated_next",
            disabled=total <= 1,
            row=0,
            callback=self._make_step_callback(1),
        )
        self._nav_buttons.append(self._next_btn)

        if show_jump:
            self._last_btn = StatefulButton(
                label=self.last_button_label or "\u23ed",
                emoji=self.last_button_emoji,
                style=self.last_button_style,
                custom_id="paginated_last",
                disabled=total <= 1,
                row=0,
                callback=self._make_jump_callback(lambda: len(self.pages) - 1),
            )
            self._nav_buttons.append(self._last_btn)

        # Derive the disabled/label state from current_page so a re-invoke
        # of this builder after the page has moved reflects the real page,
        # not page one.
        self._sync_nav_state()
        self._nav_built_for_count = len(self.pages)

    def _extract_page(self, page) -> dict:
        """Extract embed/content kwargs from a page entry.

        Only includes keys that are actually present -- omitted keys won't
        be sent to the API, preserving existing message fields.
        """
        result = {}
        if isinstance(page, dict):
            if page.get("embed") is not None:
                result["embed"] = page["embed"]
            if page.get("content") is not None:
                result["content"] = page["content"]
        elif isinstance(page, discord.Embed):
            result["embed"] = page
        elif isinstance(page, str):
            result["content"] = page
        return result

    async def send(
        self,
        content=None,
        *,
        embed=None,
        **kwargs,
    ):
        """Send the view, using the first page as initial content if not specified."""
        # Cursor mode: page 0 is not yet loaded at construction; fetch it before
        # extracting initial content so the first Discord message ships with
        # real content instead of the ``None`` placeholder.
        if self._is_cursor_mode and self.pages and self.pages[0] is None:
            await self._ensure_page_loaded(0)

        if self.pages and embed is None and content is None:
            page_kwargs = self._extract_page(self.pages[0])
            embed = page_kwargs.get("embed")
            content = page_kwargs.get("content")
        return await super().send(
            content=content,
            embed=embed,
            **kwargs,
        )

    nav_rebuild = staticmethod(lambda v: v._nav_edit_kwargs())

    async def _nav_edit_kwargs(self) -> dict:
        """The embed/content a navigation edit needs to show the current page.

        ``pop()`` passes no rebuild of its own, so without this the edit
        would swap the buttons and leave whatever the child view put on the
        message. V1 content lives in the embed, so the embed is the render.

        The navigation edit path awaits this. This implementation does no
        I/O: its pages are formatted up front.
        """
        self._clamp_page()
        if not self.pages:
            return {}
        return self._extract_page(self.pages[self.current_page])

    async def _reload_render(self) -> None:
        await self._update_page()

    async def _update_page(self):
        """Mutate nav buttons in place and refresh the page content."""
        if not self.pages:
            return

        # The cursor can be set before the list it indexes exists (a page
        # carried across a pop lands before the pages do), so clamp ahead of
        # both the fetch and the read.
        self._clamp_page()

        # Cursor mode: load the target page before extracting content. No-op
        # for eager mode and for cached cursor pages.
        await self._ensure_page_loaded(self.current_page)

        page_kwargs = self._extract_page(self.pages[self.current_page])
        if len(self.pages) != getattr(self, "_nav_built_for_count", None):
            # Page count crossed jump_threshold: rebuild + re-attach the button
            # set (first/last/goto may appear or disappear). V1 mutates the tree
            # in place, so remove the old buttons before rebuilding.
            for btn in self._nav_buttons:
                self.remove_item(btn)
            self._build_navigation_buttons()
            for btn in self._nav_buttons:
                self.add_item(btn)
        else:
            self._sync_nav_state()
        await self.refresh(**page_kwargs)


# // ========================================( V2: PaginatedLayoutView )======================================== // #


class PaginatedLayoutView(_BasePaginatedMixin, StatefulLayoutView):
    """A V2 layout view for paginated content.

    The V2 equivalent of ``PaginatedView``. Pages are lists of V2 components
    (Container, TextDisplay, etc.) that replace the view's content on each
    page turn.

    Pages can be:
    - A list of V2 items (Container, TextDisplay, etc.)
    - A callable (sync or async) that returns a list of V2 items
    - A ``str`` wrapped in a Container + TextDisplay automatically

    Customization + override hook mirror ``PaginatedView``.

    V2-only attributes:
        nav_inside_container: When ``True``, page content and the nav row
            are wrapped in a single ``Container`` so the paginator renders
            as one cohesive card. When ``False`` (default), page content
            and the nav row are separate top-level children of the view --
            the original layout. ``_build_extra_items`` items remain
            outside the wrapping Container in either mode. A page that
            mixes a Container with other top-level items cannot be
            wrapped (Discord forbids Container nesting); such pages keep
            the sibling layout with the nav row as a separate row.
        nav_divider: When ``True`` (and ``nav_inside_container`` is on), a
            divider renders between the card content and the in-card nav
            row, giving the control strip a visual separator. Default
            ``False`` keeps the flush look. No effect in sibling layout.
    """

    nav_inside_container: ClassVar[bool] = False
    nav_divider: ClassVar[bool] = False

    def __init__(
        self,
        *args,
        pages=None,
        _per_page=None,
        _formatter=None,
        _fetch_fn=None,
        _cursor_total=None,
        _cache_size=10,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.pages = pages if pages is not None else []
        self.current_page = 0
        self._per_page: Optional[int] = _per_page
        self._formatter: Optional[Callable] = _formatter
        self._fetch_fn: Optional[Callable] = _fetch_fn
        self._cursor_total: Optional[int] = _cursor_total
        self._cache_size: int = _cache_size
        self._page_cache_order: OrderedDict = OrderedDict()

        self._page_content_items: List = []
        self._extra_items: List = []
        self._nav_row: Optional[ActionRow] = None

        # Build nav row first so the composition helper can reference it.
        # Then compose: page content + nav row in the configured layout
        # (separate siblings or wrapped in one Container per
        # ``nav_inside_container``). Single seam shared with ``send`` and
        # ``_update_page`` so initial render and post-interaction renders
        # are visually identical.
        self._build_nav_buttons()
        self._compose_pagination_tree()

        # Snapshot extras: anything added by the subclass in
        # _build_extra_items() is preserved through page turns.
        pre_extra = list(self.children)
        self._build_extra_items()
        self._extra_items = [c for c in self.children if c not in pre_extra]

    def _build_extra_items(self):
        """Hook for subclasses to add components below the navigation row.

        Called ONCE during init. Items added here are snapshotted by the
        framework and preserved through every page turn; the nav row is
        mutated in place. Override to add select menus, buttons, or other
        components that should live alongside the paginated content.
        """
        pass

    def _resolve_page(self, page):
        """Resolve a page entry to a list of V2 items.

        A ``None`` entry is a cursor-mode placeholder for a not-yet-loaded
        page; it renders as a transient "Loading..." card that is replaced
        by real content once ``_ensure_page_loaded`` completes. Sync init
        can therefore run before any fetch has happened.
        """
        if page is None:
            return [Container(TextDisplay("Loading..."))]
        if isinstance(page, str):
            return [Container(TextDisplay(page))]
        if isinstance(page, list):
            return page
        if callable(page):
            result = page()
            if isinstance(result, list):
                return result
            return [result]
        return [page]

    def _compose_pagination_tree(self):
        """Add page content + nav row to the view in the configured layout.

        Single seam shared by ``__init__``, ``send``, and ``_update_page``
        so the three call sites stay structurally identical and the
        ``nav_inside_container`` flag governs every render path.

        With ``nav_inside_container=False`` (default): page content items
        are added as separate top-level children, then the nav row is
        appended as a sibling -- the original layout.

        With ``nav_inside_container=True`` AND multiple pages exist: page
        content + nav row are wrapped in one ``Container`` and that
        Container is the only child added here. Pages of one (no nav row)
        and the empty-pages placeholder are unaffected -- nothing to wrap.
        Mixed pages that a Container cannot legally hold fall back to the
        sibling layout; see :meth:`_build_nav_wrapper`.

        Extras from ``_build_extra_items`` stay outside the wrapping
        Container; the caller adds them after this method returns.
        """
        self._page_content_items = []
        if not self.pages:
            placeholder = Container(TextDisplay("No pages."))
            self.add_item(placeholder)
            self._page_content_items.append(placeholder)
            return

        # The cursor can be set before the list it indexes exists -- a page
        # carried across a pop is restored before on_load fetches the pages.
        # Clamp before indexing rather than trusting the writer.
        self._clamp_page()

        # Callable page formatters run inside the view's theme context
        # so card() calls in user formatters inherit the view's accent.
        from ...theming.context import theme_context

        with theme_context(self.get_theme()):
            items = self._resolve_page(self.pages[self.current_page])
        show_nav = len(self.pages) > 1

        if self.nav_inside_container and show_nav:
            wrapper = self._build_nav_wrapper(items)
            if wrapper is not None:
                self.add_item(wrapper)
                self._page_content_items.append(wrapper)
                return
            # Mixed page: the wrap is impossible, so the sibling layout
            # below renders the page unchanged with the nav row as a
            # separate top-level row.

        for item in items:
            self.add_item(item)
            self._page_content_items.append(item)
        if show_nav:
            self.add_item(self._nav_row)

    def _build_nav_wrapper(self, items) -> Optional[Container]:
        """Build the ``nav_inside_container`` wrapper, or decline with ``None``.

        Two page shapes wrap cleanly. A single-``Container`` page gets a
        fresh wrapper that copies the source Container's metadata (accent,
        spoiler) and adopts its children alongside the nav row: Discord
        rejects Container-in-Container (type 17 inside type 17) with HTTP
        400 "Invalid Form Body", and the source Container in ``self.pages``
        is never mutated -- page turns rebuild the wrapper from scratch each
        render, so the nav row never accumulates across visits to the same
        source page (which would duplicate its button custom_ids and trip
        Discord's "Component custom id cannot be duplicated" reject). A page
        with no Containers at all wraps directly.

        A mixed page (a Container alongside other top-level items, e.g. a
        rankings card plus ``build_header``/``build_footer`` frames or a
        standalone summary card) cannot be wrapped: nesting the Container
        is forbidden, and adopting its children would merge deliberately
        separate cards into one. Returns ``None`` so the caller falls back
        to the sibling layout with the page content intact.
        """
        # nav_divider (opt-in) inserts a divider between the card content and
        # the in-card nav row. A fresh node each call keeps the divider
        # single-owned across renders of the same source page, honoring the
        # rebuild-from-scratch contract.
        nav_prefix: list = []
        if self.nav_divider:
            from ...components.patterns.v2 import divider

            nav_prefix = [divider()]

        if len(items) == 1 and isinstance(items[0], Container):
            source = items[0]
            wrapper = Container(
                *list(source.children),
                *nav_prefix,
                self._nav_row,
                accent_color=source.accent_color,
                spoiler=source.spoiler,
            )
            # A theme-managed source keeps its marker on the wrapper, so
            # the render-time accent resolution reaches wrapped pages the
            # same as sibling-layout pages.
            if getattr(source, "_cascadeui_theme_accent", False):
                wrapper._cascadeui_theme_accent = True
            return wrapper
        if not any(isinstance(item, Container) for item in items):
            return Container(*items, *nav_prefix, self._nav_row)
        if not getattr(self, "_nav_wrap_declined", False):
            self._nav_wrap_declined = True
            logger.debug(
                f"nav_inside_container declined for {type(self).__name__}: the page mixes "
                f"a Container with other top-level items; rendering the sibling layout."
            )
        return None

    def _build_nav_buttons(self):
        """Build nav buttons into a fresh ``self._nav_row`` ActionRow.

        Pure builder: assigns ``self._nav_row`` but does NOT attach it to
        the view tree. Callers are responsible for ``self.add_item(self._nav_row)``
        when ``len(self.pages) > 1``.

        Callable more than once -- subclasses that populate ``self.pages``
        asynchronously (e.g. ``LeaderboardLayoutView`` awaits
        ``get_avatar_url``) re-invoke this after ``send()``-time page
        construction so ``_show_jump`` and the initial ``disabled`` /
        label state reflect the final page count, not the empty-list
        snapshot taken at ``__init__``. Buttons are mutated in place on
        page turns by ``_update_page``.
        """
        total = len(self.pages)
        show_jump = total >= self.jump_threshold
        self._show_jump = show_jump

        self._first_btn: Optional[StatefulButton] = None
        self._last_btn: Optional[StatefulButton] = None
        buttons: List = []

        if show_jump:
            self._first_btn = StatefulButton(
                label=self.first_button_label or "\u23ee",
                emoji=self.first_button_emoji,
                style=self.first_button_style,
                custom_id="paginated_first",
                disabled=True,
                callback=self._make_jump_callback(lambda: 0),
            )
            buttons.append(self._first_btn)

        self._prev_btn = StatefulButton(
            label=self.prev_button_label or "\u25c0",
            emoji=self.prev_button_emoji,
            style=self.prev_button_style,
            custom_id="paginated_prev",
            disabled=True,
            callback=self._make_step_callback(-1),
        )
        buttons.append(self._prev_btn)

        if show_jump:
            self._indicator_btn = StatefulButton(
                label=self._resolve_goto_label(),
                emoji=self.indicator_button_emoji,
                style=self.indicator_button_style,
                custom_id="paginated_goto",
                callback=self._open_goto_modal,
            )
        else:
            self._indicator_btn = Button(
                label=self._resolve_indicator_label(),
                style=discord.ButtonStyle.secondary,
                custom_id="paginated_indicator",
                disabled=True,
            )
        buttons.append(self._indicator_btn)

        self._next_btn = StatefulButton(
            label=self.next_button_label or "\u25b6",
            emoji=self.next_button_emoji,
            style=self.next_button_style,
            custom_id="paginated_next",
            disabled=total <= 1,
            callback=self._make_step_callback(1),
        )
        buttons.append(self._next_btn)

        if show_jump:
            self._last_btn = StatefulButton(
                label=self.last_button_label or "\u23ed",
                emoji=self.last_button_emoji,
                style=self.last_button_style,
                custom_id="paginated_last",
                disabled=total <= 1,
                callback=self._make_jump_callback(lambda: len(self.pages) - 1),
            )
            buttons.append(self._last_btn)

        # Buttons are built unconditionally so identity survives a
        # rebuild_pages() that grows total from 1 to 2+. Callers attach
        # the row to the tree when pagination is meaningful.
        self._nav_row = ActionRow(*buttons)

        # Sync disabled/label state here (not just in _update_page) so a
        # rebuild off the page-turn path (LeaderboardLayoutView.on_load,
        # any reload) reflects the current page instead of page one.
        self._sync_nav_state()
        # Record the count this button SET was built for so _update_page can
        # detect a jump_threshold crossing and rebuild (not just re-sync).
        self._nav_built_for_count = len(self.pages)

    def restore_nav_state(self, state: dict) -> None:
        """Restore the page index and rebuild the tree to match it.

        The base restores the cursor; a V2 view is its components, so the
        tree has to be recomposed against the restored page or the message
        would render page one under a nav row that reads page four.
        """
        super().restore_nav_state(state)
        if self.pages:
            self._recompose_page_tree()

    async def on_load(self) -> None:
        """Fetch the current page, then rebuild the tree around it.

        The base does the fetch; a V2 view *is* its component tree, so a slot
        that arrives here empty leaves a "Loading..." card the recompose has
        to replace. Only recompose when something was actually fetched: this
        runs on every navigation edit, and an eager view has nothing to do.
        """
        needs_recompose = (
            self._is_cursor_mode and self.pages and self.pages[self.current_page] is None
        )
        await super().on_load()
        if needs_recompose:
            self._recompose_page_tree()

    def _recompose_page_tree(self, *, rebuild_nav: bool = False) -> None:
        """Clear the tree and re-add page content, extras, and the back button.

        The single recompose sequence shared by ``send`` (cursor preload),
        ``_update_page`` (page turn), and ``LeaderboardLayoutView.on_load``
        (reload). ``rebuild_nav=True`` re-runs ``_build_nav_buttons`` first,
        for callers whose page count may have changed since construction
        (the leaderboard, whose entries are fetched async in ``on_load``).
        ``_restore_navigation_artifacts`` re-adds the auto back button when
        ``push()`` injected one; it is a no-op at plain-send time, so routing
        ``send`` through here adds no behavior.
        """
        self.clear_items()
        if rebuild_nav:
            self._build_nav_buttons()
        self._compose_pagination_tree()
        for extra in self._extra_items:
            self.add_item(extra)
        self._restore_navigation_artifacts()

    async def _update_page(self):
        """Mutate nav in place, rebuild page content, preserve extra items.

        Removes only the current page-content children and re-adds new
        ones; the nav row and any ``_build_extra_items``-registered items
        keep their identity across page turns.
        """
        if not self.pages:
            return

        # Cursor mode: load the target page before reading its content.
        # No-op for eager mode and for cached cursor pages.
        await self._ensure_page_loaded(self.current_page)

        if len(self.pages) != getattr(self, "_nav_built_for_count", None):
            # The page count crossed jump_threshold since the nav row was built
            # (a refresh_data / refresh_pages / rebuild_pages call). Rebuild the
            # button SET: _sync_nav_state only mutates existing buttons, it
            # cannot add or remove the first/last/goto buttons _show_jump gates.
            self._recompose_page_tree(rebuild_nav=True)
        else:
            self._sync_nav_state()
            self._recompose_page_tree()
        await self.refresh()
