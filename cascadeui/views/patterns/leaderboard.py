# // ========================================( Modules )======================================== // #


import asyncio
from typing import ClassVar, Dict, List, Optional, Tuple, Union

import discord
from discord.ui import Container, Item, TextDisplay

from ...components.patterns.v2 import card, divider, gallery, gap, image_section
from ...components.types import MediaInput
from ..base import _StatefulMixin
from ..persistent import _PersistentMixin
from .paginated import PaginatedLayoutView, _BasePaginatedMixin

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


def _coerce_banner(value):
    """Coerce a ``banner`` value to the media reference ``gallery`` accepts.

    ``None``, URL strings, and ``discord.File`` pass through. Objects
    carrying a string ``url`` attribute (``discord.Asset``, so
    ``guild.icon`` and ``member.display_avatar`` work directly) coerce
    to that URL. Anything else raises ``TypeError`` at the call site.
    """
    if value is None or isinstance(value, (str, discord.File)):
        return value
    url = getattr(value, "url", None)
    if isinstance(url, str):
        return url
    raise TypeError(
        f"banner must be a URL string, discord.File, or an object with a "
        f"string .url attribute (e.g. discord.Asset); got {type(value).__name__}"
    )


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

    # Entries without a ``display_name`` render as ``<@id>`` mentions, so
    # a stock leaderboard would notify every ranked player on the initial
    # send and again on every settlement refresh. The rows still render as
    # mention links; they just stop pinging. Override with a permissive
    # AllowedMentions on a board that genuinely wants to notify.
    allowed_mentions = discord.AllowedMentions.none()

    # Total entries to consider from the data source
    leaderboard_top_n: int = 10

    # Entries per page; ``None`` defaults to ``leaderboard_top_n`` (single page)
    leaderboard_per_page: Optional[int] = 5

    # Rankings card H2 title. Default when no ``title=`` kwarg is passed.
    # Falsy (``None`` or empty string) renders no text heading, so a
    # banner-only or heading-free masthead needs no override.
    title: Optional[str] = "Leaderboard"

    # Full-width banner image rendered at the top of the rankings card,
    # above the ``title`` heading when both are set. Accepts a URL
    # string, a ``discord.File``, or any object with a string ``.url``
    # (``discord.Asset``, so ``guild.icon`` works directly). ``None``
    # renders no banner. Class attribute OR ``banner=`` constructor
    # kwarg; the ``build_title`` hook overrides both.
    banner: Optional[MediaInput] = None

    # H3 subtitle rendered above the ranking rows. The library emits
    # ``f"### {subtitle}"`` verbatim when truthy; set to ``None`` (or
    # empty string) to skip the subtitle entirely, which is the
    # natural pairing when an Overview ``build_header`` card already
    # carries its own heading. Callers that want dynamic
    # content assign ``self.subtitle`` in ``__init__`` using an f-string.
    subtitle: Optional[str] = "Rankings"

    # Static message when no entries exist
    leaderboard_empty_message: str = "No entries recorded yet."

    # Render mode for entries. ``"lines"`` (default) packs all entries on
    # one page into a single TextDisplay. ``"sections"`` renders each
    # entry as a discord.py ``Section`` with a two-line body and an
    # optional avatar ``Thumbnail`` accessory. Sections consume more
    # component budget, so ``leaderboard_per_page`` is capped at 5 when
    # this is ``"sections"`` (enforced at class-definition time).
    entry_layout: str = "lines"

    # Podium emojis keyed by rank (1-indexed). ``format_rank`` reads
    # this dict to pick the rank-1/2/3 glyph; ranks beyond 3 fall back
    # to ``f"**{rank}.**"``. Override the dict to change the podium
    # treatment without overriding ``format_rank`` itself.
    podium_emojis: ClassVar[Dict[int, str]] = {
        1: "\U0001f947",  # gold medal
        2: "\U0001f948",  # silver medal
        3: "\U0001f949",  # bronze medal
    }

    # Separator rendered between the name and stat columns inside
    # ``format_entry`` (``"lines"`` mode). Override on a subclass to
    # change visual rhythm without overriding ``format_entry``
    # entirely.
    entry_separator: str = " -- "

    # Optional accent color for the rankings card. ``None`` falls
    # through to the theme default. Set to a ``discord.Color`` on a
    # subclass to give the card its own accent (useful when a
    # ``build_header`` Overview card carries its own accent and a
    # deliberate two-color layout is wanted).
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
    # ones off the render path and reload(force=True) when ready. Avoids the
    # choose-your-poison of sync-cache-defaults vs a serial per-row fetch storm.
    avatar_backfill: ClassVar[bool] = False
    _BOOL_ATTRS: ClassVar[tuple] = (
        *_BasePaginatedMixin._BOOL_ATTRS,
        "show_title_divider",
        "avatar_backfill",
    )

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
                _coerce_banner(value)
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
        if "banner" in own:
            cls._validate_attribute_value("banner", own["banner"])

    def get_entries(self) -> List[Tuple[int, dict]]:
        """Return the sorted leaderboard entries as ``(user_id, stats)`` pairs.

        Override for live data sources (e.g. reading ``store.computed``
        or calling ``StateStore.iter_scoped``). Default returns the
        ``entries=`` kwarg passed at construction.
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
        still can, but prefer the cache: a per-entry ``fetch_user`` issues one
        serial HTTP round-trip per ranked row before the first render, adding
        latency proportional to entry count. Override to resolve from a
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
        try:
            resolved = await self.resolve_avatar_urls(user_ids)
        except asyncio.CancelledError:
            # Cancelled before completing (view teardown, or a navigation that
            # later rolled back to this view). Clear the schedule stamp so a
            # recovered view re-schedules the backfill instead of rendering
            # default avatars until the entry set changes.
            self._avatar_backfilled_signature = None
            raise
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
        ``build_footer`` / ``build_title`` overrides can compute aggregate
        stats or counts from the ranked data without re-fetching. Empty
        until the first build.
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
        by the placement validator). Pages rebuild only when the entry
        signature changes, so a masthead that depends on data outside
        the entries needs ``reload(force=True)`` -- the same contract as
        the other frame hooks.
        """
        return None

    def _build_masthead(self, page: int) -> list:
        """Compose the rankings card's masthead components for one page.

        The ``build_title`` hook wins whenever it returns non-``None``:
        component(s) render as the masthead, and an explicit empty list
        renders no masthead for that page. Only ``None`` falls through
        to the declarative pair, in order: the ``banner`` image, then
        the ``## title`` heading. Both unset composes an empty masthead.
        """
        hook_value = self.build_title(page)
        if hook_value is not None:
            return _as_frame_items(hook_value)
        items: list = []
        media = _coerce_banner(self.banner)
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
        for aggregate stats; an Overview ``stats_card(...)`` is the
        typical use. Return a single V2 component, a list of components, or
        ``None`` (default) for no header.

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

        The rebuild caveat on :meth:`build_header` applies identically.
        """
        return None

    def _resolve_per_page(self) -> int:
        if self.leaderboard_per_page is not None:
            return self.leaderboard_per_page
        return self.leaderboard_top_n

    def on_leaderboard_empty(self) -> list:
        """Return the V2 component list shown when no entries exist.

        Default wraps ``leaderboard_empty_message`` in a single card.
        Override to provide a richer empty state: an intro card with a
        call-to-action, a stats legend, or a "play your first game"
        button. Returns any V2 component list that should render as the
        sole page while the leaderboard is empty.

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
        """
        await self.rebuild_pages()
        await super().on_state_changed(state)

    async def _build_leaderboard_pages(self) -> list:
        """Convert entries into a list of V2 component lists for pagination.

        Async because ``entry_layout = "sections"`` awaits
        ``get_avatar_url`` once per entry to resolve optional thumbnails.
        Lines mode never awaits but shares this coroutine so the two
        render branches sit behind one coherent builder.

        The whole build runs inside the view's theme context so the
        rankings card and every user hook invoked here (``build_title``,
        ``build_header``, ``build_footer``) inherit the
        view's accent colour, whichever caller triggered the rebuild.
        """
        from ...theming.context import theme_context

        with theme_context(self.get_theme()):
            return await self._build_leaderboard_pages_inner()

    async def _build_leaderboard_pages_inner(self) -> list:
        entries = self.get_entries()

        if not entries:
            return [self.on_leaderboard_empty()]

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

        # Resolve every avatar URL across the full top-N slice in one
        # fan-out, so the page-build loop below stays synchronous and reads
        # from the pre-resolved list by absolute entry index. A cache-first
        # override (``bot.get_user``) resolves with no HTTP at all. An
        # override that awaits ``bot.fetch_user`` per entry does NOT
        # parallelize across this gather: those calls share one rate-limit
        # bucket and run serially, so keep HTTP off this path.
        if self.entry_layout == "sections":
            avatar_urls = await asyncio.gather(
                *(self.get_avatar_url(uid, stats) for uid, stats in top),
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

            items: list = self._build_masthead(page_idx)
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
                    primary = self.format_primary(rank, uid, stats)
                    secondary = self.format_secondary(rank, uid, stats)
                    avatar = avatar_urls[start + offset]
                    if avatar:
                        items.append(image_section(primary, secondary, url=avatar))
                    else:
                        # Section requires a non-None accessory. When no avatar
                        # resolves for an entry, collapse to a stacked
                        # TextDisplay so the entry still renders cleanly. An
                        # empty half is dropped rather than joined, matching
                        # how image_section handles the same case above.
                        stacked = "\n".join(part for part in (primary, secondary) if part)
                        items.append(TextDisplay(stacked))
            else:
                lines = [
                    self.format_entry(start + offset + 1, uid, stats)
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

            # build_footer placement follows its return type: a raw component
            # folds INSIDE the rankings card as a trailing child (a caption stays
            # attached to the entries, never a floating orphan), while a Container
            # renders as its own standalone card below the rankings. Folding a raw
            # footer in also keeps the page a single wrappable Container under
            # nav_inside_container.
            footer = _as_frame_items(self.build_footer(page_idx))
            footer_cards = [f for f in footer if isinstance(f, Container)]
            footer_inline = [f for f in footer if not isinstance(f, Container)]
            card_children = [*items, *footer_inline] if footer_inline else items
            page_components: list = [card(*card_children, color=self.card_color)]
            header = _as_frame_items(self.build_header(page_idx))
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
            own card, a raw component floats. Read ``self.ranked_entries``
            for aggregate stats. ``None`` (default) renders nothing.
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
            Entries per page (default ``None`` = same as ``top_n``).
            Set lower than ``top_n`` to enable multi-page navigation.

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
        # ``bot`` (optional) powers the default ``get_avatar_url``: section
        # mode resolves avatars from ``bot.get_user`` when it is set. Held as a
        # live reference, so it is stripped from the persistence round-trip
        # (_NON_PERSISTABLE_KWARGS) and re-injected via ``on_bind`` on the
        # persistent variant. Rejected at construction when it is not a client,
        # so a typo'd bot fails here rather than as an AttributeError inside the
        # section-mode avatar gather.
        if bot is not None and not isinstance(bot, discord.Client):
            raise TypeError(f"bot must be a discord.Client (or subclass), got {type(bot).__name__}")
        self._bot = bot
        # ``title`` / ``subtitle`` / ``banner`` share the sentinel shape:
        # passing ``None`` explicitly suppresses that masthead piece,
        # while omitting the kwarg falls back to the class default.
        if title is not _UNSET:
            self.title = title
        if subtitle is not _UNSET:
            self.subtitle = subtitle
        if banner is not _UNSET:
            self.banner = _coerce_banner(banner)
        self._entries = entries or []
        # Pages build lives in ``on_load()`` so the async ``get_avatar_url``
        # hook can resolve thumbnails before the first render. ``__init__``
        # hands the paginated base an empty list until then.
        kwargs["pages"] = []
        super().__init__(*args, **kwargs)
        # The kwargs snapshot for push/pop and persistence captures the raw
        # ``banner=`` value before the coercion above ran. Write the
        # resolved value back so a ``discord.Asset`` banner round-trips as
        # its JSON-safe URL string, the only shape the registry row's JSON
        # serialization and restart reattachment can carry.
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
        snapshot.
        """
        # The paginated base preloads a cursor page here. A leaderboard is
        # always eager-mode, so the call is a no-op, but the preload seam
        # belongs to the base and overrides chain through it.
        await super().on_load()
        await self.rebuild_pages()
        # rebuild_nav=True re-runs _build_nav_buttons against the final page
        # count (entries are fetched async here, so __init__ saw an empty
        # list). The shared helper handles the clear/compose/extras/back-button
        # sequence; it restores the auto back button push() injects, so a
        # pushed leaderboard is not stranded. Subclasses that override on_load
        # must keep this call after fetching.
        self._recompose_page_tree(rebuild_nav=True)

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
        by ``build_header``).
        """
        entries = self.get_entries()
        signature = self._entries_signature_for(entries)
        if not force and signature == getattr(self, "_entries_signature", None) and self.pages:
            return
        self._entries_signature = signature
        self.pages = await self._build_leaderboard_pages()
        if self.current_page >= len(self.pages):
            self.current_page = max(0, len(self.pages) - 1)

    async def reload(self, *, force: bool = False) -> None:
        """Re-fetch entries, re-render, and re-store the board out of band.

        The inherited :meth:`reload` runs ``on_load`` then ``refresh``.
        ``on_load`` fetches through ``rebuild_pages``, which short-circuits
        when the entry signature is unchanged. ``force=True`` clears that
        signature first, so a change outside the entry data (a filter, or a
        select's highlighted option read by ``build_header``) still
        rebuilds the pages. Call this rather than ``rebuild_pages`` directly
        when triggering an out-of-band refresh.
        """
        if force:
            self._entries_signature = None
        # Forward force so a reload coalesced by refresh_cooldown_ms replays it
        # at the boundary (the deferred re-entry calls reload with the captured
        # kwargs). The signature is already nulled above, so the rebuild forces
        # even without the replay, but forwarding keeps the general contract.
        await super().reload(force=force)

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
