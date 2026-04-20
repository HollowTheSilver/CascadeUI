"""Tests for the core reducers."""

from datetime import datetime

import pytest

from cascadeui.state.reducers import (
    reduce_component_interaction,
    reduce_modal_submitted,
    reduce_navigation_replace,
    reduce_persistent_view_registered,
    reduce_persistent_view_unregistered,
    reduce_redo,
    reduce_scoped_update,
    reduce_session_created,
    reduce_session_updated,
    reduce_undo,
    reduce_view_created,
    reduce_view_destroyed,
    reduce_view_updated,
)


def make_action(action_type, payload, source=None):
    return {
        "type": action_type,
        "payload": payload,
        "source": source,
        "timestamp": datetime.now().isoformat(),
    }


def base_state():
    return {
        "sessions": {},
        "views": {},
        "components": {},
        "application": {"scoped": {}},
    }


class TestViewReducers:
    """VIEW_CREATED, VIEW_UPDATED, VIEW_DESTROYED reducer correctness."""
    async def test_view_created_adds_view(self):
        state = base_state()
        action = make_action(
            "VIEW_CREATED",
            {
                "view_id": "v1",
                "view_type": "CounterView",
                "user_id": 123,
                "session_id": "s1",
            },
        )
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

    async def test_view_destroyed_cleans_up_empty_session(self):
        state = base_state()
        state["sessions"]["s1"] = {
            "id": "s1",
            "user_id": 456,
            "members": ["v1"],
            "history": [],
            "shared_data": {},
        }
        state["views"]["v1"] = {"id": "v1", "session_id": "s1"}
        action = make_action("VIEW_DESTROYED", {"view_id": "v1"})
        result = await reduce_view_destroyed(action, state)
        assert "s1" not in result["sessions"]

    async def test_view_destroyed_deletes_session_when_last_member(self):
        """Session is deleted when its last member is destroyed,
        regardless of other data on the session dict."""
        state = base_state()
        state["sessions"]["s1"] = {
            "id": "s1",
            "user_id": 456,
            "members": ["v1"],
            "history": [{"some": "event"}],
            "shared_data": {"counter": 5},
        }
        state["views"]["v1"] = {"id": "v1", "session_id": "s1"}
        action = make_action("VIEW_DESTROYED", {"view_id": "v1"})
        result = await reduce_view_destroyed(action, state)
        assert "s1" not in result["sessions"]

    async def test_view_destroyed_keeps_session_with_other_views(self):
        state = base_state()
        state["sessions"]["s1"] = {
            "id": "s1",
            "user_id": 456,
            "members": ["v1", "v2"],
            "history": [],
            "shared_data": {},
        }
        state["views"]["v1"] = {"id": "v1", "session_id": "s1"}
        state["views"]["v2"] = {"id": "v2", "session_id": "s1"}
        action = make_action("VIEW_DESTROYED", {"view_id": "v1"})
        result = await reduce_view_destroyed(action, state)
        assert "s1" in result["sessions"]
        assert result["sessions"]["s1"]["members"] == ["v2"]

    async def test_view_destroyed_cleans_component_entries(self):
        state = base_state()
        state["views"]["v1"] = {"id": "v1", "session_id": None}
        state["components"] = {
            "btn_fire": {"id": "btn_fire", "view_id": "v1", "interactions": []},
            "btn_ready": {"id": "btn_ready", "view_id": "v1", "interactions": []},
            "btn_other": {"id": "btn_other", "view_id": "v2", "interactions": []},
        }
        action = make_action("VIEW_DESTROYED", {"view_id": "v1"})
        result = await reduce_view_destroyed(action, state)
        assert "btn_fire" not in result.get("components", {})
        assert "btn_ready" not in result.get("components", {})
        assert result["components"]["btn_other"]["view_id"] == "v2"

    async def test_view_destroyed_cleans_modal_entries(self):
        state = base_state()
        state["views"]["v1"] = {"id": "v1", "session_id": None}
        state["modals"] = {
            "v1": {"submissions": [{"user_id": 1, "values": {}}]},
            "v2": {"submissions": [{"user_id": 2, "values": {}}]},
        }
        action = make_action("VIEW_DESTROYED", {"view_id": "v1"})
        result = await reduce_view_destroyed(action, state)
        assert "v1" not in result.get("modals", {})
        assert "v2" in result["modals"]

    async def test_view_destroyed_removes_empty_components_key(self):
        """When all component entries belong to the destroyed view,
        the ``components`` key itself is removed from state."""
        state = base_state()
        state["views"]["v1"] = {"id": "v1", "session_id": None}
        state["components"] = {
            "only_btn": {"id": "only_btn", "view_id": "v1", "interactions": []},
        }
        action = make_action("VIEW_DESTROYED", {"view_id": "v1"})
        result = await reduce_view_destroyed(action, state)
        assert "components" not in result


