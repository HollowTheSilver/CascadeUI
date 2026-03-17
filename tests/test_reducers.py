"""Tests for the core reducers."""

import pytest
from datetime import datetime

from cascadeui.state.reducers import (
    reduce_view_created,
    reduce_view_updated,
    reduce_view_destroyed,
    reduce_session_created,
    reduce_component_interaction,
    reduce_modal_submitted,
)


def make_action(action_type, payload, source=None):
    return {
        "type": action_type,
        "payload": payload,
        "source": source,
        "timestamp": datetime.now().isoformat(),
    }


def base_state():
    return {"sessions": {}, "views": {}, "components": {}, "application": {}}


class TestViewReducers:
    async def test_view_created_adds_view(self):
        state = base_state()
        action = make_action("VIEW_CREATED", {
            "view_id": "v1",
            "view_type": "CounterView",
            "user_id": 123,
            "session_id": "s1",
        })
        result = await reduce_view_created(action, state)
        assert "v1" in result["views"]
        assert result["views"]["v1"]["type"] == "CounterView"

    async def test_view_created_does_not_mutate_original(self):
        state = base_state()
        action = make_action("VIEW_CREATED", {"view_id": "v1"})
        result = await reduce_view_created(action, state)
        assert "v1" not in state["views"]
        assert "v1" in result["views"]

    async def test_view_updated_changes_fields(self):
        state = base_state()
        state["views"]["v1"] = {"id": "v1", "updated_at": None, "message_id": None}
        action = make_action("VIEW_UPDATED", {"view_id": "v1", "message_id": "m99"})
        result = await reduce_view_updated(action, state)
        assert result["views"]["v1"]["message_id"] == "m99"

    async def test_view_updated_missing_view_returns_original(self):
        state = base_state()
        action = make_action("VIEW_UPDATED", {"view_id": "nonexistent"})
        result = await reduce_view_updated(action, state)
        assert result is state

    async def test_view_destroyed_removes_view(self):
        state = base_state()
        state["views"]["v1"] = {"id": "v1", "session_id": None}
        action = make_action("VIEW_DESTROYED", {"view_id": "v1"})
        result = await reduce_view_destroyed(action, state)
        assert "v1" not in result["views"]


class TestSessionReducers:
    async def test_session_created(self):
        state = base_state()
        action = make_action("SESSION_CREATED", {"session_id": "s1", "user_id": 456})
        result = await reduce_session_created(action, state)
        assert "s1" in result["sessions"]
        assert result["sessions"]["s1"]["user_id"] == 456

    async def test_session_not_overwritten_on_duplicate(self):
        state = base_state()
        state["sessions"]["s1"] = {"id": "s1", "user_id": 456, "data": {"kept": True}}
        action = make_action("SESSION_CREATED", {"session_id": "s1", "user_id": 456})
        result = await reduce_session_created(action, state)
        assert result["sessions"]["s1"]["data"]["kept"] is True


class TestComponentInteractionReducer:
    async def test_records_interaction(self):
        state = base_state()
        action = make_action("COMPONENT_INTERACTION", {
            "component_id": "btn1",
            "view_id": "v1",
            "user_id": 789,
            "value": True,
        })
        result = await reduce_component_interaction(action, state)
        assert "btn1" in result["components"]
        assert len(result["components"]["btn1"]["interactions"]) == 1

    async def test_interaction_history_capped_at_50(self):
        state = base_state()
        state["components"]["btn1"] = {"id": "btn1", "interactions": [{"i": i} for i in range(50)]}

        action = make_action("COMPONENT_INTERACTION", {
            "component_id": "btn1",
            "view_id": "v1",
            "user_id": 1,
            "value": True,
        })
        result = await reduce_component_interaction(action, state)

        interactions = result["components"]["btn1"]["interactions"]
        assert len(interactions) == 50
        assert interactions[-1]["user_id"] == 1


class TestModalSubmittedReducer:
    async def test_stores_submission(self):
        state = base_state()
        action = make_action("MODAL_SUBMITTED", {
            "view_id": "v1",
            "user_id": 123,
            "values": {"name": "Alice", "age": "25"},
        })
        result = await reduce_modal_submitted(action, state)
        assert "modals" in result
        assert "v1" in result["modals"]
        assert len(result["modals"]["v1"]["submissions"]) == 1
        assert result["modals"]["v1"]["submissions"][0]["values"]["name"] == "Alice"

    async def test_submissions_capped_at_50(self):
        state = base_state()
        state["modals"] = {"v1": {"submissions": [{"i": i} for i in range(50)]}}
        action = make_action("MODAL_SUBMITTED", {
            "view_id": "v1",
            "user_id": 1,
            "values": {"new": True},
        })
        result = await reduce_modal_submitted(action, state)
        subs = result["modals"]["v1"]["submissions"]
        assert len(subs) == 50
        assert subs[-1]["values"] == {"new": True}

    async def test_missing_view_id_returns_original(self):
        state = base_state()
        action = make_action("MODAL_SUBMITTED", {"values": {"x": "y"}})
        result = await reduce_modal_submitted(action, state)
        assert result is state
