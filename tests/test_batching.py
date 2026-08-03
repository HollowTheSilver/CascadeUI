"""Tests for action batching and atomic dispatch transactions."""

import asyncio
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

    async def test_exception_inside_batch_announces_what_committed(self):
        """An abort announces its committed prefix and starts the next batch clean.

        Reducers run inline, so an entry is queued only once its state change
        has landed. Discarding the queue reported nothing while state had
        moved, which reads to every subscriber as the change never happening.
        """
        store = get_store()
        received = []
        payloads = []

        async def handler(state, action):
            received.append(action["type"])
            payloads.append([queued["type"] for queued in action["payload"]["actions"]])

        store.subscribe("error-sub", handler)

        with pytest.raises(RuntimeError):
            async with store.batch():
                await store.dispatch("QUEUED_A", {})
                raise RuntimeError("boom")
        await store._flush_notifications()

        assert received == ["BATCH_COMPLETE"]
        assert payloads == [["QUEUED_A"]]

        # Next batch must start clean -- the aborted batch's actions
        # must not leak forward.
        async with store.batch():
            await store.dispatch("CLEAN_A", {})
        await store._flush_notifications()

        assert received == ["BATCH_COMPLETE", "BATCH_COMPLETE"]
        assert payloads == [["QUEUED_A"], ["CLEAN_A"]]

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
            # skipped while a batch is open. Exactly one BATCH_COMPLETE
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
        from helpers import RenderableLayoutView, make_interaction

        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("send-batch-sub", handler, action_filter=None)

        interaction = make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        await view.send()
        await store._flush_notifications()

        # _send_pipeline batches SESSION_CREATED + VIEW_CREATED into
        # BATCH_COMPLETE, then dispatches VIEW_UPDATED separately -- 2
        # notifications, not 3.
        assert received.count("BATCH_COMPLETE") == 1
        assert "VIEW_UPDATED" in received

    async def test_navigate_to_batches_construction_then_commits_teardown(self):
        """push() batches its construction dispatches (NAVIGATION_PUSH +
        SESSION_CREATED + VIEW_CREATED) into one BATCH_COMPLETE. The source's
        VIEW_DESTROYED commits separately afterward, in _commit_source_teardown,
        once the destination edit confirms -- the deferral that lets a failed
        edit roll back to a live source.
        """
        from helpers import RenderableLayoutView, make_interaction

        class Source(RenderableLayoutView):
            pass

        class Target(RenderableLayoutView):
            pass

        store = get_store()
        interaction = make_interaction()
        source = Source(interaction=interaction)
        await source.send()

        # Drop pre-push notifications so the count is scoped to the
        # push() call itself.
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("push-batch-sub", handler, action_filter=None)

        await source.push(Target, interaction=interaction)
        await store._flush_notifications()

        # The construction dispatches collapse into one BATCH_COMPLETE with no
        # raw leaks; VIEW_DESTROYED arrives separately as the post-edit
        # source-teardown commit.
        assert received.count("BATCH_COMPLETE") == 1
        for leaked in ("NAVIGATION_PUSH", "SESSION_CREATED", "VIEW_CREATED"):
            assert leaked not in received, f"{leaked} leaked outside the batch"
        assert "VIEW_DESTROYED" in received


