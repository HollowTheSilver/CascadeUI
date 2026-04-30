"""Tests for undo/redo middleware and state snapshot restoration."""

import copy

import pytest
from helpers import make_interaction as _make_interaction

from cascadeui.state.middleware import UndoMiddleware
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

        # Simulate push: create destination view, transfer undo stacks
        await store.dispatch(
            "VIEW_CREATED",
            {"view_id": "v_dst", "view_type": "Test", "user_id": 30, "session_id": "xfer_s"},
        )
        store._undo_enabled_views["v_dst"] = 20

        src_state = store.state["views"]["v_src"]
        dst_state = store.state["views"]["v_dst"]
        dst_state["undo_stack"] = list(src_state.get("undo_stack", []))
        dst_state["redo_stack"] = list(src_state.get("redo_stack", []))

        # Destroy old view (simulates VIEW_DESTROYED during push)
        await store.dispatch("VIEW_DESTROYED", {"view_id": "v_src"})
        assert "v_src" not in store.state["views"]

        # Undo from the destination view should restore the original value
        await store.dispatch("UNDO", {"view_id": "v_dst", "session_id": "xfer_s"})
        assert store.state["application"]["val"] == "original"

        # Redo should re-apply
        await store.dispatch("REDO", {"view_id": "v_dst", "session_id": "xfer_s"})
        assert store.state["application"]["val"] == "changed"


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

    ``UndoMiddleware.__call__`` skips snapshot capture while the store is
    batching; ``BatchContext.__aexit__`` delegates to
    ``UndoMiddleware.finalize_batch`` so exactly one snapshot of the
    pre-batch state is pushed onto each participating view's stack when
    the outermost batch commits.
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

    async def test_exception_inside_batch_pushes_no_undo(self):
        """A batch aborted by exception drops its queued actions and pushes no snapshot."""
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

        view = store.state["views"]["bv_4"]
        assert view.get("undo_stack", []) == []

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


class TestMissingSentinelDeepCopy:
    """``_MISSING`` must survive ``copy.deepcopy`` without losing identity.

    ``@cascade_reducer`` deep-copies state before every reducer. State
    contains undo-stack diffs that may carry ``_MISSING`` sentinels
    (marking "this slot did not exist pre-action"). If deepcopy creates
    fresh ``object()`` instances in place of ``_MISSING``, the identity
    check ``target_value is _MISSING`` in ``_apply_slot_diff`` fails and
    the bare ``object()`` lands in the slot value, corrupting state for
    every subsequent reducer that reads the slot.
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
        assert copied["application_slots"]["settings"] is not snapshot["application_slots"]["settings"]

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
        assert scoped_after_undo is None or isinstance(scoped_after_undo, dict), (
            f"slot corruption: state['application']['scoped'] is {type(scoped_after_undo).__name__}"
        )

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
