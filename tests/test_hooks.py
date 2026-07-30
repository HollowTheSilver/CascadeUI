"""Tests for event hooks (on/off lifecycle observation)."""

import copy

import pytest

from cascadeui.state.singleton import get_store


class TestEventHooks:
    """Store on/off hooks fire on matching actions and survive hook failures."""

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

        store._register_reducer("VIEW_CREATED", reducer)
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
        await store._flush_notifications()
        assert result is not None
        # Subscriber still fires even when a hook raised.
        assert "MY_ACTION" in post_hook_received

    async def test_hooks_and_subscribers_both_fire(self):
        """Under fire-and-forget dispatch, hooks and subscribers run
        concurrently rather than in a strict order. Both must fire, but
        the sequence is not guaranteed -- hooks often win the race
        because they await inline while subscribers are scheduled tasks.
        """
        store = get_store()
        fired = set()

        async def subscriber(state, action):
            fired.add("subscriber")

        async def hook(action, state):
            fired.add("hook")

        store.subscribe("order-sub", subscriber)
        store.on("ORDER_TEST", hook)

        await store.dispatch("ORDER_TEST", {})
        await store._flush_notifications()

        assert fired == {"subscriber", "hook"}

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


class TestAwaitMaybe:
    """The shared helper the whole sync-or-async contract rests on.

    Fifty-eight call sites resolve a caller's return value through it. Each
    proves it indirectly at best, so a regression here would surface as
    fifty-eight unrelated symptoms rather than one named failure.
    """

    async def test_plain_value_passes_through(self):
        from cascadeui.utils.hooks import await_maybe

        assert await await_maybe(42) == 42

    async def test_none_passes_through(self):
        from cascadeui.utils.hooks import await_maybe

        assert await await_maybe(None) is None

    async def test_coroutine_is_awaited(self):
        from cascadeui.utils.hooks import await_maybe

        async def work():
            return "resolved"

        assert await await_maybe(work()) == "resolved"

    async def test_result_of_an_async_dunder_call_is_awaited(self):
        """The shape ``inspect.iscoroutinefunction`` answers False for."""
        from cascadeui.utils.hooks import await_maybe

        class AsyncCallable:
            async def __call__(self):
                return "resolved"

        assert await await_maybe(AsyncCallable()()) == "resolved"

    async def test_result_of_a_partial_around_a_coroutine_function_is_awaited(self):
        import functools

        from cascadeui.utils.hooks import await_maybe

        async def work():
            return "resolved"

        assert await await_maybe(functools.partial(work)()) == "resolved"

    async def test_a_falsy_awaitable_result_is_still_returned(self):
        """Resolution keys on awaitability, not truthiness -- a hook that
        legitimately returns False or 0 must not be mistaken for one that
        returned nothing."""
        from cascadeui.utils.hooks import await_maybe

        async def work():
            return False

        assert await await_maybe(work()) is False