class TestConcurrentBatchesAreIndependent:
    """Batch membership follows the task, not the store.

    On one shared depth counter two concurrent batches read as a single
    nested batch: the second never flushed its own, and whichever exited
    last flushed both under one source_id.
    """

    async def _watch(self, store):
        seen = []

        async def handler(state, action):
            if action["type"] == "BATCH_COMPLETE":
                seen.append(
                    (
                        action.get("source"),
                        tuple(queued["payload"]["n"] for queued in action["payload"]["actions"]),
                    )
                )
            else:
                seen.append((action["type"], action["payload"].get("n")))

        store.subscribe("watcher", handler)
        return seen

    async def _batched(self, store, name):
        async with store.batch(source_id=name):
            # Yield first so both batches are open before either dispatches.
            # Dispatching on entry lets the tasks interleave into a legal
            # order by luck, and the assertion then holds without proving
            # anything about isolation.
            await asyncio.sleep(0.01)
            await store.dispatch("X", {"n": name}, source_id=name)

    async def test_each_task_flushes_its_own_batch(self):
        store = get_store()
        seen = await self._watch(store)

        await asyncio.gather(self._batched(store, "A"), self._batched(store, "B"))
        await store._flush_notifications()

        assert sorted(seen) == [("A", ("A",)), ("B", ("B",))]

    async def test_nesting_in_one_task_still_absorbs(self):
        store = get_store()
        seen = await self._watch(store)

        async with store.batch(source_id="outer"):
            await store.dispatch("X", {"n": "o"}, source_id="outer")
            async with store.batch():
                await store.dispatch("X", {"n": "i"}, source_id="outer")
        await store._flush_notifications()

        assert seen == [("outer", ("o", "i"))]

    async def test_a_foreign_batch_does_not_absorb_a_background_dispatch(self):
        store = get_store()
        seen = await self._watch(store)

        async def background():
            await asyncio.sleep(0.005)
            await store.dispatch("UNRELATED", {"n": "bg"})

        async def holder():
            async with store.batch(source_id="H"):
                await store.dispatch("X", {"n": "h"}, source_id="H")
                await asyncio.sleep(0.02)

        await asyncio.gather(holder(), background())
        await store._flush_notifications()

        # Notified under its own type, not folded into the holder's payload.
        assert ("UNRELATED", "bg") in seen
        assert ("H", ("h",)) in seen

    async def test_a_foreign_abort_does_not_discard_a_background_dispatch(self):
        store = get_store()
        seen = await self._watch(store)

        async def background():
            await asyncio.sleep(0.005)
            await store.dispatch("UNRELATED", {"n": "bg"})

        async def holder():
            with pytest.raises(RuntimeError):
                async with store.batch(source_id="H"):
                    await store.dispatch("X", {"n": "h"}, source_id="H")
                    await asyncio.sleep(0.02)
                    raise RuntimeError("boom")

        await asyncio.gather(holder(), background())
        await store._flush_notifications()

        # The reducer ran, so dropping the notification would desync every
        # subscriber from state that had already changed -- for the holder's
        # own committed prefix as much as for the foreign dispatch.
        assert ("UNRELATED", "bg") in seen
        assert ("H", ("h",)) in seen

    async def test_a_spawned_task_joins_the_nearest_open_ancestor(self):
        store = get_store()
        seen = await self._watch(store)
        spawned = None

        async def child():
            # Dispatches once the inner batch has closed and the outer has not.
            await asyncio.sleep(0.03)
            await store.dispatch("X", {"n": "child"}, source_id="outer")

        async with store.batch(source_id="outer"):
            await store.dispatch("X", {"n": "o"}, source_id="outer")
            async with store.batch():
                spawned = asyncio.create_task(child())
                await store.dispatch("X", {"n": "i"}, source_id="outer")
            await asyncio.sleep(0.05)
        await spawned
        await store._flush_notifications()

        # Not unbatched: the innermost entry is closed but an ancestor is open.
        assert seen == [("outer", ("o", "i", "child"))]

    async def test_a_batch_closed_under_another_context_does_not_raise(self):
        store = get_store()

        async def generator():
            async with store.batch():
                await store.dispatch("X", {"n": "g"})
                yield 1

        agen = generator()
        await agen.__anext__()
        # The event loop's own async-generator shutdown finalizes from a
        # different task, where the entry token does not belong.
        await asyncio.create_task(agen.aclose())

        async with store.batch():
            await store.dispatch("X", {"n": "after"})

    async def test_a_batch_context_cannot_be_re_entered(self):
        store = get_store()
        batch = store.batch()

        async with batch:
            pass

        with pytest.raises(RuntimeError, match="cannot be re-entered"):
            async with batch:
                pass

    async def test_a_dispatch_outliving_its_batch_still_notifies(self):
        """An entry is queued after the chain runs, so a suspending chain can
        outlive the batch it started in.

        Appending to a drained buffer would leave a committed state change
        with no subscriber ever told -- the shape this whole redesign exists
        to remove, reached by a different route.
        """
        store = get_store()
        seen = await self._watch(store)

        async def slow_middleware(action, state, next_fn):
            if action["type"] == "LATE":
                await asyncio.sleep(0.02)
            return await next_fn(action, state)

        store._add_middleware(slow_middleware)

        async def late_dispatcher():
            await asyncio.sleep(0.005)
            await store.dispatch("LATE", {"n": "late"})

        try:
            async with store.batch(source_id="B"):
                await store.dispatch("X", {"n": "early"}, source_id="B")
                spawned = asyncio.create_task(late_dispatcher())
                # The batch closes while LATE is still inside the chain.
                await asyncio.sleep(0.01)
            await spawned
            await store._flush_notifications()
        finally:
            store._remove_middleware(slow_middleware)

        assert ("B", ("early",)) in seen
        assert ("LATE", "late") in seen
