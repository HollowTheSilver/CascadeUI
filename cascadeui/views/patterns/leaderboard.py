# // ========================================( Modules )======================================== // #


import asyncio
from typing import ClassVar, Dict, List, Optional, Tuple, Union

import discord
from discord.ui import Container, Section, TextDisplay, Thumbnail

from ...components.patterns.v2 import card, divider, gap, key_value
from ..base import _StatefulMixin
from ..persistent import _PersistentMixin
from .paginated import PaginatedLayoutView, _BasePaginatedMixin

# Sentinel distinguishing "user passed subtitle=None" (explicit skip)
# from "user omitted the kwarg" (fall back to class default).
_UNSET: object = object()


# // ========================================( Shared Mixin )======================================== // #


class _BaseLeaderboardMixin:
    """Shared leaderboard rendering logic for V2 variants.

    Holds the data access pattern, entry formatting, summary generation,
    and empty-state handling. Concrete subclasses supply the component
    tree assembly.

    Internal. Not exported. The public hierarchy
    (``LeaderboardLayoutView`` / ``PersistentLeaderboardLayoutView``)
    is unchanged.
    """

    # Total entries to consider from the data source
    leaderboard_top_n: int = 10

    # Entries per page; ``None`` defaults to ``leaderboard_top_n`` (single page)
    leaderboard_per_page: Optional[int] = 5

    # Rankings card H2 title. Default when no ``title=`` kwarg is passed.
    title: str = "Leaderboard"

    # H3 subtitle rendered above the ranking rows. The library emits
    # ``f"### {subtitle}"`` verbatim when truthy; set to ``None`` (or
    # empty string) to skip the subtitle entirely, which is the
    # natural pairing for two-card mode where the separate summary
    # card already carries its own heading. Callers that want dynamic
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
    # subclass to give the card its own accent (useful when
    # ``build_summary`` returns a Container with its own accent and a
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
    _BOOL_ATTRS: ClassVar[tuple] = (
        *_BasePaginatedMixin._BOOL_ATTRS,
        "show_title_divider",
    )

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

        Default returns ``None``. Override in bot-context subclasses to
        resolve an avatar URL. Prefer the synchronous user cache
        (``bot.get_user(user_id).display_avatar.url``) so the resolve stays
        off the render path -- a per-entry ``fetch_user`` issues one serial
        HTTP round-trip per ranked row before the first render, adding latency
        proportional to entry count. Async so an implementation that genuinely
        must await a non-cache source still can. Only called when
        ``entry_layout = "sections"``.

        Discord's ``Section`` requires a non-None accessory, so entries
        with no resolvable avatar fall back to a stacked two-line
        ``TextDisplay`` instead of an accessory-less Section. Override
        this hook to guarantee Section rendering for every entry.
        """
        return None

    def build_summary(
        self, entries: List[Tuple[int, dict]]
    ) -> Union[Dict[str, str], Container, None]:
        """Render summary content. Return shape controls placement.

        Three return shapes are supported; the library branches on type:

        - ``Dict[str, str]`` (non-empty): wrapped in ``key_value(...)``
          and rendered inline above the rankings on page 1 only. Empty
          dict suppresses the section entirely.
        - ``Container``: shipped as a standalone top-level card
          rendered above the rankings card on every page. Use
          ``card(...)`` to build one; the returned card owns its own
          title and layout, so the rankings card stays focused on the
          ranked rows alone. Pair with ``subtitle = None``
          to drop the rankings H3 for a clean two-card look.
        - ``None``: no summary at any placement.

        Override to add game-specific aggregates (forfeits, draws,
        etc.) or to promote the summary to a persistent header card.
        Default returns one row: ``Players`` count.
        """
        return {"Players": str(len(entries))}

    def _resolve_per_page(self) -> int:
        if self.leaderboard_per_page is not None:
            return self.leaderboard_per_page
        return self.leaderboard_top_n

    def on_leaderboard_empty(self) -> list:
        """Return the V2 component list shown when no entries exist.

        Default wraps ``leaderboard_empty_message`` in a single card.
        Override to provide a richer empty state -- e.g. an intro card
        with a call-to-action, a stats legend, or a "play your first
        game" button -- returning any V2 component list that should
        render as the sole page while the leaderboard is empty.

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
        """
        entries = self.get_entries()

        if not entries:
            return [self.on_leaderboard_empty()]

        top = entries[: self.leaderboard_top_n]
        per_page = self._resolve_per_page()
        total_entries = len(top)
        total_pages = (total_entries + per_page - 1) // per_page

        # Branch on build_summary return shape:
        #   Container -> standalone card rendered on every page
        #   dict      -> inline key_value on page 1 only
        #   None/{}   -> no summary at any placement
        summary = self.build_summary(top)
        summary_card: Optional[Container] = None
        inline_summary: Optional[Dict[str, str]] = None
        if isinstance(summary, Container):
            summary_card = summary
        elif isinstance(summary, dict) and summary:
            inline_summary = summary

        # Subtitle is optional; falsy values (None, empty string) skip
        # the H3 entirely, which is the natural shape for two-card mode.
        heading = f"### {self.subtitle}" if self.subtitle else None

        # Resolve every avatar URL across the full top-N slice in one
        # fan-out, so the page-build loop below stays synchronous and reads
        # from the pre-resolved list by absolute entry index. A cache-first
        # override (``bot.get_user``) resolves with no HTTP at all. An
        # override that awaits ``bot.fetch_user`` per entry does NOT
        # parallelize across this gather -- those calls share one rate-limit
        # bucket and run serially -- so keep HTTP off this path.
        if self.entry_layout == "sections":
            avatar_urls = await asyncio.gather(
                *(self.get_avatar_url(uid, stats) for uid, stats in top),
                return_exceptions=False,
            )
        else:
            avatar_urls = []

        pages = []
        for page_idx in range(total_pages):
            start = page_idx * per_page
            end = start + per_page
            page_entries = top[start:end]

            items: list = [TextDisplay(f"## {self.title}")]
            if self.show_title_divider:
                items.append(divider())

            # Inline page-1 summary only when build_summary returned a
            # dict (not a standalone Container) and the dict is non-empty.
            if inline_summary is not None and page_idx == 0:
                items.append(key_value(inline_summary))
                items.append(gap())

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
                        items.append(
                            Section(
                                TextDisplay(primary),
                                TextDisplay(secondary),
                                accessory=Thumbnail(media=avatar),
                            )
                        )
                    else:
                        # Section requires a non-None accessory. When no avatar
                        # resolves for an entry, collapse to a stacked two-line
                        # TextDisplay so the entry still renders cleanly.
                        items.append(TextDisplay(f"{primary}\n{secondary}"))
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

            page_components: list = [card(*items, color=self.card_color)]
            if summary_card is not None:
                page_components.insert(0, summary_card)
            pages.append(page_components)

        return pages


# // ========================================( V2 Leaderboard )======================================== // #


class LeaderboardLayoutView(_BaseLeaderboardMixin, PaginatedLayoutView):
    """V2 leaderboard view with paginated card-based layout.

    Renders a sorted list of ``(user_id, stats)`` entries across one or
    more pages. Each page is a card with ranked entry lines. The summary
    header appears on the first page only.

    When all entries fit on a single page, no navigation buttons are
    shown -- the view behaves identically to a static card.

    Override hooks:
        ``format_entry(rank, user_id, stats)``
            One line per ranked player. Default shows wins and games.
        ``build_summary(entries)``
            Summary content. Return shape picks placement:
            ``Dict[str, str]`` renders inline on page 1 via ``key_value``,
            ``Container`` renders as a standalone card above the
            rankings on every page, ``None`` or empty dict suppresses
            the summary entirely.
        ``get_entries()``
            Data source. Default returns constructor ``entries=``.

    Heading text:
        ``title``
            H2 title on the rankings card (default ``"Leaderboard"``).
            Class attribute OR ``title=`` constructor kwarg; the kwarg
            wins when passed.
        ``subtitle``
            H3 subtitle above the ranking rows (default ``"Rankings"``).
            Class attribute OR ``subtitle=`` constructor kwarg. Set to
            ``None`` (or empty string) to skip the H3 entirely, which
            pairs naturally with a ``build_summary`` override that
            returns a standalone Container. Assign ``self.subtitle``
            in a subclass ``__init__`` for dynamic content (truncation
            count, filter context, etc.).

    Pagination controls:
        ``leaderboard_top_n``
            Total entries to consider from the data source (default 10).
        ``leaderboard_per_page``
            Entries per page (default ``None`` = same as ``top_n``).
            Set lower than ``top_n`` to enable multi-page navigation.

    Example::

        entries = store.computed["my_leaderboard"].get(guild_id, [])
        view = LeaderboardLayoutView(
            context=context,
            entries=entries,
            title=f"Leaderboard -- {context.guild.name}",
        )
        await view.send(ephemeral=True)
    """

    owner_only = True
    exit_policy = "delete"
    state_scope = None

    def __init__(self, *args, entries=None, title=None, subtitle=_UNSET, **kwargs):
        if title is not None:
            self.title = title
        # Subtitle uses a sentinel so the user can pass ``subtitle=None``
        # to explicitly skip the H3 heading at construction time.
        # ``subtitle=_UNSET`` (no kwarg) falls back to the class default.
        if subtitle is not _UNSET:
            self.subtitle = subtitle
        self._entries = entries or []
        # Pages build lives in ``on_load()`` so the async ``get_avatar_url``
        # hook can resolve thumbnails before the first render. ``__init__``
        # hands the paginated base an empty list until then.
        kwargs["pages"] = []
        super().__init__(*args, **kwargs)

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
        await self.rebuild_pages()
        self.clear_items()
        self._build_nav_buttons()
        self._compose_pagination_tree()
        for extra in self._extra_items:
            self.add_item(extra)
        # clear_items() above stripped the auto back button push() injects,
        # and on_load runs on every push/pop edit -- restore it or a pushed
        # leaderboard strands the user with no way back. Subclasses that
        # override on_load must keep this call after recomposing the tree.
        self._restore_navigation_artifacts()

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
        rendered pages (a filter, or a select's highlighted option folded
        into ``build_summary``).
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
        select's highlighted option folded into ``build_summary``) still
        rebuilds the pages. Call this rather than ``rebuild_pages`` directly
        when triggering an out-of-band refresh.
        """
        if force:
            self._entries_signature = None
        await super().reload()

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
