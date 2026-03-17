"""Tests for 7.4 — Undo/Redo."""

import copy
import pytest

from cascadeui.state.singleton import get_store
from cascadeui.state.undo import UndoMiddleware


class TestUndoMiddleware:
    async def test_dispatch_creates_undo_snapshot(self):
        store = get_store()
        undo_mw = UndoMiddleware(store)
        store.add_middleware(undo_mw)

        # Create a session
        await store.dispatch("SESSION_CREATED", {"session_id": "undo_s", "user_id": 1})

        # Mark a view as undo-enabled (dict: view_id -> limit)
        store._undo_enabled_views["view_1"] = 20

        # Register view in state
        await store.dispatch("VIEW_CREATED", {
            "view_id": "view_1", "view_type": "Test",
            "user_id": 1, "session_id": "undo_s",
        })

        # Set initial application state
        store.state["application"]["counter"] = 0

        # Register a custom reducer
        async def inc_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["counter"] = state["application"].get("counter", 0) + 1
            return new

        store.register_reducer("INCREMENT", inc_reducer)

        # Dispatch from the undo-enabled view
        await store.dispatch("INCREMENT", {}, source_id="view_1")

        session = store.state["sessions"]["undo_s"]
        assert "undo_stack" in session
        assert len(session["undo_stack"]) == 1

    async def test_undo_restores_previous_state_via_reducer(self):
        """Undo should restore state through the UNDO reducer."""
        store = get_store()
        undo_mw = UndoMiddleware(store)
        store.add_middleware(undo_mw)

        await store.dispatch("SESSION_CREATED", {"session_id": "undo_s2", "user_id": 2})
        store._undo_enabled_views["v2"] = 20

        await store.dispatch("VIEW_CREATED", {
            "view_id": "v2", "view_type": "Test",
            "user_id": 2, "session_id": "undo_s2",
        })

        store.state["application"]["val"] = "before"

        async def set_val(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"]["val"]
            return new

        store.register_reducer("SET_VAL", set_val)

        await store.dispatch("SET_VAL", {"val": "after"}, source_id="v2")
        assert store.state["application"]["val"] == "after"

        # Verify undo stack has a snapshot
        session = store.state["sessions"]["undo_s2"]
        assert len(session.get("undo_stack", [])) == 1

        # Dispatch UNDO through the reducer pipeline
        await store.dispatch("UNDO", {"session_id": "undo_s2"})

        assert store.state["application"]["val"] == "before"

        # Verify redo stack was populated
        session = store.state["sessions"]["undo_s2"]
        assert len(session.get("redo_stack", [])) == 1
        assert len(session.get("undo_stack", [])) == 0

    async def test_redo_via_reducer(self):
        """Redo should re-apply via the REDO reducer."""
        store = get_store()
        undo_mw = UndoMiddleware(store)
        store.add_middleware(undo_mw)

        await store.dispatch("SESSION_CREATED", {"session_id": "redo_s", "user_id": 6})
        store._undo_enabled_views["v_redo"] = 20

        await store.dispatch("VIEW_CREATED", {
            "view_id": "v_redo", "view_type": "Test",
            "user_id": 6, "session_id": "redo_s",
        })

        store.state["application"]["x"] = 1

        async def set_x(action, state):
            new = copy.deepcopy(state)
            new["application"]["x"] = action["payload"]["x"]
            return new

        store.register_reducer("SET_X", set_x)

        await store.dispatch("SET_X", {"x": 2}, source_id="v_redo")
        assert store.state["application"]["x"] == 2

        # Undo
        await store.dispatch("UNDO", {"session_id": "redo_s"})
        assert store.state["application"]["x"] == 1

        # Redo
        await store.dispatch("REDO", {"session_id": "redo_s"})
        assert store.state["application"]["x"] == 2

    async def test_stack_depth_limit(self):
        store = get_store()
        undo_mw = UndoMiddleware(store)
        store.add_middleware(undo_mw)

        await store.dispatch("SESSION_CREATED", {"session_id": "limit_s", "user_id": 3})

        # Register view with a limit of 5
        store._undo_enabled_views["v_limit"] = 5

        await store.dispatch("VIEW_CREATED", {
            "view_id": "v_limit", "view_type": "Test",
            "user_id": 3, "session_id": "limit_s",
        })

        async def noop(action, state):
            new = copy.deepcopy(state)
            new["application"]["n"] = action["payload"].get("n", 0)
            return new

        store.register_reducer("NOOP_ACTION", noop)

        for i in range(10):
            await store.dispatch("NOOP_ACTION", {"n": i}, source_id="v_limit")

        session = store.state["sessions"]["limit_s"]
        assert len(session["undo_stack"]) <= 5

    async def test_empty_undo_stack_is_noop(self):
        store = get_store()

        await store.dispatch("SESSION_CREATED", {"session_id": "empty_s", "user_id": 4})

        # UNDO on empty stack should not crash
        await store.dispatch("UNDO", {"session_id": "empty_s"})

        session = store.state["sessions"]["empty_s"]
        assert session.get("undo_stack", []) == []

    async def test_lifecycle_actions_not_recorded(self):
        """Internal lifecycle actions like VIEW_CREATED should not create undo snapshots."""
        store = get_store()
        undo_mw = UndoMiddleware(store)
        store.add_middleware(undo_mw)

        store._undo_enabled_views["v_lifecycle"] = 20

        await store.dispatch("SESSION_CREATED", {"session_id": "lc_s", "user_id": 5})
        await store.dispatch("VIEW_CREATED", {
            "view_id": "v_lifecycle", "view_type": "Test",
            "user_id": 5, "session_id": "lc_s",
        })

        session = store.state["sessions"]["lc_s"]
        # Only lifecycle actions dispatched — no undo entries
        assert session.get("undo_stack", []) == []

    async def test_undo_does_not_corrupt_stacks(self):
        """Undo/redo via reducer should use deepcopy — no reference sharing."""
        store = get_store()
        undo_mw = UndoMiddleware(store)
        store.add_middleware(undo_mw)

        await store.dispatch("SESSION_CREATED", {"session_id": "safe_s", "user_id": 7})
        store._undo_enabled_views["v_safe"] = 20

        await store.dispatch("VIEW_CREATED", {
            "view_id": "v_safe", "view_type": "Test",
            "user_id": 7, "session_id": "safe_s",
        })

        store.state["application"]["items"] = ["a"]

        async def add_item(action, state):
            new = copy.deepcopy(state)
            new["application"]["items"] = state["application"]["items"] + [action["payload"]["item"]]
            return new

        store.register_reducer("ADD_ITEM", add_item)

        await store.dispatch("ADD_ITEM", {"item": "b"}, source_id="v_safe")
        assert store.state["application"]["items"] == ["a", "b"]

        # Undo
        await store.dispatch("UNDO", {"session_id": "safe_s"})
        assert store.state["application"]["items"] == ["a"]

        # Mutate live state — should NOT affect redo stack
        store.state["application"]["items"].append("CORRUPTED")

        # Redo
        await store.dispatch("REDO", {"session_id": "safe_s"})
        assert store.state["application"]["items"] == ["a", "b"]
