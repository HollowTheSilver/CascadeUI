"""Tests for action batching and atomic dispatch transactions."""

import copy

import pytest

from cascadeui.state.singleton import get_store
from cascadeui.state.store import StateStore


class TestBatchContext:
    """Batched dispatches produce a single subscriber notification."""
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

        store._register_reducer("SET_VAL", reducer)

        async with store.batch() as batch:
            await store.dispatch("SET_VAL", {"val": 1})
            await store.dispatch("SET_VAL", {"val": 2})
        await store._flush_notifications()

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

        store._register_reducer("SET_A", set_a)
        store._register_reducer("SET_B", set_b)

        async with store.batch() as batch:
            await store.dispatch("SET_A", {"val": 10})
            await store.dispatch("SET_B", {"val": 20})

        assert store.state["application"]["a"] == 10
        assert store.state["application"]["b"] == 20

    async def test_middleware_runs_per_action_in_batch(self):
        """Middleware should execute for each action within the batch."""
        store = get_store()
        mw_calls = []

        async def tracking_mw(action, state, next_fn):
            mw_calls.append(action["type"])
            return await next_fn(action, state)

        store._add_middleware(tracking_mw)

        async with store.batch() as batch:
            await store.dispatch("X", {})
            await store.dispatch("Y", {})

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

        store._register_reducer("SET_A", reducer)
        store._register_reducer("SET_B", reducer)

        async with store.batch() as batch:
            await store.dispatch("SET_A", {})
            await store.dispatch("SET_B", {})
        await store._flush_notifications()

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
            await store.dispatch("UNWANTED_A", {})
            await store.dispatch("UNWANTED_B", {})

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

        store._register_reducer("ADD_ITEM", append_reducer)

        async with store.batch() as batch:
            await store.dispatch("ADD_ITEM", {"item": "first"})
            seen_values.append(list(store.state["application"].get("items", [])))
            await store.dispatch("ADD_ITEM", {"item": "second"})
            seen_values.append(list(store.state["application"].get("items", [])))

        # After first dispatch, should have ["first"]
        assert seen_values[0] == ["first"]
        # After second dispatch, should have ["first", "second"]
        assert seen_values[1] == ["first", "second"]


class TestTransitiveBatching:
    """store.dispatch() calls inside a batch block queue into the batch."""

    async def test_store_dispatch_is_batched_transitively(self):
        """Calling store.dispatch() inside ``async with store.batch()`` must
        not fire its own notification. Regression guard: store.dispatch()
        queues actions into the open batch instead of bypassing it.
        """
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("transitive-sub", handler)

        async with store.batch():
            # Direct store.dispatch() calls -- the path library helpers
            # (_register_state, update_session, view.dispatch) all take.
            await store.dispatch("INNER_A", {})
            await store.dispatch("INNER_B", {})
        await store._flush_notifications()

        # One BATCH_COMPLETE, not two INNER_A/INNER_B notifications.
        assert received == ["BATCH_COMPLETE"]

    async def test_mixed_batch_and_store_dispatch(self):
        """Mixing store.dispatch() (back-compat shim) and store.dispatch()
        in the same batch block should collapse into one notification.
        """
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("mixed-sub", handler)

        async with store.batch() as batch:
            await store.dispatch("SHIM_CALL", {})
            await store.dispatch("DIRECT_CALL", {})
        await store._flush_notifications()

        assert received == ["BATCH_COMPLETE"]

    async def test_batched_actions_appear_in_payload(self):
        """BATCH_COMPLETE payload lists every action queued in the batch,
        regardless of whether they came through store.dispatch() or the
        store.dispatch() transitive path.
        """
        store = get_store()
        captured = []

        async def handler(state, action):
            if action["type"] == "BATCH_COMPLETE":
                captured.append([a["type"] for a in action["payload"]["actions"]])

        store.subscribe("payload-sub", handler)

        async with store.batch() as batch:
            await store.dispatch("A", {})
            await store.dispatch("B", {})
            await store.dispatch("C", {})
        await store._flush_notifications()

        assert captured == [["A", "B", "C"]]

    async def test_nested_batches_absorb_into_outer(self):
        """An inner batch inside an outer batch fires no BATCH_COMPLETE of
        its own; all actions collapse into the outer's single notification.
        """
        store = get_store()
        received = []
        payloads = []

        async def handler(state, action):
            received.append(action["type"])
            if action["type"] == "BATCH_COMPLETE":
                payloads.append([a["type"] for a in action["payload"]["actions"]])

        store.subscribe("nested-sub", handler)

        async with store.batch():
            await store.dispatch("OUTER_1", {})
            async with store.batch():
                await store.dispatch("INNER_1", {})
                await store.dispatch("INNER_2", {})
            await store.dispatch("OUTER_2", {})
        await store._flush_notifications()

        assert received == ["BATCH_COMPLETE"]
        assert payloads == [["OUTER_1", "INNER_1", "INNER_2", "OUTER_2"]]

    async def test_exception_inside_batch_drops_queued_actions(self):
        """If an exception propagates out of a batch block, its queued
        actions are dropped (no BATCH_COMPLETE, no stale queue leaking
        into the next batch).
        """
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("error-sub", handler)

        with pytest.raises(RuntimeError):
            async with store.batch():
                await store.dispatch("QUEUED_A", {})
                raise RuntimeError("boom")
        await store._flush_notifications()

        assert received == []
        # Next batch must start clean -- the aborted batch's actions
        # must not leak forward.
        async with store.batch():
            await store.dispatch("CLEAN_A", {})
        await store._flush_notifications()

        assert received == ["BATCH_COMPLETE"]
        assert store._batched_actions == []

    async def test_batched_dispatch_skips_per_action_profiling(self):
        """Per-action samples are suppressed inside a batch; the whole batch
        produces one BATCH_COMPLETE sample at the outer __aexit__ so the
        coalesced notify/hooks cost stays observable.
        """
        store = get_store()
        store.clear_perf()
        store.enable_perf()
        try:
            async with store.batch():
                await store.dispatch("BATCHED_A", {})
                await store.dispatch("BATCHED_B", {})
            await store._flush_notifications()
            # No BATCHED_A / BATCHED_B samples -- per-action profiling is
            # skipped when _batch_depth > 0. Exactly one BATCH_COMPLETE
            # sample accounts for the whole batch.
            assert len(store._perf_samples) == 1
            sample = store._perf_samples[0]
            assert sample["action"] == "BATCH_COMPLETE"
            assert sample["batch_size"] == 2
        finally:
            store.disable_perf()