class TestSessionReducers:
    """SESSION_CREATED and SESSION_UPDATED reducer correctness."""
    async def test_session_created(self):
        state = base_state()
        action = make_action("SESSION_CREATED", {"session_id": "s1", "user_id": 456})
        result = await reduce_session_created(action, state)
        assert "s1" in result["sessions"]
        assert result["sessions"]["s1"]["user_id"] == 456

    async def test_session_not_overwritten_on_duplicate(self):
        state = base_state()
        state["sessions"]["s1"] = {"id": "s1", "user_id": 456, "shared_data": {"kept": True}}
        action = make_action("SESSION_CREATED", {"session_id": "s1", "user_id": 456})
        result = await reduce_session_created(action, state)
        assert result["sessions"]["s1"]["shared_data"]["kept"] is True

    async def test_session_updated_merges_data(self):
        state = base_state()
        state["sessions"]["s1"] = {
            "id": "s1",
            "user_id": 123,
            "created_at": "t0",
            "updated_at": "t0",
            "shared_data": {"lang": "en"},
        }
        action = make_action("SESSION_UPDATED", {"session_id": "s1", "shared_data": {"theme": "dark"}})
        result = await reduce_session_updated(action, state)
        assert result["sessions"]["s1"]["shared_data"] == {"lang": "en", "theme": "dark"}
        assert result["sessions"]["s1"]["updated_at"] == action["timestamp"]

    async def test_session_updated_overwrites_existing_key(self):
        state = base_state()
        state["sessions"]["s1"] = {
            "id": "s1",
            "updated_at": "t0",
            "shared_data": {"lang": "en"},
        }
        action = make_action("SESSION_UPDATED", {"session_id": "s1", "shared_data": {"lang": "fr"}})
        result = await reduce_session_updated(action, state)
        assert result["sessions"]["s1"]["shared_data"]["lang"] == "fr"

    async def test_session_updated_no_op_for_missing_session(self):
        state = base_state()
        action = make_action("SESSION_UPDATED", {"session_id": "ghost", "shared_data": {"x": 1}})
        result = await reduce_session_updated(action, state)
        assert result is state

    async def test_session_updated_no_data_key_only_touches_timestamp(self):
        state = base_state()
        state["sessions"]["s1"] = {
            "id": "s1",
            "updated_at": "t0",
            "shared_data": {"lang": "en"},
        }
        action = make_action("SESSION_UPDATED", {"session_id": "s1"})
        result = await reduce_session_updated(action, state)
        assert result["sessions"]["s1"]["shared_data"] == {"lang": "en"}
        assert result["sessions"]["s1"]["updated_at"] == action["timestamp"]

    async def test_session_updated_does_not_mutate_original(self):
        state = base_state()
        state["sessions"]["s1"] = {
            "id": "s1",
            "updated_at": "t0",
            "shared_data": {"lang": "en"},
        }
        action = make_action("SESSION_UPDATED", {"session_id": "s1", "shared_data": {"theme": "dark"}})
        await reduce_session_updated(action, state)
        assert state["sessions"]["s1"]["shared_data"] == {"lang": "en"}


class TestComponentInteractionReducer:
    """COMPONENT_INTERACTION records interactions and caps history at 50."""
    async def test_records_interaction(self):
        state = base_state()
        action = make_action(
            "COMPONENT_INTERACTION",
            {
                "component_id": "btn1",
                "view_id": "v1",
                "user_id": 789,
                "value": True,
            },
        )
        result = await reduce_component_interaction(action, state)
        assert "btn1" in result["components"]
        assert result["components"]["btn1"]["view_id"] == "v1"
        assert len(result["components"]["btn1"]["interactions"]) == 1

    async def test_interaction_history_capped_at_50(self):
        state = base_state()
        state["components"]["btn1"] = {
            "id": "btn1",
            "view_id": "v1",
            "interactions": [{"i": i} for i in range(50)],
        }

        action = make_action(
            "COMPONENT_INTERACTION",
            {
                "component_id": "btn1",
                "view_id": "v1",
                "user_id": 1,
                "value": True,
            },
        )
        result = await reduce_component_interaction(action, state)

        interactions = result["components"]["btn1"]["interactions"]
        assert len(interactions) == 50
        assert interactions[-1]["user_id"] == 1


