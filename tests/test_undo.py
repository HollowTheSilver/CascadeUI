"""Tests for undo/redo middleware and state snapshot restoration."""

import asyncio
import copy
import logging
import threading

import pytest
from helpers import make_interaction as _make_interaction

from cascadeui.state.actions import ActionCreators
from cascadeui.state.middleware import UndoMiddleware
from cascadeui.state.middleware.undo import _diff_application_slots
from cascadeui.state.singleton import get_store
from cascadeui.state.slots import access_slot, read_slot
from cascadeui.views.layout import StatefulLayoutView


class TestUndoMiddleware:
    """Undo/redo snapshot creation, restoration, and view-local stack behavior."""

    async def test_dispatch_creates_undo_snapshot(self):
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        # Create a session
        await store.dispatch("SESSION_CREATED", {"session_id": "undo_s", "user_id": 1})

        # Mark a view as undo-enabled (dict: view_id -> limit)
        store._undo_enabled_views["view_1"] = 20

        # Register view in state
        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "view_1",
                "view_type": "Test",
                "user_id": 1,
                "session_id": "undo_s",
            },
        )

        # Set initial application state
        store.state["application"]["counter"] = 0

        # Register a custom reducer
        async def inc_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["counter"] = state["application"].get("counter", 0) + 1
            return new

        store._register_reducer("INCREMENT", inc_reducer)

        # Dispatch from the undo-enabled view
        await store.dispatch("INCREMENT", {}, source_id="view_1")

        view = store.state["views"]["view_1"]
        assert "undo_stack" in view
        assert len(view["undo_stack"]) == 1

    async def test_undo_restores_previous_state_via_reducer(self):
        """Undo should restore state through the UNDO reducer."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "undo_s2", "user_id": 2})
        store._undo_enabled_views["v2"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "v2",
                "view_type": "Test",
                "user_id": 2,
                "session_id": "undo_s2",
            },
        )

        store.state["application"]["val"] = "before"

        async def set_val(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"]["val"]
            return new

        store._register_reducer("SET_VAL", set_val)

        await store.dispatch("SET_VAL", {"val": "after"}, source_id="v2")
        assert store.state["application"]["val"] == "after"

        # Verify undo stack has a snapshot on the view
        view = store.state["views"]["v2"]
        assert len(view.get("undo_stack", [])) == 1

        # Dispatch UNDO through the reducer pipeline
        await store.dispatch("UNDO", {"view_id": "v2", "session_id": "undo_s2"})

        assert store.state["application"]["val"] == "before"

        # Verify redo stack was populated on the view
        view = store.state["views"]["v2"]
        assert len(view.get("redo_stack", [])) == 1
        assert len(view.get("undo_stack", [])) == 0

    async def test_redo_via_reducer(self):
        """Redo should re-apply via the REDO reducer."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "redo_s", "user_id": 6})
        store._undo_enabled_views["v_redo"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "v_redo",
                "view_type": "Test",
                "user_id": 6,
                "session_id": "redo_s",
            },
        )

        store.state["application"]["x"] = 1

        async def set_x(action, state):
            new = copy.deepcopy(state)
            new["application"]["x"] = action["payload"]["x"]
            return new

        store._register_reducer("SET_X", set_x)

        await store.dispatch("SET_X", {"x": 2}, source_id="v_redo")
        assert store.state["application"]["x"] == 2

        # Undo
        await store.dispatch("UNDO", {"view_id": "v_redo", "session_id": "redo_s"})
        assert store.state["application"]["x"] == 1

        # Redo
        await store.dispatch("REDO", {"view_id": "v_redo", "session_id": "redo_s"})
        assert store.state["application"]["x"] == 2

    async def test_stack_depth_limit(self):
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "limit_s", "user_id": 3})

        # Register view with a limit of 5
        store._undo_enabled_views["v_limit"] = 5

        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "v_limit",
                "view_type": "Test",
                "user_id": 3,
                "session_id": "limit_s",
            },
        )

        async def noop(action, state):
            new = copy.deepcopy(state)
            new["application"]["n"] = action["payload"].get("n", 0)
            return new

        store._register_reducer("NOOP_ACTION", noop)

        for i in range(10):
            await store.dispatch("NOOP_ACTION", {"n": i}, source_id="v_limit")

        view = store.state["views"]["v_limit"]
        assert len(view["undo_stack"]) <= 5

    async def test_empty_undo_stack_is_noop(self):
        store = get_store()

        await store.dispatch("SESSION_CREATED", {"session_id": "empty_s", "user_id": 4})
        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_empty", "view_type": "Test", "user_id": 4, "session_id": "empty_s"},
        )

        # UNDO on empty stack should not crash
        await store.dispatch("UNDO", {"view_id": "v_empty", "session_id": "empty_s"})

        view = store.state["views"]["v_empty"]
        assert view.get("undo_stack", []) == []

    async def test_lifecycle_actions_not_recorded(self):
        """Internal lifecycle actions like VIEW_CREATED should not create undo snapshots."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        store._undo_enabled_views["v_lifecycle"] = 20

        await store.dispatch("SESSION_CREATED", {"session_id": "lc_s", "user_id": 5})
        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "v_lifecycle",
                "view_type": "Test",
                "user_id": 5,
                "session_id": "lc_s",
            },
        )

        view = store.state["views"]["v_lifecycle"]
        # Only lifecycle actions dispatched -- no undo entries
        assert view.get("undo_stack", []) == []

    async def test_dispatch_scoped_creates_undo_snapshot(self):
        """dispatch_scoped (SCOPED_UPDATE) should create undo snapshots when enable_undo is set."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "scoped_s", "user_id": 10})
        store._undo_enabled_views["v_scoped"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "v_scoped",
                "view_type": "Test",
                "user_id": 10,
                "session_id": "scoped_s",
            },
        )

        # Dispatch a SCOPED_UPDATE (what dispatch_scoped() sends)
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 10}, "data": {"theme": "dark"}},
            source_id="v_scoped",
        )

        view = store.state["views"]["v_scoped"]
        assert len(view.get("undo_stack", [])) == 1

        # Verify the scoped state was written
        scoped = read_slot(store.state, "scoped", "user:10")
        assert scoped["theme"] == "dark"

    async def test_undo_restores_scoped_state(self):
        """Undo should restore scoped state changed by SCOPED_UPDATE."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "scoped_undo_s", "user_id": 11})
        store._undo_enabled_views["v_scoped_undo"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "v_scoped_undo",
                "view_type": "Test",
                "user_id": 11,
                "session_id": "scoped_undo_s",
            },
        )

        # Set initial scoped state
        access_slot(store.state, "scoped")["user:11"] = {"theme": "light"}

        # Dispatch scoped update
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 11}, "data": {"theme": "dark"}},
            source_id="v_scoped_undo",
        )
        assert read_slot(store.state, "scoped", "user:11")["theme"] == "dark"

        # Undo should restore the original theme
        await store.dispatch("UNDO", {"view_id": "v_scoped_undo", "session_id": "scoped_undo_s"})
        assert read_slot(store.state, "scoped", "user:11")["theme"] == "light"

    async def test_undo_does_not_corrupt_stacks(self):
        """Undo/redo via reducer should use deepcopy -- no reference sharing."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "safe_s", "user_id": 7})
        store._undo_enabled_views["v_safe"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "v_safe",
                "view_type": "Test",
                "user_id": 7,
                "session_id": "safe_s",
            },
        )

        store.state["application"]["items"] = ["a"]

        async def add_item(action, state):
            new = copy.deepcopy(state)
            new["application"]["items"] = state["application"]["items"] + [
                action["payload"]["item"]
            ]
            return new

        store._register_reducer("ADD_ITEM", add_item)

        await store.dispatch("ADD_ITEM", {"item": "b"}, source_id="v_safe")
        assert store.state["application"]["items"] == ["a", "b"]

        # Undo
        await store.dispatch("UNDO", {"view_id": "v_safe", "session_id": "safe_s"})
        assert store.state["application"]["items"] == ["a"]

        # Mutate live state -- should NOT affect redo stack
        store.state["application"]["items"].append("CORRUPTED")

        # Redo
        await store.dispatch("REDO", {"view_id": "v_safe", "session_id": "safe_s"})
        assert store.state["application"]["items"] == ["a", "b"]

    async def test_cross_view_undo_notification(self):
        """View B should be notified when View A undoes, even if B doesn't subscribe to UNDO."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "cross_s", "user_id": 20})
        store._undo_enabled_views["v_a"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_a", "view_type": "Test", "user_id": 20, "session_id": "cross_s"},
        )

        store.state["application"]["theme"] = "light"

        async def set_theme(action, state):
            new = copy.deepcopy(state)
            new["application"]["theme"] = action["payload"]["theme"]
            return new

        store._register_reducer("SET_THEME", set_theme)

        # View B subscribes to SET_THEME with a selector (not UNDO)
        notified = []

        def selector_b(state):
            return state.get("application", {}).get("theme")

        async def on_change_b(state, action):
            notified.append(action["type"])

        store.subscribe("v_b", on_change_b, {"SET_THEME"}, selector_b)

        # View A dispatches SET_THEME
        await store.dispatch("SET_THEME", {"theme": "dark"}, source_id="v_a")
        await store._flush_notifications()
        assert "SET_THEME" in notified

        notified.clear()

        # View A undoes -- View B should be notified even though it doesn't subscribe to UNDO
        await store.dispatch("UNDO", {"view_id": "v_a", "session_id": "cross_s"})
        await store._flush_notifications()
        assert store.state["application"]["theme"] == "light"
        assert "UNDO" in notified

    async def test_cross_view_undo_notification_from_a_batch(self):
        """An UNDO dispatched inside a batch skipped view B's filter bypass,
        so B kept showing the undone value."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "batch_s", "user_id": 22})
        store._undo_enabled_views["v_batch_a"] = 20
        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_batch_a", "view_type": "Test", "user_id": 22, "session_id": "batch_s"},
        )
        store.state["application"]["theme"] = "light"

        async def set_theme(action, state):
            new = copy.deepcopy(state)
            new["application"]["theme"] = action["payload"]["theme"]
            return new

        store._register_reducer("SET_THEME", set_theme)
        notified = []

        async def on_change_b(state, action):
            notified.append(action["type"])

        store.subscribe(
            "v_batch_b", on_change_b, {"SET_THEME"}, lambda s: s["application"].get("theme")
        )
        await store.dispatch("SET_THEME", {"theme": "dark"}, source_id="v_batch_a")
        await store._flush_notifications()
        notified.clear()

        async with store.batch():
            await store.dispatch("UNDO", {"view_id": "v_batch_a", "session_id": "batch_s"})
        await store._flush_notifications()

        assert store.state["application"]["theme"] == "light"
        assert notified == ["BATCH_COMPLETE"]

    async def test_cross_view_redo_notification(self):
        """View B should be notified when View A redoes."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "redo_cross_s", "user_id": 21})
        store._undo_enabled_views["v_redo_a"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "v_redo_a",
                "view_type": "Test",
                "user_id": 21,
                "session_id": "redo_cross_s",
            },
        )

        store.state["application"]["color"] = "red"

        async def set_color(action, state):
            new = copy.deepcopy(state)
            new["application"]["color"] = action["payload"]["color"]
            return new

        store._register_reducer("SET_COLOR", set_color)

        notified = []

        def selector(state):
            return state.get("application", {}).get("color")

        async def on_change(state, action):
            notified.append(action["type"])

        store.subscribe("v_redo_b", on_change, {"SET_COLOR"}, selector)

        await store.dispatch("SET_COLOR", {"color": "blue"}, source_id="v_redo_a")
        await store._flush_notifications()
        notified.clear()

        await store.dispatch("UNDO", {"view_id": "v_redo_a", "session_id": "redo_cross_s"})
        await store._flush_notifications()
        assert "UNDO" in notified
        notified.clear()

        await store.dispatch("REDO", {"view_id": "v_redo_a", "session_id": "redo_cross_s"})
        await store._flush_notifications()
        assert "REDO" in notified
        assert store.state["application"]["color"] == "blue"

    async def test_unrelated_view_not_notified_on_undo(self):
        """View C with unrelated state should NOT be notified on undo (selector filters it)."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "unrel_s", "user_id": 22})
        store._undo_enabled_views["v_unrel_a"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "v_unrel_a",
                "view_type": "Test",
                "user_id": 22,
                "session_id": "unrel_s",
            },
        )

        store.state["application"]["score"] = 0
        store.state["application"]["unrelated"] = "fixed"

        async def inc_score(action, state):
            new = copy.deepcopy(state)
            new["application"]["score"] = state["application"].get("score", 0) + 1
            return new

        store._register_reducer("INC_SCORE", inc_score)

        # View C watches "unrelated" -- which never changes during undo
        notified_c = []

        def selector_c(state):
            return state.get("application", {}).get("unrelated")

        async def on_change_c(state, action):
            notified_c.append(action["type"])

        store.subscribe("v_c", on_change_c, {"INC_SCORE"}, selector_c)

        await store.dispatch("INC_SCORE", {}, source_id="v_unrel_a")
        notified_c.clear()  # Clear the INC_SCORE notification

        # Undo -- score changes back but "unrelated" doesn't
        await store.dispatch("UNDO", {"view_id": "v_unrel_a", "session_id": "unrel_s"})
        assert store.state["application"]["score"] == 0
        assert notified_c == []  # Selector filtered it out

    async def test_shared_data_included_in_undo_snapshot(self):
        """update_session changes are captured by undo snapshots."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "sd_undo", "user_id": 20})
        store._undo_enabled_views["v_sd"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_sd", "view_type": "Test", "user_id": 20, "session_id": "sd_undo"},
        )

        # Set initial session data
        await store.dispatch(
            "SESSION_UPDATED",
            {"session_id": "sd_undo", "shared_data": {"lang": "en"}},
            source_id="v_sd",
        )

        assert store.state["sessions"]["sd_undo"]["shared_data"]["lang"] == "en"
        assert len(store.state["views"]["v_sd"].get("undo_stack", [])) == 1

        # Change session data
        await store.dispatch(
            "SESSION_UPDATED",
            {"session_id": "sd_undo", "shared_data": {"lang": "fr"}},
            source_id="v_sd",
        )
        assert store.state["sessions"]["sd_undo"]["shared_data"]["lang"] == "fr"

        # Undo should restore to "en"
        await store.dispatch("UNDO", {"view_id": "v_sd", "session_id": "sd_undo"})
        assert store.state["sessions"]["sd_undo"]["shared_data"]["lang"] == "en"

    async def test_shared_data_redo_after_undo(self):
        """Redo restores session data that was undone."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "sd_redo", "user_id": 21})
        store._undo_enabled_views["v_sr"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_sr", "view_type": "Test", "user_id": 21, "session_id": "sd_redo"},
        )

        await store.dispatch(
            "SESSION_UPDATED",
            {"session_id": "sd_redo", "shared_data": {"mode": "easy"}},
            source_id="v_sr",
        )
        await store.dispatch(
            "SESSION_UPDATED",
            {"session_id": "sd_redo", "shared_data": {"mode": "hard"}},
            source_id="v_sr",
        )

        # Undo to "easy"
        await store.dispatch("UNDO", {"view_id": "v_sr", "session_id": "sd_redo"})
        assert store.state["sessions"]["sd_redo"]["shared_data"]["mode"] == "easy"

        # Redo back to "hard"
        await store.dispatch("REDO", {"view_id": "v_sr", "session_id": "sd_redo"})
        assert store.state["sessions"]["sd_redo"]["shared_data"]["mode"] == "hard"

    async def test_shared_data_and_application_undo_together(self):
        """Undo restores both application state and session data atomically."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "sd_both", "user_id": 22})
        store._undo_enabled_views["v_both"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_both", "view_type": "Test", "user_id": 22, "session_id": "sd_both"},
        )

        # Set initial states
        store.state.setdefault("application", {})["score"] = 0
        await store.dispatch(
            "SESSION_UPDATED",
            {"session_id": "sd_both", "shared_data": {"difficulty": "normal"}},
            source_id="v_both",
        )

        # Custom reducer that changes application state
        async def score_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["score"] = action["payload"].get("value", 0)
            return new

        store._register_reducer("SET_SCORE", score_reducer)

        # Change both application state and session data
        await store.dispatch("SET_SCORE", {"value": 100}, source_id="v_both")
        await store.dispatch(
            "SESSION_UPDATED",
            {"session_id": "sd_both", "shared_data": {"difficulty": "hard"}},
            source_id="v_both",
        )

        assert store.state["application"]["score"] == 100
        assert store.state["sessions"]["sd_both"]["shared_data"]["difficulty"] == "hard"

        # Undo the session data change
        await store.dispatch("UNDO", {"view_id": "v_both", "session_id": "sd_both"})
        assert store.state["sessions"]["sd_both"]["shared_data"]["difficulty"] == "normal"
        assert store.state["application"]["score"] == 100  # Score from SET_SCORE still there

        # Undo the score change
        await store.dispatch("UNDO", {"view_id": "v_both", "session_id": "sd_both"})
        assert store.state["application"]["score"] == 0  # Restored
        assert store.state["sessions"]["sd_both"]["shared_data"]["difficulty"] == "normal"

    async def test_undo_stack_survives_view_transfer(self):
        """Undo stacks transferred to a new view (simulating push/pop) remain functional."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "xfer_s", "user_id": 30})
        store._undo_enabled_views["v_src"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_src", "view_type": "Test", "user_id": 30, "session_id": "xfer_s"},
        )

        store.state["application"]["val"] = "original"

        async def set_val(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"]["val"]
            return new

        store._register_reducer("SET_VAL", set_val)

        await store.dispatch("SET_VAL", {"val": "changed"}, source_id="v_src")
        assert store.state["application"]["val"] == "changed"

        # Simulate push: create destination view, transfer undo stacks through
        # a VIEW_UPDATED dispatch (the same reducer-routed path _navigate_to
        # uses), not an in-place write to the state row.
        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_dst", "view_type": "Test", "user_id": 30, "session_id": "xfer_s"},
        )
        store._undo_enabled_views["v_dst"] = 20

        src_state = store.state["views"]["v_src"]
        await store.dispatch(
            "VIEW_UPDATED",
            ActionCreators.view_updated(
                "v_dst",
                undo_stack=list(src_state.get("undo_stack", [])),
                redo_stack=list(src_state.get("redo_stack", [])),
            ),
        )

        # Destroy old view (simulates VIEW_DESTROYED during push)
        await store.dispatch("VIEW_DESTROYED", {"view_id": "v_src"})
        assert "v_src" not in store.state["views"]

        # Undo from the destination view should restore the original value
        await store.dispatch("UNDO", {"view_id": "v_dst", "session_id": "xfer_s"})
        assert store.state["application"]["val"] == "original"

        # Redo should re-apply
        await store.dispatch("REDO", {"view_id": "v_dst", "session_id": "xfer_s"})
        assert store.state["application"]["val"] == "changed"


