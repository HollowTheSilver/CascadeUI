"""Tests for LeaderboardLayoutView and PersistentLeaderboardLayoutView."""

import pytest
from discord.ui import Container, LayoutView, Section, TextDisplay, Thumbnail
from helpers import make_interaction as _make_interaction

from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.patterns.leaderboard import (
    LeaderboardLayoutView,
    PersistentLeaderboardLayoutView,
    _BaseLeaderboardMixin,
)
from cascadeui.views.patterns.paginated import PaginatedLayoutView
from cascadeui.views.persistent import _PersistentMixin


# // ========================================( LeaderboardLayoutView )======================================== // #


SAMPLE_ENTRIES = [
    (111, {"wins": 10, "games": 15}),
    (222, {"wins": 7, "games": 12}),
    (333, {"wins": 3, "games": 8}),
]


def _page_text(view, page_idx=0):
    """Join all TextDisplay content inside a page's container(s)."""
    page = view.pages[page_idx]
    containers = [c for c in page if isinstance(c, Container)]
    texts = list(containers[0].walk_children())
    return " ".join(
        getattr(t, "content", "") for t in texts if isinstance(t, TextDisplay)
    )


async def _make_view(cls=LeaderboardLayoutView, **kwargs):
    """Construct a leaderboard and build its initial page set."""
    interaction = _make_interaction()
    view = cls(interaction=interaction, **kwargs)
    await view.rebuild_pages()
    return view