class TestModalSubmittedReducer:
    """MODAL_SUBMITTED stores submissions and caps history at 50."""
    async def test_stores_submission(self):
        state = base_state()
        action = make_action(
            "MODAL_SUBMITTED",
            {
                "view_id": "v1",
                "user_id": 123,
                "values": {"name": "Alice", "age": "25"},
            },
        )
        result = await reduce_modal_submitted(action, state)
        assert "modals" in result
        assert "v1" in result["modals"]
        assert len(result["modals"]["v1"]["submissions"]) == 1
        assert result["modals"]["v1"]["submissions"][0]["values"]["name"] == "Alice"

    async def test_submissions_capped_at_50(self):
        state = base_state()
        state["modals"] = {"v1": {"submissions": [{"i": i} for i in range(50)]}}
        action = make_action(
            "MODAL_SUBMITTED",
            {
                "view_id": "v1",
                "user_id": 1,
                "values": {"new": True},
            },
        )
        result = await reduce_modal_submitted(action, state)
        subs = result["modals"]["v1"]["submissions"]
        assert len(subs) == 50
        assert subs[-1]["values"] == {"new": True}

    async def test_missing_view_id_returns_original(self):
        state = base_state()
        action = make_action("MODAL_SUBMITTED", {"values": {"x": "y"}})
        result = await reduce_modal_submitted(action, state)
        assert result is state

    async def test_form_originated_dispatch_carries_source(self):
        """A form text-edit modal dispatches MODAL_SUBMITTED with ``source``
        set to the form's view id (see ``_build_form_modal`` → ``CascadeModal``
        ``view_id`` wiring in ``views/patterns/form.py``). The reducer does
        not read ``source`` but must not drop it; the devtools History tab
        and user hooks rely on it surviving the pipeline.
        """
        state = base_state()
        action = make_action(
            "MODAL_SUBMITTED",
            {
                "view_id": "form_abc",
                "user_id": 7,
                "values": {"username": "alice"},
            },
            source="form_abc",
        )
        result = await reduce_modal_submitted(action, state)
        # Reducer writes the submission under view_id
        assert "form_abc" in result["modals"]
        submission = result["modals"]["form_abc"]["submissions"][-1]
        assert submission["values"] == {"username": "alice"}
        # Source propagation is a pipeline contract — confirm the action
        # dict still carries it untouched for downstream devtools/hooks.
        assert action["source"] == "form_abc"