class TestLibraryInternalBatching:
    """The library's own pipelines wrap multi-dispatch sequences in batch()
    so subscribers see a single BATCH_COMPLETE rather than each action."""

    async def test_send_pipeline_batches_registration_dispatches(self):
        """send() batches SESSION_CREATED + VIEW_CREATED into one
        BATCH_COMPLETE. VIEW_UPDATED fires separately after the Discord
        send because it is outside the state-registration batch.
        """
        from helpers import make_interaction

        from cascadeui.views.layout import StatefulLayoutView

        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("send-batch-sub", handler, action_filter=None)

        interaction = make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        await view.send()
        await store._flush_notifications()

        # _send_pipeline batches SESSION_CREATED + VIEW_CREATED into
        # BATCH_COMPLETE, then dispatches VIEW_UPDATED separately -- 2
        # notifications, not 3.
        assert received.count("BATCH_COMPLETE") == 1
        assert "VIEW_UPDATED" in received

    async def test_navigate_to_collapses_four_dispatches(self):
        """_navigate_to issues NAVIGATION_PUSH + SESSION_CREATED +
        VIEW_CREATED + VIEW_DESTROYED. All four collapse into one
        BATCH_COMPLETE under the library's batch() wrap.
        """
        from helpers import make_interaction

        from cascadeui.views.layout import StatefulLayoutView

        class Source(StatefulLayoutView):
            pass

        class Target(StatefulLayoutView):
            pass

        store = get_store()
        interaction = make_interaction()
        source = Source(interaction=interaction)
        await source.send()

        # Drop pre-push notifications so the count is scoped to the
        # _navigate_to() call itself.
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("push-batch-sub", handler, action_filter=None)

        await source.push(Target, interaction=interaction)
        await store._flush_notifications()

        # Exactly one BATCH_COMPLETE, no raw NAVIGATION_PUSH /
        # SESSION_CREATED / VIEW_CREATED / VIEW_DESTROYED leaks.
        assert received.count("BATCH_COMPLETE") == 1
        for leaked in ("NAVIGATION_PUSH", "SESSION_CREATED", "VIEW_CREATED", "VIEW_DESTROYED"):
            assert leaked not in received, f"{leaked} leaked outside the batch"
