"""Tests for the middleware system."""

from datetime import datetime

import pytest

from cascadeui import get_store


def make_action(action_type, payload=None, source=None):
    return {
        "type": action_type,
        "payload": payload or {},
        "source": source,
        "timestamp": datetime.now().isoformat(),
    }


class TestMiddlewarePipeline:
    """Middleware receives actions, can modify or block them, and chains correctly."""
    async def test_middleware_receives_action(self):
        store = get_store()
        received = []

        async def spy_middleware(action, state, next_fn):
            received.append(action["type"])
            return await next_fn(action, state)

        store._add_middleware(spy_middleware)

        async def noop_reducer(action, state):
            return state

        store._register_reducer("TEST_ACTION", noop_reducer)
        await store.dispatch("TEST_ACTION", {"key": "value"})

        assert "TEST_ACTION" in received
        store._remove_middleware(spy_middleware)

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

        store._add_middleware(first_mw)
        store._add_middleware(second_mw)

        async def noop_reducer(action, state):
            order.append("reducer")
            return state

        store._register_reducer("ORDER_TEST", noop_reducer)
        await store.dispatch("ORDER_TEST")

        assert order == [
            "first_before",
            "second_before",
            "reducer",
            "second_after",
            "first_after",
        ]

        store._remove_middleware(first_mw)
        store._remove_middleware(second_mw)

    async def test_middleware_can_short_circuit(self):
        store = get_store()
        reducer_called = []

        async def blocking_middleware(action, state, next_fn):
            if action["type"] == "BLOCKED":
                return state  # Don't call next_fn
            return await next_fn(action, state)

        store._add_middleware(blocking_middleware)

        async def tracking_reducer(action, state):
            reducer_called.append(action["type"])
            return state

        store._register_reducer("BLOCKED", tracking_reducer)
        store._register_reducer("ALLOWED", tracking_reducer)

        await store.dispatch("BLOCKED")
        await store.dispatch("ALLOWED")

        assert "BLOCKED" not in reducer_called
        assert "ALLOWED" in reducer_called

        store._remove_middleware(blocking_middleware)

    async def test_no_middleware_still_works(self):
        store = get_store()
        result = []

        async def reducer(action, state):
            result.append(True)
            return state

        store._register_reducer("PLAIN_TEST", reducer)
        await store.dispatch("PLAIN_TEST")

        assert result == [True]

    async def test_remove_middleware(self):
        store = get_store()
        called = []

        async def removable(action, state, next_fn):
            called.append(True)
            return await next_fn(action, state)

        store._add_middleware(removable)
        store._remove_middleware(removable)

        async def noop(action, state):
            return state

        store._register_reducer("REMOVE_TEST", noop)
        await store.dispatch("REMOVE_TEST")

        assert called == []


class TestHasMiddleware:
    """v3.0.0: ``StateStore.has_middleware(cls)`` is the public seam
    for asking whether an instance of a middleware class is installed.
    Replaces ad-hoc reads of the private ``_middleware`` list.
    """

    async def test_returns_false_when_not_installed(self):
        from cascadeui.state.middleware import UndoMiddleware

        store = get_store()
        # Ensure clean slate for this assertion regardless of test order.
        for mw in list(store._middleware):
            if isinstance(mw, UndoMiddleware):
                store._remove_middleware(mw)
        assert store.has_middleware(UndoMiddleware) is False

    async def test_returns_true_after_install(self):
        from cascadeui.state.middleware import UndoMiddleware

        store = get_store()
        mw = UndoMiddleware()
        store._add_middleware(mw)
        await mw.initialize(store)
        try:
            assert store.has_middleware(UndoMiddleware) is True
        finally:
            store._remove_middleware(mw)

    async def test_subclass_match(self):
        store = get_store()

        class _Base:
            async def __call__(self, action, state, next_fn):
                return await next_fn(action, state)

        class _Derived(_Base):
            pass

        instance = _Derived()
        store._add_middleware(instance)
        try:
            assert store.has_middleware(_Base) is True
            assert store.has_middleware(_Derived) is True
        finally:
            store._remove_middleware(instance)

    async def test_idempotent_install_pattern(self):
        """Demonstrates the canonical use case from v2_settings."""
        from cascadeui.state.middleware import UndoMiddleware

        store = get_store()
        for mw in list(store._middleware):
            if isinstance(mw, UndoMiddleware):
                store._remove_middleware(mw)

        if not store.has_middleware(UndoMiddleware):
            _undo_mw = UndoMiddleware()
            store._add_middleware(_undo_mw)
            await _undo_mw.initialize(store)

        # Calling the pattern again must not double-install.
        if not store.has_middleware(UndoMiddleware):
            _undo_mw = UndoMiddleware()
            store._add_middleware(_undo_mw)
            await _undo_mw.initialize(store)

        installed = sum(1 for m in store._middleware if isinstance(m, UndoMiddleware))
        assert installed == 1

        for mw in list(store._middleware):
            if isinstance(mw, UndoMiddleware):
                store._remove_middleware(mw)
