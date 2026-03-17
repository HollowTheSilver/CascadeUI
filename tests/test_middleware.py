"""Tests for the middleware system."""

import pytest
from datetime import datetime

from cascadeui import get_store


def make_action(action_type, payload=None, source=None):
    return {
        "type": action_type,
        "payload": payload or {},
        "source": source,
        "timestamp": datetime.now().isoformat(),
    }


class TestMiddlewarePipeline:
    async def test_middleware_receives_action(self):
        store = get_store()
        received = []

        async def spy_middleware(action, state, next_fn):
            received.append(action["type"])
            return await next_fn(action, state)

        store.add_middleware(spy_middleware)

        async def noop_reducer(action, state):
            return state

        store.register_reducer("TEST_ACTION", noop_reducer)
        await store.dispatch("TEST_ACTION", {"key": "value"})

        assert "TEST_ACTION" in received
        store.remove_middleware(spy_middleware)

    async def test_middleware_chain_order(self):
        store = get_store()
        order = []

        async def first_mw(action, state, next_fn):
            order.append("first_before")
            result = await next_fn(action, state)
            order.append("first_after")
            return result

        async def second_mw(action, state, next_fn):
            order.append("second_before")
            result = await next_fn(action, state)
            order.append("second_after")
            return result

        store.add_middleware(first_mw)
        store.add_middleware(second_mw)

        async def noop_reducer(action, state):
            order.append("reducer")
            return state

        store.register_reducer("ORDER_TEST", noop_reducer)
        await store.dispatch("ORDER_TEST")

        assert order == [
            "first_before", "second_before",
            "reducer",
            "second_after", "first_after",
        ]

        store.remove_middleware(first_mw)
        store.remove_middleware(second_mw)

    async def test_middleware_can_short_circuit(self):
        store = get_store()
        reducer_called = []

        async def blocking_middleware(action, state, next_fn):
            if action["type"] == "BLOCKED":
                return state  # Don't call next_fn
            return await next_fn(action, state)

        store.add_middleware(blocking_middleware)

        async def tracking_reducer(action, state):
            reducer_called.append(action["type"])
            return state

        store.register_reducer("BLOCKED", tracking_reducer)
        store.register_reducer("ALLOWED", tracking_reducer)

        await store.dispatch("BLOCKED")
        await store.dispatch("ALLOWED")

        assert "BLOCKED" not in reducer_called
        assert "ALLOWED" in reducer_called

        store.remove_middleware(blocking_middleware)

    async def test_no_middleware_still_works(self):
        store = get_store()
        result = []

        async def reducer(action, state):
            result.append(True)
            return state

        store.register_reducer("PLAIN_TEST", reducer)
        await store.dispatch("PLAIN_TEST")

        assert result == [True]

    async def test_remove_middleware(self):
        store = get_store()
        called = []

        async def removable(action, state, next_fn):
            called.append(True)
            return await next_fn(action, state)

        store.add_middleware(removable)
        store.remove_middleware(removable)

        async def noop(action, state):
            return state

        store.register_reducer("REMOVE_TEST", noop)
        await store.dispatch("REMOVE_TEST")

        assert called == []