class TestUndoSnapshotPurity:
    """``_views_with_undo_pushed`` builds a fresh ``views`` mapping and never
    mutates the input state. The batch-commit path rebinds ``store.state``
    rather than writing the committed live dict in place."""

    def test_helper_does_not_mutate_input_state(self):
        undo_mw = UndoMiddleware()
        original_view = {"undo_stack": [], "redo_stack": ["old"]}
        original_views = {"v1": original_view}
        state = {"views": original_views}

        new_views = undo_mw._views_with_undo_pushed(state, "v1", {"snap": 1}, 20)

        # Input is untouched -- same object identities, no pushed snapshot.
        assert state["views"] is original_views
        assert state["views"]["v1"] is original_view
        assert original_view["undo_stack"] == []
        assert original_view["redo_stack"] == ["old"]
        # Return is a fresh mapping carrying the snapshot, redo cleared.
        assert new_views is not original_views
        assert new_views["v1"] is not original_view
        assert new_views["v1"]["undo_stack"] == [{"snap": 1}]
        assert new_views["v1"]["redo_stack"] == []

    def test_helper_returns_none_for_absent_view(self):
        undo_mw = UndoMiddleware()
        assert undo_mw._views_with_undo_pushed({"views": {}}, "ghost", {}, 20) is None

    def test_helper_honors_limit(self):
        undo_mw = UndoMiddleware()
        state = {"views": {"v1": {"undo_stack": [1, 2, 3], "redo_stack": []}}}
        new_views = undo_mw._views_with_undo_pushed(state, "v1", 4, limit=3)
        assert new_views["v1"]["undo_stack"] == [2, 3, 4]

    async def test_batch_finalize_rebinds_state_object(self):
        """A batched undo-enabled dispatch must leave the pre-batch state
        object unmutated -- the undo write lands on a newly-bound
        ``store.state``, never on the dict a subscriber may already hold."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "rb_s", "user_id": 1})
        store._undo_enabled_views["rb_view"] = 20
        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "rb_view", "view_type": "T", "user_id": 1, "session_id": "rb_s"},
        )

        async def set_val(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"]["v"]
            return new

        store._register_reducer("SET_VAL", set_val)

        async with store.batch(source_id="rb_view"):
            await store.dispatch("SET_VAL", {"v": 1}, source_id="rb_view")

        # The undo snapshot landed in the live state.
        assert len(store.state["views"]["rb_view"]["undo_stack"]) == 1
        captured = store.state

        # A second batch must not retro-mutate the previously-captured dict.
        async with store.batch(source_id="rb_view"):
            await store.dispatch("SET_VAL", {"v": 2}, source_id="rb_view")

        assert store.state is not captured
        assert len(captured["views"]["rb_view"]["undo_stack"]) == 1
        assert len(store.state["views"]["rb_view"]["undo_stack"]) == 2


class TestUndoDepthProperties:
    """Public view.undo_depth / view.redo_depth mirror the stored snapshot counts."""

    async def test_depth_zero_before_any_dispatch(self):
        """A fresh undo-enabled view reports zero depth on both stacks."""

        class _V(StatefulLayoutView):
            enable_undo = True

        view = _V(interaction=_make_interaction())
        assert view.undo_depth == 0
        assert view.redo_depth == 0

    async def test_depth_reads_live_stack_lengths(self):
        """The properties read directly from the view's undo_stack / redo_stack slots."""

        class _V(StatefulLayoutView):
            enable_undo = True

        view = _V(interaction=_make_interaction())
        store = get_store()

        store.state["views"][view.id] = {
            "undo_stack": [{"application": {}, "shared_data": {}}] * 3,
            "redo_stack": [{"application": {}, "shared_data": {}}],
        }

        assert view.undo_depth == 3
        assert view.redo_depth == 1

    async def test_depth_zero_when_view_missing_from_state(self):
        """Properties return 0 when the view has no state entry yet (pre-send)."""

        class _V(StatefulLayoutView):
            pass

        view = _V(interaction=_make_interaction())
        assert view.undo_depth == 0
        assert view.redo_depth == 0


