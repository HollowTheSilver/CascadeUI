"""Tests for 7.5 — Event Hooks."""

import copy
import pytest

from cascadeui.state.singleton import get_store


class TestEventHooks:
    async def test_hook_fires_on_matching_action(self):
        store = get_store()
        received = []

        async def my_hook(action, state):
            received.append(action["type"])

        store.on("view_created", my_hook)

        # Use a simple reducer to handle the action
        async def reducer(action, state):
            new = copy.deepcopy(state)
            new["views"]["test_view"] = {"id": "test_view"}
            return new

        store.register_reducer("VIEW_CREATED", reducer)
        await store.dispatch("VIEW_CREATED", {"view_id": "test_view"})

        assert "VIEW_CREATED" in received

    async def test_off_removes_hook(self):
        store = get_store()
        received = []

        async def my_hook(action, state):
            received.append(action["type"])

        store.on("view_destroyed", my_hook)
        store.off("view_destroyed", my_hook)

        await store.dispatch("VIEW_DESTROYED", {"view_id": "x"})
        assert received == []

    async def test_failing_hook_does_not_break_dispatch(self):
        store = get_store()
        post_hook_received = []

        async def bad_hook(action, state):
            raise RuntimeError("Hook exploded")

        async def good_handler(state, action):
            post_hook_received.append(action["type"])

        store.on("MY_ACTION", bad_hook)
        store.subscribe("hook-test-sub", good_handler)

        # Dispatch should complete without raising
        result = await store.dispatch("MY_ACTION", {})
        assert result is not None
        # Subscriber should still have been notified (hooks fire after subscribers)
        assert "MY_ACTION" in post_hook_received

    async def test_hooks_fire_after_subscribers(self):
        store = get_store()
        order = []

        async def subscriber(state, action):
            order.append("subscriber")

        async def hook(action, state):
            order.append("hook")

        store.subscribe("order-sub", subscriber)
        store.on("ORDER_TEST", hook)

        await store.dispatch("ORDER_TEST", {})

        assert order == ["subscriber", "hook"]

    async def test_raw_action_type_as_hook_name(self):
        """You can pass the raw action type directly to on()."""
        store = get_store()
        received = []

        async def hook(action, state):
            received.append(action["type"])

        store.on("CUSTOM_ACTION", hook)
        await store.dispatch("CUSTOM_ACTION", {})
        assert "CUSTOM_ACTION" in received

    async def test_multiple_hooks_same_event(self):
        store = get_store()
        received = []

        async def hook_a(action, state):
            received.append("a")

        async def hook_b(action, state):
            received.append("b")

        store.on("MULTI_HOOK", hook_a)
        store.on("MULTI_HOOK", hook_b)

        await store.dispatch("MULTI_HOOK", {})
        assert received == ["a", "b"]
