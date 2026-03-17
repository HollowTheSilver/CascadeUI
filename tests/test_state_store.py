"""Tests for the StateStore dispatch, subscribe, and reducer system."""

import copy
import pytest

from cascadeui.state.store import StateStore
from cascadeui.state.singleton import get_store


class TestStateStoreSingleton:
    def test_returns_same_instance(self):
        a = StateStore()
        b = StateStore()
        assert a is b

    def test_get_store_returns_singleton(self):
        store = get_store()
        assert isinstance(store, StateStore)
        assert store is get_store()

    def test_initial_state_has_required_keys(self):
        store = get_store()
        assert "sessions" in store.state
        assert "views" in store.state
        assert "components" in store.state
        assert "application" in store.state


class TestDispatch:
    async def test_dispatch_with_registered_reducer(self):
        store = get_store()

        async def my_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["test"] = action["payload"]["value"]
            return new

        store.register_reducer("TEST_ACTION", my_reducer)
        result = await store.dispatch("TEST_ACTION", {"value": 42})

        assert result["application"]["test"] == 42

    async def test_dispatch_unknown_action_does_not_crash(self):
        store = get_store()
        result = await store.dispatch("NONEXISTENT_ACTION", {"foo": "bar"})
        assert result is not None

    async def test_dispatch_records_history(self):
        store = get_store()
        await store.dispatch("SOME_ACTION", {"data": 1})
        assert len(store.history) == 1
        assert store.history[0]["type"] == "SOME_ACTION"

    async def test_history_is_capped(self):
        store = get_store()
        store.history_limit = 5

        for i in range(10):
            await store.dispatch("FILL_ACTION", {"i": i})

        assert len(store.history) == 5


class TestSubscribers:
    async def test_subscriber_receives_notification(self):
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("test-sub", handler)
        await store.dispatch("PING", {})

        assert "PING" in received

    async def test_action_filter_blocks_unmatched(self):
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("filtered-sub", handler, action_filter={"WANTED"})
        await store.dispatch("UNWANTED", {})
        await store.dispatch("WANTED", {})

        assert received == ["WANTED"]

    async def test_unsubscribe_stops_notifications(self):
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("temp-sub", handler)
        await store.dispatch("BEFORE", {})
        store.unsubscribe("temp-sub")
        await store.dispatch("AFTER", {})

        assert received == ["BEFORE"]

    async def test_subscriber_with_no_filter_gets_everything(self):
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("all-sub", handler, action_filter=None)
        await store.dispatch("A", {})
        await store.dispatch("B", {})

        assert received == ["A", "B"]


class TestSelectors:
    async def test_selector_skips_unchanged(self):
        """Subscriber with selector should NOT be notified when selected value stays the same."""
        store = get_store()
        received = []

        async def counter_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["counter"] = action["payload"]["value"]
            return new

        store.register_reducer("SET_COUNTER", counter_reducer)

        async def handler(state, action):
            received.append(action["type"])

        selector = lambda state: state.get("application", {}).get("counter")
        store.subscribe("selector-sub", handler, selector=selector)

        await store.dispatch("SET_COUNTER", {"value": 5})
        assert len(received) == 1

        # Same value again — should be skipped
        await store.dispatch("SET_COUNTER", {"value": 5})
        assert len(received) == 1

    async def test_selector_notifies_on_change(self):
        """Subscriber with selector should be notified when selected value changes."""
        store = get_store()
        received = []

        async def counter_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["counter"] = action["payload"]["value"]
            return new

        store.register_reducer("SET_COUNTER", counter_reducer)

        async def handler(state, action):
            received.append(state["application"]["counter"])

        selector = lambda state: state.get("application", {}).get("counter")
        store.subscribe("selector-sub", handler, selector=selector)

        await store.dispatch("SET_COUNTER", {"value": 1})
        await store.dispatch("SET_COUNTER", {"value": 2})
        await store.dispatch("SET_COUNTER", {"value": 3})

        assert received == [1, 2, 3]

    async def test_selector_with_action_filter(self):
        """Selector and action filter work together — both must pass."""
        store = get_store()
        received = []

        async def reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"]["val"]
            return new

        store.register_reducer("A", reducer)
        store.register_reducer("B", reducer)

        async def handler(state, action):
            received.append(action["type"])

        selector = lambda state: state.get("application", {}).get("val")
        store.subscribe("combo-sub", handler, action_filter={"A"}, selector=selector)

        # B is filtered by action_filter
        await store.dispatch("B", {"val": 10})
        assert len(received) == 0

        # A passes action filter, selector value is new
        await store.dispatch("A", {"val": 10})
        assert len(received) == 1

        # A passes action filter, but selector value hasn't changed
        await store.dispatch("A", {"val": 10})
        assert len(received) == 1

    async def test_no_selector_always_notifies(self):
        """Without a selector, subscriber receives every matching action."""
        store = get_store()
        received = []

        async def noop_reducer(action, state):
            return copy.deepcopy(state)

        store.register_reducer("NOOP", noop_reducer)

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("no-sel-sub", handler)

        await store.dispatch("NOOP", {})
        await store.dispatch("NOOP", {})
        await store.dispatch("NOOP", {})

        assert len(received) == 3

    async def test_unsubscribe_cleans_up_selector_memo(self):
        """Unsubscribing should remove memoized selector values."""
        store = get_store()

        selector = lambda state: state.get("application", {}).get("x")
        store.subscribe("memo-sub", lambda s, a: None, selector=selector)

        # Force a value into the memo
        store._last_selected["memo-sub"] = 42
        store.unsubscribe("memo-sub")

        assert "memo-sub" not in store._last_selected


class TestReducers:
    async def test_register_and_unregister_reducer(self):
        store = get_store()

        async def temp_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["temp"] = True
            return new

        store.register_reducer("TEMP", temp_reducer)
        assert "TEMP" in store.reducers

        store.unregister_reducer("TEMP")
        assert "TEMP" not in store.reducers

    async def test_custom_reducer_overrides_core(self):
        store = get_store()
        store._load_core_reducers()

        async def custom_view_created(action, state):
            new = copy.deepcopy(state)
            new["application"]["custom_fired"] = True
            return new

        store.register_reducer("VIEW_CREATED", custom_view_created)
        await store.dispatch("VIEW_CREATED", {"view_id": "test"})

        assert store.state["application"].get("custom_fired") is True