class TestBatchUndoIntegration:
    """Batched dispatches produce a single undo entry per participating view.

    ``UndoMiddleware.__call__`` captures a diff per action while a batch is
    open but pushes nothing; ``BatchContext.__aexit__`` delegates to
    ``UndoMiddleware.finalize_batch``, which merges those records
    first-write-wins so exactly one snapshot lands on each participating
    view's stack when the outermost batch commits.
    """

    async def _register_view(self, store, view_id, session_id, user_id, limit=20):
        """Create a session + view + mark undo-enabled. Mirrors TestUndoMiddleware setup."""
        await store.dispatch("SESSION_CREATED", {"session_id": session_id, "user_id": user_id})
        store._undo_enabled_views[view_id] = limit
        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": view_id,
                "view_type": "Test",
                "user_id": user_id,
                "session_id": session_id,
            },
        )

    async def test_a_batch_whose_undo_merge_raises_is_still_announced(self, caplog):
        """A value whose == refuses bool() (a numpy array) in shared_data made
        the batch's undo merge raise: the batch was never announced, and the
        error escaped `async with batch` into the caller."""

        class _Ambiguous:
            def __bool__(self):
                raise ValueError("The truth value of an array is ambiguous.")

        class _ArrayLike:
            __hash__ = None

            def __eq__(self, other):
                return _Ambiguous()

            def __ne__(self, other):
                return _Ambiguous()

        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await store.dispatch(
            "SESSION_CREATED",
            {"session_id": "arr_s", "shared_data": {"grid": _ArrayLike(), "turn": 1}},
        )
        await self._register_view(store, "arr_v", "arr_s", user_id=1)
        told = []

        async def on_change(state, action):
            told.append(action["type"])

        store.subscribe("arr_sub", on_change)
        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            async with store.batch(source_id="arr_v"):
                await store.dispatch(
                    "SESSION_UPDATED",
                    {"session_id": "arr_s", "shared_data": {"turn": 2}},
                    source_id="arr_v",
                )
            await store._flush_notifications()

        assert store.state["sessions"]["arr_s"]["shared_data"]["turn"] == 2
        assert told[-1] == "BATCH_COMPLETE"
        assert "Undo could not record a batch for view arr_v" in caplog.text

    async def test_a_batch_whose_merge_cannot_compare_a_slot_is_still_announced(self, caplog):
        """The merge compares a slot's value from before the batch with its
        value after it. A value whose == refuses bool() there made the batch's
        exit raise into the caller, and the batch was never announced."""

        class _Ambiguous:
            def __bool__(self):
                raise ValueError("The truth value of an array is ambiguous.")

        class _ArrayLike:
            __hash__ = None

            def __eq__(self, other):
                return _Ambiguous() if isinstance(other, _ArrayLike) else False

            def __ne__(self, other):
                return _Ambiguous() if isinstance(other, _ArrayLike) else True

        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        await self._register_view(store, "grid_v", "grid_s", user_id=1)
        store.state["application"]["grid"] = _ArrayLike()

        async def set_grid(action, state):
            return {**state, "application": {**state["application"], "grid": action["payload"]}}

        store._register_reducer("SET_GRID", set_grid)
        told = []

        async def on_change(state, action):
            told.append(action["type"])

        store.subscribe("grid_sub", on_change)
        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            async with store.batch(source_id="grid_v"):
                await store.dispatch("SET_GRID", "cleared", source_id="grid_v")
                await store.dispatch("SET_GRID", _ArrayLike(), source_id="grid_v")
            await store._flush_notifications()

        assert told[-1] == "BATCH_COMPLETE"
        # The comparison runs where the view's step is built, so the view is named.
        assert "Undo could not record a batch for view grid_v" in caplog.text

    async def test_batch_produces_single_undo_entry(self):
        """N dispatches inside one batch produce exactly one undo snapshot."""
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await self._register_view(store, "bv_1", "batch_s1", user_id=1)
        store.state["application"]["counter"] = 0

        async def inc(action, state):
            new = copy.deepcopy(state)
            new["application"]["counter"] = state["application"].get("counter", 0) + 1
            return new

        store._register_reducer("INC", inc)

        async with store.batch():
            await store.dispatch("INC", {}, source_id="bv_1")
            await store.dispatch("INC", {}, source_id="bv_1")
            await store.dispatch("INC", {}, source_id="bv_1")

        view = store.state["views"]["bv_1"]
        assert len(view["undo_stack"]) == 1
        assert store.state["application"]["counter"] == 3

    async def test_concurrent_batches_do_not_share_undo_slots(self):
        """One batch's slot writes stay out of a concurrent batch's undo entry.

        Diffing a batch's entry snapshot against live state pulled in every
        other open batch's writes, so an UNDO reverted slots the view had
        never touched.
        """
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await self._register_view(store, "iso_a", "iso_sa", user_id=1)
        await self._register_view(store, "iso_b", "iso_sb", user_id=2)

        async def write(action, state):
            new = copy.deepcopy(state)
            new["application"][action["payload"]["slot"]] = action["payload"]["value"]
            return new

        store._register_reducer("ISO_WRITE", write)

        async def batched(view_id, slot):
            async with store.batch(source_id=view_id):
                # Yield first so both batches are genuinely open when either
                # writes; dispatching on entry lets them serialise by luck.
                await asyncio.sleep(0.01)
                await store.dispatch("ISO_WRITE", {"slot": slot, "value": 1}, source_id=view_id)

        await asyncio.gather(batched("iso_a", "slot_a"), batched("iso_b", "slot_b"))

        entry_a = store.state["views"]["iso_a"]["undo_stack"][-1]["application_slots"]
        entry_b = store.state["views"]["iso_b"]["undo_stack"][-1]["application_slots"]

        assert set(entry_a) == {"slot_a"}
        assert set(entry_b) == {"slot_b"}

    async def test_undo_after_batch_restores_pre_batch_state(self):
        """UNDO rewinds to the state visible at batch entry, not to any intermediate step."""
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await self._register_view(store, "bv_2", "batch_s2", user_id=2)
        store.state["application"]["val"] = "pre"

        async def set_val(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"]["val"]
            return new

        store._register_reducer("SET_VAL", set_val)

        async with store.batch():
            await store.dispatch("SET_VAL", {"val": "mid"}, source_id="bv_2")
            await store.dispatch("SET_VAL", {"val": "post"}, source_id="bv_2")

        assert store.state["application"]["val"] == "post"

        await store.dispatch("UNDO", {"view_id": "bv_2", "session_id": "batch_s2"})
        assert store.state["application"]["val"] == "pre"

    async def test_nested_batches_produce_one_entry(self):
        """Inner batches absorb into the outer; only the outermost commit pushes a snapshot."""
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await self._register_view(store, "bv_3", "batch_s3", user_id=3)
        store.state["application"]["n"] = 0

        async def inc(action, state):
            new = copy.deepcopy(state)
            new["application"]["n"] = state["application"].get("n", 0) + 1
            return new

        store._register_reducer("INC", inc)

        async with store.batch():
            await store.dispatch("INC", {}, source_id="bv_3")
            async with store.batch():
                await store.dispatch("INC", {}, source_id="bv_3")
                await store.dispatch("INC", {}, source_id="bv_3")
            await store.dispatch("INC", {}, source_id="bv_3")

        view = store.state["views"]["bv_3"]
        assert len(view["undo_stack"]) == 1
        assert store.state["application"]["n"] == 4

    async def test_a_batched_shared_data_write_is_undoable_from_an_empty_session(self):
        """A batch whose only change is shared_data still owes an undo entry.

        The pre-state to restore here is an empty dict, so any guard keyed on
        the truthiness of the captured shared_data drops exactly the case
        where the batch created that data.
        """
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await self._register_view(store, "bv_shared", "batch_shared", user_id=9)
        assert store.state["sessions"]["batch_shared"].get("shared_data") == {}

        async with store.batch(source_id="bv_shared"):
            await store.dispatch(
                "SESSION_UPDATED",
                {"session_id": "batch_shared", "shared_data": {"k": "val"}},
                source_id="bv_shared",
            )

        assert store.state["sessions"]["batch_shared"]["shared_data"] == {"k": "val"}
        assert len(store.state["views"]["bv_shared"].get("undo_stack", [])) == 1

        await store.dispatch("UNDO", {"view_id": "bv_shared", "session_id": "batch_shared"})
        assert store.state["sessions"]["batch_shared"]["shared_data"] == {}

    async def test_a_record_landing_on_an_actionless_ancestor_still_pushes(self):
        """An ancestor batch can hold undo records without holding actions.

        A dispatch resolves its batch before the middleware chain and queues
        after it, while ``UndoMiddleware`` re-resolves the lineage afterwards.
        When a chain suspends past the nested batch it started in, the action
        is refused and notified immediately while the record lands on the
        still-open ancestor. Returning early on an empty action list dropped
        that snapshot for a change that had already committed.
        """
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await self._register_view(store, "bv_anc", "batch_anc", user_id=11)

        async def slow_middleware(action, state, next_fn):
            if action["type"] == "LATE_WRITE":
                await asyncio.sleep(0.02)
            return await next_fn(action, state)

        async def write(action, state):
            new = copy.deepcopy(state)
            new["application"]["k"] = action["payload"]["v"]
            return new

        store._add_middleware(slow_middleware)
        store._register_reducer("LATE_WRITE", write)
        try:

            async def late():
                await asyncio.sleep(0.005)
                await store.dispatch("LATE_WRITE", {"v": 1}, source_id="bv_anc")

            async with store.batch():  # ancestor: dispatches nothing itself
                async with store.batch():  # nested: closes under the suspended chain
                    spawned = asyncio.create_task(late())
                    await asyncio.sleep(0.01)
                await spawned
        finally:
            store._remove_middleware(slow_middleware)

        assert store.state["application"]["k"] == 1
        assert len(store.state["views"]["bv_anc"].get("undo_stack", [])) == 1

    async def test_an_aborted_batch_is_undoable_up_to_the_raise(self):
        """An abort commits its prefix, so that prefix owes an undo entry.

        Reducers run inline, so the dispatches before the raise have already
        changed state. Pushing no snapshot left that change with nothing able
        to revert it, and an ``undo()`` would have reverted some earlier
        change instead.
        """
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await self._register_view(store, "bv_4", "batch_s4", user_id=4)
        store.state["application"]["val"] = "safe"

        async def set_val(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"]["val"]
            return new

        store._register_reducer("SET_VAL", set_val)

        with pytest.raises(RuntimeError):
            async with store.batch():
                await store.dispatch("SET_VAL", {"val": "changed"}, source_id="bv_4")
                raise RuntimeError("abort")

        assert store.state["application"]["val"] == "changed"
        view = store.state["views"]["bv_4"]
        assert len(view.get("undo_stack", [])) == 1

        await store.dispatch("UNDO", {"view_id": "bv_4", "session_id": "batch_s4"})
        assert store.state["application"]["val"] == "safe"

    async def test_enable_undo_false_receives_no_entry(self):
        """Views without enable_undo get no snapshot even when they dispatch inside a batch."""
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await store.dispatch("SESSION_CREATED", {"session_id": "batch_s5", "user_id": 5})
        # Deliberately NOT registering bv_5 in _undo_enabled_views.
        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "bv_5",
                "view_type": "Test",
                "user_id": 5,
                "session_id": "batch_s5",
            },
        )
        store.state["application"]["val"] = "start"

        async def set_val(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"]["val"]
            return new

        store._register_reducer("SET_VAL", set_val)

        async with store.batch():
            await store.dispatch("SET_VAL", {"val": "end"}, source_id="bv_5")

        view = store.state["views"]["bv_5"]
        assert view.get("undo_stack", []) == []

    async def test_mixed_dispatchers_each_get_one_entry(self):
        """Two undo-enabled views dispatching in one batch each receive exactly one snapshot."""
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await self._register_view(store, "bv_6a", "batch_s6", user_id=6)
        await self._register_view(store, "bv_6b", "batch_s6", user_id=6)

        store.state["application"]["a"] = 0
        store.state["application"]["b"] = 0

        async def bump_a(action, state):
            new = copy.deepcopy(state)
            new["application"]["a"] = state["application"].get("a", 0) + 1
            return new

        async def bump_b(action, state):
            new = copy.deepcopy(state)
            new["application"]["b"] = state["application"].get("b", 0) + 1
            return new

        store._register_reducer("BUMP_A", bump_a)
        store._register_reducer("BUMP_B", bump_b)

        async with store.batch():
            await store.dispatch("BUMP_A", {}, source_id="bv_6a")
            await store.dispatch("BUMP_A", {}, source_id="bv_6a")
            await store.dispatch("BUMP_B", {}, source_id="bv_6b")

        assert len(store.state["views"]["bv_6a"]["undo_stack"]) == 1
        assert len(store.state["views"]["bv_6b"]["undo_stack"]) == 1

    async def test_cross_session_shared_data_deepcopy(self):
        """Views in different sessions get independent shared_data snapshots."""
        store = get_store()
        _undo_mw = UndoMiddleware()
        store._add_middleware(_undo_mw)
        await _undo_mw.initialize(store)

        await self._register_view(store, "bv_7a", "sess_A", user_id=71)
        await self._register_view(store, "bv_7b", "sess_B", user_id=72)

        # Seed distinct shared_data per session.
        store.state["sessions"]["sess_A"]["shared_data"] = {"name": "alpha"}
        store.state["sessions"]["sess_B"]["shared_data"] = {"name": "beta"}

        async def touch(action, state):
            new = copy.deepcopy(state)
            new["application"]["touched"] = state["application"].get("touched", 0) + 1
            return new

        store._register_reducer("TOUCH", touch)

        async with store.batch():
            await store.dispatch("TOUCH", {}, source_id="bv_7a")
            await store.dispatch("TOUCH", {}, source_id="bv_7b")

        snap_a = store.state["views"]["bv_7a"]["undo_stack"][0]
        snap_b = store.state["views"]["bv_7b"]["undo_stack"][0]

        assert snap_a["shared_data"] == {"name": "alpha"}
        assert snap_b["shared_data"] == {"name": "beta"}

        # Deepcopy guarantee: mutating the live session's shared_data after
        # the batch must not alter the captured snapshot.
        store.state["sessions"]["sess_A"]["shared_data"]["name"] = "MUTATED"
        assert snap_a["shared_data"]["name"] == "alpha"


class TestUndoSlotIsolation:
    """Per-slot undo diffs -- a view's undo must not clobber sibling slots
    owned by other views.

    Undo snapshots store per-slot diffs keyed on the slot names an
    action actually touched. These tests pin the contract.
    """

    async def test_undo_does_not_clobber_sibling_slot_owned_by_another_view(self):
        """View A's undo restores A's slot only; B's slot is untouched."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

        await store.dispatch("SESSION_CREATED", {"session_id": "iso_s", "user_id": 1})
        store._undo_enabled_views["view_A"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "view_A", "view_type": "A", "user_id": 1, "session_id": "iso_s"},
        )
        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "view_B", "view_type": "B", "user_id": 1, "session_id": "iso_s"},
        )

        store.state["application"]["slot_a"] = {"val": "a_initial"}
        store.state["application"]["slot_b"] = {"val": "b_initial"}

        async def set_a(action, state):
            new = copy.deepcopy(state)
            new["application"]["slot_a"] = {"val": action["payload"]["val"]}
            return new

        async def set_b(action, state):
            new = copy.deepcopy(state)
            new["application"]["slot_b"] = {"val": action["payload"]["val"]}
            return new

        store._register_reducer("SET_A", set_a)
        store._register_reducer("SET_B", set_b)

        # View A changes slot_a (undo-enabled).
        await store.dispatch("SET_A", {"val": "a_new"}, source_id="view_A")
        assert store.state["application"]["slot_a"] == {"val": "a_new"}

        # View B changes slot_b concurrently (undo-disabled, no snapshot taken).
        await store.dispatch("SET_B", {"val": "b_new"}, source_id="view_B")
        assert store.state["application"]["slot_b"] == {"val": "b_new"}

        # View A undoes. slot_a should revert; slot_b must survive.
        await store.dispatch("UNDO", {"view_id": "view_A", "session_id": "iso_s"})
        assert store.state["application"]["slot_a"] == {"val": "a_initial"}
        assert store.state["application"]["slot_b"] == {"val": "b_new"}

    async def test_undo_deletes_slot_added_by_action(self):
        """Action that adds a new slot: undo removes the slot entirely."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

        await store.dispatch("SESSION_CREATED", {"session_id": "add_s", "user_id": 2})
        store._undo_enabled_views["view_add"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "view_add", "view_type": "A", "user_id": 2, "session_id": "add_s"},
        )

        # Pre-state has no 'new_slot'.
        assert "new_slot" not in store.state["application"]

        async def add_slot(action, state):
            new = copy.deepcopy(state)
            new["application"]["new_slot"] = {"fresh": True}
            return new

        store._register_reducer("ADD_SLOT", add_slot)

        await store.dispatch("ADD_SLOT", {}, source_id="view_add")
        assert store.state["application"]["new_slot"] == {"fresh": True}

        await store.dispatch("UNDO", {"view_id": "view_add", "session_id": "add_s"})
        # Slot must be gone (not just set to {}), so the post-state is
        # identical to pre-state.
        assert "new_slot" not in store.state["application"]

    async def test_undo_restores_slot_deleted_by_action(self):
        """Action that removes a slot: undo restores it."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

        await store.dispatch("SESSION_CREATED", {"session_id": "del_s", "user_id": 3})
        store._undo_enabled_views["view_del"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "view_del", "view_type": "A", "user_id": 3, "session_id": "del_s"},
        )

        store.state["application"]["doomed"] = {"keep": "me"}

        async def drop_slot(action, state):
            new = copy.deepcopy(state)
            new["application"].pop("doomed", None)
            return new

        store._register_reducer("DROP_SLOT", drop_slot)

        await store.dispatch("DROP_SLOT", {}, source_id="view_del")
        assert "doomed" not in store.state["application"]

        await store.dispatch("UNDO", {"view_id": "view_del", "session_id": "del_s"})
        assert store.state["application"]["doomed"] == {"keep": "me"}

    async def test_redo_reapplies_slot_addition(self):
        """Redo of an add-slot action re-adds the slot with the same value."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

        await store.dispatch("SESSION_CREATED", {"session_id": "redo_s", "user_id": 4})
        store._undo_enabled_views["view_redo"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "view_redo", "view_type": "A", "user_id": 4, "session_id": "redo_s"},
        )

        async def add(action, state):
            new = copy.deepcopy(state)
            new["application"]["rebirth"] = {"count": 1}
            return new

        store._register_reducer("ADD", add)

        await store.dispatch("ADD", {}, source_id="view_redo")
        assert store.state["application"]["rebirth"] == {"count": 1}

        await store.dispatch("UNDO", {"view_id": "view_redo", "session_id": "redo_s"})
        assert "rebirth" not in store.state["application"]

        await store.dispatch("REDO", {"view_id": "view_redo", "session_id": "redo_s"})
        assert store.state["application"]["rebirth"] == {"count": 1}

    async def test_redo_does_not_clobber_sibling_slot(self):
        """Redo restores only the touched slot; other views' writes survive."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

        await store.dispatch("SESSION_CREATED", {"session_id": "rdo_s", "user_id": 5})
        store._undo_enabled_views["v_rdo"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_rdo", "view_type": "A", "user_id": 5, "session_id": "rdo_s"},
        )
        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_other", "view_type": "B", "user_id": 5, "session_id": "rdo_s"},
        )

        store.state["application"]["a"] = {"v": 0}
        store.state["application"]["b"] = {"v": 0}

        async def bump_a(action, state):
            new = copy.deepcopy(state)
            new["application"]["a"] = {"v": state["application"].get("a", {}).get("v", 0) + 1}
            return new

        async def bump_b(action, state):
            new = copy.deepcopy(state)
            new["application"]["b"] = {"v": state["application"].get("b", {}).get("v", 0) + 1}
            return new

        store._register_reducer("BUMP_A", bump_a)
        store._register_reducer("BUMP_B", bump_b)

        await store.dispatch("BUMP_A", {}, source_id="v_rdo")  # a -> 1
        await store.dispatch("UNDO", {"view_id": "v_rdo", "session_id": "rdo_s"})  # a -> 0
        await store.dispatch("BUMP_B", {}, source_id="v_other")  # b -> 1, no undo snapshot

        # Now redo v_rdo's BUMP_A. Should restore a=1 without touching b.
        await store.dispatch("REDO", {"view_id": "v_rdo", "session_id": "rdo_s"})
        assert store.state["application"]["a"] == {"v": 1}
        assert store.state["application"]["b"] == {"v": 1}

    async def test_undo_redo_round_trip_preserves_identity_for_unchanged_slots(self):
        """Unchanged slots carry through UNDO and REDO as object references
        the diff never touches -- no spurious deepcopies."""
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

        await store.dispatch("SESSION_CREATED", {"session_id": "id_s", "user_id": 6})
        store._undo_enabled_views["v_id"] = 20

        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_id", "view_type": "A", "user_id": 6, "session_id": "id_s"},
        )

        sentinel_dict = {"deep": "untouched"}
        store.state["application"]["untouched"] = sentinel_dict
        store.state["application"]["target"] = {"v": 0}

        async def touch_target(action, state):
            new = copy.deepcopy(state)
            new["application"]["target"] = {"v": 1}
            return new

        store._register_reducer("TOUCH_TARGET", touch_target)

        await store.dispatch("TOUCH_TARGET", {}, source_id="v_id")
        await store.dispatch("UNDO", {"view_id": "v_id", "session_id": "id_s"})

        # The 'untouched' slot should not have been re-materialized -- the
        # UNDO reducer only touched the 'target' slot, so the identity of
        # 'untouched' carries through.
        assert store.state["application"]["untouched"] == {"deep": "untouched"}


class TestUndoIsPerKeyInsideDictSlots:
    """A dict slot is restored key by key, so one view's undo leaves other keys alone.

    Every user's scoped data lives in the one ``scoped`` slot. Restoring the
    whole slot on one user's undo erased every other user's scoped writes,
    in every guild and every feature, and a redo put back stale copies.
    """

    @staticmethod
    async def _install(store):
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

    @staticmethod
    async def _register_view(store, view_id, session_id, user_id):
        await store.dispatch("SESSION_CREATED", {"session_id": session_id, "user_id": user_id})
        store._undo_enabled_views[view_id] = 20
        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": view_id, "view_type": "Test", "user_id": user_id, "session_id": session_id},
        )

    @staticmethod
    def _board_setter(store):
        async def set_key(action, state):
            new = copy.deepcopy(state)
            new["application"].setdefault("board", {})[action["payload"]["key"]] = action[
                "payload"
            ]["value"]
            return new

        store._register_reducer("PK_SET", set_key)

    async def test_one_users_undo_leaves_another_users_scoped_state(self):
        from helpers import RenderableLayoutView, make_interaction

        class Settings(RenderableLayoutView):
            enable_undo = True
            state_scope = "user"

        store = get_store()
        await self._install(store)
        a = Settings(interaction=make_interaction(user_id=100, guild_id=200))
        b = Settings(interaction=make_interaction(user_id=101, guild_id=201))
        await a.send()
        await b.send()

        await a.dispatch_scoped({"dm": False})
        await b.dispatch_scoped({"theme": "light"})
        await a.undo()

        assert store.get_scoped("user", user_id=100) == {}
        assert store.get_scoped("user", user_id=101) == {"theme": "light"}

        await b.dispatch_scoped({"theme": "dark"})
        await a.redo()

        assert store.get_scoped("user", user_id=100) == {"dm": False}
        assert store.get_scoped("user", user_id=101) == {
            "theme": "dark"
        }, "a redo must not put back a stale copy of another user's key"

    async def test_undo_restores_a_removed_key_and_keeps_another_writers_key(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_a", "pk_sa", user_id=1)
        self._board_setter(store)
        store.state["application"]["board"] = {"a": 1, "b": 2}

        async def drop_a(action, state):
            new = copy.deepcopy(state)
            del new["application"]["board"]["a"]
            return new

        store._register_reducer("PK_DROP", drop_a)

        await store.dispatch("PK_DROP", {}, source_id="pk_a")
        await store.dispatch("PK_SET", {"key": "c", "value": 3})
        await store.dispatch("UNDO", {"view_id": "pk_a", "session_id": "pk_sa"})

        assert store.state["application"]["board"] == {"a": 1, "b": 2, "c": 3}

    async def test_undoing_a_slots_creation_keeps_it_while_another_writer_holds_a_key(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_b", "pk_sb", user_id=1)
        self._board_setter(store)

        await store.dispatch("PK_SET", {"key": "mine", "value": 1}, source_id="pk_b")
        await store.dispatch("PK_SET", {"key": "theirs", "value": 1})
        await store.dispatch("UNDO", {"view_id": "pk_b", "session_id": "pk_sb"})

        assert store.state["application"]["board"] == {"theirs": 1}

    async def test_a_batch_restores_each_key_to_its_value_before_the_batch(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_c", "pk_sc", user_id=1)
        self._board_setter(store)
        store.state["application"]["board"] = {"k1": 0}

        async with store.batch(source_id="pk_c"):
            await store.dispatch("PK_SET", {"key": "k1", "value": 1}, source_id="pk_c")
            await store.dispatch("PK_SET", {"key": "k1", "value": 2}, source_id="pk_c")
            await store.dispatch("PK_SET", {"key": "k2", "value": 9}, source_id="pk_c")
        await store.dispatch("PK_SET", {"key": "k3", "value": 7})
        await store.dispatch("UNDO", {"view_id": "pk_c", "session_id": "pk_sc"})

        assert store.state["application"]["board"] == {"k1": 0, "k3": 7}

    async def test_a_key_a_batch_wrote_and_put_back_is_left_to_later_writers(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_d", "pk_sd", user_id=1)
        self._board_setter(store)
        store.state["application"]["board"] = {"k1": 0}

        async with store.batch(source_id="pk_d"):
            await store.dispatch("PK_SET", {"key": "k1", "value": 5}, source_id="pk_d")
            await store.dispatch("PK_SET", {"key": "k1", "value": 0}, source_id="pk_d")
            await store.dispatch("PK_SET", {"key": "k2", "value": 1}, source_id="pk_d")
        await store.dispatch("PK_SET", {"key": "k1", "value": 8})
        await store.dispatch("UNDO", {"view_id": "pk_d", "session_id": "pk_sd"})

        assert store.state["application"]["board"] == {"k1": 8}

    async def test_a_batch_that_replaces_a_dict_slot_restores_the_dict(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_e", "pk_se", user_id=1)
        self._board_setter(store)
        # k2 is a key the batch never touched: only the whole-slot fold keeps it.
        store.state["application"]["board"] = {"k1": 0, "k2": 5}

        async def replace(action, state):
            new = copy.deepcopy(state)
            new["application"]["board"] = 5
            return new

        store._register_reducer("PK_REPLACE", replace)

        async with store.batch(source_id="pk_e"):
            await store.dispatch("PK_SET", {"key": "k1", "value": 1}, source_id="pk_e")
            await store.dispatch("PK_REPLACE", {}, source_id="pk_e")
        await store.dispatch("UNDO", {"view_id": "pk_e", "session_id": "pk_se"})

        assert store.state["application"]["board"] == {"k1": 0, "k2": 5}

    async def test_a_batch_that_created_then_replaced_a_slot_undoes_to_absent(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_i", "pk_si", user_id=1)
        self._board_setter(store)

        async def drop(action, state):
            new = copy.deepcopy(state)
            del new["application"]["board"][action["payload"]["key"]]
            return new

        async def replace(action, state):
            new = copy.deepcopy(state)
            new["application"]["board"] = 5
            return new

        store._register_reducer("PK_DEL", drop)
        store._register_reducer("PK_REPLACE", replace)

        async with store.batch(source_id="pk_i"):
            await store.dispatch("PK_SET", {"key": "tmp", "value": 1}, source_id="pk_i")
            await store.dispatch("PK_DEL", {"key": "tmp"}, source_id="pk_i")
            await store.dispatch("PK_REPLACE", {}, source_id="pk_i")
        await store.dispatch("UNDO", {"view_id": "pk_i", "session_id": "pk_si"})

        assert "board" not in store.state["application"]

    async def test_a_key_a_batch_added_and_removed_is_left_to_later_writers(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_j", "pk_sj", user_id=1)
        self._board_setter(store)
        store.state["application"]["board"] = {"k1": 0}

        async def drop(action, state):
            new = copy.deepcopy(state)
            del new["application"]["board"][action["payload"]["key"]]
            return new

        store._register_reducer("PK_DEL", drop)

        async with store.batch(source_id="pk_j"):
            await store.dispatch("PK_SET", {"key": "tmp", "value": 1}, source_id="pk_j")
            await store.dispatch("PK_DEL", {"key": "tmp"}, source_id="pk_j")
            await store.dispatch("PK_SET", {"key": "k2", "value": 1}, source_id="pk_j")
        await store.dispatch("PK_SET", {"key": "tmp", "value": 2})
        await store.dispatch("UNDO", {"view_id": "pk_j", "session_id": "pk_sj"})

        assert store.state["application"]["board"] == {"k1": 0, "tmp": 2}

    async def test_undo_redo_undo_removes_a_created_slot_each_time(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_f", "pk_sf", user_id=1)
        self._board_setter(store)

        await store.dispatch("PK_SET", {"key": "mine", "value": 1}, source_id="pk_f")
        await store.dispatch("UNDO", {"view_id": "pk_f", "session_id": "pk_sf"})
        assert "board" not in store.state["application"]
        await store.dispatch("REDO", {"view_id": "pk_f", "session_id": "pk_sf"})
        assert store.state["application"]["board"] == {"mine": 1}
        await store.dispatch("UNDO", {"view_id": "pk_f", "session_id": "pk_sf"})
        assert "board" not in store.state["application"]

    async def test_undoing_the_creation_of_an_empty_slot_removes_it(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_g", "pk_sg", user_id=1)

        async def vivify(action, state):
            new = copy.deepcopy(state)
            new["application"].setdefault("empty", {})
            return new

        store._register_reducer("PK_VIVIFY", vivify)

        await store.dispatch("PK_VIVIFY", {}, source_id="pk_g")
        await store.dispatch("UNDO", {"view_id": "pk_g", "session_id": "pk_sg"})

        assert "empty" not in store.state["application"]

    async def test_a_batch_that_created_a_slot_and_emptied_it_undoes_to_absent(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_h", "pk_sh", user_id=1)
        self._board_setter(store)

        async def drop(action, state):
            new = copy.deepcopy(state)
            del new["application"]["board"][action["payload"]["key"]]
            return new

        store._register_reducer("PK_DEL", drop)

        async with store.batch(source_id="pk_h"):
            await store.dispatch("PK_SET", {"key": "tmp", "value": 1}, source_id="pk_h")
            await store.dispatch("PK_DEL", {"key": "tmp"}, source_id="pk_h")
        assert store.state["application"]["board"] == {}
        await store.dispatch("UNDO", {"view_id": "pk_h", "session_id": "pk_sh"})

        assert "board" not in store.state["application"]

    async def test_undo_in_a_shared_guild_bucket_keeps_another_users_keys(self):
        """Keys are restored at every depth, not only the slot's own: a guild
        bucket is one key of the scoped slot and every member writes into it."""
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_ga", "pk_sga", user_id=1)
        await self._register_view(store, "pk_gb", "pk_sgb", user_id=2)

        async def write(view_id, data):
            payload = {"scope": "guild", "identifiers": {"guild_id": 9}, "data": data}
            await store.dispatch("SCOPED_UPDATE", payload, source_id=view_id)

        await write("pk_gb", {"lang": "en"})
        await write("pk_ga", {"theme": "dark"})
        await write("pk_gb", {"lang": "fr"})
        await store.dispatch("UNDO", {"view_id": "pk_ga", "session_id": "pk_sga"})

        assert store.get_scoped("guild", guild_id=9) == {"lang": "fr"}

        await write("pk_gb", {"lang": "de"})
        await store.dispatch("REDO", {"view_id": "pk_ga", "session_id": "pk_sga"})

        assert store.get_scoped("guild", guild_id=9) == {"lang": "de", "theme": "dark"}

    async def test_a_batch_that_created_and_deleted_a_slot_pushes_nothing(self):
        """Its undo would otherwise erase what another view wrote there later."""
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_ca", "pk_sca", user_id=1)
        await self._register_view(store, "pk_cb", "pk_scb", user_id=2)

        async def make(action, state):
            new = copy.deepcopy(state)
            new["application"]["scratch"] = {"k": action["payload"]}
            return new

        async def remove(action, state):
            new = copy.deepcopy(state)
            new["application"].pop("scratch", None)
            return new

        store._register_reducer("PK_MAKE", make)
        store._register_reducer("PK_REMOVE", remove)
        async with store.batch(source_id="pk_ca"):
            await store.dispatch("PK_MAKE", 1, source_id="pk_ca")
            await store.dispatch("PK_REMOVE", None, source_id="pk_ca")

        assert store.state["views"]["pk_ca"].get("undo_stack", []) == []
        await store.dispatch("PK_MAKE", "B", source_id="pk_cb")
        await store.dispatch("UNDO", {"view_id": "pk_ca", "session_id": "pk_sca"})

        assert store.state["application"]["scratch"] == {"k": "B"}

    async def test_undo_keeps_a_value_another_writer_replaced_the_dict_with(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_na", "pk_sna", user_id=1)
        await self._register_view(store, "pk_nb", "pk_snb", user_id=2)
        self._board_setter(store)

        async def replace_board(action, state):
            new = copy.deepcopy(state)
            new["application"]["board"] = 5
            return new

        store._register_reducer("PK_REPLACE", replace_board)
        await store.dispatch("PK_SET", {"key": "k", "value": 1}, source_id="pk_na")
        await store.dispatch("PK_REPLACE", None, source_id="pk_nb")
        await store.dispatch("UNDO", {"view_id": "pk_na", "session_id": "pk_sna"})

        # The undo ran, rather than raising in the reducer and leaving state as it was.
        assert len(store.state["views"]["pk_na"]["redo_stack"]) == 1
        assert store.state["application"]["board"] == 5
        await store.dispatch("REDO", {"view_id": "pk_na", "session_id": "pk_sna"})
        assert store.state["application"]["board"] == 5

    async def test_a_change_of_type_alone_is_undone(self):
        """``1`` and ``True`` compare equal, and are still different values."""
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_ta", "pk_sta", user_id=1)
        self._board_setter(store)
        await store.dispatch("PK_SET", {"key": "count", "value": 1}, source_id="pk_ta")
        await store.dispatch("PK_SET", {"key": "other", "value": 1}, source_id="pk_ta")

        async def retype(action, state):
            new = copy.deepcopy(state)
            new["application"]["board"]["count"] = True
            new["application"]["board"]["other"] = 2
            return new

        store._register_reducer("PK_RETYPE", retype)
        await store.dispatch("PK_RETYPE", None, source_id="pk_ta")
        await store.dispatch("UNDO", {"view_id": "pk_ta", "session_id": "pk_sta"})

        assert type(store.state["application"]["board"]["count"]) is int

    async def test_a_change_of_type_inside_a_list_is_undone(self):
        """A list compares equal across ``1`` and ``True``, so only its items
        tell the change apart."""
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_tl", "pk_stl", user_id=1)
        store.state["application"]["counts"] = [1, 2]

        async def retype(action, state):
            return {**state, "application": {**state["application"], "counts": [True, 2]}}

        store._register_reducer("PK_RETYPE_LIST", retype)
        await store.dispatch("PK_RETYPE_LIST", None, source_id="pk_tl")
        await store.dispatch("UNDO", {"view_id": "pk_tl", "session_id": "pk_stl"})

        assert type(store.state["application"]["counts"][0]) is int

    async def test_a_change_of_type_in_a_dict_inside_a_list_is_undone(self):
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_td", "pk_std", user_id=1)
        store.state["application"]["rows"] = [{"n": 1}]

        async def retype(action, state):
            return {**state, "application": {**state["application"], "rows": [{"n": True}]}}

        store._register_reducer("PK_RETYPE_ROWS", retype)
        await store.dispatch("PK_RETYPE_ROWS", None, source_id="pk_td")
        await store.dispatch("UNDO", {"view_id": "pk_td", "session_id": "pk_std"})

        assert type(store.state["application"]["rows"][0]["n"]) is int

    async def test_an_equal_value_written_again_leaves_no_step(self):
        """A rebuilt list equal to the one it replaced was recorded as a step
        whose entry was None, and undoing it wrote None over the slot."""
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_eq", "pk_seq", user_id=1)
        store.state["application"]["tags"] = ["a", "b"]

        async def rebuild(action, state):
            application = state["application"]
            return {**state, "application": {**application, "tags": list(application["tags"])}}

        store._register_reducer("PK_REBUILD", rebuild)
        await store.dispatch("PK_REBUILD", None, source_id="pk_eq")

        assert store.state["views"]["pk_eq"].get("undo_stack", []) == []

    @staticmethod
    def _nested_setters(store):
        async def set_x(action, state):
            new = copy.deepcopy(state)
            new["application"]["board"]["a"]["x"] = action["payload"]
            return new

        async def replace(action, state):
            new = copy.deepcopy(state)
            new["application"]["board"] = action["payload"]
            return new

        store._register_reducer("PK_SET_X", set_x)
        store._register_reducer("PK_REPLACE_WITH", replace)

    async def test_a_batch_that_replaces_a_dict_slot_restores_its_nested_keys(self):
        """The fold put back only the slot's own keys, so a nested key the batch
        changed before replacing the slot came back with the batch's value."""
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_nf", "pk_snf", user_id=1)
        self._nested_setters(store)
        store.state["application"]["board"] = {"a": {"x": 0, "y": 5}, "b": 1}

        async with store.batch(source_id="pk_nf"):
            await store.dispatch("PK_SET_X", 1, source_id="pk_nf")
            await store.dispatch("PK_REPLACE_WITH", 5, source_id="pk_nf")
        await store.dispatch("UNDO", {"view_id": "pk_nf", "session_id": "pk_snf"})

        assert store.state["application"]["board"] == {"a": {"x": 0, "y": 5}, "b": 1}

    async def test_a_batch_that_adds_a_nested_dict_then_replaces_the_slot_undoes_without_it(
        self,
    ):
        """A nested dict the batch created came back as an undo marker instead
        of being removed."""
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_nc", "pk_snc", user_id=1)
        self._board_setter(store)
        self._nested_setters(store)
        store.state["application"]["board"] = {"b": 1}

        async with store.batch(source_id="pk_nc"):
            await store.dispatch("PK_SET", {"key": "a", "value": {"x": 1}}, source_id="pk_nc")
            await store.dispatch("PK_REPLACE_WITH", 5, source_id="pk_nc")
        await store.dispatch("UNDO", {"view_id": "pk_nc", "session_id": "pk_snc"})

        assert store.state["application"]["board"] == {"b": 1}

    @staticmethod
    async def _batch_around(store, view_id, first, between, last):
        """Run a batch of ``first`` then ``last`` while another task dispatches ``between``."""
        midway, written = asyncio.Event(), asyncio.Event()

        async def elsewhere():
            await midway.wait()
            await store.dispatch(*between)
            written.set()

        async def batched():
            async with store.batch(source_id=view_id):
                await store.dispatch(*first, source_id=view_id)
                midway.set()
                await written.wait()
                await store.dispatch(*last, source_id=view_id)

        # Created before the batch opens, so its dispatch does not join it.
        other = asyncio.ensure_future(elsewhere())
        await asyncio.wait_for(asyncio.gather(other, batched()), 5)

    async def test_a_batch_whose_slot_another_writer_replaced_keeps_its_step(self):
        """Another task replaced the dict slot with a plain value between the
        batch's writes. Folding the batch's per-key entries into that value
        raised, and the batch was left with no undo step."""
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_mw", "pk_smw", user_id=1)
        self._board_setter(store)
        self._nested_setters(store)
        store.state["application"]["board"] = {"k": 0}

        await self._batch_around(
            store,
            "pk_mw",
            ("PK_SET", {"key": "k", "value": 1}),
            ("PK_REPLACE_WITH", 5),
            ("PK_REPLACE_WITH", 6),
        )

        assert len(store.state["views"]["pk_mw"]["undo_stack"]) == 1

    async def test_a_batch_whose_nested_dict_another_writer_replaced_keeps_its_step(self):
        """The same, one level down: the other writer's plain value stays and
        the step is kept."""
        store = get_store()
        await self._install(store)
        await self._register_view(store, "pk_mn", "pk_smn", user_id=1)
        self._board_setter(store)
        self._nested_setters(store)
        store.state["application"]["board"] = {"a": {"x": 0}, "b": 1}

        await self._batch_around(
            store,
            "pk_mn",
            ("PK_SET_X", 1),
            ("PK_SET", {"key": "a", "value": 7}),
            ("PK_REPLACE_WITH", 5),
        )

        assert len(store.state["views"]["pk_mn"]["undo_stack"]) == 1
        await store.dispatch("UNDO", {"view_id": "pk_mn", "session_id": "pk_smn"})
        assert store.state["application"]["board"] == {"a": 7, "b": 1}