class TestLeaderboardLayoutViewInit:
    """Subclass hierarchy and basic construction."""

    def test_is_subclass_of_paginated_layout_view(self):
        assert issubclass(LeaderboardLayoutView, PaginatedLayoutView)

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(LeaderboardLayoutView, StatefulLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(LeaderboardLayoutView, LayoutView)

    def test_inherits_base_mixin(self):
        assert issubclass(LeaderboardLayoutView, _BaseLeaderboardMixin)

    def test_init_with_entries(self):
        interaction = _make_interaction()
        view = LeaderboardLayoutView(interaction=interaction, entries=SAMPLE_ENTRIES)
        assert view.get_entries() == SAMPLE_ENTRIES

    def test_init_without_entries_defaults_empty(self):
        interaction = _make_interaction()
        view = LeaderboardLayoutView(interaction=interaction)
        assert view.get_entries() == []

    def test_title_kwarg_overrides_class_default(self):
        interaction = _make_interaction()
        view = LeaderboardLayoutView(
            interaction=interaction, entries=SAMPLE_ENTRIES, title="Custom Title"
        )
        assert view.title == "Custom Title"

    def test_default_title(self):
        interaction = _make_interaction()
        view = LeaderboardLayoutView(interaction=interaction)
        assert view.title == "Leaderboard"

    def test_subtitle_kwarg_overrides_class_default(self):
        interaction = _make_interaction()
        view = LeaderboardLayoutView(
            interaction=interaction, entries=SAMPLE_ENTRIES, subtitle="Top Players"
        )
        assert view.subtitle == "Top Players"

    def test_default_subtitle(self):
        interaction = _make_interaction()
        view = LeaderboardLayoutView(interaction=interaction)
        assert view.subtitle == "Rankings"

    def test_subtitle_kwarg_none_explicitly_skips(self):
        """Passing ``subtitle=None`` at construction overrides the class default."""
        interaction = _make_interaction()
        view = LeaderboardLayoutView(
            interaction=interaction, entries=SAMPLE_ENTRIES, subtitle=None
        )
        assert view.subtitle is None

    def test_default_policies(self):
        interaction = _make_interaction()
        view = LeaderboardLayoutView(interaction=interaction)
        assert view.owner_only is True
        assert view.exit_policy == "delete"
        assert view.state_scope is None

    def test_default_entry_layout_is_lines(self):
        interaction = _make_interaction()
        view = LeaderboardLayoutView(interaction=interaction)
        assert view.entry_layout == "lines"

    def test_pages_empty_before_rebuild(self):
        """Async init hoist: pages are built in send(), not __init__."""
        interaction = _make_interaction()
        view = LeaderboardLayoutView(interaction=interaction, entries=SAMPLE_ENTRIES)
        assert view.pages == []


class TestLeaderboardLayoutViewRendering:
    """Component tree construction via paginated pages."""

    async def test_empty_entries_renders_empty_message(self):
        view = await _make_view()
        assert len(view.pages) == 1
        assert "No entries recorded yet" in _page_text(view)

    async def test_entries_render_card_with_rankings(self):
        view = await _make_view(entries=SAMPLE_ENTRIES)
        content = _page_text(view)
        assert "<@111>" in content
        assert "<@222>" in content
        assert "<@333>" in content

    async def test_title_appears_in_rendered_card(self):
        view = await _make_view(entries=SAMPLE_ENTRIES, title="Test Board")
        assert "Test Board" in _page_text(view)

    async def test_top_n_limits_displayed_entries(self):
        class SmallBoard(LeaderboardLayoutView):
            leaderboard_top_n = 3

        many_entries = [(i, {"wins": 100 - i, "games": 100}) for i in range(20)]
        view = await _make_view(cls=SmallBoard, entries=many_entries)
        content = _page_text(view)
        assert "<@0>" in content
        assert "<@1>" in content
        assert "<@2>" in content
        assert "<@3>" not in content

    async def test_default_subtitle_renders_rankings_h3(self):
        view = await _make_view(entries=SAMPLE_ENTRIES)
        assert "### Rankings" in _page_text(view)

    async def test_class_level_subtitle_override(self):
        class HallOfFame(LeaderboardLayoutView):
            subtitle = "Hall of Fame"

        view = await _make_view(cls=HallOfFame, entries=SAMPLE_ENTRIES)
        content = _page_text(view)
        assert "### Hall of Fame" in content
        assert "### Rankings" not in content

    async def test_dynamic_subtitle_via_init(self):
        class DynamicBoard(LeaderboardLayoutView):
            def __init__(self, *args, **kwargs):
                total = len(kwargs.get("entries") or [])
                super().__init__(*args, **kwargs)
                self.subtitle = f"Top {total} of {total}"

        view = await _make_view(cls=DynamicBoard, entries=SAMPLE_ENTRIES)
        assert "### Top 3 of 3" in _page_text(view)

    async def test_subtitle_none_class_level_skips_h3(self):
        class NoSubtitle(LeaderboardLayoutView):
            subtitle = None

        view = await _make_view(cls=NoSubtitle, entries=SAMPLE_ENTRIES)
        assert "### " not in _page_text(view)

    async def test_subtitle_empty_string_skips_h3(self):
        class EmptySubtitle(LeaderboardLayoutView):
            subtitle = ""

        view = await _make_view(cls=EmptySubtitle, entries=SAMPLE_ENTRIES)
        assert "### " not in _page_text(view)

    async def test_subtitle_kwarg_none_skips_h3_at_construction(self):
        interaction = _make_interaction()
        view = LeaderboardLayoutView(
            interaction=interaction, entries=SAMPLE_ENTRIES, subtitle=None
        )
        await view.rebuild_pages()
        assert "### " not in _page_text(view)


class TestLeaderboardOverrideHooks:
    """Custom format_entry and build_summary overrides."""

    async def test_custom_format_entry(self):
        class CustomFormat(LeaderboardLayoutView):
            def format_entry(self, rank, user_id, stats):
                return f"#{rank} user={user_id}"

        view = await _make_view(cls=CustomFormat, entries=SAMPLE_ENTRIES)
        assert "#1 user=111" in _page_text(view)

    async def test_custom_build_summary(self):
        class CustomSummary(LeaderboardLayoutView):
            def build_summary(self, entries):
                total = sum(e[1].get("games", 0) for e in entries)
                return {"Total games": str(total)}

        view = await _make_view(cls=CustomSummary, entries=SAMPLE_ENTRIES)
        assert "35" in _page_text(view)

    async def test_empty_summary_suppresses_section(self):
        class NoSummary(LeaderboardLayoutView):
            def build_summary(self, entries):
                return {}

        view = await _make_view(cls=NoSummary, entries=SAMPLE_ENTRIES)
        assert "Players" not in _page_text(view)

    async def test_build_summary_dict_renders_inline_page_1(self):
        """Default return shape: dict wraps in key_value inline on page 1."""
        view = await _make_view(entries=SAMPLE_ENTRIES)
        # Page 1 contains the inline key_value summary
        assert "Players" in _page_text(view, page_idx=0)

    async def test_build_summary_returning_container_renders_two_cards(self):
        """Container return ships as a second top-level component every page."""
        from cascadeui.components.patterns.v2 import card

        class TwoCardBoard(LeaderboardLayoutView):
            leaderboard_per_page = 2

            def build_summary(self, entries):
                return card(TextDisplay("## Overview"), TextDisplay("3 players"))

        many_entries = [(i, {"wins": 10 - i, "games": 10}) for i in range(6)]
        view = await _make_view(cls=TwoCardBoard, entries=many_entries)
        # Two top-level Containers per page (summary card + rankings card)
        for page in view.pages:
            containers = [c for c in page if isinstance(c, Container)]
            assert len(containers) == 2

    async def test_build_summary_returning_container_suppresses_inline_summary(self):
        """Container return skips the inline key_value path entirely."""
        from cascadeui.components.patterns.v2 import card

        class StandaloneSummary(LeaderboardLayoutView):
            def build_summary(self, entries):
                return card(TextDisplay("## Standalone"))

        view = await _make_view(cls=StandaloneSummary, entries=SAMPLE_ENTRIES)
        # Check the rankings card (second container) has no inline stats
        page_0 = view.pages[0]
        containers = [c for c in page_0 if isinstance(c, Container)]
        assert len(containers) == 2
        # First container is the user's summary card, second is rankings
        rankings_card = containers[1]
        rankings_text = " ".join(
            getattr(t, "content", "")
            for t in rankings_card.walk_children()
            if isinstance(t, TextDisplay)
        )
        assert "Players" not in rankings_text

    async def test_build_summary_returning_none_has_no_summary(self):
        """None return: no summary at any placement."""
        class NoSummary(LeaderboardLayoutView):
            def build_summary(self, entries):
                return None

        view = await _make_view(cls=NoSummary, entries=SAMPLE_ENTRIES)
        page_0 = view.pages[0]
        containers = [c for c in page_0 if isinstance(c, Container)]
        # Only the rankings card, no standalone summary card
        assert len(containers) == 1

    async def test_custom_get_entries(self):
        class LiveBoard(LeaderboardLayoutView):
            def get_entries(self):
                return [(999, {"wins": 50, "games": 50})]

        view = await _make_view(cls=LiveBoard)
        assert "<@999>" in _page_text(view)

    async def test_custom_empty_message(self):
        class CustomEmpty(LeaderboardLayoutView):
            leaderboard_empty_message = "Nothing here yet!"

        view = await _make_view(cls=CustomEmpty)
        assert "Nothing here yet!" in _page_text(view)

    async def test_on_leaderboard_empty_override(self):
        """Subclass override replaces the default empty-state component tree."""
        from cascadeui.components.patterns.v2 import card

        class RichEmpty(LeaderboardLayoutView):
            def on_leaderboard_empty(self):
                return [card(TextDisplay("## Play your first game to appear here!"))]

        view = await _make_view(cls=RichEmpty)
        assert "Play your first game to appear here!" in _page_text(view)

    async def test_on_state_changed_rebuilds_pages(self):
        """on_state_changed refetches entries via get_entries() before rendering."""
        live_entries = [(1, {"wins": 5, "games": 10})]

        class LiveBoard(LeaderboardLayoutView):
            def get_entries(self):
                return live_entries

        view = await _make_view(cls=LiveBoard)
        assert "<@1>" in _page_text(view)

        live_entries.clear()
        live_entries.append((42, {"wins": 99, "games": 100}))

        view._message = None  # refresh() short-circuits with no message
        await view.on_state_changed({})

        assert "<@42>" in _page_text(view)
        assert "<@1>" not in _page_text(view)


# // ========================================( Pagination )======================================== // #


class TestLeaderboardPagination:
    """Multi-page behavior when entries exceed per_page."""

    async def test_single_page_when_entries_fit(self):
        """Three entries with default top_n=10 produces one page."""
        view = await _make_view(entries=SAMPLE_ENTRIES)
        assert len(view.pages) == 1

    async def test_multi_page_when_entries_exceed_per_page(self):
        """10 entries with per_page=3 produces 4 pages (3+3+3+1)."""
        class PagedBoard(LeaderboardLayoutView):
            leaderboard_top_n = 10
            leaderboard_per_page = 3

        entries = [(i, {"wins": 100 - i, "games": 100}) for i in range(10)]
        view = await _make_view(cls=PagedBoard, entries=entries)
        assert len(view.pages) == 4

    async def test_per_page_defaults_to_top_n(self):
        """When leaderboard_per_page is None, uses top_n as page size."""
        class SmallBoard(LeaderboardLayoutView):
            leaderboard_top_n = 5
            leaderboard_per_page = None

        entries = [(i, {"wins": 100 - i, "games": 100}) for i in range(5)]
        view = await _make_view(cls=SmallBoard, entries=entries)
        assert len(view.pages) == 1

    async def test_cross_page_rank_numbering(self):
        """Page 2 starts at rank per_page+1, not rank 1.

        The default ``format_rank`` renders ranks 1-3 as medal emoji and
        rank 4+ as bold numbers. Assertions split on that boundary:
        page 1 holds the podium, page 2 holds the bold-number tail.
        """
        class PagedBoard(LeaderboardLayoutView):
            leaderboard_top_n = 6
            leaderboard_per_page = 3

        entries = [(i, {"wins": 100 - i, "games": 100}) for i in range(6)]
        view = await _make_view(cls=PagedBoard, entries=entries)
        page1 = _page_text(view, 0)
        page2 = _page_text(view, 1)

        assert "\U0001f947" in page1
        assert "\U0001f948" in page1
        assert "\U0001f949" in page1
        assert "**4.**" in page2
        assert "**5.**" in page2
        assert "**6.**" in page2

    async def test_summary_only_on_first_page(self):
        """Summary key_value appears on page 1 only."""
        class PagedBoard(LeaderboardLayoutView):
            leaderboard_top_n = 6
            leaderboard_per_page = 3

        entries = [(i, {"wins": 100 - i, "games": 100}) for i in range(6)]
        view = await _make_view(cls=PagedBoard, entries=entries)
        assert "Players" in _page_text(view, 0)
        assert "Players" not in _page_text(view, 1)

    async def test_title_on_every_page(self):
        """Title heading appears on all pages."""
        class PagedBoard(LeaderboardLayoutView):
            leaderboard_top_n = 6
            leaderboard_per_page = 3

        entries = [(i, {"wins": 100 - i, "games": 100}) for i in range(6)]
        view = await _make_view(cls=PagedBoard, entries=entries)
        for page_idx in range(len(view.pages)):
            assert "Leaderboard" in _page_text(view, page_idx)

    async def test_rebuild_pages_refreshes_data(self):
        """rebuild_pages re-reads get_entries and rebuilds the page list."""
        view = await _make_view(entries=SAMPLE_ENTRIES)
        assert len(view.pages) == 1

        view._entries = [(i, {"wins": i, "games": i}) for i in range(20)]
        await view.rebuild_pages()
        assert "<@0>" in _page_text(view)

    async def test_rebuild_pages_clamps_current_page(self):
        """rebuild_pages clamps current_page when entries shrink."""
        class PagedBoard(LeaderboardLayoutView):
            leaderboard_top_n = 10
            leaderboard_per_page = 3

        entries = [(i, {"wins": i, "games": i}) for i in range(10)]
        view = await _make_view(cls=PagedBoard, entries=entries)
        view.current_page = 3
        view._entries = SAMPLE_ENTRIES
        await view.rebuild_pages()
        assert view.current_page == 0

    def test_has_paginated_nav_buttons_when_multi_page(self):
        """Multi-page leaderboards get paginated navigation buttons.

        The nav buttons are created unconditionally by the paginated base
        ``__init__``; they simply disable when only one page exists.
        Synchronous check -- no page build required.
        """
        class PagedBoard(LeaderboardLayoutView):
            leaderboard_top_n = 10
            leaderboard_per_page = 3

        interaction = _make_interaction()
        view = PagedBoard(
            interaction=interaction,
            entries=[(i, {"wins": 100 - i, "games": 100}) for i in range(10)],
        )
        assert hasattr(view, "_prev_btn")
        assert hasattr(view, "_next_btn")

    async def test_no_nav_buttons_disabled_when_single_page(self):
        """Single-page leaderboard has prev/next disabled."""
        view = await _make_view(entries=SAMPLE_ENTRIES)
        assert view._prev_btn.disabled is True
        assert view._next_btn.disabled is True


# // ========================================( PersistentLeaderboardLayoutView )======================================== // #


class TestPersistentLeaderboardLayoutView:
    """MRO composition and persistent defaults."""

    def test_inherits_persistent_mixin(self):
        assert issubclass(PersistentLeaderboardLayoutView, _PersistentMixin)

    def test_inherits_leaderboard_layout_view(self):
        assert issubclass(PersistentLeaderboardLayoutView, LeaderboardLayoutView)

    def test_inherits_paginated_layout_view(self):
        assert issubclass(PersistentLeaderboardLayoutView, PaginatedLayoutView)

    def test_inherits_base_mixin(self):
        assert issubclass(PersistentLeaderboardLayoutView, _BaseLeaderboardMixin)

    def test_persistent_flag_is_true(self):
        assert PersistentLeaderboardLayoutView._persistent is True

    def test_default_owner_only_is_false(self):
        assert PersistentLeaderboardLayoutView.owner_only is False

    def test_default_exit_policy_is_disable(self):
        assert PersistentLeaderboardLayoutView.exit_policy == "disable"

    def test_requires_persistence_key(self):
        interaction = _make_interaction()
        with pytest.raises(ValueError, match="persistence_key"):
            PersistentLeaderboardLayoutView(interaction=interaction, entries=SAMPLE_ENTRIES)

    def test_constructs_with_persistence_key(self):
        interaction = _make_interaction()
        view = PersistentLeaderboardLayoutView(
            interaction=interaction,
            entries=SAMPLE_ENTRIES,
            persistence_key="leaderboard:test:200",
            title="Test Leaderboard",
        )
        assert view.get_entries() == SAMPLE_ENTRIES
        assert view.title == "Test Leaderboard"


# // ========================================( Split Format Hooks )======================================== // #


class TestFormatHooks:
    """Split-hook surface: format_rank, format_name, format_stats, format_accessory."""

    def _make_view(self, entries):
        return LeaderboardLayoutView(interaction=_make_interaction(), entries=entries)

    def test_default_format_rank_returns_gold_for_rank_one(self):
        view = self._make_view(SAMPLE_ENTRIES)
        assert view.format_rank(1) == "\U0001f947"

    def test_default_format_rank_returns_silver_for_rank_two(self):
        view = self._make_view(SAMPLE_ENTRIES)
        assert view.format_rank(2) == "\U0001f948"

    def test_default_format_rank_returns_bronze_for_rank_three(self):
        view = self._make_view(SAMPLE_ENTRIES)
        assert view.format_rank(3) == "\U0001f949"

    def test_default_format_rank_falls_back_to_bold_at_rank_four(self):
        view = self._make_view(SAMPLE_ENTRIES)
        assert view.format_rank(4) == "**4.**"

    def test_default_format_rank_bold_for_large_ranks(self):
        view = self._make_view(SAMPLE_ENTRIES)
        assert view.format_rank(42) == "**42.**"

    def test_default_format_name_returns_mention(self):
        view = self._make_view(SAMPLE_ENTRIES)
        assert view.format_name(111, {"wins": 1, "games": 1}) == "<@111>"

    def test_format_name_returns_display_name_verbatim(self):
        view = self._make_view(SAMPLE_ENTRIES)
        stats = {"wins": 1, "games": 1, "display_name": "Demo Player"}
        assert view.format_name(111, stats) == "Demo Player"

    def test_format_name_passes_markdown_through_verbatim(self):
        view = self._make_view(SAMPLE_ENTRIES)
        stats = {"wins": 1, "games": 1, "display_name": "**Bold Name**"}
        assert view.format_name(111, stats) == "**Bold Name**"

    def test_default_format_stats_returns_wins_games(self):
        view = self._make_view(SAMPLE_ENTRIES)
        assert view.format_stats(111, {"wins": 7, "games": 10}) == "7W / 10G"

    def test_default_format_accessory_returns_none(self):
        view = self._make_view(SAMPLE_ENTRIES)
        assert view.format_accessory(111, {"wins": 1, "games": 1}) is None

    def test_format_entry_composes_all_four_hooks(self):
        view = self._make_view(SAMPLE_ENTRIES)
        line = view.format_entry(1, 111, {"wins": 7, "games": 10})
        assert "\U0001f947" in line
        assert "<@111>" in line
        assert "7W / 10G" in line

    def test_format_entry_omits_accessory_when_none(self):
        view = self._make_view(SAMPLE_ENTRIES)
        line = view.format_entry(4, 111, {"wins": 7, "games": 10})
        assert not line.endswith(" ")
        assert line == "**4.** <@111> -- 7W / 10G"

    def test_format_entry_appends_accessory_when_present(self):
        class FlaggedBoard(LeaderboardLayoutView):
            def format_accessory(self, user_id, stats):
                return "\U0001f525"

        view = FlaggedBoard(interaction=_make_interaction(), entries=SAMPLE_ENTRIES)
        line = view.format_entry(4, 111, {"wins": 7, "games": 10})
        assert line == "**4.** <@111> -- 7W / 10G \U0001f525"

    def test_custom_format_rank_reaches_default_format_entry(self):
        """Overriding one hook propagates through the default composition."""
        class NumericBoard(LeaderboardLayoutView):
            def format_rank(self, rank):
                return f"#{rank}"

        view = NumericBoard(interaction=_make_interaction(), entries=SAMPLE_ENTRIES)
        line = view.format_entry(1, 111, {"wins": 7, "games": 10})
        assert line.startswith("#1 ")
        assert "\U0001f947" not in line

    def test_custom_format_stats_reaches_default_format_entry(self):
        class MmrBoard(LeaderboardLayoutView):
            def format_stats(self, user_id, stats):
                return f"{stats['mmr']} MMR"

        mmr_entries = [(111, {"mmr": 1500, "wins": 0, "games": 0})]
        view = MmrBoard(interaction=_make_interaction(), entries=mmr_entries)
        line = view.format_entry(1, 111, {"mmr": 1500, "wins": 0, "games": 0})
        assert "1500 MMR" in line

    def test_format_rank_default_inherited_by_battleship_style_subclass(self):
        """A subclass that overrides only format_stats still gets medal ranks."""
        class StatOnlyBoard(LeaderboardLayoutView):
            def format_stats(self, user_id, stats):
                return "CUSTOM"

        view = StatOnlyBoard(interaction=_make_interaction(), entries=SAMPLE_ENTRIES)
        line = view.format_entry(1, 111, {"wins": 0, "games": 0})
        assert line.startswith("\U0001f947 ")
        assert "CUSTOM" in line


# // ========================================( progress_bar Integration )======================================== // #


class TestProgressBarInEntry:
    """progress_bar().content embedded inside a format_stats override."""

    def test_progress_bar_content_renders_inline(self):
        from cascadeui import progress_bar

        class BarBoard(LeaderboardLayoutView):
            def format_stats(self, user_id, stats):
                bar = progress_bar(
                    stats["wins"], stats["games"] or 1, width=6, show_percent=True
                ).content
                return f"{stats['wins']}W / {stats['games']}G \u2022 {bar}"

        view = BarBoard(
            interaction=_make_interaction(),
            entries=[(111, {"wins": 3, "games": 6})],
        )
        line = view.format_entry(1, 111, {"wins": 3, "games": 6})
        assert "3W / 6G" in line
        assert "50%" in line


# // ========================================( entry_layout Validation )======================================== // #


class TestEntryLayoutValidation:
    """Class-definition-time validation for entry_layout and its coupling with per_page."""

    def test_default_entry_layout_is_lines(self):
        assert LeaderboardLayoutView.entry_layout == "lines"

    def test_explicit_lines_is_accepted(self):
        class LinesBoard(LeaderboardLayoutView):
            entry_layout = "lines"

        assert LinesBoard.entry_layout == "lines"

    def test_explicit_sections_is_accepted(self):
        class SectionsBoard(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 5

        assert SectionsBoard.entry_layout == "sections"

    def test_sections_accepts_per_page_equal_to_five(self):
        """The cap is inclusive: per_page == 5 is the max permitted with sections."""
        class AtCapBoard(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 5

        assert AtCapBoard.leaderboard_per_page == 5

    def test_sections_accepts_per_page_below_cap(self):
        class BelowCapBoard(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 3

        assert BelowCapBoard.leaderboard_per_page == 3

    def test_typo_entry_layout_rejected_at_class_definition(self):
        """Invalid enum string fails at class-definition time via __init_subclass__."""
        with pytest.raises(ValueError, match="entry_layout"):

            class TypoBoard(LeaderboardLayoutView):
                entry_layout = "section"  # missing trailing 's'

    def test_sections_with_per_page_above_cap_rejected(self):
        """sections + per_page > 5 raises at class-definition time."""
        with pytest.raises(ValueError, match="leaderboard_per_page <= 5"):

            class OverCapBoard(LeaderboardLayoutView):
                entry_layout = "sections"
                leaderboard_per_page = 10

    def test_sections_with_per_page_six_is_rejected(self):
        """Boundary check: the smallest overflow value is caught."""
        with pytest.raises(ValueError, match="leaderboard_per_page <= 5"):

            class JustOverBoard(LeaderboardLayoutView):
                entry_layout = "sections"
                leaderboard_per_page = 6

    def test_lines_mode_allows_larger_per_page(self):
        """The cap is a sections-only concern; lines mode is unconstrained."""
        class WideLinesBoard(LeaderboardLayoutView):
            entry_layout = "lines"
            leaderboard_per_page = 20

        assert WideLinesBoard.leaderboard_per_page == 20


# // ========================================( Section Render Mode )======================================== // #


class TestSectionRender:
    """entry_layout = 'sections' renders Section+Thumbnail components."""

    async def test_sections_render_section_instances_with_avatars(self):
        """Each entry becomes a Section with Thumbnail when avatars resolve."""
        class AvatarBoard(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 5

            async def get_avatar_url(self, user_id, stats):
                return f"https://example.test/{user_id}.png"

        view = await _make_view(cls=AvatarBoard, entries=SAMPLE_ENTRIES)
        container = [c for c in view.pages[0] if isinstance(c, Container)][0]
        sections = [c for c in container.walk_children() if isinstance(c, Section)]
        assert len(sections) == len(SAMPLE_ENTRIES)

    async def test_sections_primary_and_secondary_content(self):
        """Default format_primary shows rank+name; format_secondary shows stats."""
        class AvatarBoard(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 5

            async def get_avatar_url(self, user_id, stats):
                return f"https://example.test/{user_id}.png"

        view = await _make_view(cls=AvatarBoard, entries=SAMPLE_ENTRIES)
        container = [c for c in view.pages[0] if isinstance(c, Container)][0]
        sections = [c for c in container.walk_children() if isinstance(c, Section)]
        first = sections[0]
        texts = [t for t in first.walk_children() if isinstance(t, TextDisplay)]
        body = " ".join(t.content for t in texts)
        assert "<@111>" in body
        assert "10W / 15G" in body

    async def test_no_avatar_falls_back_to_stacked_textdisplay(self):
        """Default get_avatar_url returns None: no Section, stacked text instead."""
        class SectionsBoard(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 5

        view = await _make_view(cls=SectionsBoard, entries=SAMPLE_ENTRIES)
        container = [c for c in view.pages[0] if isinstance(c, Container)][0]
        sections = [c for c in container.walk_children() if isinstance(c, Section)]
        thumbnails = [c for c in container.walk_children() if isinstance(c, Thumbnail)]
        assert sections == []
        assert thumbnails == []
        # Entry content still lands in stacked TextDisplay form.
        joined = " ".join(
            t.content for t in container.walk_children() if isinstance(t, TextDisplay)
        )
        assert "<@111>" in joined
        assert "10W / 15G" in joined

    async def test_get_avatar_url_attaches_thumbnail(self):
        """Returning a URL attaches a Thumbnail accessory to the section."""
        class AvatarBoard(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 5

            async def get_avatar_url(self, user_id, stats):
                return f"https://example.test/avatar/{user_id}.png"

        view = await _make_view(cls=AvatarBoard, entries=SAMPLE_ENTRIES)
        container = [c for c in view.pages[0] if isinstance(c, Container)][0]
        thumbnails = [c for c in container.walk_children() if isinstance(c, Thumbnail)]
        assert len(thumbnails) == len(SAMPLE_ENTRIES)

    async def test_get_avatar_url_partial_returns_mixed(self):
        """Returning URL for some entries and None for others produces a mixed page."""
        class MixedBoard(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 5

            async def get_avatar_url(self, user_id, stats):
                return f"https://example.test/{user_id}.png" if user_id == 111 else None

        view = await _make_view(cls=MixedBoard, entries=SAMPLE_ENTRIES)
        container = [c for c in view.pages[0] if isinstance(c, Container)][0]
        thumbnails = [c for c in container.walk_children() if isinstance(c, Thumbnail)]
        assert len(thumbnails) == 1

    async def test_format_primary_override(self):
        """Overriding format_primary changes the section's top line."""
        class CustomPrimary(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 5

            def format_primary(self, rank, user_id, stats):
                return f"ENTRY #{rank}"

            async def get_avatar_url(self, user_id, stats):
                return f"https://example.test/{user_id}.png"

        view = await _make_view(cls=CustomPrimary, entries=SAMPLE_ENTRIES)
        container = [c for c in view.pages[0] if isinstance(c, Container)][0]
        sections = [c for c in container.walk_children() if isinstance(c, Section)]
        first_texts = [t for t in sections[0].walk_children() if isinstance(t, TextDisplay)]
        assert any("ENTRY #1" in t.content for t in first_texts)

    async def test_format_secondary_override(self):
        """Overriding format_secondary changes the section's bottom line."""
        class CustomSecondary(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_per_page = 5

            def format_secondary(self, rank, user_id, stats):
                return f"subtitle-{user_id}"

            async def get_avatar_url(self, user_id, stats):
                return f"https://example.test/{user_id}.png"

        view = await _make_view(cls=CustomSecondary, entries=SAMPLE_ENTRIES)
        container = [c for c in view.pages[0] if isinstance(c, Container)][0]
        sections = [c for c in container.walk_children() if isinstance(c, Section)]
        first_texts = [t for t in sections[0].walk_children() if isinstance(t, TextDisplay)]
        assert any("subtitle-111" in t.content for t in first_texts)

    async def test_sections_mode_pagination(self):
        """Sections mode still paginates when entries exceed per_page."""
        class PagedSections(LeaderboardLayoutView):
            entry_layout = "sections"
            leaderboard_top_n = 6
            leaderboard_per_page = 3

            async def get_avatar_url(self, user_id, stats):
                return f"https://example.test/{user_id}.png"

        entries = [(i, {"wins": 100 - i, "games": 100}) for i in range(6)]
        view = await _make_view(cls=PagedSections, entries=entries)
        assert len(view.pages) == 2

        for page_idx in (0, 1):
            container = [c for c in view.pages[page_idx] if isinstance(c, Container)][0]
            sections = [c for c in container.walk_children() if isinstance(c, Section)]
            assert len(sections) == 3