class TestShallowSpreadInvariants:
    """Reducers use shallow spread, not deepcopy: identity-preserving on
    no-ops, unchanged branches shared by reference, input state never
    mutated at any depth.  Guards the #153 rewrite against regression.
    """

    async def test_view_created_shares_unchanged_sessions_branch(self):
        """Creating a view that has no session_id leaves sessions branch
        as the same object reference (shallow spread, not deepcopy).
        """
        state = base_state()
        state["sessions"]["s1"] = {"id": "s1", "members": []}
        action = make_action("VIEW_CREATED", {"view_id": "v1"})
        result = await reduce_view_created(action, state)
        # Views branch changed; sessions branch untouched
        assert result["views"] is not state["views"]
        assert result["sessions"] is state["sessions"]

    async def test_view_updated_shares_unchanged_branches(self):
        """Updating one view leaves sessions, components, application
        shared by reference with the input state.
        """
        state = base_state()
        state["views"]["v1"] = {"id": "v1", "message_id": None}
        state["sessions"]["s1"] = {"id": "s1", "members": []}
        state["components"]["btn"] = {"id": "btn", "view_id": "other"}

        action = make_action("VIEW_UPDATED", {"view_id": "v1", "message_id": "m9"})
        result = await reduce_view_updated(action, state)

        assert result["sessions"] is state["sessions"]
        assert result["components"] is state["components"]
        assert result["application"] is state["application"]

    async def test_scoped_update_shares_unchanged_top_level_branches(self):
        """SCOPED_UPDATE touches the ``application.scoped`` subtree. Views and
        sessions stay at the same object reference; application is fresh because
        ``scoped`` lives inside it.
        """
        state = base_state()
        state["views"]["v1"] = {"id": "v1"}
        state["sessions"]["s1"] = {"id": "s1"}
        state["application"]["some_other_slot"] = {"k": "v"}

        action = make_action(
            "SCOPED_UPDATE",
            {
                "scope": "global",
                "identifiers": {},
                "data": {"counter": 1},
            },
        )
        result = await reduce_scoped_update(action, state)

        assert result["views"] is state["views"]
        assert result["sessions"] is state["sessions"]
        # Sibling application slots carry through by reference.
        assert result["application"]["some_other_slot"] is state["application"]["some_other_slot"]
        assert result["application"]["scoped"]["global"]["counter"] == 1

    async def test_session_updated_no_data_does_not_deepcopy_members(self):
        """Touching only timestamp should share the members list by
        reference, not produce a deep copy.
        """
        state = base_state()
        members = ["v1", "v2"]
        state["sessions"]["s1"] = {
            "id": "s1",
            "members": members,
            "shared_data": {"lang": "en"},
        }
        action = make_action("SESSION_UPDATED", {"session_id": "s1"})
        result = await reduce_session_updated(action, state)

        # Same members list reference (shallow spread of session dict)
        assert result["sessions"]["s1"]["members"] is members

    async def test_all_reducers_return_state_unchanged_on_bad_payload(self):
        """Every reducer with a payload guard returns state identity when
        the payload is malformed or targets a missing entity.
        """
        state = base_state()

        # Missing required ids
        cases = [
            (reduce_view_created, make_action("VIEW_CREATED", {})),
            (reduce_view_updated, make_action("VIEW_UPDATED", {})),
            (reduce_view_destroyed, make_action("VIEW_DESTROYED", {})),
            (reduce_session_created, make_action("SESSION_CREATED", {})),
            (reduce_session_updated, make_action("SESSION_UPDATED", {})),
            (reduce_component_interaction, make_action("COMPONENT_INTERACTION", {})),
            (reduce_modal_submitted, make_action("MODAL_SUBMITTED", {})),
            (reduce_persistent_view_registered, make_action("PERSISTENT_VIEW_REGISTERED", {})),
            (reduce_persistent_view_unregistered, make_action("PERSISTENT_VIEW_UNREGISTERED", {})),
            (reduce_scoped_update, make_action("SCOPED_UPDATE", {})),
            (reduce_navigation_replace, make_action("NAVIGATION_REPLACE", {})),
        ]
        for reducer, action in cases:
            result = await reducer(action, state)
            assert result is state, f"{reducer.__name__} did not preserve state identity on no-op"

    async def test_duplicate_session_created_preserves_identity(self):
        """Dispatching SESSION_CREATED for an existing session returns the
        original state (no-op).  Avoids an unnecessary copy on the session-
        already-exists path hit by every push/pop in a session.
        """
        state = base_state()
        state["sessions"]["s1"] = {"id": "s1", "user_id": 7, "shared_data": {}}
        action = make_action("SESSION_CREATED", {"session_id": "s1", "user_id": 7})
        result = await reduce_session_created(action, state)
        assert result is state

    async def test_view_destroyed_does_not_mutate_input(self):
        """After destroying a view, the original state must still contain
        the view and its session membership untouched.
        """
        state = base_state()
        state["sessions"]["s1"] = {"id": "s1", "members": ["v1", "v2"]}
        state["views"]["v1"] = {"id": "v1", "session_id": "s1"}
        state["views"]["v2"] = {"id": "v2", "session_id": "s1"}
        snapshot_members = list(state["sessions"]["s1"]["members"])

        action = make_action("VIEW_DESTROYED", {"view_id": "v1"})
        await reduce_view_destroyed(action, state)

        assert "v1" in state["views"]
        assert state["sessions"]["s1"]["members"] == snapshot_members

    async def test_component_interaction_does_not_mutate_input_history(self):
        """Appending an interaction must not grow the input state's list."""
        state = base_state()
        state["components"]["btn"] = {
            "id": "btn",
            "view_id": "v1",
            "interactions": [{"i": 1}],
        }
        snapshot_len = len(state["components"]["btn"]["interactions"])

        action = make_action(
            "COMPONENT_INTERACTION",
            {"component_id": "btn", "view_id": "v1", "user_id": 5, "value": True},
        )
        result = await reduce_component_interaction(action, state)

        assert len(state["components"]["btn"]["interactions"]) == snapshot_len
        assert len(result["components"]["btn"]["interactions"]) == snapshot_len + 1