class TestMissingSentinelDeepCopy:
    """``_MISSING`` must survive the reducer copy without losing identity.

    ``@cascade_reducer`` deep-copies state before every reducer. State
    contains undo-stack diffs that may carry ``_MISSING`` sentinels
    (marking "this slot did not exist pre-action"). If the copy creates
    fresh ``object()`` instances in place of ``_MISSING``, the identity
    check ``target_value is _MISSING`` in ``_apply_slot_diff`` fails and
    the bare ``object()`` lands in the slot value, corrupting state for
    every subsequent reducer that reads the slot.

    ``copy.deepcopy`` is the mechanism the sentinel defends against
    directly, via ``__deepcopy__`` returning self. The reducer boundary
    reaches it indirectly: ``_copy_state`` walks dicts and lists itself
    and delegates everything else, so the sentinel rides the delegation.
    That indirection is why the last test here drives a real dispatch
    rather than the copy helper -- a fast path added to ``_copy_state``
    would leave the direct ``copy.deepcopy`` tests green.
    """

    def test_deepcopy_preserves_identity(self):
        from cascadeui.state.middleware.undo import _MISSING

        copied = copy.deepcopy(_MISSING)
        assert copied is _MISSING

    def test_copy_preserves_identity(self):
        from cascadeui.state.middleware.undo import _MISSING

        copied = copy.copy(_MISSING)
        assert copied is _MISSING

    def test_deepcopy_inside_nested_dict_preserves_identity(self):
        from cascadeui.state.middleware.undo import _MISSING

        snapshot = {
            "application_slots": {"scoped": _MISSING, "settings": {"k": 1}},
            "shared_data": {"x": "y"},
        }
        copied = copy.deepcopy(snapshot)
        assert copied["application_slots"]["scoped"] is _MISSING
        assert copied["application_slots"]["settings"] == {"k": 1}
        assert (
            copied["application_slots"]["settings"] is not snapshot["application_slots"]["settings"]
        )

    async def test_the_sentinel_survives_a_custom_reducer_dispatch(self):
        """A second custom-reducer dispatch copies a state holding ``_MISSING``.

        The first dispatch adds a slot, so the undo diff records the
        sentinel. The second routes that whole state through the reducer
        copy. If the sentinel loses identity there, UNDO stores the bare
        object AS the slot value instead of popping the slot.
        """
        from cascadeui.state.middleware.undo import _MISSING, _MissingSentinel
        from cascadeui.utils.decorators import cascade_reducer

        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

        @cascade_reducer("SENTINEL_PROBE_ADD")
        async def _add(action, state):
            state["application"].setdefault("prefs", {})["theme"] = "dark"
            return state

        @cascade_reducer("SENTINEL_PROBE_OTHER")
        async def _other(action, state):
            state["application"]["counter"] = 1
            return state

        await store.dispatch("SESSION_CREATED", {"session_id": "sent_s", "user_id": 7})
        await store.dispatch(
            "VIEW_CREATED",
            {
                "view_id": "sent_v",
                "view_type": "Test",
                "user_id": 7,
                "session_id": "sent_s",
            },
        )
        store._undo_enabled_views["sent_v"] = 20

        await store.dispatch("SENTINEL_PROBE_ADD", {}, source_id="sent_v")
        diff = store.state["views"]["sent_v"]["undo_stack"][-1]["application_slots"]
        assert diff["prefs"]["theme"] is _MISSING, "the add must record the real sentinel"

        await store.dispatch("SENTINEL_PROBE_OTHER", {}, source_id="sent_v")
        carried = store.state["views"]["sent_v"]["undo_stack"][0]["application_slots"]
        assert (
            carried["prefs"]["theme"] is _MISSING
        ), "the reducer copy must preserve sentinel identity"
        assert carried["prefs"].created, "the reducer copy must keep the slot's created flag"

        await store.dispatch("UNDO", {"view_id": "sent_v", "session_id": "sent_s"})
        await store.dispatch("UNDO", {"view_id": "sent_v", "session_id": "sent_s"})

        application = store.state["application"]
        assert not isinstance(
            application.get("prefs"), _MissingSentinel
        ), "a sentinel landed in the slot instead of popping it"
        assert "prefs" not in application

    async def test_undo_followed_by_dispatch_does_not_corrupt_application_slot(self):
        """End-to-end regression for the live-bot SETTINGS_UPDATED crash.

        Before the fix: dispatch SCOPED_UPDATE -> dispatch UNDO ->
        dispatch SCOPED_UPDATE again. The second dispatch would crash
        because ``state["application"]["scoped"]`` had been replaced
        with a deepcopy of the ``_MISSING`` sentinel during the
        intermediate reducer wrappers.
        """
        from cascadeui.state.store import StateStore
        from cascadeui.utils.decorators import cascade_reducer

        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

        await store.dispatch("SESSION_CREATED", {"session_id": "regr_s", "user_id": 7})
        store._undo_enabled_views["regr_v"] = 20

        # First write -- creates the "scoped" slot.
        await store.dispatch(
            "SCOPED_UPDATE",
            {
                "scope": "user",
                "identifiers": {"user_id": 7},
                "data": {"theme": "dark"},
                "slot_name": "scoped",
            },
            source_id="regr_v",
        )

        # Undo -- should pop the "scoped" slot since it didn't exist
        # pre-action. The slot should be GONE from state, not replaced
        # with a bare object().
        await store.dispatch("UNDO", {"view_id": "regr_v", "session_id": "regr_s"})

        scoped_after_undo = store.state.get("application", {}).get("scoped")
        # The slot was popped (not present) OR is a real dict (re-created
        # by a clean reducer call). It must NEVER be a bare object that
        # fails iteration.
        assert scoped_after_undo is None or isinstance(
            scoped_after_undo, dict
        ), f"slot corruption: state['application']['scoped'] is {type(scoped_after_undo).__name__}"

        # Subsequent SCOPED_UPDATE must succeed (this is what crashed
        # in the live bot at v2_settings.py:75).
        await store.dispatch(
            "SCOPED_UPDATE",
            {
                "scope": "user",
                "identifiers": {"user_id": 7},
                "data": {"theme": "light"},
                "slot_name": "scoped",
            },
            source_id="regr_v",
        )

        result = StateStore.get_scoped_from(store.state, "user", user_id=7)
        assert result == {"theme": "light"}


