"""Tests for 7.8 — Navigation Stack (Push/Pop)."""

import copy
import pytest

from cascadeui.state.singleton import get_store
from cascadeui.state.reducers import reduce_navigation_push, reduce_navigation_pop


class TestNavigationPushReducer:
    async def test_push_adds_to_stack(self):
        state = {
            "sessions": {
                "user_1": {
                    "id": "user_1",
                    "views": [],
                    "history": [],
                    "data": {},
                }
            },
            "views": {},
            "application": {},
        }

        action = {
            "type": "NAVIGATION_PUSH",
            "payload": {
                "session_id": "user_1",
                "class_name": "HomeView",
                "module": "test.views",
                "kwargs": {},
                "state_snapshot": None,
            },
            "source": None,
            "timestamp": "2026-01-01T00:00:00",
        }

        new_state = await reduce_navigation_push(action, state)
        stack = new_state["sessions"]["user_1"]["nav_stack"]
        assert len(stack) == 1
        assert stack[0]["class_name"] == "HomeView"

    async def test_multiple_pushes(self):
        state = {
            "sessions": {
                "s1": {"id": "s1", "views": [], "history": [], "data": {}, "nav_stack": []},
            },
            "views": {},
            "application": {},
        }

        for name in ["ViewA", "ViewB", "ViewC"]:
            action = {
                "type": "NAVIGATION_PUSH",
                "payload": {
                    "session_id": "s1",
                    "class_name": name,
                    "module": "test",
                    "kwargs": {},
                    "state_snapshot": None,
                },
                "source": None,
                "timestamp": "2026-01-01T00:00:00",
            }
            state = await reduce_navigation_push(action, state)

        stack = state["sessions"]["s1"]["nav_stack"]
        assert len(stack) == 3
        assert [e["class_name"] for e in stack] == ["ViewA", "ViewB", "ViewC"]


class TestNavigationPopReducer:
    async def test_pop_removes_top(self):
        state = {
            "sessions": {
                "s1": {
                    "id": "s1",
                    "nav_stack": [
                        {"class_name": "Home", "module": "t", "kwargs": {}, "state_snapshot": None},
                        {"class_name": "Settings", "module": "t", "kwargs": {}, "state_snapshot": None},
                    ],
                }
            },
        }

        action = {
            "type": "NAVIGATION_POP",
            "payload": {"session_id": "s1"},
            "source": None,
            "timestamp": "2026-01-01T00:00:00",
        }

        new_state = await reduce_navigation_pop(action, state)
        stack = new_state["sessions"]["s1"]["nav_stack"]
        assert len(stack) == 1
        assert stack[0]["class_name"] == "Home"

    async def test_pop_on_empty_stack_is_noop(self):
        state = {
            "sessions": {
                "s1": {"id": "s1", "nav_stack": []},
            },
        }

        action = {
            "type": "NAVIGATION_POP",
            "payload": {"session_id": "s1"},
            "source": None,
            "timestamp": "2026-01-01T00:00:00",
        }

        new_state = await reduce_navigation_pop(action, state)
        assert new_state["sessions"]["s1"]["nav_stack"] == []


class TestNavigationStackIntegration:
    async def test_push_pop_through_store(self):
        """Push and pop through the real store dispatch."""
        store = get_store()

        # Create a session first
        await store.dispatch("SESSION_CREATED", {
            "session_id": "nav_test",
            "user_id": 1,
        })

        # Push
        await store.dispatch("NAVIGATION_PUSH", {
            "session_id": "nav_test",
            "class_name": "PageA",
            "module": "test",
            "kwargs": {},
            "state_snapshot": None,
        })

        stack = store.state["sessions"]["nav_test"]["nav_stack"]
        assert len(stack) == 1

        # Push another
        await store.dispatch("NAVIGATION_PUSH", {
            "session_id": "nav_test",
            "class_name": "PageB",
            "module": "test",
            "kwargs": {},
            "state_snapshot": None,
        })

        stack = store.state["sessions"]["nav_test"]["nav_stack"]
        assert len(stack) == 2

        # Pop
        await store.dispatch("NAVIGATION_POP", {"session_id": "nav_test"})
        stack = store.state["sessions"]["nav_test"]["nav_stack"]
        assert len(stack) == 1
        assert stack[0]["class_name"] == "PageA"
