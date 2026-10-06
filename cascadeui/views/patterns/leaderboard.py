# // ========================================( Modules )======================================== // #


import asyncio
from collections.abc import Mapping
from typing import ClassVar, Dict, List, Optional, Tuple, Union

import discord
from discord.ui import Container, Item, Section, TextDisplay

from ...components.patterns.v2 import (
    _media_ref_url,
    _resolve_media_ref,
    card,
    divider,
    gallery,
    gap,
    image_section,
)
from ...components.types import MAX_MESSAGE_COMPONENTS, MediaInput
from ...utils.hooks import await_maybe, is_async_callable
from ..base import RenderOutcome, _StatefulMixin
from ..layout import _OVER_CAPACITY_MARKER
from ..persistent import _PersistentMixin
from .paginated import PaginatedLayoutView, _BasePaginatedMixin

# What ``get_entries()`` resolves to. Public so a subclass can annotate its own
# override, and the same annotation whether that override is sync or async: an
# async function's return annotation names the value it resolves to.
EntryList = List[Tuple[int, dict]]

# Sentinel distinguishing an explicitly passed ``None`` (suppress that
# masthead piece) from an omitted kwarg (fall back to the class default).
# Shared by the ``title`` / ``subtitle`` / ``banner`` constructor kwargs.
_UNSET: object = object()