class TestUndoDiffSkipsUntouchedSlots:
    """An untouched slot is the same object on both sides, not merely equal.

    Reducers shallow-spread, so a slot the action did not write is identical
    by reference. Dict equality has no identity shortcut, so comparing it
    walked the whole slot to prove what ``is`` already answered -- making
    every undo-tracked action cost O(all application data).
    """

    class _CountingDict(dict):
        """Records equality comparisons so the skip can be asserted directly."""

        compares = 0

        def __eq__(self, other):
            type(self).compares += 1
            return super().__eq__(other)

        def __ne__(self, other):
            type(self).compares += 1
            return super().__ne__(other)

        __hash__ = None

    def test_an_identical_sibling_is_never_compared(self):
        sibling = self._CountingDict({"a": 1})
        type(sibling).compares = 0
        pre = {"scoped": {"x": 1}, "sibling": sibling}
        post = {"scoped": {"x": 2}, "sibling": sibling}

        diff = _diff_application_slots(pre, post)

        assert type(sibling).compares == 0, "an identical slot must not be compared at all"
        assert "sibling" not in diff
        assert "scoped" in diff

    def test_a_changed_slot_is_still_diffed(self):
        pre = {"scoped": {"x": 1}}
        post = {"scoped": {"x": 2}}

        diff = _diff_application_slots(pre, post)

        assert diff["scoped"] == {"x": 1}


class TestUndoSkipsSnapshotsThatRestoreNothing:
    """An entry that can put nothing back must not consume a stack slot.

    ``undo_limit`` bounds the stack, so entries recording no change evict the
    ones a user actually wants to reach. A view dispatching actions that write
    no application slot (a selection change, a view-local toggle, a
    re-render) cleared its own history without ever touching it.

    The guard reads whether ``shared_data`` *changed*, not whether it holds
    anything. Those come apart on the case that matters: an action creating a
    session's first shared_data records a pre-value of ``{}``, which is falsy
    and is exactly what an UNDO has to put back.
    """

    async def _setup(self, limit=3):
        store = get_store()
        store.state = store._build_initial_state()
        store._middleware = []
        mw = UndoMiddleware()
        store._add_middleware(mw)
        await mw.initialize(store)
        await store.dispatch("SESSION_CREATED", {"session_id": "s", "user_id": 1})
        store._undo_enabled_views["v1"] = limit
        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v1", "view_type": "T", "user_id": 1, "session_id": "s"},
        )
        store.state["application"]["real"] = {"n": 0}
        return store

    def _stack(self, store):
        return store.state["views"]["v1"].get("undo_stack", [])

    async def test_actions_writing_no_slot_do_not_evict_real_history(self):
        from cascadeui.utils import cascade_reducer

        store = await self._setup(limit=3)

        @cascade_reducer("EVICT_REAL_WRITE")
        def _real(action, state):
            state["application"]["real"]["n"] += 1
            return state

        @cascade_reducer("EVICT_NOOP")
        def _noop(action, state):
            return state

        await store.dispatch("EVICT_REAL_WRITE", {}, source_id="v1")
        for _ in range(4):
            await store.dispatch("EVICT_NOOP", {}, source_id="v1")

        assert len(self._stack(store)) == 1
        assert self._stack(store)[0]["application_slots"], "the real write must still be reachable"

    async def test_a_session_gaining_its_first_shared_data_is_still_recorded(self):
        store = await self._setup()

        await store.dispatch(
            "SESSION_UPDATED", {"session_id": "s", "shared_data": {"x": 1}}, source_id="v1"
        )

        stack = self._stack(store)
        assert len(stack) == 1, "an empty pre-value is the restore target, not an absent one"
        assert stack[0]["shared_data"] == {}

    async def test_a_normal_slot_write_is_unaffected(self):
        from cascadeui.utils import cascade_reducer

        store = await self._setup()

        @cascade_reducer("UNAFFECTED_WRITE")
        def _real(action, state):
            state["application"]["real"]["n"] += 1
            return state

        await store.dispatch("UNAFFECTED_WRITE", {}, source_id="v1")

        assert len(self._stack(store)) == 1

    async def test_a_batch_that_writes_and_reverts_pushes_nothing(self):
        from cascadeui.utils import cascade_reducer

        store = await self._setup()

        @cascade_reducer("BATCH_SET")
        def _set(action, state):
            state["application"]["real"]["n"] = action["payload"]["n"]
            return state

        async with store.batch(source_id="v1"):
            await store.dispatch("BATCH_SET", {"n": 9}, source_id="v1")
            await store.dispatch("BATCH_SET", {"n": 0}, source_id="v1")

        assert self._stack(store) == [], "the batch left the slot as it found it"

    async def test_a_batch_that_changes_something_still_pushes(self):
        from cascadeui.utils import cascade_reducer

        store = await self._setup()

        @cascade_reducer("BATCH_KEEP")
        def _set(action, state):
            state["application"]["real"]["n"] = action["payload"]["n"]
            return state

        async with store.batch(source_id="v1"):
            await store.dispatch("BATCH_KEEP", {"n": 9}, source_id="v1")

        assert len(self._stack(store)) == 1