class TestUndoRedoReducers:
    """Undo/redo reducers apply per-slot diffs without mutating prior state."""

    async def test_undo_applies_slot_diff_and_builds_inverse_redo(self):
        """UNDO restores the diff's pre-values and captures the current
        post-values into the redo stack for the same slot names."""
        state = base_state()
        state["application"] = {"counter": 5, "other": "untouched"}
        state["views"]["v1"] = {
            "id": "v1",
            "session_id": "s1",
            "undo_stack": [
                {"application_slots": {"counter": 4}, "shared_data": {}},
            ],
            "redo_stack": [],
        }
        state["sessions"]["s1"] = {"id": "s1", "shared_data": {}, "members": ["v1"]}

        action = make_action(
            "UNDO",
            {"view_id": "v1", "session_id": "s1"},
            source="v1",
        )
        result = await reduce_undo(action, state)

        # Diff'd slot reverts; sibling slot survives untouched
        assert result["application"]["counter"] == 4
        assert result["application"]["other"] == "untouched"
        assert result["views"]["v1"]["undo_stack"] == []

        # Redo captures the inverse: current slot value before the UNDO
        redo_top = result["views"]["v1"]["redo_stack"][-1]
        assert redo_top["application_slots"] == {"counter": 5}

        # Input state identity preserved
        assert state["application"]["counter"] == 5
        assert state["views"]["v1"]["redo_stack"] == []

    async def test_redo_applies_slot_diff_and_builds_inverse_undo(self):
        """REDO restores the diff's post-values and captures the current
        pre-values into the undo stack for the same slot names."""
        state = base_state()
        state["application"] = {"counter": 4, "other": "untouched"}
        state["views"]["v1"] = {
            "id": "v1",
            "session_id": "s1",
            "undo_stack": [],
            "redo_stack": [
                {"application_slots": {"counter": 5}, "shared_data": {}},
            ],
        }
        state["sessions"]["s1"] = {"id": "s1", "shared_data": {}, "members": ["v1"]}

        action = make_action(
            "REDO",
            {"view_id": "v1", "session_id": "s1"},
            source="v1",
        )
        result = await reduce_redo(action, state)

        assert result["application"]["counter"] == 5
        assert result["application"]["other"] == "untouched"
        assert result["views"]["v1"]["redo_stack"] == []

        undo_top = result["views"]["v1"]["undo_stack"][-1]
        assert undo_top["application_slots"] == {"counter": 4}
        assert state["application"]["counter"] == 4

    async def test_undo_empty_stack_returns_identity(self):
        state = base_state()
        state["views"]["v1"] = {"id": "v1", "undo_stack": [], "redo_stack": []}
        action = make_action("UNDO", {"view_id": "v1"}, source="v1")
        result = await reduce_undo(action, state)
        assert result is state

    async def test_undo_preserves_shared_data_restoration(self):
        """UNDO restores both the slot-diff and shared_data, and captures
        the live shared_data into the redo snapshot."""
        from cascadeui.state.middleware.undo import _MISSING

        state = base_state()
        state["application"] = {"val": "new"}
        state["views"]["v1"] = {
            "id": "v1",
            "session_id": "s1",
            "undo_stack": [
                {
                    "application_slots": {"val": "old"},
                    "shared_data": {"theme": "dark"},
                },
            ],
            "redo_stack": [],
        }
        state["sessions"]["s1"] = {
            "id": "s1",
            "shared_data": {"theme": "light"},
            "members": ["v1"],
        }

        action = make_action(
            "UNDO",
            {"view_id": "v1", "session_id": "s1"},
            source="v1",
        )
        result = await reduce_undo(action, state)

        assert result["application"]["val"] == "old"
        assert result["sessions"]["s1"]["shared_data"] == {"theme": "dark"}

        redo_top = result["views"]["v1"]["redo_stack"][-1]
        assert redo_top["application_slots"] == {"val": "new"}
        assert redo_top["shared_data"] == {"theme": "light"}
        # Identity sentinel is not in play for this case (value existed pre)
        assert _MISSING not in redo_top["application_slots"].values()

    async def test_undo_with_missing_sentinel_deletes_slot(self):
        """``_MISSING`` in a diff tells UNDO to pop the slot entirely,
        and the redo diff remembers the current value so REDO re-adds it."""
        from cascadeui.state.middleware.undo import _MISSING

        state = base_state()
        state["application"] = {"fresh": {"count": 1}, "keep": True}
        state["views"]["v1"] = {
            "id": "v1",
            "session_id": "s1",
            "undo_stack": [
                {"application_slots": {"fresh": _MISSING}, "shared_data": {}},
            ],
            "redo_stack": [],
        }
        state["sessions"]["s1"] = {"id": "s1", "shared_data": {}, "members": ["v1"]}

        action = make_action(
            "UNDO",
            {"view_id": "v1", "session_id": "s1"},
            source="v1",
        )
        result = await reduce_undo(action, state)

        # Slot gone
        assert "fresh" not in result["application"]
        # Sibling survives
        assert result["application"]["keep"] is True
        # Redo remembers the current value so REDO can re-add it
        redo_top = result["views"]["v1"]["redo_stack"][-1]
        assert redo_top["application_slots"] == {"fresh": {"count": 1}}
