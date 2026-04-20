"""Tests for the V2 DevTools inspector."""

# // ========================================( Modules )======================================== // #


from discord.ui import ActionRow, Container, LayoutView, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.devtools import DevToolsCog, InspectorView
from cascadeui.state.singleton import get_store
from cascadeui.views.patterns import TabLayoutView

# // ========================================( Class )======================================== // #


class TestInspectorViewInit:
    """Init, inheritance, and configuration."""

    def test_is_subclass_of_tab_layout_view(self):
        assert issubclass(InspectorView, TabLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(InspectorView, LayoutView)

    def test_init_with_interaction(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        assert view.state_store is not None
        # InspectorView inherits the default (session_continuity = False),
        # so the derived session_id carries the per-instance UUID suffix.
        prefix = f"{InspectorView._class_session_key()}:user_100:"
        assert view.session_id.startswith(prefix)
        assert len(view.session_id) == len(prefix) + 8

    def test_has_six_tabs(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        assert len(view._tab_names) == 6

    def test_instance_limiting_config(self):
        assert InspectorView.instance_limit == 1
        assert InspectorView.instance_scope == "user_guild"
        assert InspectorView.instance_policy == "replace"

    def test_has_tab_buttons(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        action_rows = [c for c in view.children if isinstance(c, ActionRow)]
        # tab_overflow_policy = "pin_first": Overview alone on row 1,
        # the remaining five tabs on row 2.
        assert len(action_rows) >= 2
        assert len(action_rows[0].children) == 1
        assert len(action_rows[1].children) == 5


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
            view.session_id: {"id": view.session_id, "members": [view.id]},
            "OtherView:user_100": {"id": "OtherView:user_100", "members": ["v2"]},
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
    """Overview tab returns containers with store stats and an exit row."""
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
    """Views tab shows an alert when empty and view cards when populated."""
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
    """Sessions tab shows an alert when empty and session cards when populated."""
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
                "members": ["v1"],
                "nav_stack": [],
                "shared_data": {"counter": 5},
                "created_at": "2026-03-30T12:00:00.000000",
            }
        }

        result = await view.build_sessions()
        containers = [c for c in result if isinstance(c, Container)]
        assert len(containers) >= 1


class TestHistoryTab:
    """History tab shows an alert when empty and action cards when populated."""
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
    """Config tab shows reducer, middleware, and persistence cards."""
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
    """_truncate joins short lists and adds overflow count for long ones."""
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
    """_format_timestamp extracts time from ISO strings and handles N/A."""
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


# // ========================================( Computed Aggregations )======================================== // #


class TestComputedAggregations:
    """Module-level @computed registrations consumed by the inspector."""

    def test_module_exports_aggregation_functions(self):
        # All four decorated functions survive @computed and remain importable.
        from cascadeui.devtools import (
            application_keys,
            state_size_bytes,
            total_sessions,
            total_views,
        )
        assert callable(total_views)
        assert callable(total_sessions)
        assert callable(application_keys)
        assert callable(state_size_bytes)

    def test_total_views_reflects_state(self):
        store = get_store()
        store.state["views"] = {"a": {}, "b": {}, "c": {}}
        assert store.computed["total_views"] == 3

    def test_total_sessions_reflects_state(self):
        store = get_store()
        store.state["sessions"] = {"s1": {}, "s2": {}}
        assert store.computed["total_sessions"] == 2

    def test_application_keys_reflects_state(self):
        store = get_store()
        store.state["application"] = {"theme": "dark", "count": 3, "name": "Ada"}
        keys = store.computed["application_keys"]
        assert isinstance(keys, list)
        assert set(keys) == {"theme", "count", "name"}

    def test_state_size_bytes_positive_for_nonempty_state(self):
        store = get_store()
        store.state["application"] = {"note": "hello"}
        assert store.computed["state_size_bytes"] > 0

    def test_selector_caches_until_input_changes(self):
        store = get_store()
        store.state["views"] = {"x": {}, "y": {}}
        first = store.computed["total_views"]
        # Second read with no state change returns the cached value.
        second = store.computed["total_views"]
        assert first == second == 2

    def test_inspector_overview_consumes_computed_totals(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        store = get_store()
        # Two external views + the inspector itself; Overview subtracts one for self.
        store.state["views"] = {
            view.id: {"id": view.id, "type": "InspectorView"},
            "ext_1": {"id": "ext_1", "type": "CounterView"},
            "ext_2": {"id": "ext_2", "type": "CounterView"},
        }
        assert store.computed["total_views"] - 1 == 2


# // ========================================( Selector + Subscriptions )======================================== // #


class TestInspectorSelector:
    """Selector returns frozenset identity, not counts -- push/pop regression."""

    def test_selector_returns_frozensets(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        state = {
            "views": {view.id: {}, "other_1": {}, "other_2": {}},
            "sessions": {view.session_id: {}, "other_session": {}},
        }
        selected = view.state_selector(state)
        assert isinstance(selected, tuple)
        assert len(selected) == 2
        assert isinstance(selected[0], frozenset)
        assert isinstance(selected[1], frozenset)

    def test_selector_excludes_own_view_and_session(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        state = {
            "views": {view.id: {}, "other": {}},
            "sessions": {view.session_id: {}, "other_session": {}},
        }
        views, sessions = view.state_selector(state)
        assert view.id not in views
        assert "other" in views
        assert view.session_id not in sessions
        assert "other_session" in sessions

    def test_selector_detects_view_id_swap_at_stable_count(self):
        """Push/pop swaps one view ID for another while total count stays N.

        A count-based selector would return the same tuple before and after
        and short-circuit the notification; the identity tuple changes.
        """
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)

        before = {"views": {"a": {}, "b": {}}, "sessions": {}}
        after = {"views": {"a": {}, "c": {}}, "sessions": {}}

        assert view.state_selector(before) != view.state_selector(after)

    def test_selector_stable_when_state_unchanged(self):
        interaction = _make_interaction()
        view = InspectorView(interaction=interaction)
        state = {"views": {"a": {}, "b": {}}, "sessions": {"s1": {}}}

        assert view.state_selector(state) == view.state_selector(state)


class TestInspectorSubscribedActions:
    """subscribed_actions covers every navigation + view update path."""

    def test_includes_view_lifecycle(self):
        assert "VIEW_CREATED" in InspectorView.subscribed_actions
        assert "VIEW_UPDATED" in InspectorView.subscribed_actions
        assert "VIEW_DESTROYED" in InspectorView.subscribed_actions

    def test_includes_navigation_family(self):
        assert "NAVIGATION_PUSH" in InspectorView.subscribed_actions
        assert "NAVIGATION_POP" in InspectorView.subscribed_actions
        assert "NAVIGATION_REPLACE" in InspectorView.subscribed_actions

    def test_includes_session_lifecycle(self):
        assert "SESSION_CREATED" in InspectorView.subscribed_actions
        assert "SESSION_UPDATED" in InspectorView.subscribed_actions


# // ========================================( Ghost Cleanup Helper )======================================== // #


class TestCleanupGhostView:
    """_cleanup_ghost_view runs the three-step registry + subscriber + state mop-up.

    Tests target the ghost case (state row exists, no live instance) since
    that is the helper's documented purpose. The live-instance branch of
    _unregister_view is covered in test_state_store.
    """

    async def test_removes_subscriber_entry(self):
        from cascadeui.devtools import _cleanup_ghost_view

        store = get_store()
        store.subscribers["ghost_2"] = (lambda s: None, None, None)
        store.state["views"] = {"ghost_2": {"id": "ghost_2", "type": "X"}}

        await _cleanup_ghost_view(store, "ghost_2")
        assert "ghost_2" not in store.subscribers

    async def test_dispatches_view_destroyed_and_clears_state(self):
        from cascadeui.devtools import _cleanup_ghost_view

        store = get_store()
        store.state["views"] = {"ghost_3": {"id": "ghost_3", "type": "X"}}

        await _cleanup_ghost_view(store, "ghost_3")
        assert "ghost_3" not in store.state.get("views", {})

    async def test_idempotent_when_view_already_gone(self):
        """Helper must not crash when called twice or on a missing view_id.

        Matches the real call pattern in ``_exit_all_views``: iterate the
        state list and clean each entry, even if an earlier cleanup dropped
        it from _active_views.
        """
        from cascadeui.devtools import _cleanup_ghost_view

        store = get_store()
        store.state["views"] = {"ghost_4": {"id": "ghost_4", "type": "X"}}

        await _cleanup_ghost_view(store, "ghost_4")
        # Second call on an already-cleaned view_id must be a silent no-op.
        await _cleanup_ghost_view(store, "ghost_4")
        assert "ghost_4" not in store.state.get("views", {})


# // ========================================( Purge Reducer None Path )======================================== // #


class TestPurgeStaleReducerNullPath:
    """INSPECTOR_PURGED_STALE with inspector_id=None purges everything (CLI path)."""

    async def test_none_inspector_id_purges_components(self):
        store = get_store()
        store.state["components"] = {"c1": {"view_id": "some_view"}}
        store.state["modals"] = {"m1": {"data": "x"}}

        await store.dispatch("INSPECTOR_PURGED_STALE", {"inspector_id": None})
        assert "components" not in store.state
        assert "modals" not in store.state

    async def test_missing_inspector_id_key_is_noop(self):
        store = get_store()
        store.state["components"] = {"c1": {"view_id": "some_view"}}

        await store.dispatch("INSPECTOR_PURGED_STALE", {})
        # Defensive guard: empty payload is a no-op.
        assert store.state.get("components") == {"c1": {"view_id": "some_view"}}

    async def test_present_inspector_id_preserves_matching_rows(self):
        store = get_store()
        store.state["components"] = {
            "c1": {"view_id": "keep_me"},
            "c2": {"view_id": "drop_me"},
        }

        await store.dispatch("INSPECTOR_PURGED_STALE", {"inspector_id": "keep_me"})
        assert store.state.get("components") == {"c1": {"view_id": "keep_me"}}