class TestUndoRecordsOnlyItsOwnAction:
    """A middleware installed after UndoMiddleware may await before passing the
    action on. Undo diffed the state it was handed against the result, so a
    write another dispatch committed during that wait landed in this view's
    entry, and its undo erased the other user's key."""

    @staticmethod
    async def _setup(store, *later_middleware):
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        for middleware in later_middleware:
            store._add_middleware(middleware)
        store._undo_enabled_views["a"] = 20

        async def set_key(action, state):
            new = copy.deepcopy(state)
            board = new["application"].setdefault("board", {})
            board[action["payload"]["key"]] = 1
            return new

        store._register_reducer("SET_KEY", set_key)
        await store.dispatch("VIEW_CREATED", {"view_id": "a", "view_type": "Test"})

    @staticmethod
    def _stack(store):
        return store.state["views"]["a"].get("undo_stack", [])

    async def test_a_write_committed_during_a_later_middlewares_wait_is_not_undone(self):
        store = get_store()
        release = asyncio.Event()

        async def audit(action, state, next_fn):
            if action.get("source") == "a":
                await release.wait()
            return await next_fn(action, state)

        await self._setup(store, audit)
        first = asyncio.ensure_future(store.dispatch("SET_KEY", {"key": "1"}, source_id="a"))
        try:
            await asyncio.sleep(0)
            await store.dispatch("SET_KEY", {"key": "2"}, source_id="b")
        finally:
            release.set()
            await first
        assert set(self._stack(store)[-1]["application_slots"]["board"]) == {"1"}

        await store.dispatch("UNDO", {"view_id": "a"}, source_id="a")
        assert store.state["application"]["board"] == {"2": 1}

    async def test_an_entry_survives_a_later_middleware_awaiting_after_the_reducer(self):
        # The entry was written onto the state that middleware handed back,
        # which another dispatch had replaced while it awaited, so it was lost
        # and the undo found nothing to restore.
        store = get_store()
        release = asyncio.Event()

        async def audit(action, state, next_fn):
            result = await next_fn(action, state)
            if action.get("source") == "a":
                await release.wait()
            return result

        await self._setup(store, audit)
        first = asyncio.ensure_future(store.dispatch("SET_KEY", {"key": "1"}, source_id="a"))
        try:
            await asyncio.sleep(0)
            await store.dispatch("SET_KEY", {"key": "2"}, source_id="b")
        finally:
            release.set()
            await first
        assert len(self._stack(store)) == 1

        await store.dispatch("UNDO", {"view_id": "a"}, source_id="a")
        assert store.state["application"]["board"] == {"2": 1}

    async def test_an_action_a_later_middleware_stops_pushes_no_entry(self):
        # With a write from another dispatch landing while the action waited,
        # an entry holding only that write was pushed for an action no
        # reducer ran.
        store = get_store()
        release = asyncio.Event()

        async def audit(action, state, next_fn):
            if action.get("source") == "a":
                await release.wait()
            return await next_fn(action, state)

        async def block(action, state, next_fn):
            if action.get("source") == "a":
                return state
            return await next_fn(action, state)

        await self._setup(store, audit, block)
        first = asyncio.ensure_future(store.dispatch("SET_KEY", {"key": "1"}, source_id="a"))
        try:
            await asyncio.sleep(0)
            await store.dispatch("SET_KEY", {"key": "2"}, source_id="b")
        finally:
            release.set()
            await first
        assert self._stack(store) == []


class TestUndoRecordsAtTheCommit:
    """Undo recorded an action's entry when the chain returned to it. With a
    middleware around it that awaits or dispatches after ``next_fn``, entries
    landed out of the order the store committed them, a batch could lose its
    entry, and an edit a middleware returned but never committed was recorded."""

    @staticmethod
    async def _setup(store, *, above=(), below=()):
        for middleware in above:
            store._add_middleware(middleware)
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)
        for middleware in below:
            store._add_middleware(middleware)

        async def set_k(action, state):
            app = state.get("application", {})
            slot = {**app.get("slot", {}), "k": action["payload"]["v"]}
            return {**state, "application": {**app, "slot": slot}}

        async def inc(action, state):
            app = state.get("application", {})
            return {**state, "application": {**app, "c": {"n": app.get("c", {}).get("n", 0) + 1}}}

        store._register_reducer("SET", set_k)
        store._register_reducer("INC", inc)
        await store.dispatch("SESSION_CREATED", {"session_id": "s"})
        await store.dispatch("VIEW_CREATED", {"view_id": "a", "session_id": "s"})
        store._undo_enabled_views["a"] = 20

    @staticmethod
    async def _undo(store):
        await store.dispatch("UNDO", {"view_id": "a", "session_id": "s"}, source_id="a")

    @staticmethod
    def _k(store):
        return store.state["application"].get("slot", {}).get("k", "absent")

    @staticmethod
    def _saga(store):
        async def saga(action, state, next_fn):
            result = await next_fn(action, state)
            if action["type"] == "SET" and action["payload"]["v"] == 1:
                await store.dispatch("SET", {"v": 2}, source_id="a")
            return result

        return saga

    async def test_a_follow_up_dispatched_after_the_reducer_undoes_in_order(self):
        # The first undo took back both writes and the second put the
        # follow-up's back.
        store = get_store()
        await self._setup(store, below=[self._saga(store)])
        await store.dispatch("SET", {"v": 1}, source_id="a")
        await self._undo(store)
        assert self._k(store) == 1
        await self._undo(store)
        assert self._k(store) == "absent"

    async def test_a_batch_ending_while_a_reducer_awaits_keeps_its_undo_entry(self):
        # The batch's entry was written while a reducer in another task
        # awaited, and that reducer's result, read before, replaced it.
        # Queued behind that reducer instead, the entry landed after the batch
        # had notified, so a view showing its undo depth read an empty stack.
        store = get_store()
        await self._setup(store)
        started, release, go = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow(action, state):
            started.set()
            await release.wait()
            return {**state, "application": {**state.get("application", {}), "other": 1}}

        store._register_reducer("SLOW", slow)
        depth_when_told = []

        async def told(state, action=None):
            depth_when_told.append(len(store.state["views"]["a"].get("undo_stack", [])))

        # As the acting view, told inline with the batch's notification.
        store.subscribe("a", told, action_filter={"BATCH_COMPLETE"})

        async def elsewhere():
            await go.wait()
            await store.dispatch("SLOW", {})

        async def batched():
            async with store.batch(source_id="a"):
                await store.dispatch("SET", {"v": 1}, source_id="a")
                go.set()
                await asyncio.wait_for(started.wait(), 2)

        # Spawned before the batch opens, so it does not join it.
        holder = asyncio.ensure_future(elsewhere())
        batch = asyncio.ensure_future(batched())
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.sleep(0.01)
            assert not batch.done()  # its undo entry waits for that reducer
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(holder, batch), 2)
        assert depth_when_told == [1]
        assert store.state["application"]["other"] == 1
        await self._undo(store)
        assert self._k(store) == "absent"

    async def test_a_batch_cancelled_while_its_undo_entry_waits_is_announced_and_undoable(self):
        # Cancelled while its entry waited for a reducer in another task, the
        # batch told no subscriber and lost its entry, though its change had
        # committed.
        from helpers import until

        store = get_store()
        await self._setup(store)
        started, release, go = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow(action, state):
            started.set()
            await release.wait()
            return {**state, "application": {**state.get("application", {}), "other": 1}}

        store._register_reducer("SLOW", slow)
        told = []

        async def watch(state, action=None):
            told.append(action["type"])

        store.subscribe("watcher", watch, action_filter={"BATCH_COMPLETE"})

        async def elsewhere():
            await go.wait()
            await store.dispatch("SLOW", {})

        async def batched():
            async with store.batch(source_id="a"):
                await store.dispatch("SET", {"v": 1}, source_id="a")
                go.set()
                await asyncio.wait_for(started.wait(), 2)

        # Spawned before the batch opens, so it does not join it.
        holder = asyncio.ensure_future(elsewhere())
        batch = asyncio.ensure_future(batched())
        try:
            # The batch's exit is queued for the state turn the reducer holds.
            await until(lambda: store._state_lock is not None and store._state_lock[2][0] == 1)
            batch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(batch, 2)
            await store._flush_notifications()
            assert told == ["BATCH_COMPLETE"]
        finally:
            release.set()
            await asyncio.wait_for(holder, 2)
        assert len(store.state["views"]["a"]["undo_stack"]) == 1
        await self._undo(store)
        assert self._k(store) == "absent"

    async def test_a_batch_with_a_follow_up_undoes_to_before_the_batch(self):
        # The follow-up's record arrived first, so the merge kept its
        # pre-value and then pruned the key: the batch left no entry.
        store = get_store()
        await self._setup(store, below=[self._saga(store)])
        async with store.batch(source_id="a"):
            await store.dispatch("SET", {"v": 1}, source_id="a")
        assert len(store.state["views"]["a"]["undo_stack"]) == 1
        await self._undo(store)
        assert self._k(store) == "absent"

    async def test_an_action_applied_twice_has_an_entry_per_application(self):
        # Both entries held the value from before the first application.
        store = get_store()

        async def twice(action, state, next_fn):
            if action["type"] == "INC":
                await next_fn(action, state)
            return await next_fn(action, state)

        await self._setup(store, above=[twice])
        await store.dispatch("INC", {}, source_id="a")
        assert store.state["application"]["c"]["n"] == 2
        await self._undo(store)
        assert store.state["application"]["c"]["n"] == 1
        await self._undo(store)
        assert "c" not in store.state["application"]

    async def test_an_application_a_later_middleware_stops_has_no_entry(self):
        # The second pass through Undo pushed a duplicate of the first.
        store = get_store()
        calls = []

        async def twice(action, state, next_fn):
            if action["type"] == "INC":
                await next_fn(action, state)
            return await next_fn(action, state)

        async def block_second(action, state, next_fn):
            if action["type"] == "INC":
                calls.append(1)
                if len(calls) == 2:
                    return state
            return await next_fn(action, state)

        await self._setup(store, above=[twice], below=[block_second])
        await store.dispatch("INC", {}, source_id="a")
        assert len(store.state["views"]["a"]["undo_stack"]) == 1

    async def test_an_edit_a_later_middleware_returns_but_never_commits_is_not_recorded(self):
        store = get_store()

        async def decorate(action, state, next_fn):
            result = await next_fn(action, state)
            application = {**result.get("application", {}), "phantom": {"z": 1}}
            return {**result, "application": application}

        await self._setup(store, below=[decorate])
        await store.dispatch("SET", {"v": 1}, source_id="a")
        assert "phantom" not in store.state["application"]
        entry = store.state["views"]["a"]["undo_stack"][-1]
        assert set(entry["application_slots"]) == {"slot"}

    async def test_a_middleware_above_undo_gets_the_state_the_action_produced(self):
        # Undo handed back the store's live state, which held another
        # dispatch's write that landed while a middleware below it waited.
        store = get_store()
        seen = {}
        gate = asyncio.Event()

        async def audit(action, state, next_fn):
            result = await next_fn(action, state)
            if action["payload"].get("hold"):
                seen["other"] = "other" in result.get("application", {})
            return result

        async def wait_after(action, state, next_fn):
            result = await next_fn(action, state)
            if action["payload"].get("hold"):
                await gate.wait()
            return result

        async def set_other(action, state):
            return {**state, "application": {**state.get("application", {}), "other": 1}}

        await self._setup(store, above=[audit], below=[wait_after])
        store._register_reducer("OTHER", set_other)
        held = asyncio.ensure_future(store.dispatch("SET", {"v": 1, "hold": True}, source_id="a"))
        try:
            await asyncio.sleep(0)
            await store.dispatch("OTHER", {}, source_id="b")
        finally:
            gate.set()
            await held
        assert seen["other"] is False
        assert store.state["application"]["other"] == 1

    @pytest.mark.parametrize("position", ["above", "below"])
    async def test_a_field_a_middleware_passed_on_undoes_with_the_action(self, position):
        # The entry held only what the reducer changed from the mapping it
        # was handed, so a field the middleware added stayed after the undo.
        store = get_store()

        async def stamp(action, state, next_fn):
            if action["type"] == "SET":
                application = {**state.get("application", {}), "stamp": {"n": 1}}
                state = {**state, "application": application}
            return await next_fn(action, state)

        await self._setup(store, **{position: [stamp]})
        await store.dispatch("SET", {"v": 1}, source_id="a")
        assert store.state["application"]["stamp"] == {"n": 1}
        await self._undo(store)
        assert self._k(store) == "absent"
        assert "stamp" not in store.state["application"]

    async def test_a_step_that_cannot_be_recorded_says_so_and_the_change_stands(self, caplog):
        # The log named a commit callback, not Undo or the view that lost
        # its step.
        store = get_store()
        await self._setup(store)
        session = store.state["sessions"]["s"]
        store.state["sessions"]["s"] = {**session, "shared_data": {"lock": threading.Lock()}}
        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            await store.dispatch("SET", {"v": 1}, source_id="a")
        assert self._k(store) == 1
        assert not store.state["views"]["a"].get("undo_stack")
        messages = [r.getMessage() for r in caplog.records if r.name.startswith("cascadeui")]
        assert any("Undo could not record SET for view a" in m for m in messages)

    async def test_a_batch_step_sits_where_its_first_change_committed(self):
        # The batch's entry went on top when the batch ended, above an entry
        # another task pushed while it was open, so two undos walked back out
        # of order: absent, then 1.
        store = get_store()
        await self._setup(store)
        started, gate = asyncio.Event(), asyncio.Event()

        async def batched():
            async with store.batch(source_id="a"):
                await store.dispatch("SET", {"v": 1}, source_id="a")
                started.set()
                await gate.wait()

        task = asyncio.ensure_future(batched())
        await asyncio.wait_for(started.wait(), 2)
        await store.dispatch("SET", {"v": 2}, source_id="a")
        gate.set()
        await asyncio.wait_for(task, 2)

        seen = [self._k(store)]
        for _ in range(2):
            await self._undo(store)
            seen.append(self._k(store))
        assert seen == [2, 1, "absent"]

    async def test_writes_interleaved_with_a_batch_undo_back_to_before_it(self):
        # Placed by its last change, the batch's entry was undone first and
        # the step after it put back a value the batch had written.
        store = get_store()
        await self._setup(store)
        first, second = asyncio.Event(), asyncio.Event()

        async def batched():
            async with store.batch(source_id="a"):
                await store.dispatch("SET", {"v": 1}, source_id="a")
                first.set()
                await second.wait()
                await store.dispatch("SET", {"v": 3}, source_id="a")

        task = asyncio.ensure_future(batched())
        await asyncio.wait_for(first.wait(), 2)
        await store.dispatch("SET", {"v": 2}, source_id="a")
        second.set()
        await asyncio.wait_for(task, 2)

        seen = [self._k(store)]
        for _ in range(2):
            await self._undo(store)
            seen.append(self._k(store))
        assert seen == [3, 1, "absent"]

    async def test_a_redo_during_a_batch_is_undone_before_the_batch(self):
        # The entry a redo put back carried no stamp, so the batch's entry,
        # from a change made before the redo, went above it.
        # From inside the batch: an undo or redo from another task waits for
        # the batch to end.
        store = get_store()
        await self._setup(store)
        await store.dispatch("INC", {}, source_id="a")

        async def batched():
            async with store.batch(source_id="a"):
                await store.dispatch("SET", {"v": 1}, source_id="a")
                await self._undo(store)
                await store.dispatch("REDO", {"view_id": "a", "session_id": "s"}, source_id="a")

        await asyncio.wait_for(batched(), 2)

        await self._undo(store)
        assert "c" not in store.state["application"]
        assert self._k(store) == 1