def _as_frame_items(value) -> list:
    """Normalize a frame-hook return into a component list.

    The frame hooks accept ``None`` (no frame), a single V2 component,
    or a list of components; page assembly always splices lists. Bare
    strings wrap in ``TextDisplay``, matching ``card()``'s forgiveness.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [TextDisplay(item) if isinstance(item, str) else item for item in items]


def _normalize_entries(entries, owner: str, source: str):
    """Resolve leaderboard entries to the ``(user_id, stats)`` pairs the board reads.

    Every consumer unpacks two items and then reads the second as a mapping,
    so a wrong shape surfaces deep inside a hashing helper or a page builder
    as an attribute error naming ``items`` or an unpack error naming an int.
    Neither says what the board wanted.

    A mapping of id to stats and a one-shot iterable are the same data in a
    different container, so both are converted. Anything else raises here,
    where the parameter and the source are still known.

    Args:
        entries: The caller's value.
        owner: Class name for the message.
        source: Where the value came from, so the message points at the
            constructor kwarg or the overridden hook rather than at neither.
    """
    if entries is None:
        raise TypeError(
            f"{owner}: {source} returned None. Return a sequence of "
            f"(user_id, stats_dict) pairs, or an empty list for no entries."
        )
    if hasattr(entries, "items"):
        entries = list(entries.items())
    elif not hasattr(entries, "__len__") or not hasattr(entries, "__getitem__"):
        try:
            entries = list(entries)
        except TypeError:
            raise TypeError(
                f"{owner}: {source} must be a sequence of (user_id, stats_dict) "
                f"pairs, got {type(entries).__name__}"
            ) from None
    for index, entry in enumerate(entries):
        # A mapping answers len() and then indexes by key, so entry[1] would
        # raise KeyError from inside this check instead of naming the shape.
        # Strings and sets fail the same reads for their own reasons.
        if (
            isinstance(entry, (str, bytes))
            or hasattr(entry, "items")
            or not hasattr(entry, "__len__")
            or not hasattr(entry, "__getitem__")
        ):
            raise TypeError(
                f"{owner}: {source}[{index}] must be a (user_id, stats_dict) pair, "
                f"got {type(entry).__name__}: {entry!r}"
            )
        if len(entry) != 2:
            raise ValueError(
                f"{owner}: {source}[{index}] must have exactly two items "
                f"(user_id, stats_dict), got {len(entry)}: {entry!r}"
            )
        if not hasattr(entry[1], "items"):
            raise TypeError(
                f"{owner}: {source}[{index}] stats must be a mapping, got "
                f"{type(entry[1]).__name__}: {entry[1]!r}\n"
                f"  Fix: pair each id with a dict, e.g. (user_id, {{'score': 50}})."
            )
    return entries


def _coerce_banner(value, owner: str):
    """Coerce a ``banner`` value to the media reference ``gallery`` accepts.

    ``None`` passes through, since a banner is optional where a builder's
    media argument is required. An empty or whitespace-only reference
    normalizes to ``None`` for the same reason, rather than raising the
    builders' required-media error: a blank banner means no banner, and
    the masthead skips an absent one. Everything else resolves through
    the same coercion the V2 media builders use, so an Asset, a File, and
    a URL string mean here exactly what they mean there.
    """
    if value is None:
        return None
    resolved = _resolve_media_ref(value, owner=owner, param="banner=")
    url = _media_ref_url(resolved)
    if url is not None and not url.strip():
        return None
    return resolved


# A Section row costs four components (the Section, two text nodes, and
# the thumbnail accessory), and the default single-page frame costs four
# more. Measured against the message cap: nine rows land exactly on it,
# ten overflow. A paged board fits fewer because it also pays a nav row.
_SECTIONS_ROW_COMPONENTS = 4
_SECTIONS_FRAME_COMPONENTS = 4
_SECTIONS_SINGLE_PAGE_MAX = (
    MAX_MESSAGE_COMPONENTS - _SECTIONS_FRAME_COMPONENTS
) // _SECTIONS_ROW_COMPONENTS

_DEFAULT_AVATAR_CDN = "https://cdn.discordapp.com/embed/avatars/{index}.png"


def _default_avatar_url(user_id: int) -> str:
    """Discord's default-avatar CDN URL for an unresolved user id.

    Discord assigns one of six default avatars by ``(id >> 22) % 6`` under
    the post-migration username system. The default ``get_avatar_url`` uses
    this when a bot is available but the user is not in cache, so every
    section-mode entry renders a thumbnail rather than the uneven
    TextDisplay fallback.
    """
    return _DEFAULT_AVATAR_CDN.format(index=(user_id >> 22) % 6)


def _per_page_advice(per_page: Optional[int]) -> str:
    """Phrase the per-page fix for whichever value the board is carrying.

    A None per_page collapses every entry onto one page, which is the
    configuration most likely to overflow -- and the one where "lower
    leaderboard_per_page" is not an instruction anyone can follow.
    """
    if per_page is None:
        return "set leaderboard_per_page to split the board across pages"
    return f"lower leaderboard_per_page (currently {per_page!r})"


# // ========================================( Shared Mixin )======================================== // #


class _BaseLeaderboardMixin:
    """Shared leaderboard rendering logic for V2 variants.

    Holds the data access pattern, entry formatting, page-frame hooks,
    and empty-state handling. Concrete subclasses supply the component
    tree assembly.

    Internal. Not exported. The public hierarchy
    (``LeaderboardLayoutView`` / ``PersistentLeaderboardLayoutView``)
    is unchanged.
    """

    # Rows without a display_name render as <@id>, so a stock board would
    # ping every ranked player when sent; the rows still render as mention
    # links. Applied to every render. Override to let one notify.
    allowed_mentions = discord.AllowedMentions.none()

    # Total entries to consider from the data source
    leaderboard_top_n: int = 10

    # Entries per page; ``None`` puts every entry on one page
    leaderboard_per_page: Optional[int] = 5

    # Rankings card H2 heading; the title= kwarg overrides it. Falsy renders
    # no heading, so a banner-only masthead needs no override.
    title: Optional[str] = "Leaderboard"

    # Full-width image atop the rankings card: a URL, a discord.File, or
    # anything with a string .url (guild.icon works). build_title overrides it.
    banner: Optional[MediaInput] = None

    # H3 above the ranked rows; falsy skips it. Per board, pass subtitle=.
    subtitle: Optional[str] = "Rankings"

    # Static message when no entries exist
    leaderboard_empty_message: str = "No entries recorded yet."

    # "lines" packs a page into one TextDisplay; "sections" renders a Section
    # per entry with its avatar (stacked text when none resolves), capping
    # leaderboard_per_page at 5.
    entry_layout: str = "lines"

    # The rank 1-3 glyphs format_rank reads; later ranks render "**{rank}.**".
    podium_emojis: ClassVar[Dict[int, str]] = {
        1: "\U0001f947",  # gold medal
        2: "\U0001f948",  # silver medal
        3: "\U0001f949",  # bronze medal
    }

    # Between the name and stat columns in format_entry ("lines" mode).
    entry_separator: str = " -- "

    # The rankings card's accent; None falls through to the theme.
    card_color: Optional[discord.Color] = None

    # Whether to render a horizontal divider below the title and above
    # the rest of the card content. Disable for a more compact card
    # without rewriting ``_build_leaderboard_pages``.
    show_title_divider: bool = True

    _POSITIVE_INT_ATTRS: ClassVar[tuple] = (
        *_BasePaginatedMixin._POSITIVE_INT_ATTRS,
        "leaderboard_top_n",
        "leaderboard_per_page",
    )
    _ENUM_ATTRS: ClassVar[dict] = {
        **_StatefulMixin._ENUM_ATTRS,
        "entry_layout": {"lines", "sections"},
    }
    # Opt-in: render Discord default avatars immediately, then resolve the real
    # ones off the render path and reload(force=True) when ready. Avoids
    # choosing between cached defaults and a per-row fetch before the first render.
    avatar_backfill: ClassVar[bool] = False
    _BOOL_ATTRS: ClassVar[tuple] = (
        *_BasePaginatedMixin._BOOL_ATTRS,
        "show_title_divider",
        "avatar_backfill",
    )
    _STR_OR_NONE_ATTRS: ClassVar[tuple] = ("title", "subtitle")

    @classmethod
    def _validate_attribute_value(cls, name: str, value) -> None:
        """Extend the base dispatch with the ``banner`` media check.

        Routing through this seam keeps definition-time validation and
        ``set_class_attribute`` in agreement: both reject an
        unrecognizable ``banner`` with the coercion helper's directed
        ``TypeError`` at the point of the mistake.
        """
        if name == "banner":
            if value is not None:
                _coerce_banner(value, cls.__name__)
            return
        if name == "podium_emojis":
            # Read with .get() for every row, so a wrong shape would fail
            # inside the render, naming neither the attribute nor the fix.
            if not isinstance(value, Mapping) or not all(
                isinstance(rank, int) and not isinstance(rank, bool) and isinstance(text, str)
                for rank, text in value.items()
            ):
                raise TypeError(
                    f"{cls.__name__}.podium_emojis must map ranks (int) to the text shown "
                    f"for them, like {{1: '1st'}}; got {value!r}"
                )
            return
        if name in ("entry_separator", "leaderboard_empty_message"):
            # The empty-board message becomes a text display's content, which
            # Discord refuses as anything but a string, naming nothing.
            if not isinstance(value, str):
                raise TypeError(f"{cls.__name__}.{name} must be a str, got {type(value).__name__}")
            return
        super()._validate_attribute_value(name, value)

    @classmethod
    def _validate_class_attributes(cls) -> None:
        """Extend base validation with the entry_layout / per_page coupling.

        ``entry_layout = "sections"`` renders one Discord Section per
        entry plus a wrapping card, which pushes the V2 40-component
        budget when combined with a high ``leaderboard_per_page``. The
        library caps sections-mode pages at 5 entries and enforces the
        constraint at class-definition time so the typo / misuse fails
        at module import, not at first render.
        """
        super()._validate_class_attributes()
        own = cls.__dict__
        layout = own.get("entry_layout", getattr(cls, "entry_layout", "lines"))
        per_page = own.get("leaderboard_per_page", getattr(cls, "leaderboard_per_page", None))
        if layout == "sections" and per_page is not None and per_page > 5:
            raise ValueError(
                f"{cls.__name__}.entry_layout='sections' requires "
                f"leaderboard_per_page <= 5 (Discord component budget); "
                f"got leaderboard_per_page={per_page}."
            )
        for name in ("banner", "podium_emojis", "entry_separator", "leaderboard_empty_message"):
            if name in own:
                cls._validate_attribute_value(name, own[name])

        # format_entry is synchronous, so an async column hook would render
        # "<coroutine object ...>" in the row with nothing raised.
        for name in ("format_rank", "format_name", "format_stats", "format_accessory"):
            hook = own.get(name)
            if hook is not None and is_async_callable(hook):
                raise TypeError(
                    f"{cls.__name__}.{name} must be synchronous; it composes the "
                    f"rank row inside format_entry, which cannot await it, so an "
                    f"async one renders as a coroutine repr in the board."
                    f"\n  Fix: keep it a plain def. Resolve anything that needs "
                    f"awaiting in get_entries or on_load, and read the result here."
                )

    def get_entries(self) -> EntryList:
        """Return the sorted leaderboard entries as ``(user_id, stats)`` pairs.

        Override for live data sources. A synchronous override reads
        something already in memory (``store.computed``,
        ``StateStore.iter_scoped``, a cache the view owns); ``async def``
        is accepted for a source that must be awaited, such as a database.

        An async override is read on every rebuild, and a rebuild runs on
        every store dispatch the view is notified for. Where that is too
        much traffic for the source, fetch in :meth:`on_load` instead,
        cache on an attribute of your own, and return the cache from a
        synchronous override here: :meth:`reload` is then the explicit
        re-fetch, and it serializes and coalesces those fetches.

        Default returns the ``entries=`` kwarg passed at construction.
        """
        return self._entries

    def format_rank(self, rank: int) -> str:
        """Render the rank column for one entry.

        Default reads ``self.podium_emojis`` for ranks 1-3 and renders
        ``f"**{rank}.**"`` for every lower rank. Subclasses change the
        podium treatment by overriding the ``podium_emojis`` class
        attribute; override this method only when the rank-glyph
        choice depends on entry data beyond the rank number.
        """
        return self.podium_emojis.get(rank, f"**{rank}.**")

    def format_name(self, user_id: int, stats: dict) -> str:
        """Render the name column for one entry.

        Routes to the right Discord syntax based on what the entry
        carries: entries with a ``display_name`` render the label
        verbatim; entries without one render as a mention
        (``<@user_id>``). The default imposes no formatting opinion --
        callers control styling by embedding markdown directly in
        ``display_name`` (``**Bold**``, ``*italic*``, ``[link](url)``,
        plain text, etc.). Override this method only when routing logic
        needs to depend on stat fields beyond ``display_name``.
        """
        display = stats.get("display_name")
        return display if display else f"<@{user_id}>"

    def format_stats(self, user_id: int, stats: dict) -> str:
        """Render the inline stat column for one entry.

        Default returns ``{wins}W / {games}G``. Override to surface
        game-specific stats -- win rate, MMR, forfeits, streak, etc.
        """
        return f"{stats.get('wins', 0)}W / {stats.get('games', 0)}G"

    def format_accessory(self, user_id: int, stats: dict) -> Optional[str]:
        """Render an optional right-side accessory for one entry.

        Default returns ``None`` (no accessory). Override to add a
        trailing tag -- streak emoji, "new" badge, country flag,
        etc. The return value is appended to the composed entry
        line with a leading space.
        """
        return None

    def format_entry(self, rank: int, user_id: int, stats: dict) -> str:
        """Compose one ranked line from the four format hooks.

        The default implementation is intentionally a thin composition
        of ``format_rank``, ``format_name``, ``format_stats``, and
        ``format_accessory`` so subclasses can override the smallest
        piece they need. Override this method directly when the layout
        itself needs to change (multi-line, different separator, etc.).

        Used by ``entry_layout = "lines"``. Section mode renders through
        ``format_primary`` + ``format_secondary`` instead.
        """
        rank_str = self.format_rank(rank)
        name_str = self.format_name(user_id, stats)
        stats_str = self.format_stats(user_id, stats)
        accessory = self.format_accessory(user_id, stats)
        base = f"{rank_str} {name_str}{self.entry_separator}{stats_str}"
        return f"{base} {accessory}" if accessory else base

    def format_primary(self, rank: int, user_id: int, stats: dict) -> str:
        """Top line of a section-rendered entry.

        Used only when ``entry_layout = "sections"``. Default composes
        ``format_rank`` and ``format_name``; override to change the
        section's primary label.
        """
        return f"{self.format_rank(rank)} {self.format_name(user_id, stats)}"

    def format_secondary(self, rank: int, user_id: int, stats: dict) -> str:
        """Bottom line of a section-rendered entry.

        Used only when ``entry_layout = "sections"``. Default returns
        ``format_stats(...)`` so the section body shows the same stat
        string the lines mode would. Override to show a different
        subtitle (e.g. a progress bar, streak emoji, join date).
        """
        return self.format_stats(user_id, stats)

    async def get_avatar_url(self, user_id: int, stats: dict) -> Optional[str]:
        """Return a URL for the per-entry thumbnail in section mode.

        Only called when ``entry_layout = "sections"``. The default resolves
        avatars when a ``bot`` is available -- passed via the ``bot=``
        constructor kwarg, or injected through ``on_bind`` on the persistent
        variant:

        - a cache hit (``bot.get_user``) returns the member's 128px avatar,
          a synchronous lookup that keeps the resolve off the render path;
        - a cache miss returns a Discord default-avatar URL, so every entry
          still renders a Section thumbnail instead of an uneven mix;
        - with no bot, returns ``None`` and the entry falls back to a stacked
          two-line ``TextDisplay`` (Discord's ``Section`` requires a non-None
          accessory).

        Async so an override that genuinely must await a non-cache source
        still can, but prefer the cache: a per-entry ``fetch_user`` issues
        one HTTP request per ranked row before the first render, and every
        one of them shares a single rate-limit bucket, so the wait grows with
        entry count. Override to resolve from a
        different source, or to force the TextDisplay fallback (return
        ``None``) even when a bot is set.
        """
        cached = getattr(self, "_avatar_cache", {}).get(user_id)
        if cached:
            return cached
        bot = self.bot
        if bot is None:
            return None
        user = bot.get_user(user_id)
        if user is not None:
            return user.display_avatar.with_size(128).url
        return _default_avatar_url(user_id)

    def _maybe_schedule_avatar_backfill(self, top, avatar_urls) -> None:
        """Schedule an off-path avatar resolve when section-mode entries missed
        the cache and rendered defaults.

        Runs at most once per entry set: the signature guard stops the
        resolve -> reload(force=True) cycle from looping when some ids stay
        unresolvable. No-op unless ``avatar_backfill`` is set and a bot is
        available. The miss signal is the default-avatar URL the default
        ``get_avatar_url`` returns on a cache miss, so this pairs with the
        default resolver, not a custom ``get_avatar_url`` override.
        """
        if not self.avatar_backfill or self.bot is None:
            return
        signature = self._entries_signature_for(top)
        if getattr(self, "_avatar_backfilled_signature", None) == signature:
            return
        unresolved = [
            uid
            for idx, (uid, _stats) in enumerate(top)
            if avatar_urls[idx] == _default_avatar_url(uid)
        ]
        if not unresolved:
            return
        self._avatar_backfilled_signature = signature
        self.task_manager.create_task(self.id, self._backfill_avatars(unresolved))

    async def _backfill_avatars(self, user_ids: list) -> None:
        """Resolve missed avatars off-path, then reload to render them."""
        # This runs in a task that inherited the acting-interaction contextvar;
        # clear it so the reload below cannot route through the acting-view fast
        # path on a stale interaction (mirrors _deferred_refresh's reset).
        from ...state.store import _CURRENT_INTERACTION

        _CURRENT_INTERACTION.set(None)
        resolved = await await_maybe(self.resolve_avatar_urls(user_ids))
        if not resolved:
            return
        cache = self.__dict__.setdefault("_avatar_cache", {})
        cache.update(resolved)
        await self.reload(force=True)

    async def resolve_avatar_urls(self, user_ids: list) -> dict:
        """Resolve avatar URLs for cache-missed entries, off the render path.

        Called by the ``avatar_backfill`` pass in a background task after the
        first render painted defaults. The default fetches each id via the bot
        (one HTTP round-trip per id, acceptable off-path). Override to batch via
        ``guild.query_members(user_ids=...)`` for a large top-N, or to resolve
        from another source. Return a ``{user_id: url}`` mapping; omit ids that
        cannot be resolved.
        """
        bot = self.bot
        if bot is None:
            return {}
        resolved: dict = {}
        for uid in user_ids:
            try:
                user = await bot.fetch_user(uid)
            except Exception:
                continue
            resolved[uid] = user.display_avatar.with_size(128).url
        return resolved

    @property
    def bot(self) -> Optional[discord.Client]:
        """The ``discord.Client`` from the ``bot=`` kwarg (or ``on_bind``), or ``None``.

        Read-only. ``get_avatar_url`` and ``resolve_avatar_urls`` overrides read
        this to resolve avatars instead of reaching into ``_bot``.
        """
        return getattr(self, "_bot", None)

    @property
    def ranked_entries(self) -> List[Tuple[int, dict]]:
        """The loaded top-N ``(user_id, stats)`` slice for the current render.

        Populated before each page build, so ``build_header`` /
        ``build_footer`` / ``build_title`` overrides can compute stats over
        the ranked rows without re-fetching. It holds only the top
        ``leaderboard_top_n``; totals across every entry come from
        :meth:`get_entries`. Empty until the first build.
        """
        return getattr(self, "_ranked_entries", [])

    def build_title(self, page: int) -> Union[Item, List[Item], None]:
        """Render optional components replacing the card's masthead.

        The masthead is the rankings card's identity strip: the
        ``banner`` image and the ``## title`` heading. Called once per
        page with the zero-based page index. Return a single V2
        component or a list to render as the masthead for that page;
        return an empty list to render no masthead on that page; return
        ``None`` (default) to compose the masthead from the declarative
        pair instead -- the ``banner`` image when set, then the
        ``title`` heading when truthy. ``show_title_divider`` draws
        below whichever masthead renders and is skipped when the
        masthead is empty.

        The returned components render inside the rankings card, so
        they must be Container-legal children (``gallery(...)``,
        ``TextDisplay``, ``Section``; a nested ``card(...)`` is rejected
        by ``card()`` itself during the page build). The empty-state
        page has no rankings card, so the masthead renders at the page's
        top level there; the same components stay legal. Pages rebuild
        only when the entry signature changes, so a masthead that depends
        on data outside the entries needs ``reload(force=True)``, the same
        contract as the other frame hooks.
        """
        return None

    async def _build_masthead(self, page: int) -> list:
        """Compose the rankings card's masthead components for one page.

        The ``build_title`` hook wins whenever it returns non-``None``:
        component(s) render as the masthead, and an explicit empty list
        renders no masthead for that page. Only ``None`` falls through
        to the declarative pair, in order: the ``banner`` image, then
        the ``## title`` heading. Both unset composes an empty masthead.
        """
        hook_value = await await_maybe(self.build_title(page))
        if hook_value is not None:
            return _as_frame_items(hook_value)
        items: list = []
        media = _coerce_banner(self.banner, type(self).__name__)
        if media:
            items.append(gallery(media))
        if self.title:
            items.append(TextDisplay(f"## {self.title}"))
        return items

    def build_header(self, page: int) -> Union[Item, List[Item], None]:
        """Render optional content above the rankings card.

        Called once per page on every page rebuild, with the zero-based
        ``page`` index (gate on it to frame only some pages, e.g.
        ``if page != 0: return None``). The returned component(s) are
        prepended to the page's top-level components as-is: a ``Container``
        renders as its own card, anything else floats as a bare top-level
        item. Unlike ``build_footer``, there is no return-type branching
        here: the value is placed as given. Read ``self.ranked_entries``
        for stats over the ranked rows, and the full list
        :meth:`get_entries` returns for totals across every entry; an
        Overview ``stats_card(...)`` is the typical use. Return a single V2
        component, a list of components, or ``None`` (default) for no
        header. On the empty-state page the
        return renders above the masthead, at the page's top level;
        ``ranked_entries`` is empty there, so a hook computing aggregates
        must tolerate an empty slice.

        Pages rebuild only when the entry signature changes, so a header
        that depends on data outside the entries needs ``reload(force=True)``.
        A ``Container`` header also opts the page out of the
        ``nav_inside_container`` wrap: the rankings card is a Container and
        Discord forbids Container nesting, so those pages keep the sibling
        layout with the nav row as a separate row.
        """
        return None

    def build_footer(self, page: int) -> Union[Item, List[Item], None]:
        """Render optional footer components, placed by return type.

        Called once per page with the zero-based page index. A raw
        component (a caption ``TextDisplay``, a link row, a closing image)
        folds INSIDE the rankings card as a trailing child, so it stays
        attached to the ranking rows rather than floating; a ``Container``
        (a ``card(...)``) renders as its own standalone card below the
        rankings. Return a single component, a list (each item placed by
        its own type), or ``None`` (default) for no footer.

        The rebuild caveat on :meth:`build_header` applies identically. On
        the empty-state page there is no rankings card to fold into, so
        every footer component (raw or ``Container``) renders at the
        page's top level below the empty-state content, in returned order.
        """
        return None

    def _resolve_per_page(self) -> int:
        if self.leaderboard_per_page is not None:
            return self.leaderboard_per_page
        return self.leaderboard_top_n

    def on_leaderboard_empty(self) -> Union[Item, List[Item]]:
        """Return the V2 components shown when no entries exist.

        Default wraps ``leaderboard_empty_message`` in a single card.
        Override to provide a richer empty state: an intro card with a
        call-to-action, a stats legend, or a "play your first game"
        button. Returns the V2 component list that should render as the
        sole page while the leaderboard is empty; a single component is
        accepted and wrapped, same as a page value.

        The masthead (``banner`` / ``title``, or whatever ``build_title``
        returns) is composed above this return rather than inside it, so
        the board keeps its identity while empty and an override inherits
        it without composing it. ``build_title`` returning ``[]`` renders
        no masthead on this page, same as on any other. The
        ``build_header`` / ``build_footer`` frames compose around this
        return the same way: header components render above the masthead,
        footer components below this return. There is no rankings card
        here, so every frame component sits at the page's top level in
        returned order -- a raw footer floats below the empty-state
        content instead of folding into a card.

        ``ranked_entries`` is empty here, so a hook that reads it for
        aggregate stats sees an empty board rather than the last
        populated build's slice.

        Returns:
            A list of V2 components that become the single empty-state
            page in the paginated view.
        """
        return [card(TextDisplay(self.leaderboard_empty_message))]

    async def on_state_changed(self, state):
        """Re-fetch entries and rebuild pages before the paginated refresh.

        Live-data subclasses (typically persistent boards subscribed to
        ``SCOPED_UPDATE`` or a custom action) override ``get_entries()``
        to read the current state. This hook runs ``rebuild_pages()``
        first so the paginated ``_update_page()`` call picks up the new
        entry set instead of rendering from a stale ``self.pages``.

        ``rebuild_pages`` short-circuits when the entry signature has
        not changed, so button-click dispatches that do not mutate
        leaderboard data skip the avatar-resolve fan-out entirely.

        The rebuild takes the view's reload turn, so it runs after a
        reload already fetching rather than beside it: an older fetch
        finishing last would otherwise overwrite the newer one.
        """
        async with self._within_reload_turn("rebuild_pages()"):
            await self.rebuild_pages()
        await super().on_state_changed(state)

    async def _build_leaderboard_pages(self, entries=None) -> list:
        """Convert entries into a list of V2 component lists for pagination.

        Async because ``entry_layout = "sections"`` awaits
        ``get_avatar_url`` once per entry to resolve optional thumbnails.
        Lines mode never awaits but shares this coroutine so the two
        render branches sit behind one coherent builder.

        ``entries`` accepts a set already read from ``get_entries()`` so one
        rebuild costs one read; omit it and the hook is consulted here.

        The whole build runs inside the view's theme context so the
        rankings card and every user hook invoked here (``build_title``,
        ``build_header``, ``build_footer``) inherit the
        view's accent colour, whichever caller triggered the rebuild.
        """
        from ...theming.context import theme_context

        with theme_context(self.get_theme()):
            return await self._build_leaderboard_pages_inner(entries)

    async def _build_leaderboard_pages_inner(self, entries=None) -> list:
        # rebuild_pages passes what it already read, so an async source is
        # queried once per rebuild and renders the set its signature describes.
        if entries is None:
            entries = await await_maybe(self.get_entries())
        entries = _normalize_entries(entries, type(self).__name__, "get_entries()")

        if not entries:
            # Cleared before the masthead and frames compose, so a hook
            # reading ranked_entries sees an empty board rather than the
            # slice from the last populated build.
            self._ranked_entries = []
            items = await self._build_masthead(0)
            if items and self.show_title_divider:
                items.append(divider())
            # The board keeps its frames while empty. With no rankings card to
            # fold into, header and footer render at the page's top level.
            header = _as_frame_items(await await_maybe(self.build_header(0)))
            footer = _as_frame_items(await await_maybe(self.build_footer(0)))
            # Composed here, not in the default hook (see on_leaderboard_empty);
            # _resolve_page accepts every shape a page value may take.
            return [
                [
                    *header,
                    *items,
                    *self._resolve_page(await await_maybe(self.on_leaderboard_empty())),
                    *footer,
                ]
            ]

        top = entries[: self.leaderboard_top_n]
        # Expose the loaded top-N slice so build_header / build_footer /
        # build_title overrides can read the ranked data (aggregate stats,
        # counts) without re-fetching.
        self._ranked_entries = top
        per_page = self._resolve_per_page()
        total_entries = len(top)
        total_pages = (total_entries + per_page - 1) // per_page

        # Subtitle is optional; falsy values (None, empty string) skip
        # the H3 entirely, which is the natural shape for a two-card look.
        heading = f"### {self.subtitle}" if self.subtitle else None

        # One fan-out over the whole slice, so the page loop below reads by
        # absolute entry index. Per-entry fetch_user calls share one rate-limit
        # bucket, so the gather is paced by it (see get_avatar_url).
        if self.entry_layout == "sections":
            avatar_urls = await asyncio.gather(
                *(await_maybe(self.get_avatar_url(uid, stats)) for uid, stats in top),
                return_exceptions=False,
            )
            self._maybe_schedule_avatar_backfill(top, avatar_urls)
        else:
            avatar_urls = []

        pages = []
        for page_idx in range(total_pages):
            start = page_idx * per_page
            end = start + per_page
            page_entries = top[start:end]

            items: list = await self._build_masthead(page_idx)
            if items and self.show_title_divider:
                items.append(divider())

            if self.entry_layout == "sections":
                if heading is not None:
                    items.append(TextDisplay(heading))
                else:
                    # Preserve the vertical rhythm the H3 would have occupied
                    # so the rank rows do not butt up against the divider.
                    items.append(gap())
                for offset, (uid, stats) in enumerate(page_entries):
                    rank = start + offset + 1
                    primary = await await_maybe(self.format_primary(rank, uid, stats))
                    secondary = await await_maybe(self.format_secondary(rank, uid, stats))
                    avatar = avatar_urls[start + offset]
                    # A whitespace-only override return can never resolve as
                    # media, so it takes the same stacked fallback as None
                    # instead of raising the empty-media error from inside a
                    # page build the library drives.
                    if isinstance(avatar, str) and not avatar.strip():
                        avatar = None
                    if avatar:
                        items.append(image_section(primary, secondary, url=avatar))
                    else:
                        # A Section needs an accessory, so an entry with no
                        # avatar stacks as a TextDisplay, dropping an empty half
                        # as image_section does.
                        stacked = "\n".join(part for part in (primary, secondary) if part)
                        items.append(TextDisplay(stacked))
            else:
                lines = [
                    await await_maybe(self.format_entry(start + offset + 1, uid, stats))
                    for offset, (uid, stats) in enumerate(page_entries)
                ]
                body = "\n".join(lines)
                if heading:
                    items.append(TextDisplay(f"{heading}\n{body}"))
                else:
                    # Preserve the vertical rhythm the H3 would have occupied
                    # so the rank rows do not butt up against the divider.
                    items.append(gap())
                    items.append(TextDisplay(body))

            # Placed by return type (see build_footer). A raw footer folded in
            # also keeps the page one Container nav_inside_container can wrap.
            footer = _as_frame_items(await await_maybe(self.build_footer(page_idx)))
            footer_cards = [f for f in footer if isinstance(f, Container)]
            footer_inline = [f for f in footer if not isinstance(f, Container)]
            card_children = [*items, *footer_inline] if footer_inline else items
            page_components: list = [card(*card_children, color=self.card_color)]
            header = _as_frame_items(await await_maybe(self.build_header(page_idx)))
            pages.append([*header, *page_components, *footer_cards])

        return pages


# // ========================================( V2 Leaderboard )======================================== // #


class LeaderboardLayoutView(_BaseLeaderboardMixin, PaginatedLayoutView):
    """V2 leaderboard view with paginated card-based layout.

    Renders a sorted list of ``(user_id, stats)`` entries across one or
    more pages. Each page is a card with ranked entry lines. The
    ``build_header`` / ``build_footer`` hooks add optional frame content
    above and below the card, on whichever pages the override chooses.

    When all entries fit on a single page, no navigation buttons are
    shown -- the view behaves identically to a static card.

    Override hooks:
        ``format_entry(rank, user_id, stats)``
            One line per ranked player. Default shows wins and games.
        ``build_title(page)``
            Optional components replacing the rankings card's masthead
            (the ``banner`` image + ``## title`` heading). ``None``
            (default) composes the masthead from the declarative
            ``banner`` / ``title`` pair.
        ``build_header(page)``
            Content above the rankings card -- an Overview ``stats_card``,
            a banner image, any component. A ``Container`` renders as its
            own card, a raw component floats. ``self.ranked_entries``
            holds the ranked rows, ``get_entries()`` every entry.
            ``None`` (default) renders nothing.
        ``build_footer(page)``
            Content below the rankings, placed by return type: a raw
            component folds inside the rankings card (below the entries),
            a ``Container`` renders as its own card below. ``None``
            (default) renders nothing.
        ``get_entries()``
            Data source. Default returns constructor ``entries=``.

    Card masthead:
        ``banner``
            Full-width image at the top of the rankings card, above the
            title heading when both are set. URL string, ``discord.File``,
            or anything with a string ``.url`` (``guild.icon`` works
            directly). Default ``None`` (no banner). Class attribute OR
            ``banner=`` constructor kwarg.
        ``title``
            H2 heading on the rankings card (default ``"Leaderboard"``).
            Class attribute OR ``title=`` constructor kwarg. Pass
            ``title=None`` (or an empty string) to render no text
            heading: with ``banner`` set, the banner alone is the
            masthead; with neither, the card starts at its content and
            the title divider is skipped. Mutating ``banner`` or
            ``title`` on a live view needs ``reload(force=True)``:
            the entry-signature short-circuit skips rebuilds when the
            ranked data is unchanged.
        ``subtitle``
            H3 subtitle above the ranking rows (default ``"Rankings"``).
            Class attribute OR ``subtitle=`` constructor kwarg. Set to
            ``None`` (or empty string) to skip the H3 entirely, which
            pairs naturally with an Overview ``build_header`` card that
            already carries its own heading. Assign ``self.subtitle``
            in a subclass ``__init__`` for dynamic content (truncation
            count, filter context, etc.).

    Pagination controls:
        ``leaderboard_top_n``
            Total entries to consider from the data source (default 10).
        ``leaderboard_per_page``
            Entries per page (default 5). ``None`` puts every entry on one
            page, sized by ``leaderboard_top_n``.

    Avatar resolution:
        ``bot``
            Optional ``bot=`` constructor kwarg (a ``discord.Client``).
            When set, the default ``get_avatar_url`` resolves each
            section-mode entry's avatar from the bot's user cache (a
            Discord default avatar on a miss); without it, avatars
            resolve to ``None`` and entries fall back to the two-line
            ``TextDisplay``. The persistent variant receives the bot
            through ``on_bind`` instead. Override ``get_avatar_url`` for
            a different source.

    Example::

        entries = store.computed["my_leaderboard"].get(guild_id, [])
        view = LeaderboardLayoutView(
            context=context,
            entries=entries,
            title=f"Leaderboard - {context.guild.name}",
        )
        await view.send(ephemeral=True)
    """

    owner_only = True
    exit_policy = "delete"
    state_scope = None

    def __init__(
        self, *args, entries=None, title=_UNSET, subtitle=_UNSET, banner=_UNSET, bot=None, **kwargs
    ):
        # Checked here, so a wrong bot fails at construction rather than as an
        # AttributeError inside the avatar gather. A live reference, it is left
        # out of the persistence round-trip (_NON_PERSISTABLE_KWARGS).
        if bot is not None and not isinstance(bot, discord.Client):
            raise TypeError(f"bot must be a discord.Client (or subclass), got {type(bot).__name__}")
        self._bot = bot
        # ``title`` / ``subtitle`` / ``banner`` share the sentinel shape:
        # passing ``None`` explicitly suppresses that masthead piece,
        # while omitting the kwarg falls back to the class default.
        if title is not _UNSET:
            type(self)._validate_str_or_none("title", title)
            self.title = title
        if subtitle is not _UNSET:
            type(self)._validate_str_or_none("subtitle", subtitle)
            self.subtitle = subtitle
        if banner is not _UNSET:
            self.banner = _coerce_banner(banner, type(self).__name__)
        self._entries = (
            _normalize_entries(entries, type(self).__name__, "entries=") if entries else []
        )
        # A reload(force=True) not yet taken by a rebuild; the next
        # rebuild_pages consumes it. See reload().
        self._force_rebuild = False
        # Pages build lives in ``on_load()`` so the async ``get_avatar_url``
        # hook can resolve thumbnails before the first render. ``__init__``
        # hands the paginated base an empty list until then.
        kwargs["pages"] = []
        super().__init__(*args, **kwargs)
        # The kwargs snapshot holds banner= as passed; a discord.Asset would
        # fail the registry row's JSON, so the resolved URL replaces it.
        if "banner" in self._init_kwargs:
            self._init_kwargs["banner"] = self.banner

    async def on_load(self) -> None:
        """Fetch entries and rebuild the page tree before display.

        Called automatically before the initial send (via the send
        pipeline), on every push/pop edit, and by :meth:`reload`. Fetches
        through :meth:`rebuild_pages` (which short-circuits when the entry
        signature is unchanged), then recomposes the nav buttons and page
        tree in the canonical order (page content -> nav row -> extras) so
        the first render and every :meth:`reload` call are visually
        identical. The render-hash short-circuit in :meth:`refresh` skips
        the message edit when the recomposed tree matches the displayed one,
        so a no-change ``reload()`` ships nothing.

        The paginated base initializes with an empty page list (see
        ``__init__``), so the component tree holds a "No pages."
        placeholder until this runs. Rebuilding the nav row against the
        final page count keeps ``_show_jump`` and the initial ``disabled``
        state matched to the real total, not the empty-list ``__init__``
        snapshot. An override calls ``super().on_load()`` after anything it
        fetches: the recompose it ends with rebuilds the nav row against the
        final page count and restores the Back button ``push()`` adds.
        """
        # Entries first: the base recomposes the tree when an instance
        # override changed what it is built from, and that clamps the page
        # against whatever list it finds.
        await self.rebuild_pages()
        await super().on_load()
        # The nav row is rebuilt against the final page count, which __init__
        # could not know.
        self._recompose_page_tree(rebuild_nav=True)

    def _recompose_page_tree(self, *, rebuild_nav: bool = False) -> None:
        """Compose the page tree, restating an overflow in board terms.

        Every path that rebuilds a board arrives here: the initial load, a
        state change that grew the entry list, and an explicit reload. The
        restatement belongs at this one seam rather than at each caller, or
        the automatic path reports the overflow in a vocabulary that does
        not apply to a board while the explicit one reports it correctly.
        """
        try:
            super()._recompose_page_tree(rebuild_nav=rebuild_nav)
        except ValueError as exc:
            if _OVER_CAPACITY_MARKER not in str(exc):
                raise
            raise ValueError(self._over_budget_message()) from exc

    def _page_renders_sections(self) -> bool:
        """Report whether the built page actually carries Section rows.

        A bound client is what lets an avatar resolve, not what guarantees
        one did. The page in hand is the only place the answer is settled.
        """
        pages = self.pages or []
        if not pages:
            return self.bot is not None
        page = pages[min(self.current_page, len(pages) - 1)]
        items = page if isinstance(page, list) else [page]
        for item in items:
            if isinstance(item, Section):
                return True
            walk = getattr(item, "walk_children", None)
            if walk is not None and any(isinstance(child, Section) for child in walk()):
                return True
        return False

    def _over_budget_message(self) -> str:
        """Explain a component-budget overflow in leaderboard terms.

        The layout error the tree raises names components and suggests
        folding text nodes, which is the right advice for a hand-composed
        view and the wrong advice for a board, whose size is set by how many
        entries a page carries.

        The rows are only the whole story when they can account for the
        overflow. A section row costs four components once a client is bound
        and one without, so the same class fits offline and overflows in
        production; a lines board costs one either way, and a five-row lines
        board that overflows is carrying its weight in the page frame
        instead. Naming the rows there would send the reader to two knobs
        that cannot fix it.

        The per-row cost is counted off the page that was just built rather
        than inferred from the client. Whether a section row becomes a
        Section is decided by whether an avatar resolved, so a
        ``get_avatar_url`` override returning nothing degrades every row to
        a stacked text node with a client still bound -- and pricing those
        rows at four blames them for a page whose weight is in its frame.
        """
        owner = type(self).__name__
        per_page = self.leaderboard_per_page
        rows = per_page if per_page is not None else self.leaderboard_top_n
        sections = self.entry_layout == "sections"
        per_row = _SECTIONS_ROW_COMPONENTS if sections and self._page_renders_sections() else 1
        rows_cost = rows * per_row

        if rows_cost <= MAX_MESSAGE_COMPONENTS // 2:
            # The entries are a minority of the budget, so the frame is
            # where it went.
            return (
                f"{owner} exceeds Discord's {MAX_MESSAGE_COMPONENTS}-component "
                f"budget for one message. Its {rows} entries account for about "
                f"{rows_cost} of that, so the rest is page frame: whatever "
                f"build_header, build_footer, and _build_extra_items add to "
                f"every page.\n"
                f"  Fix: trim those hooks, or {_per_page_advice(per_page)} "
                f"to leave them more room."
            )

        bound = "bound" if self.bot is not None else "not bound"
        # Every clause below is conditioned on what the page did, not on
        # what the layout asked for.
        as_sections = sections and per_row > 1
        if as_sections:
            note = (
                " A section row resolves an avatar into a thumbnail only when a "
                "client is bound, so a board that fits with none can overflow "
                "once one is."
            )
            alternative = (
                ", or set entry_layout='lines', which renders one component "
                "per row whatever the client state."
            )
        elif sections:
            note = (
                " Its section rows render as stacked text because no avatar "
                "resolved, so they already cost the one component a lines row "
                "costs and entry_layout is not the knob here."
            )
            alternative = ", or trim build_header / build_footer."
        else:
            note = ""
            alternative = ", or trim build_header / build_footer."
        return (
            f"{owner} composes {rows} entries per page in "
            f"entry_layout={self.entry_layout!r} with a client {bound}, which "
            f"costs {per_row} component(s) per row and exceeds Discord's "
            f"{MAX_MESSAGE_COMPONENTS}-component budget for one message."
            f"{note}\n"
            f"  Fix: {_per_page_advice(per_page)} so "
            f"fewer entries share a page, lower leaderboard_top_n (currently "
            f"{self.leaderboard_top_n})" + alternative
        )

    async def rebuild_pages(self, *, force: bool = False) -> None:
        """Re-fetch entries and rebuild the page list.

        Called automatically by ``on_state_changed`` whenever a subscribed
        action fires, so subscription-driven boards refresh without any
        extra plumbing. For an out-of-band refresh (a manual button,
        ``on_restore``), call :meth:`reload`, which rebuilds and ships the
        edit; this method only rebuilds the page list. Updates the current
        page if entry count shrinks. Async because page construction
        awaits ``get_avatar_url`` under ``entry_layout = "sections"``.

        Short-circuits when ``get_entries()`` returns a sequence that
        matches the signature captured on the previous rebuild. State
        dispatches that do not touch leaderboard data (e.g. the
        ``COMPONENT_INTERACTION`` fired by a page-flip button) return
        without re-resolving avatars. ``force=True`` bypasses the signature
        check -- for when something outside the entry data changed the
        rendered pages (a filter, or a select's highlighted option read
        by ``build_header``). A pending ``reload(force=True)`` forces the
        next rebuild the same way.
        """
        # Taken before the first await, so a rebuild already past this point
        # when the forced reload was requested leaves the request for the next.
        force = force or self._force_rebuild
        self._force_rebuild = False
        entries = _normalize_entries(
            await await_maybe(self.get_entries()), type(self).__name__, "get_entries()"
        )
        signature = self._entries_signature_for(entries)
        if not force and signature == getattr(self, "_entries_signature", None) and self.pages:
            return
        pages = await self._build_leaderboard_pages(entries)
        # Stamped only after the build returns: a raising hook (an avatar
        # resolver, a frame hook) leaves the signature unstamped, so the next
        # rebuild retries instead of short-circuiting onto the stale pages.
        self._entries_signature = signature
        self.pages = pages
        if self.current_page >= len(self.pages):
            self.current_page = max(0, len(self.pages) - 1)

    async def reload(self, *, force: bool = False) -> Optional[RenderOutcome]:
        """Re-fetch entries, re-render, and re-store the board out of band.

        The inherited :meth:`reload` runs ``on_load`` then ``refresh``.
        ``on_load`` fetches through ``rebuild_pages``, which short-circuits
        when the entry signature is unchanged. ``force=True`` makes the
        next page rebuild run regardless, so a change outside the entry
        data (a filter, or a select's highlighted option read by
        ``build_header``) still rebuilds the pages, including when this
        reload queues behind one already running. Call this rather than
        ``rebuild_pages`` directly when triggering an out-of-band refresh.
        """
        # A request rather than a cleared signature: a run already in
        # flight stamps the signature when it finishes, which would erase a
        # clear made while it ran and leave this reload unforced.
        if force:
            self._force_rebuild = True
        # Forwarded so a reload coalesced by refresh_cooldown_ms replays it at
        # the boundary (the deferred re-entry calls reload with the captured
        # kwargs).
        return await super().reload(force=force)

    @staticmethod
    def _entries_signature_for(entries) -> tuple:
        """Hash-safe signature of the entry list for rebuild short-circuit.

        Uses ``(user_id, tuple(sorted(stats.items())))`` per entry so
        stats dicts with different insertion orders still compare equal.
        Non-hashable stat values (lists, nested dicts) degrade to
        ``repr()`` so the signature is always comparable. Mirrors the
        render-hash pattern but scoped to leaderboard data shape, not
        component tree shape.
        """

        def _stat_key(value):
            try:
                hash(value)
            except TypeError:
                return repr(value)
            return value

        return tuple(
            (uid, tuple(sorted((k, _stat_key(v)) for k, v in stats.items())))
            for uid, stats in entries
        )


# // ========================================( Persistent Leaderboard )======================================== // #


class PersistentLeaderboardLayoutView(_PersistentMixin, LeaderboardLayoutView):
    """Persistent V2 leaderboard that survives bot restarts.

    Compose ``_PersistentMixin`` with ``LeaderboardLayoutView`` so the
    admin-posted panel gets ``timeout=None``, restart re-attachment,
    and ``persistence_key`` dedup -- without duplicating the rendering
    logic.

    Subclasses override ``get_entries()`` to read live data (typically
    from ``store.computed`` or ``StateStore.iter_scoped``) and set
    ``subscribed_actions`` for auto-refresh on data changes.

    On restart, ``on_restore`` rebuilds pages from ``get_entries()``
    so the display reflects current data. Page position resets to the
    first page.

    Example::

        class ServerLeaderboard(PersistentLeaderboardLayoutView):
            subscribed_actions = {"SCOPED_UPDATE"}
            title = "Server Rankings"

            def get_entries(self):
                store = get_store()
                return store.computed["my_leaderboards"].get(self.guild_id, [])
    """

    owner_only = False
    exit_policy = "disable"

    async def on_bind(self, bot):
        """Capture the bot so the default ``get_avatar_url`` can resolve avatars.

        The persistent variant cannot carry ``bot`` through the constructor
        round-trip (it is stripped as non-serializable via
        ``_NON_PERSISTABLE_KWARGS``), so the library injects it here at both
        the initial send and every restart, before ``on_restore`` renders.
        """
        await super().on_bind(bot)
        self._bot = bot

    async def on_restore(self, bot):
        """Re-render the board from live data on every restart.

        Reattach registered the lazily-built ``"No pages."`` placeholder
        tree, because ``on_load`` had not run yet, so the real ranking
        selects and nav buttons are absent from discord.py's view store
        until a render ships. :meth:`reload` runs ``on_load`` (which fetches
        through ``rebuild_pages`` and recomposes the tree, warm now that the
        gateway is ready) and then edits the message, and that edit re-stores
        the real components so clicks route immediately. Without the render
        the panel's controls drop clicks until the next state change. The
        restore always ships one edit: a freshly restored view has no
        render-hash baseline to short-circuit against, though an unchanged
        entry set still spares the avatar re-fetch.
        """
        await self.reload()
