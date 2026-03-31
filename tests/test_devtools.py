"""Tests for the V2 DevTools inspector."""

# // ========================================( Modules )======================================== // #


import pytest
from discord.ui import ActionRow, Container, LayoutView, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.devtools import DevToolsCog, InspectorView, StateInspector
from cascadeui.state.singleton import get_store
from cascadeui.views.layout_patterns import TabLayoutView

# // ========================================( Class )======================================== // #


class TestInspectorViewInit:
    """Init, inheritance, and configuration."""

    def test_is_subclass_of_tab_layout_view(self):
        assert issubclass(InspectorView, TabLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(InspectorView, LayoutView)

    def test_state_inspector_alias(self):
        assert StateInspector is InspectorView

    def test_init_with_interaction(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        assert view.state_store is not None
        assert view.session_id == "InspectorView:user_100"

    def test_has_five_tabs(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        assert len(view._tab_names) == 5

    def test_session_limiting_config(self):
        assert InspectorView.session_limit == 1
        assert InspectorView.session_scope == "user_guild"
        assert InspectorView.session_policy == "replace"

    def test_has_tab_buttons(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        action_rows = [c for c in view.children if isinstance(c, ActionRow)]
        assert len(action_rows) >= 1
        # First ActionRow should contain 5 tab buttons
        tab_row = action_rows[0]
        assert len(tab_row.children) == 5


# // ========================================( Filtering )======================================== // #


class TestInspectorFiltering:
    """Self-filtering excludes the inspector's own data."""

    def test_filtered_views_excludes_self(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        store = get_store()

        # Manually add some view state
        store.state["views"] = {
            view.id: {"id": view.id, "type": "InspectorView"},
            "other_view": {"id": "other_view", "type": "CounterView"},
        }

        filtered = view._filtered_views()
        assert view.id not in filtered
        assert "other_view" in filtered

    def test_filtered_sessions_excludes_self(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        store = get_store()

        store.state["sessions"] = {
            view.session_id: {"id": view.session_id, "views": [view.id]},
            "OtherView:user_100": {"id": "OtherView:user_100", "views": ["v2"]},
        }

        filtered = view._filtered_sessions()
        assert view.session_id not in filtered
        assert "OtherView:user_100" in filtered

    def test_filtered_history_excludes_self(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        store = get_store()

        store.history = [
            {"type": "VIEW_CREATED", "source": view.id, "timestamp": "2026-01-01T00:00:00"},
            {"type": "COUNTER_UPDATED", "source": "other_id", "timestamp": "2026-01-01T00:00:01"},
        ]

        filtered = view._filtered_history()
        assert len(filtered) == 1
        assert filtered[0]["type"] == "COUNTER_UPDATED"

    def test_filtered_active_views_excludes_self(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        store = get_store()

        store._active_views[view.id] = view
        store._active_views["other_id"] = "other_view_instance"

        filtered = view._filtered_active_views()
        assert view.id not in filtered
        assert "other_id" in filtered


# // ========================================( Tab Builders )======================================== // #


class TestOverviewTab:
    async def test_returns_list(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = await view.build_overview()
        assert isinstance(result, list)
        assert len(result) >= 3  # overview card + gap + app card + exit row

    async def test_contains_containers(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = await view.build_overview()
        containers = [c for c in result if isinstance(c, Container)]
        assert len(containers) >= 2  # overview + app state

    async def test_exit_row_present(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = await view.build_overview()
        assert isinstance(result[-1], ActionRow)


class TestViewsTab:
    async def test_empty_state_shows_alert(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = await view.build_views()
        # With no views, should show an alert Container
        containers = [c for c in result if isinstance(c, Container)]
        assert len(containers) >= 1

    async def test_with_views_shows_cards(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        store = get_store()

        store.state["views"] = {
            "test_view_1": {
                "id": "test_view_1",
                "type": "CounterView",
                "user_id": 123,
                "channel_id": 456,
                "message_id": 789,
            }
        }
        store._active_views["test_view_1"] = "instance"

        result = await view.build_views()
        containers = [c for c in result if isinstance(c, Container)]
        # Should have views card + registry card
        assert len(containers) >= 2


class TestSessionsTab:
    async def test_empty_state_shows_alert(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = await view.build_sessions()
        containers = [c for c in result if isinstance(c, Container)]
        assert len(containers) >= 1

    async def test_with_sessions_shows_card(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        store = get_store()

        store.state["sessions"] = {
            "TestView:user_999": {
                "id": "TestView:user_999",
                "user_id": 999,
                "views": ["v1"],
                "nav_stack": [],
                "data": {"counter": 5},
                "created_at": "2026-03-30T12:00:00.000000",
            }
        }

        result = await view.build_sessions()
        containers = [c for c in result if isinstance(c, Container)]
        assert len(containers) >= 1


class TestHistoryTab:
    async def test_empty_state_shows_alert(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = await view.build_history()
        containers = [c for c in result if isinstance(c, Container)]
        assert len(containers) >= 1

    async def test_with_history_shows_card(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        store = get_store()

        store.history = [
            {"type": "COUNTER_UPDATED", "source": "some_view", "timestamp": "2026-03-30T12:00:00"},
            {"type": "VIEW_CREATED", "source": "other_view", "timestamp": "2026-03-30T12:00:01"},
        ]

        result = await view.build_history()
        containers = [c for c in result if isinstance(c, Container)]
        assert len(containers) >= 1


class TestConfigTab:
    async def test_returns_list_with_cards(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = await view.build_config()
        assert isinstance(result, list)
        containers = [c for c in result if isinstance(c, Container)]
        # reducers + middleware + persistence = 3 cards
        assert len(containers) >= 3

    async def test_exit_row_present(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = await view.build_config()
        assert isinstance(result[-1], ActionRow)


# // ========================================( Helpers )======================================== // #


class TestTruncate:
    def test_short_list(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = view._truncate(["a", "b", "c"])
        assert result == "a, b, c"

    def test_empty_list(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = view._truncate([])
        assert result == "None"

    def test_long_list_truncated(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        items = [f"VERY_LONG_ACTION_TYPE_{i}" for i in range(20)]
        result = view._truncate(items, max_len=80)
        assert "... +" in result
        assert "more" in result


class TestFormatTimestamp:
    def test_iso_timestamp(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        result = view._format_timestamp("2026-03-30T12:34:56.789000")
        assert result == "12:34:56"

    def test_na_passthrough(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        assert view._format_timestamp("N/A") == "N/A"
        assert view._format_timestamp(None) == "N/A"
        assert view._format_timestamp("") == "N/A"