class TestUndoAcrossAPushOrABatchInFlight:
    """Steps taken while a push loads or while another task's batch is open
    reached the undo history out of place."""

    @staticmethod
    async def _set_up():
        from cascadeui.setup import setup_middleware

        await setup_middleware(UndoMiddleware())
        store = get_store()

        async def set_pref(action, state):
            app = state.get("application", {})
            prefs = {**app.get("prefs", {}), action["payload"]["k"]: action["payload"]["v"]}
            return {**state, "application": {**app, "prefs": prefs}}

        store._register_reducer("SET_PREF", set_pref)
        return store

    @staticmethod
    def _prefs(store):
        return store.state.get("application", {}).get("prefs")

    async def test_a_change_made_while_a_push_loads_reaches_the_new_screen(self):
        """The stacks were carried when the push began, so the change was
        missing from the new screen's history."""
        from helpers import RenderableLayoutView

        store = await self._set_up()
        loading = asyncio.Event()

        class Hub(RenderableLayoutView):
            enable_undo = True

        class Detail(RenderableLayoutView):
            enable_undo = True

            async def on_load(self):
                await loading.wait()

        hub = Hub(interaction=_make_interaction())
        await hub.send()
        await hub.dispatch("SET_PREF", {"k": "a", "v": 1})
        pushing = asyncio.create_task(hub.push(Detail, interaction=_make_interaction()))
        for _ in range(10):
            await asyncio.sleep(0)
        await hub.dispatch("SET_PREF", {"k": "b", "v": 2})
        loading.set()
        detail = await pushing

        steps = []
        while detail.undo_depth:
            await detail.undo()
            steps.append(self._prefs(store))
        assert steps == [{"a": 1}, None]

    async def test_an_undo_during_another_tasks_batch_waits_for_its_step(self):
        """The undo took back an older step over the batch's writes, and the
        batch's step then restored a value already undone."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        await view.dispatch("SET_PREF", {"k": "a", "v": 1})
        inside, finish = asyncio.Event(), asyncio.Event()

        async def batched():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "a", "v": 2})
                inside.set()
                await finish.wait()
                await view.dispatch("SET_PREF", {"k": "b", "v": 3})

        batch_task = asyncio.create_task(batched())
        await inside.wait()
        undoing = asyncio.create_task(view.undo())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not undoing.done()
        finish.set()
        await asyncio.wait_for(asyncio.gather(batch_task, undoing), 5)

        assert self._prefs(store) == {"a": 1}
        steps = []
        while view.undo_depth:
            await view.undo()
            steps.append(self._prefs(store))
        assert steps == [None]

    async def test_a_change_committed_after_a_push_follows_the_panel(self):
        """A middleware that awaits held the change until the push had torn
        the view down, and its step went onto a stack nothing reads."""
        from helpers import RenderableLayoutView

        from cascadeui.setup import setup_middleware

        store = await self._set_up()
        release = asyncio.Event()

        async def slow(action, state, next_fn):
            if action["type"] == "SET_PREF" and action["payload"]["k"] == "b":
                await release.wait()
            return await next_fn(action, state)

        await setup_middleware(slow)

        class Hub(RenderableLayoutView):
            enable_undo = True

        class Detail(RenderableLayoutView):
            enable_undo = True

        hub = Hub(interaction=_make_interaction())
        await hub.send()
        await hub.dispatch("SET_PREF", {"k": "a", "v": 1})
        changing = asyncio.create_task(hub.dispatch("SET_PREF", {"k": "b", "v": 2}))
        await asyncio.sleep(0)
        detail = await hub.push(Detail, interaction=_make_interaction())
        release.set()
        await asyncio.wait_for(changing, 5)

        steps = []
        while detail.undo_depth:
            await detail.undo()
            steps.append(self._prefs(store))
        assert steps == [{"a": 1}, None]

    async def test_a_batch_ending_after_a_push_commits_follows_the_panel(self):
        """The view that pushed still had its row until its teardown landed,
        so the batch's step went onto that row and was removed with it."""
        from helpers import RenderableLayoutView

        from cascadeui.setup import setup_middleware

        store = await self._set_up()
        inside, finish, destroy = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Hub(RenderableLayoutView):
            enable_undo = True

        class Detail(RenderableLayoutView):
            enable_undo = True

        hub = Hub(interaction=_make_interaction())
        await hub.send()
        await hub.dispatch("SET_PREF", {"k": "a", "v": 1})

        async def hold_teardown(action, state, next_fn):
            if action["type"] == "VIEW_DESTROYED" and action["payload"]["view_id"] == hub.id:
                await destroy.wait()
            return await next_fn(action, state)

        await setup_middleware(hold_teardown)

        async def batched():
            async with hub.batch():
                await hub.dispatch("SET_PREF", {"k": "b", "v": 2})
                inside.set()
                await finish.wait()

        batch_task = asyncio.create_task(batched())
        await inside.wait()
        pushing = asyncio.create_task(hub.push(Detail, interaction=_make_interaction()))
        for _ in range(50):
            if hub._successor is not None:
                break
            await asyncio.sleep(0)
        assert hub._successor is not None and hub.id in store.state["views"]
        finish.set()
        await asyncio.wait_for(batch_task, 5)
        destroy.set()
        detail = await asyncio.wait_for(pushing, 5)

        steps = []
        while detail.undo_depth:
            await detail.undo()
            steps.append(self._prefs(store))
        assert steps == [{"a": 1}, None]

    async def test_a_change_held_ahead_of_undo_across_a_push_keeps_its_step(self):
        """A middleware ahead of UndoMiddleware held the change until the push
        had torn the view down, so Undo found no live source and recorded
        nothing: the change could not be undone."""
        from helpers import RenderableLayoutView

        from cascadeui.setup import setup_middleware

        release = asyncio.Event()

        async def slow(action, state, next_fn):
            if action["type"] == "SET_PREF" and action["payload"]["k"] == "b":
                await release.wait()
            return await next_fn(action, state)

        await setup_middleware(slow)
        store = await self._set_up()

        class Hub(RenderableLayoutView):
            enable_undo = True

        class Detail(RenderableLayoutView):
            enable_undo = True

        hub = Hub(interaction=_make_interaction())
        await hub.send()
        await hub.dispatch("SET_PREF", {"k": "a", "v": 1})
        changing = asyncio.create_task(hub.dispatch("SET_PREF", {"k": "b", "v": 2}))
        await asyncio.sleep(0)
        detail = await hub.push(Detail, interaction=_make_interaction())
        assert hub.id not in store._active_views
        release.set()
        await asyncio.wait_for(changing, 5)

        steps = []
        while detail.undo_depth:
            await detail.undo()
            steps.append(self._prefs(store))
        assert steps == [{"a": 1}, None]

    async def test_a_batch_interleaving_with_a_write_to_its_key_undoes_to_the_start(self):
        """The batch's step sat below a write another task made between its
        changes, and held that write's result as the batch's starting value,
        so undoing everything ended on it."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        await view.dispatch("SET_PREF", {"k": "b", "v": 54})
        started, finish = asyncio.Event(), asyncio.Event()

        async def batched():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "a", "v": 7})
                started.set()
                await finish.wait()
                await view.dispatch("SET_PREF", {"k": "c", "v": 57})
                await view.dispatch("SET_PREF", {"k": "b", "v": 90})

        batch_task = asyncio.create_task(batched())
        await started.wait()
        await view.dispatch("SET_PREF", {"k": "c", "v": 29})
        finish.set()
        await asyncio.wait_for(batch_task, 5)

        steps = []
        while view.undo_depth:
            await view.undo()
            steps.append(self._prefs(store))
        assert steps == [{"a": 7, "b": 90}, {"b": 54}, None]

    async def test_an_undo_on_the_screen_a_batch_pushed_to_waits_for_the_batch(self):
        """The wait covered only the view the batch started on, so an Undo on
        the screen it pushed to took back an older step over the batch's
        writes."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        class Hub(RenderableLayoutView):
            enable_undo = True

        class Detail(RenderableLayoutView):
            enable_undo = True

        hub = Hub(interaction=_make_interaction())
        await hub.send()
        await hub.dispatch("SET_PREF", {"k": "a", "v": 1})
        pushed, finish = asyncio.Event(), asyncio.Event()
        held = {}

        async def batched():
            async with hub.batch():
                await hub.dispatch("SET_PREF", {"k": "a", "v": 2})
                held["detail"] = await hub.push(Detail, interaction=_make_interaction())
                pushed.set()
                await finish.wait()

        batch_task = asyncio.create_task(batched())
        await pushed.wait()
        undoing = asyncio.create_task(held["detail"].undo())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not undoing.done()
        finish.set()
        await asyncio.wait_for(asyncio.gather(batch_task, undoing), 5)

        assert self._prefs(store) == {"a": 1}

    async def test_an_undo_held_by_a_middleware_waits_for_a_batch_opened_meanwhile(self):
        """undo() found no batch open and dispatched, a middleware held the
        undo while another task's batch wrote the view's key, and the undo then
        took back an older step under the batch's write."""
        from helpers import RenderableLayoutView

        from cascadeui.setup import setup_middleware

        held, release = asyncio.Event(), asyncio.Event()

        async def slow(action, state, next_fn):
            if action["type"] == "UNDO" and not release.is_set():
                held.set()
                await release.wait()
            return await next_fn(action, state)

        await setup_middleware(slow)
        store = await self._set_up()

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        await view.dispatch("SET_PREF", {"k": "a", "v": 73})
        undoing = asyncio.create_task(view.undo())
        await held.wait()
        wrote, finish = asyncio.Event(), asyncio.Event()

        async def batched():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "a", "v": 64})
                wrote.set()
                await finish.wait()

        batch_task = asyncio.create_task(batched())
        await wrote.wait()
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert not undoing.done()
        finish.set()
        await asyncio.wait_for(asyncio.gather(batch_task, undoing), 5)

        assert self._prefs(store) == {"a": 73}
        await view.undo()
        assert self._prefs(store) is None

    async def test_an_undo_with_a_payload_that_is_not_a_mapping_leaves_the_store_usable(self):
        """The payload was read before the state turn's try, so the raise left
        the turn held and every later dispatch waited forever."""
        store = await self._set_up()
        await store.dispatch("UNDO", "not a mapping", source_id="x")

        await asyncio.wait_for(store.dispatch("SET_PREF", {"k": "a", "v": 1}), 5)

        assert self._prefs(store) == {"a": 1}

    async def test_an_undo_through_a_middleware_that_built_its_state_is_skipped(self, caplog):
        """The middleware built its mapping before the batch's writes; waiting
        and then reducing it erased them and the batch's step."""
        from helpers import RenderableLayoutView

        from cascadeui.setup import setup_middleware

        async def stamp(action, state, next_fn):
            application = {**state.get("application", {}), "stamp": action["type"]}
            return await next_fn(action, {**state, "application": application})

        await setup_middleware(stamp)
        store = await self._set_up()

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        await view.dispatch("SET_PREF", {"k": "a", "v": 1})
        inside, finish = asyncio.Event(), asyncio.Event()

        async def batched():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "b", "v": 2})
                inside.set()
                await finish.wait()
                await view.dispatch("SET_PREF", {"k": "c", "v": 3})

        batch_task = asyncio.create_task(batched())
        await inside.wait()
        caplog.set_level(logging.WARNING, logger="cascadeui")
        payload = {"view_id": view.id, "session_id": view.session_id}
        await asyncio.wait_for(store.dispatch("UNDO", payload, source_id=view.id), 5)
        finish.set()
        await asyncio.wait_for(batch_task, 5)

        assert self._prefs(store) == {"a": 1, "b": 2, "c": 3}
        assert view.undo_depth == 2
        assert any("Skipped UNDO" in r.getMessage() for r in caplog.records)

    async def test_a_held_change_that_follows_the_panel_keeps_shared_data(self):
        """The source's row was gone when the step was recorded, so the step
        held no session and its undo wiped the session's shared_data."""
        from helpers import RenderableLayoutView

        from cascadeui.setup import setup_middleware

        release = asyncio.Event()

        async def slow(action, state, next_fn):
            if action["type"] == "SET_PREF" and action["payload"]["k"] == "b":
                await release.wait()
            return await next_fn(action, state)

        await setup_middleware(slow)
        await self._set_up()

        class Hub(RenderableLayoutView):
            enable_undo = True

        class Detail(RenderableLayoutView):
            enable_undo = True

        hub = Hub(interaction=_make_interaction())
        await hub.send()
        await hub.update_session(theme="dark")
        changing = asyncio.create_task(hub.dispatch("SET_PREF", {"k": "b", "v": 2}))
        await asyncio.sleep(0)
        detail = await hub.push(Detail, interaction=_make_interaction())
        release.set()
        await asyncio.wait_for(changing, 5)

        await detail.undo()

        assert detail.shared_data == {"theme": "dark"}

    async def test_two_interleaved_batches_undo_to_the_start(self):
        """A batch's step counted as if all its changes happened at its first
        stamp, so undoing everything ended on one of its own writes."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        await store.dispatch("SET_PREF", {"k": "x", "v": 0})
        a_in, b_in, a_go, b_go = (asyncio.Event() for _ in range(4))

        async def first():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "y", "v": 1})
                a_in.set()
                await a_go.wait()
                await view.dispatch("SET_PREF", {"k": "x", "v": 2})

        async def second():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "x", "v": 1})
                b_in.set()
                await b_go.wait()
                await view.dispatch("SET_PREF", {"k": "z", "v": 1})

        one = asyncio.create_task(first())
        await a_in.wait()
        two = asyncio.create_task(second())
        await b_in.wait()
        a_go.set()
        await asyncio.wait_for(one, 5)
        b_go.set()
        await asyncio.wait_for(two, 5)

        while view.undo_depth:
            await view.undo()
        assert self._prefs(store) == {"x": 0}

    @pytest.mark.parametrize("variant", ["put_back_by_another_task", "put_back_by_the_batch"])
    async def test_a_key_the_batch_ends_as_it_found_undoes_to_the_start(self, variant):
        """The prune dropped a key whose final value matched the batch's start
        before the merge saw that another task's step changed it in between."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        async def del_pref(action, state):
            app = state.get("application", {})
            prefs = dict(app.get("prefs", {}))
            prefs.pop(action["payload"]["k"], None)
            return {**state, "application": {**app, "prefs": prefs}}

        store._register_reducer("DEL_PREF", del_pref)

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        by_task = variant == "put_back_by_another_task"
        if not by_task:
            await store.dispatch("SET_PREF", {"k": "x", "v": 5})
        start = self._prefs(store)
        inside, finish = asyncio.Event(), asyncio.Event()

        async def batched():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "x", "v": 1 if by_task else 7})
                inside.set()
                await finish.wait()
                if by_task:
                    await view.dispatch("DEL_PREF", {"k": "x"})
                else:
                    await view.dispatch("SET_PREF", {"k": "x", "v": 5})

        batch_task = asyncio.create_task(batched())
        await inside.wait()
        await view.dispatch("SET_PREF", {"k": "x", "v": 2 if by_task else 9})
        finish.set()
        await asyncio.wait_for(batch_task, 5)

        while view.undo_depth:
            await view.undo()
        assert (self._prefs(store) or None) == (start or None)

    async def test_an_undo_waits_the_bound_once(self, monkeypatch, caplog):
        """undo() waited the bound and its reducer waited it again, so an undo
        could take twice as long as documented, with two warnings."""
        from helpers import RenderableLayoutView

        from cascadeui.state import store as store_module

        monkeypatch.setattr(store_module, "_UNDO_BATCH_WAIT_SECONDS", 0.2)
        await self._set_up()

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        await view.dispatch("SET_PREF", {"k": "a", "v": 1})
        inside, finish = asyncio.Event(), asyncio.Event()

        async def batched():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "b", "v": 2})
                inside.set()
                await finish.wait()

        batch_task = asyncio.create_task(batched())
        await inside.wait()
        caplog.set_level(logging.WARNING, logger="cascadeui")
        await asyncio.wait_for(view.undo(), 5)
        finish.set()
        await asyncio.wait_for(batch_task, 5)

        waits = [r for r in caplog.records if "for a batch holding its steps" in r.getMessage()]
        assert len(waits) == 1

    async def test_a_change_during_a_continue_reaches_the_new_panel(self):
        """The Continue carried the stacks and never brought them up to date,
        so a change made while it ran had no undo step, and the new view kept
        the old one alive."""
        from unittest.mock import AsyncMock

        from helpers import RenderableLayoutView

        store = await self._set_up()

        class Eph(RenderableLayoutView):
            enable_undo = True
            auto_refresh_ephemeral = True

        old = Eph(interaction=_make_interaction())
        await old.send(ephemeral=True)
        await old.dispatch("SET_PREF", {"k": "a", "v": 1})
        gate, deleting = asyncio.Event(), asyncio.Event()

        async def slow_delete(*args, **kwargs):
            deleting.set()
            await gate.wait()

        old._message.delete = AsyncMock(side_effect=slow_delete)
        reopening = asyncio.create_task(old._reopen_ephemeral(_make_interaction()))
        await asyncio.wait_for(deleting.wait(), 5)
        await old.dispatch("SET_PREF", {"k": "b", "v": 2})
        gate.set()
        await asyncio.wait_for(reopening, 5)
        new = old.current_view

        assert new._undo_carried is None
        steps = []
        while new.undo_depth:
            await new.undo()
            steps.append(self._prefs(store))
        assert steps == [{"a": 1}, None]

    async def test_a_batch_ending_during_the_continue_keeps_its_step(self):
        """The Continue linked the new panel only after the old view's exit, so
        a batch that ended while that exit ran put its step on the old view,
        and the exit removed it: the new panel had one step fewer."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        class Eph(RenderableLayoutView):
            enable_undo = True
            auto_refresh_ephemeral = True

        old = Eph(interaction=_make_interaction())
        await old.send(ephemeral=True)
        await old.dispatch("SET_PREF", {"k": "a", "v": 1})
        gate = asyncio.Event()

        async def release_in_the_exit(action, state, next_fn):
            result = await next_fn(action, state)
            payload = action.get("payload") or {}
            if action["type"] == "VIEW_DESTROYED" and payload.get("view_id") == old.id:
                gate.set()
                for _ in range(30):
                    await asyncio.sleep(0)
            return result

        async def batch_body():
            async with old.batch():
                await old.dispatch("SET_PREF", {"k": "b", "v": 2})
                await gate.wait()

        store._add_middleware(release_in_the_exit)
        try:
            batching = asyncio.create_task(batch_body())
            for _ in range(5):
                await asyncio.sleep(0)
            await asyncio.wait_for(old._reopen_ephemeral(_make_interaction()), 5)
            await asyncio.wait_for(batching, 5)
            await store._flush_notifications()
        finally:
            store._middleware.remove(release_in_the_exit)

        new = old.current_view
        steps = []
        while new.undo_depth:
            await new.undo()
            steps.append(self._prefs(store))
        assert steps == [{"a": 1}, None], f"a step was lost: {steps}"

    async def test_a_change_from_the_view_just_pushed_from_keeps_its_step(self):
        """The view a push tore down was gone from the registry, so its
        dispatch right after push() returned recorded no step."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        class Hub(RenderableLayoutView):
            enable_undo = True

        class Detail(RenderableLayoutView):
            enable_undo = True

        hub = Hub(interaction=_make_interaction())
        await hub.send()
        await hub.dispatch("SET_PREF", {"k": "a", "v": 1})
        detail = await hub.push(Detail, interaction=_make_interaction())
        await hub.dispatch("SET_PREF", {"k": "b", "v": 2})

        steps = []
        while detail.undo_depth:
            await detail.undo()
            steps.append(self._prefs(store))
        assert steps == [{"a": 1}, None]

    async def test_an_interleaved_step_that_changed_a_slots_type_keeps_the_batch_per_key(self):
        """The merge widened the batch's per-key entry to a whole-slot restore,
        which took back a key the batch never wrote."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        async def put(action, state):
            app = state.get("application", {})
            slot, value = action["payload"]["slot"], action["payload"]["value"]
            return {**state, "application": {**app, slot: value}}

        async def put_key(action, state):
            app = state.get("application", {})
            slot = {**app.get("cfg", {}), action["payload"]["k"]: action["payload"]["v"]}
            return {**state, "application": {**app, "cfg": slot}}

        store._register_reducer("PUT", put)
        store._register_reducer("PUT_KEY", put_key)

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        await store.dispatch("PUT", {"slot": "cfg", "value": "legacy"})
        inside, finish = asyncio.Event(), asyncio.Event()

        async def batched():
            async with view.batch():
                await view.dispatch("PUT", {"slot": "other", "value": 1})
                inside.set()
                await finish.wait()
                await view.dispatch("PUT_KEY", {"k": "y", "v": 2})

        batch_task = asyncio.create_task(batched())
        await inside.wait()
        await view.dispatch("PUT", {"slot": "cfg", "value": {"x": 1}})
        finish.set()
        await asyncio.wait_for(batch_task, 5)
        await view.undo()
        await store.dispatch("PUT", {"slot": "cfg", "value": {"z": 9}})

        await view.undo()

        assert store.state["application"]["cfg"] == {"z": 9}

    @pytest.mark.parametrize("via", ["view", "store"])
    async def test_an_undo_waiting_on_a_batch_follows_a_push_that_lands_meanwhile(self, via):
        """The push carried the history to the new screen while the undo
        waited for the batch, and the undo then found the old screen's
        history gone and did nothing."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        class Hub(RenderableLayoutView):
            enable_undo = True

        class Detail(RenderableLayoutView):
            enable_undo = True

        hub = Hub(interaction=_make_interaction())
        await hub.send()
        await hub.dispatch("SET_PREF", {"k": "a", "v": 1})
        inside, finish = asyncio.Event(), asyncio.Event()

        async def batched():
            async with hub.batch():
                await hub.dispatch("SET_PREF", {"k": "b", "v": 2})
                inside.set()
                await finish.wait()

        batch_task = asyncio.create_task(batched())
        await inside.wait()
        if via == "view":
            undoing = asyncio.create_task(hub.undo())
        else:
            payload = {"view_id": hub.id, "session_id": hub.session_id}
            undoing = asyncio.create_task(store.dispatch("UNDO", payload, source_id=hub.id))
        for _ in range(10):
            await asyncio.sleep(0)
        assert not undoing.done()
        detail = await hub.push(Detail, interaction=_make_interaction())
        finish.set()
        await asyncio.wait_for(asyncio.gather(batch_task, undoing), 5)

        assert self._prefs(store) == {"a": 1}
        assert (detail.undo_depth, detail.redo_depth) == (1, 1)

    async def test_a_redo_waiting_on_a_batch_follows_a_push_that_lands_meanwhile(self):
        """A batch that ends with its keys as it found them leaves the redo
        history in place, and the redo waiting for it found the old screen's
        history gone."""
        from helpers import RenderableLayoutView

        store = await self._set_up()

        async def del_pref(action, state):
            app = state.get("application", {})
            prefs = {k: v for k, v in app.get("prefs", {}).items() if k != action["payload"]["k"]}
            return {**state, "application": {**app, "prefs": prefs}}

        store._register_reducer("DEL_PREF", del_pref)

        class Hub(RenderableLayoutView):
            enable_undo = True

        class Detail(RenderableLayoutView):
            enable_undo = True

        hub = Hub(interaction=_make_interaction())
        await hub.send()
        await hub.dispatch("SET_PREF", {"k": "z", "v": 0})
        await hub.dispatch("SET_PREF", {"k": "a", "v": 1})
        await hub.undo()
        inside, finish = asyncio.Event(), asyncio.Event()

        async def batched():
            async with hub.batch():
                await hub.dispatch("SET_PREF", {"k": "b", "v": 2})
                inside.set()
                await finish.wait()
                await hub.dispatch("DEL_PREF", {"k": "b"})

        batch_task = asyncio.create_task(batched())
        await inside.wait()
        redoing = asyncio.create_task(hub.redo())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not redoing.done()
        detail = await hub.push(Detail, interaction=_make_interaction())
        finish.set()
        await asyncio.wait_for(asyncio.gather(batch_task, redoing), 5)

        assert self._prefs(store) == {"z": 0, "a": 1}
        assert (detail.undo_depth, detail.redo_depth) == (2, 0)


