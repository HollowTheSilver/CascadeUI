"""Tests for 7.7 — Action Batching / Transactions."""

import copy
import pytest

from cascadeui.state.store import StateStore
from cascadeui.state.singleton import get_store


class TestBatchContext:
    async def test_batch_produces_single_notification(self):
        """Two dispatches in a batch should produce one subscriber notification."""
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("batch-sub", handler)

        async def reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"].get("val", 0)
            return new

        store.register_reducer("SET_VAL", reducer)

        async with store.batch() as batch:
            await batch.dispatch("SET_VAL", {"val": 1})
            await batch.dispatch("SET_VAL", {"val": 2})

        # Should receive exactly one BATCH_COMPLETE, not two SET_VAL
        assert received == ["BATCH_COMPLETE"]

    async def test_state_reflects_both_actions(self):
        """State should reflect all batched actions after the batch exits."""
        store = get_store()

        async def set_a(action, state):
            new = copy.deepcopy(state)
            new["application"]["a"] = action["payload"]["val"]
            return new

        async def set_b(action, state):
            new = copy.deepcopy(state)
            new["application"]["b"] = action["payload"]["val"]
            return new

        store.register_reducer("SET_A", set_a)
        store.register_reducer("SET_B", set_b)

        async with store.batch() as batch:
            await batch.dispatch("SET_A", {"val": 10})
            await batch.dispatch("SET_B", {"val": 20})

        assert store.state["application"]["a"] == 10
        assert store.state["application"]["b"] == 20

    async def test_middleware_runs_per_action_in_batch(self):
        """Middleware should execute for each action within the batch."""
        store = get_store()
        mw_calls = []

        async def tracking_mw(action, state, next_fn):
            mw_calls.append(action["type"])
            return await next_fn(action, state)

        store.add_middleware(tracking_mw)

        async with store.batch() as batch:
            await batch.dispatch("X", {})
            await batch.dispatch("Y", {})

        assert "X" in mw_calls
        assert "Y" in mw_calls

    async def test_batch_action_filter_matching(self):
        """Subscriber with action filter should be notified if any batched action matches."""
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("filtered-batch", handler, action_filter={"SET_A"})

        async def reducer(action, state):
            return copy.deepcopy(state)

        store.register_reducer("SET_A", reducer)
        store.register_reducer("SET_B", reducer)

        async with store.batch() as batch:
            await batch.dispatch("SET_A", {})
            await batch.dispatch("SET_B", {})

        # Should be notified because SET_A was in the batch
        assert len(received) == 1

    async def test_batch_filter_no_match(self):
        """Subscriber with action filter should NOT be notified if no batched action matches."""
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("unmatched-batch", handler, action_filter={"WANTED"})

        async with store.batch() as batch:
            await batch.dispatch("UNWANTED_A", {})
            await batch.dispatch("UNWANTED_B", {})

        assert received == []

    async def test_empty_batch_no_notification(self):
        """An empty batch should not fire any notification."""
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("empty-batch", handler)

        async with store.batch() as batch:
            pass  # No dispatches

        assert received == []

    async def test_batch_state_flows_sequentially(self):
        """Each dispatch in a batch should see the state from the previous dispatch."""
        store = get_store()
        seen_values = []

        async def append_reducer(action, state):
            new = copy.deepcopy(state)
            items = new["application"].get("items", [])
            items.append(action["payload"]["item"])
            new["application"]["items"] = items
            return new

        store.register_reducer("ADD_ITEM", append_reducer)

        async with store.batch() as batch:
            await batch.dispatch("ADD_ITEM", {"item": "first"})
            seen_values.append(list(store.state["application"].get("items", [])))
            await batch.dispatch("ADD_ITEM", {"item": "second"})
            seen_values.append(list(store.state["application"].get("items", [])))

        # After first dispatch, should have ["first"]
        assert seen_values[0] == ["first"]
        # After second dispatch, should have ["first", "second"]
        assert seen_values[1] == ["first", "second"]