class TestUndoToTheBottom:
    """Batches and single changes from several tasks interleave on one view,
    some of them deleting keys; undoing every step returns the view's history
    to where it began. Each seed is one fixed interleaving. The cases here
    left a key behind: a batch placing its step while another was still open
    did not count that one's changes, a batch whose step pruned to nothing
    took its changes with it, and one batch's changes were read as if all
    were made at its first stamp."""

    @staticmethod
    async def _run(seed: int, deletes: bool):
        import random

        from helpers import RenderableLayoutView

        from cascadeui.setup import setup_middleware
        from cascadeui.state import singleton
        from cascadeui.state.store import StateStore

        StateStore._instance = None
        singleton._store_instance = None
        rng = random.Random(seed)
        await setup_middleware(UndoMiddleware())
        store = get_store()

        async def set_pref(action, state):
            app = state.get("application", {})
            prefs = {**app.get("prefs", {}), action["payload"]["k"]: action["payload"]["v"]}
            return {**state, "application": {**app, "prefs": prefs}}

        async def del_pref(action, state):
            app = state.get("application", {})
            prefs = {k: v for k, v in app.get("prefs", {}).items() if k != action["payload"]["k"]}
            return {**state, "application": {**app, "prefs": prefs}}

        store._register_reducer("SET_PREF", set_pref)
        store._register_reducer("DEL_PREF", del_pref)

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        ops = ["SET_PREF", "DEL_PREF"] if deletes else ["SET_PREF"]

        async def ticks(count):
            for _ in range(count):
                await asyncio.sleep(0)

        async def change():
            op = rng.choice(ops)
            key = rng.choice("xy")
            payload = {"k": key, "v": rng.randint(0, 2)} if op == "SET_PREF" else {"k": key}
            await view.dispatch(op, payload)

        async def batch():
            await ticks(rng.randint(0, 3))
            async with view.batch():
                for _ in range(rng.randint(1, 3)):
                    await change()
                    await ticks(rng.randint(0, 3))

        async def single():
            await ticks(rng.randint(0, 6))
            await change()

        tasks = [batch() if rng.random() < 0.6 else single() for _ in range(rng.randint(2, 5))]
        await asyncio.gather(*tasks)
        while view.undo_depth:
            await view.undo()
        return store.state.get("application", {}).get("prefs") or None

    @pytest.mark.parametrize("deletes", [False, True], ids=["writes", "writes_and_deletes"])
    async def test_every_interleaving_undoes_to_the_start(self, deletes):
        left = {}
        for seed in range(300):
            bottom = await self._run(seed, deletes)
            if bottom is not None:
                left[seed] = bottom
        assert left == {}

    @staticmethod
    async def _batch_view():
        from helpers import RenderableLayoutView

        from cascadeui.setup import setup_middleware

        await setup_middleware(UndoMiddleware())
        store = get_store()

        async def set_pref(action, state):
            app = state.get("application", {})
            prefs = {**app.get("prefs", {}), action["payload"]["k"]: action["payload"]["v"]}
            return {**state, "application": {**app, "prefs": prefs}}

        async def del_pref(action, state):
            app = state.get("application", {})
            prefs = {k: v for k, v in app.get("prefs", {}).items() if k != action["payload"]["k"]}
            return {**state, "application": {**app, "prefs": prefs}}

        store._register_reducer("SET_PREF", set_pref)
        store._register_reducer("DEL_PREF", del_pref)

        class Shared(RenderableLayoutView):
            enable_undo = True

        view = Shared(interaction=_make_interaction())
        await view.send()
        return store, view

    async def test_a_batch_whose_step_pruned_away_still_counts_for_one_placed_later(self):
        """A batch that ended as it began pushed no step, and its changes went
        with it, so a batch placed later could not see that it had changed a
        key inside that batch's window, and pruned the key."""
        store, view = await self._batch_view()
        a_wrote, b_wrote, a_last, b_done = (asyncio.Event() for _ in range(4))

        async def batch_a():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "x", "v": 1})
                a_wrote.set()
                await b_wrote.wait()
                await view.dispatch("SET_PREF", {"k": "y", "v": 0})
                a_last.set()
                await b_done.wait()

        async def batch_b():
            await a_wrote.wait()
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "x", "v": 2})
                await view.dispatch("DEL_PREF", {"k": "x"})
                b_wrote.set()
                await a_last.wait()
                await view.dispatch("SET_PREF", {"k": "x", "v": 1})
            b_done.set()

        await asyncio.wait_for(asyncio.gather(batch_a(), batch_b()), 5)

        while view.undo_depth:
            await view.undo()
        assert (store.state.get("application", {}).get("prefs") or None) is None

    async def test_two_batch_steps_queued_behind_a_reducer_see_each_other(self):
        """A batch waiting to place its step behind another task's reducer
        had already emptied its records, so a batch placed ahead of it could
        not see its change inside that batch's window."""
        store, view = await self._batch_view()
        hold = asyncio.Event()

        async def slow(action, state):
            await hold.wait()
            return state

        store._register_reducer("SLOW", slow)
        b_first, a_wrote, b_last, go = (asyncio.Event() for _ in range(4))
        exits = []

        async def batch_b():
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "z", "v": 1})
                b_first.set()
                await a_wrote.wait()
                await view.dispatch("SET_PREF", {"k": "x", "v": 2})
                b_last.set()
                await go.wait()
                exits.append("b")

        async def batch_a():
            await b_first.wait()
            async with view.batch():
                await view.dispatch("SET_PREF", {"k": "x", "v": 1})
                a_wrote.set()
                await go.wait()
                await asyncio.sleep(0)
                exits.append("a")

        tasks = [asyncio.create_task(batch_b()), asyncio.create_task(batch_a())]
        await b_last.wait()
        holding = asyncio.create_task(store.dispatch("SLOW", {}))
        for _ in range(5):
            await asyncio.sleep(0)
        go.set()
        for _ in range(20):
            await asyncio.sleep(0)
        hold.set()
        await asyncio.wait_for(asyncio.gather(holding, *tasks), 5)

        assert exits == ["b", "a"]
        while view.undo_depth:
            await view.undo()
        assert (store.state.get("application", {}).get("prefs") or None) is None
