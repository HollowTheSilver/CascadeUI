"""Tests for the middleware system."""

import asyncio
import copy
import json
from datetime import datetime

import pytest

from cascadeui import get_store
from cascadeui.persistence import InMemoryBackend
from cascadeui.persistence.schema import TABLE_APPLICATION_SLOTS, TABLE_PERSISTENT_VIEWS
from cascadeui.setup import setup_middleware
from cascadeui.state import slots as _slots_module
from cascadeui.state.middleware.persistence import PersistenceMiddleware
from cascadeui.state.slots import access_slot


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

    @pytest.mark.parametrize("path", ["plain", "batched", "profiled"])
    async def test_a_blocked_action_is_not_announced(self, path):
        """Blocked by a middleware returning without next_fn, the action was
        still announced: its hook ran (posting a result for something that
        never happened) and subscribers were told."""
        store = get_store()
        hooked, told = [], []

        async def admins_only(action, state, next_fn):
            if action["type"] == "RESET_SCORES":
                return state
            return await next_fn(action, state)

        async def reset(action, state):
            return {**state, "application": {**state["application"], "scores": {}}}

        async def audit(action, state):
            hooked.append(action["type"])

        async def board(state, action):
            told.append(action["type"])

        store._register_reducer("RESET_SCORES", reset)
        store._add_middleware(admins_only)
        store.on("RESET_SCORES", audit)
        store.subscribe("board", board)
        if path == "profiled":
            store.enable_perf()
        try:
            if path == "batched":
                async with store.batch():
                    await store.dispatch("RESET_SCORES")
            else:
                await store.dispatch("RESET_SCORES")
            await store._flush_notifications()
        finally:
            store._remove_middleware(admins_only)

        assert hooked == []
        assert told == []

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


class TestMiddlewareThatAwaits:
    """A middleware may await before passing the action on (an audit row
    written first). The chain handed the reducer the state it read before
    that await, so a write another dispatch committed meanwhile was undone."""

    @staticmethod
    def _setter(key):
        async def reducer(action, state):
            state = {**state, "application": {**state.get("application", {}), key: 1}}
            return state

        return reducer

    async def test_a_concurrent_dispatchs_write_is_kept(self):
        store = get_store()
        release = asyncio.Event()

        async def audit(action, state, next_fn):
            if action["type"] == "SET_A":
                await release.wait()
            return await next_fn(action, state)

        store._add_middleware(audit)
        store._register_reducer("SET_A", self._setter("a"))
        store._register_reducer("SET_B", self._setter("b"))
        first = asyncio.ensure_future(store.dispatch("SET_A", {}))
        try:
            await asyncio.sleep(0)
            await store.dispatch("SET_B", {})
        finally:
            release.set()
            await first
        assert store.state["application"] == {"a": 1, "b": 1}

    async def test_a_declining_reducer_keeps_a_write_committed_meanwhile(self):
        # A reducer declines by returning the mapping it was handed. Behind a
        # middleware that rebuilt that mapping and another that awaited, it
        # predates the other dispatch's commit, and adopting it undid that.
        store = get_store()
        waiting, release = asyncio.Event(), asyncio.Event()

        async def rebuild(action, state, next_fn):
            return await next_fn(action, {**state})

        async def slow(action, state, next_fn):
            if action["type"] == "MAYBE":
                waiting.set()
                await release.wait()
            return await next_fn(action, state)

        async def decline(action, state):
            return state

        store._add_middleware(rebuild)
        store._add_middleware(slow)
        store._register_reducer("MAYBE", decline)
        store._register_reducer("SET_B", self._setter("b"))
        first = asyncio.ensure_future(store.dispatch("MAYBE", {}))
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            await store.dispatch("SET_B", {})
        finally:
            release.set()
            await first
            store._remove_middleware(slow)
            store._remove_middleware(rebuild)
        assert store.state["application"].get("b") == 1

    async def test_the_next_middleware_is_handed_the_current_state(self):
        # A middleware that compares what it is handed with what the reducer
        # returns (undo, persistence) otherwise counts another dispatch's
        # write as this action's.
        store = get_store()
        release = asyncio.Event()
        seen = []

        async def audit(action, state, next_fn):
            if action["type"] == "SET_A":
                await release.wait()
            return await next_fn(action, state)

        async def observe(action, state, next_fn):
            if action["type"] == "SET_A":
                seen.append(dict(state.get("application", {})))
            return await next_fn(action, state)

        store._add_middleware(audit)
        store._add_middleware(observe)
        store._register_reducer("SET_A", self._setter("a"))
        store._register_reducer("SET_B", self._setter("b"))
        first = asyncio.ensure_future(store.dispatch("SET_A", {}))
        try:
            await asyncio.sleep(0)
            await store.dispatch("SET_B", {})
        finally:
            release.set()
            await first
        assert seen == [{"b": 1}]

    @pytest.mark.parametrize("followed", [False, True])
    async def test_a_middleware_that_replaces_the_state_has_its_state_reduced(self, followed):
        # Followed by another middleware, the mapping it built was replaced
        # with the store's state before the reducer saw it.
        store = get_store()

        async def stamp(action, state, next_fn):
            return await next_fn(action, {**state, "stamped": True})

        async def passthrough(action, state, next_fn):
            return await next_fn(action, state)

        store._add_middleware(stamp)
        if followed:
            store._add_middleware(passthrough)
        store._register_reducer("SET_A", self._setter("a"))
        await store.dispatch("SET_A", {})
        assert store.state["stamped"] is True


class TestAReducerThatAwaits:
    """Reducers run one at a time. A reducer that awaits (a database call,
    say) returned the state it read before its await, over whatever another
    dispatch committed meanwhile, and the other write was lost."""

    @staticmethod
    def _holding(store, action_type, key):
        """Register a reducer that awaits ``release``; return (started, release)."""
        started, release = asyncio.Event(), asyncio.Event()

        async def reducer(action, state):
            started.set()
            await release.wait()
            return {**state, "application": {**state.get("application", {}), key: 1}}

        store._register_reducer(action_type, reducer)
        return started, release

    @staticmethod
    def _setter(key):
        async def reducer(action, state):
            return {**state, "application": {**state.get("application", {}), key: 1}}

        return reducer

    @staticmethod
    def _snapshotting(store, action_type, key):
        """Like ``_holding``, but copying the state before the await, as
        ``@cascade_reducer`` does; one reading it after would see a write
        made in place meanwhile, so could not tell whether it waited."""
        started, release = asyncio.Event(), asyncio.Event()

        async def reducer(action, state):
            state = copy.deepcopy(state)
            started.set()
            await release.wait()
            state.setdefault("application", {})[key] = 1
            return state

        store._register_reducer(action_type, reducer)
        return started, release

    async def test_a_concurrent_dispatchs_write_is_kept(self):
        store = get_store()
        started, release = self._holding(store, "SET_SLOW", "slow")
        store._register_reducer("SET_FAST", self._setter("fast"))
        slow = asyncio.ensure_future(store.dispatch("SET_SLOW", {}))
        await asyncio.wait_for(started.wait(), 2)
        fast = asyncio.ensure_future(store.dispatch("SET_FAST", {}))
        await asyncio.sleep(0.01)
        try:
            assert not fast.done()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(slow, fast), 2)
        assert store.state["application"] == {"slow": 1, "fast": 1}

    async def test_a_dispatch_from_inside_a_reducer_is_refused(self, caplog):
        # Waiting for the turn this task holds would never end.
        store = get_store()

        async def outer(action, state):
            await store.dispatch("INNER", {})
            return {**state, "application": {**state.get("application", {}), "outer": 1}}

        store._register_reducer("OUTER", outer)
        store._register_reducer("INNER", self._setter("inner"))
        with caplog.at_level("ERROR", logger="cascadeui"):
            await asyncio.wait_for(store.dispatch("OUTER", {}), 2)
        assert store.state["application"] == {}
        assert any(
            "dispatch('INNER') was called from inside the reducer for 'OUTER'" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    async def test_a_long_wait_names_the_reducer_holding_the_turn(self, monkeypatch, caplog):
        import cascadeui.state.store as store_module

        monkeypatch.setattr(store_module, "_STATE_TURN_WAIT_WARN_SECONDS", 0.01)
        store = get_store()
        started, release = self._holding(store, "SET_SLOW", "slow")
        store._register_reducer("SET_FAST", self._setter("fast"))
        slow = asyncio.ensure_future(store.dispatch("SET_SLOW", {}))
        await asyncio.wait_for(started.wait(), 2)
        with caplog.at_level("WARNING", logger="cascadeui"):
            fast = asyncio.ensure_future(store.dispatch("SET_FAST", {}))
            try:
                await asyncio.sleep(0.05)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(slow, fast), 2)
        assert any(
            "the reducer for 'SET_FAST' has waited" in r.getMessage()
            and "for the reducer for 'SET_SLOW' to return" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    async def test_a_wait_that_ends_in_time_logs_nothing(self, monkeypatch, caplog):
        import cascadeui.state.store as store_module

        monkeypatch.setattr(store_module, "_STATE_TURN_WAIT_WARN_SECONDS", 0.05)
        store = get_store()
        started, release = self._holding(store, "SET_SLOW", "slow")
        store._register_reducer("SET_FAST", self._setter("fast"))
        slow = asyncio.ensure_future(store.dispatch("SET_SLOW", {}))
        await asyncio.wait_for(started.wait(), 2)
        with caplog.at_level("WARNING", logger="cascadeui"):
            fast = asyncio.ensure_future(store.dispatch("SET_FAST", {}))
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(slow, fast), 2)
            await asyncio.sleep(0.1)
        assert not [r for r in caplog.records if r.name.startswith("cascadeui")]

    def test_a_store_used_by_two_loops_takes_turns_in_each(self):
        # Each asyncio.run is a new loop, and a lock a waiter touched in the
        # first refuses the second.
        store = get_store()
        events = {}

        async def slow(action, state):
            events["started"].set()
            await events["release"].wait()
            return {**state, "application": {**state.get("application", {}), "slow": 1}}

        store._register_reducer("SET_SLOW", slow)
        store._register_reducer("SET_FAST", self._setter("fast"))

        async def contend():
            events["started"], events["release"] = asyncio.Event(), asyncio.Event()
            first = asyncio.ensure_future(store.dispatch("SET_SLOW", {}))
            await asyncio.wait_for(events["started"].wait(), 2)
            second = asyncio.ensure_future(store.dispatch("SET_FAST", {}))
            await asyncio.sleep(0)
            events["release"].set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)

        asyncio.run(contend())
        asyncio.run(contend())
        assert store.state["application"] == {"slow": 1, "fast": 1}

    async def test_a_dispatch_from_a_task_the_reducer_started_is_refused(self):
        # The task's dispatch waited for the reducer while the reducer awaited
        # the task, and neither returned.
        store = get_store()
        refused = []

        async def outer(action, state):
            try:
                await asyncio.gather(store.dispatch("INNER", {}))
            except RuntimeError as e:
                refused.append(str(e))
            return {**state, "application": {**state.get("application", {}), "outer": 1}}

        store._register_reducer("OUTER", outer)
        store._register_reducer("INNER", self._setter("inner"))
        await asyncio.wait_for(store.dispatch("OUTER", {}), 2)

        assert store.state["application"] == {"outer": 1}
        assert len(refused) == 1
        assert "from a task started inside the reducer for 'OUTER'" in refused[0]

    async def test_a_task_a_reducer_started_dispatches_once_that_reducer_returned(self):
        # Refusing by the task rather than the turn would also refuse it while
        # a later reducer of the same task runs.
        store = get_store()
        go, release = asyncio.Event(), asyncio.Event()
        spawned = []

        async def later():
            await go.wait()
            await store.dispatch("INNER", {})

        async def outer(action, state):
            spawned.append(asyncio.ensure_future(later()))
            return {**state, "application": {**state.get("application", {}), "outer": 1}}

        async def hold(action, state):
            go.set()
            await release.wait()
            return {**state, "application": {**state.get("application", {}), "hold": 1}}

        store._register_reducer("OUTER", outer)
        store._register_reducer("HOLD", hold)
        store._register_reducer("INNER", self._setter("inner"))

        async def same_task():
            await store.dispatch("OUTER", {})
            asyncio.get_running_loop().call_later(0.05, release.set)
            await store.dispatch("HOLD", {})

        # Both dispatches in one task, bounded from outside it: before 3.12,
        # wait_for would run each in a task of its own.
        runner = asyncio.ensure_future(same_task())
        done, _ = await asyncio.wait({runner}, timeout=2)
        assert runner in done
        runner.result()
        await asyncio.wait_for(spawned[0], 2)

        assert store.state["application"] == {"outer": 1, "hold": 1, "inner": 1}

    async def test_a_wait_that_starts_as_the_turn_changes_hands_is_warned(
        self, monkeypatch, caplog
    ):
        # A released turn reads free until the waiter it woke takes it, and a
        # dispatch arriving then waited behind that waiter with no warning.
        import cascadeui.state.store as store_module

        monkeypatch.setattr(store_module, "_STATE_TURN_WAIT_WARN_SECONDS", 0.05)
        store = get_store()
        started, release = self._holding(store, "SET_SLOW", "slow")
        never = asyncio.Event()

        async def hang(action, state):
            await never.wait()
            return state

        store._register_reducer("HANG", hang)
        store._register_reducer("SET_FAST", self._setter("fast"))

        async def slow_then_fast():
            await store.dispatch("SET_SLOW", {})
            await store.dispatch("SET_FAST", {})

        first = asyncio.ensure_future(slow_then_fast())
        await asyncio.wait_for(started.wait(), 2)
        hanging = asyncio.ensure_future(store.dispatch("HANG", {}))
        await asyncio.sleep(0.01)
        try:
            with caplog.at_level("WARNING", logger="cascadeui"):
                release.set()
                await asyncio.sleep(0.2)
            assert any(
                "the reducer for 'SET_FAST' has waited" in r.getMessage()
                for r in caplog.records
                if r.name.startswith("cascadeui")
            )
        finally:
            never.set()
            await asyncio.wait_for(asyncio.gather(first, hanging), 2)

    async def test_set_scoped_during_an_awaiting_reducer_is_kept(self):
        store = get_store()
        started, release = self._snapshotting(store, "SET_SLOW", "slow")
        slow = asyncio.ensure_future(store.dispatch("SET_SLOW", {}))
        await asyncio.wait_for(started.wait(), 2)
        store.set_scoped("user", {"theme": "dark"}, user_id=1)
        release.set()
        await asyncio.wait_for(slow, 2)

        assert store.get_scoped("user", user_id=1) == {"theme": "dark"}
        assert store.state["application"]["slow"] == 1

    def test_set_scoped_outside_a_running_loop_writes_at_once(self):
        store = get_store()
        store.set_scoped("user", {"theme": "dark"}, user_id=1)

        assert store.get_scoped("user", user_id=1) == {"theme": "dark"}

    async def test_slots_restored_during_an_awaiting_reducer_are_kept(self):
        # The restore wrote into the live state while a reducer awaited, and
        # that reducer's result, read before it, dropped the restored slot.
        store = get_store()
        persistence = PersistenceMiddleware(backend=InMemoryBackend())
        await setup_middleware(persistence)
        try:
            access_slot(store.state, "restored_during", persistent=True)

            async def save(action, state):
                app = state.get("application", {})
                return {**state, "application": {**app, "restored_during": {"x": 1}}}

            store._register_reducer("SAVE", save)
            await store.dispatch("SAVE", {})
            await persistence.flush_all()
            application = dict(store.state["application"])
            del application["restored_during"]
            store.state = {**store.state, "application": application}
            started, release = self._snapshotting(store, "SET_SLOW", "slow")
            slow = asyncio.ensure_future(store.dispatch("SET_SLOW", {}))
            await asyncio.wait_for(started.wait(), 2)
            restoring = asyncio.ensure_future(store.persistence_manager._rehydrate_application())
            await asyncio.sleep(0.01)
            release.set()
            await asyncio.wait_for(asyncio.gather(slow, restoring), 2)

            assert store.state["application"]["restored_during"] == {"x": 1}
            assert store.state["application"]["slow"] == 1
        finally:
            _slots_module._PERSISTENT_SLOTS.discard("restored_during")
            await persistence.close()

    async def test_registry_rows_restored_during_an_awaiting_reducer_are_kept(self):
        # Seeded in place, the rows vanished from the mirror when that reducer
        # committed, so the panels' exits later retired nothing.
        store = get_store()
        backend = InMemoryBackend()
        persistence = PersistenceMiddleware(backend=backend)
        await setup_middleware(persistence)
        try:
            await backend.row_upsert(
                TABLE_PERSISTENT_VIEWS,
                {"persistence_key": "restored-panel", "view_class": "Panel", "message_id": 7},
                ["persistence_key"],
            )
            started, release = self._snapshotting(store, "SET_SLOW", "slow")
            slow = asyncio.ensure_future(store.dispatch("SET_SLOW", {}))
            await asyncio.wait_for(started.wait(), 2)
            restoring = asyncio.ensure_future(store.persistence_manager._rehydrate_registry())
            await asyncio.sleep(0.01)
            release.set()
            await asyncio.wait_for(asyncio.gather(slow, restoring), 2)

            assert store.state["persistent_views"]["restored-panel"]["message_id"] == "7"
            assert store.state["application"]["slow"] == 1
        finally:
            await persistence.close()

    def test_a_turn_left_on_a_closed_loop_holds_nothing(self, caplog):
        # A reducer suspended when its loop closed, never cancelled, still read
        # as holding the turn: writes queued behind it, and collecting it later
        # gave back the next loop's turn from under its holder.
        caplog.set_level("ERROR", logger="cascadeui")
        store = get_store()

        async def stuck(action, state):
            await asyncio.Event().wait()
            return state

        store._register_reducer("STUCK", stuck)
        store._register_reducer("SET_FAST", self._setter("fast"))
        dead_loop = asyncio.new_event_loop()
        dead = dead_loop.create_task(store.dispatch("STUCK", {}))
        dead_loop.run_until_complete(asyncio.sleep(0.01))
        dead_loop.close()
        wrote, landed, live = [], [], []

        async def next_run():
            store._write_when_free(lambda: wrote.append("at once"))
            landed.extend(wrote)
            started, release = self._holding(store, "SET_SLOW", "slow")
            slow = asyncio.ensure_future(store.dispatch("SET_SLOW", {}))
            await asyncio.wait_for(started.wait(), 2)
            holder = store._state_holder
            # What collecting it does. The dispatch's other context resets
            # refuse a close from this context and raise from it.
            try:
                dead.get_coro().close()
            except ValueError:
                pass
            live.append(store._state_holder is holder)
            release.set()
            await asyncio.wait_for(slow, 2)
            await store.dispatch("SET_FAST", {})

        asyncio.run(next_run())

        assert landed == ["at once"]
        assert live == [True]
        assert store.state["application"] == {"slow": 1, "fast": 1}
        # Closed, the reducer stopped: a close taken for a reducer error would
        # have run the rest of its dispatch.
        assert not [r for r in caplog.records if "Error in reducer" in r.getMessage()]

    async def test_views_a_bot_close_released_are_not_written_back(self):
        # The drop at the close landed while a reducer awaited, and its result,
        # read before the drop, put the released view back.
        store = get_store()
        store.state = {**store.state, "views": {**store.state["views"], "gone": {"id": "gone"}}}
        started, release = self._holding(store, "SET_SLOW", "slow")
        slow = asyncio.ensure_future(store.dispatch("SET_SLOW", {}))
        await asyncio.wait_for(started.wait(), 2)
        store._released_ids.add("gone")
        store._drop_released()
        release.set()
        await asyncio.wait_for(slow, 2)
        assert "gone" not in store.state["views"]
        assert store.state["application"]["slow"] == 1


class TestMiddlewareThatRaisesAfterNextFn:
    """A middleware that raised after ``next_fn`` returned left the change the
    reducer had committed unannounced and unsaved: memory held it, while
    subscribers, hooks, and disk did not."""

    async def test_the_change_is_announced_and_saved_before_the_error(self):
        store = get_store()
        backend = InMemoryBackend()
        persistence = PersistenceMiddleware(backend=backend)

        async def analytics(action, state, next_fn):
            result = await next_fn(action, state)
            if action["type"] == "PURCHASE":
                raise ConnectionError("analytics down")
            return result

        async def purchase(action, state):
            application = {**state.get("application", {}), "orders": {"o1": 5}}
            return {**state, "application": application}

        told, hooked = [], []
        try:
            await setup_middleware(persistence, analytics)
            access_slot(store.state, "orders", persistent=True)
            store._register_reducer("PURCHASE", purchase)

            async def subscriber(state, action=None):
                told.append(action["type"] if action else None)

            store.subscribe("buyer", subscriber, action_filter={"PURCHASE"})
            store.on("PURCHASE", lambda action, *rest: hooked.append(action["type"]))
            with pytest.raises(ConnectionError, match="analytics down"):
                await store.dispatch("PURCHASE", {})
            await store._flush_notifications()
            await persistence.flush_all()
            assert store.state["application"]["orders"] == {"o1": 5}
            assert told == ["PURCHASE"]
            assert hooked == ["PURCHASE"]
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS, {"slot_name": "orders"})
            assert json.loads(rows[0]["payload"]) == {"o1": 5}
        finally:
            _slots_module._PERSISTENT_SLOTS.discard("orders")
            await persistence.close()

    async def test_an_error_before_the_reducer_announces_nothing(self):
        store = get_store()
        hooked = []

        async def refuse(action, state, next_fn):
            raise ValueError("refused")

        async def change(action, state):
            return {**state, "application": {**state.get("application", {}), "x": 1}}

        store._add_middleware(refuse)
        store._register_reducer("CHANGE", change)
        store.on("CHANGE", lambda action, *rest: hooked.append(1))
        with pytest.raises(ValueError, match="refused"):
            await store.dispatch("CHANGE", {})
        assert "x" not in store.state["application"]
        assert hooked == []

    async def test_inside_a_batch_the_error_reaches_the_caller(self):
        # Inside a batch the error was dropped, so the caller went on as if
        # the middleware had succeeded.
        store = get_store()

        async def analytics(action, state, next_fn):
            result = await next_fn(action, state)
            if action["type"] == "CHANGE":
                raise ConnectionError("analytics down")
            return result

        async def change(action, state):
            return {**state, "application": {**state.get("application", {}), "x": 1}}

        store._add_middleware(analytics)
        store._register_reducer("CHANGE", change)
        announced = []
        store.on(
            "BATCH_COMPLETE",
            lambda action, *rest: announced.append(
                [a["type"] for a in action["payload"]["actions"]]
            ),
        )
        with pytest.raises(ConnectionError, match="analytics down"):
            async with store.batch():
                await store.dispatch("CHANGE", {})
        await store._flush_notifications()

        assert store.state["application"]["x"] == 1
        assert announced == [["CHANGE"]]


class TestADispatchCutOffAfterItsCommit:
    """The change a reducer committed stands, so it is announced even when the
    dispatch is cancelled after it, or when the announcement itself fails."""

    @staticmethod
    async def _setup(store, middleware):
        store._add_middleware(middleware)

        async def set_k(action, state):
            application = {**state.get("application", {}), action["payload"]["key"]: 1}
            return {**state, "application": application}

        store._register_reducer("SET_K", set_k)

    @pytest.mark.parametrize("batched", [False, True])
    async def test_a_cancel_after_the_commit_still_tells_subscribers(self, batched):
        # A cancel is not an Exception, so it went past the handler that
        # announces a committed change, and no subscriber was told. Hooks
        # that had not started are skipped, unless the batch collecting the
        # action announces it.
        store = get_store()
        gate = asyncio.Event()
        committed = asyncio.Event()

        async def audit_after(action, state, next_fn):
            result = await next_fn(action, state)
            if action["payload"].get("hold"):
                committed.set()
                await gate.wait()
            return result

        await self._setup(store, audit_after)
        hooked, announced = [], []
        store.on("SET_K", lambda action, *rest: hooked.append(action["payload"]["key"]))
        store.on(
            "BATCH_COMPLETE",
            lambda action, *rest: announced.append(
                [a["payload"]["key"] for a in action["payload"]["actions"]]
            ),
        )

        async def run():
            if batched:
                async with store.batch():
                    await store.dispatch("SET_K", {"key": "first"})
                    await store.dispatch("SET_K", {"key": "held", "hold": True})
            else:
                await store.dispatch("SET_K", {"key": "held", "hold": True})

        told = []
        store.subscribe(
            "observer",
            lambda state, action=None: told.append(action["type"]),
            action_filter={"SET_K", "BATCH_COMPLETE"},
        )
        task = asyncio.ensure_future(run())
        await asyncio.wait_for(committed.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await store._flush_notifications()
        assert store.state["application"]["held"] == 1
        if batched:
            assert told == ["BATCH_COMPLETE"]
            assert announced == [["first", "held"]]
            assert hooked == ["first", "held"]
        else:
            assert told == ["SET_K"]
            assert hooked == []

    @pytest.mark.parametrize("perf", [False, True])
    async def test_a_middleware_error_the_announcement_replaces_is_logged(self, caplog, perf):
        # The announcement's own exception (here a cancel during the acting
        # subscriber's render) replaced the middleware's, which vanished.
        store = get_store()
        if perf:
            store.enable_perf()
        rendering = asyncio.Event()

        async def raise_after(action, state, next_fn):
            result = await next_fn(action, state)
            raise ConnectionError("audit write failed")

        async def acting(state, action=None):
            rendering.set()
            await asyncio.Event().wait()

        await self._setup(store, raise_after)
        store.subscribe("acting", acting, action_filter={"SET_K"})
        with caplog.at_level("ERROR", logger="cascadeui"):
            task = asyncio.ensure_future(store.dispatch("SET_K", {"key": "k"}, source_id="acting"))
            await asyncio.wait_for(rendering.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        store.disable_perf()
        assert any(
            "audit write failed" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    @pytest.mark.parametrize("batched", [False, True])
    @pytest.mark.parametrize("perf", [False, True])
    async def test_a_cancel_during_the_acting_render_skips_the_hooks(self, batched, perf):
        # Hooks not yet started when a cancel lands do not run, as before;
        # the subscribers scheduled before the acting render are still told.
        store = get_store()
        rendering = asyncio.Event()

        async def pass_on(action, state, next_fn):
            return await next_fn(action, state)

        async def acting(state, action=None):
            rendering.set()
            await asyncio.Event().wait()

        await self._setup(store, pass_on)
        store.subscribe("acting", acting, action_filter={"SET_K", "BATCH_COMPLETE"})
        told = []
        store.subscribe(
            "observer",
            lambda state, action=None: told.append(action["type"]),
            action_filter={"SET_K", "BATCH_COMPLETE"},
        )
        hooked = []
        store.on("SET_K", lambda action, *rest: hooked.append("SET_K"))
        store.on("BATCH_COMPLETE", lambda action, *rest: hooked.append("BATCH_COMPLETE"))

        async def run():
            if batched:
                async with store.batch(source_id="acting"):
                    await store.dispatch("SET_K", {"key": "k"})
            else:
                await store.dispatch("SET_K", {"key": "k"}, source_id="acting")

        if perf:
            store.enable_perf()
        try:
            task = asyncio.ensure_future(run())
            await asyncio.wait_for(rendering.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await store._flush_notifications()
        finally:
            store.disable_perf()
        assert told == (["BATCH_COMPLETE"] if batched else ["SET_K"])
        assert hooked == []

    async def test_a_cancel_during_a_hook_skips_the_hooks_after_it(self):
        # Hooks not yet started when a cancel lands do not run: running them
        # later put them after a later dispatch's hooks (a cancelled send's
        # view_created after its view_destroyed).
        store = get_store()
        inside = asyncio.Event()
        calls = []

        async def pass_on(action, state, next_fn):
            return await next_fn(action, state)

        async def slow(action, *rest):
            calls.append("slow")
            inside.set()
            await asyncio.Event().wait()

        await self._setup(store, pass_on)
        store.on("SET_K", slow)
        store.on("SET_K", lambda action, *rest: calls.append("after"))
        task = asyncio.ensure_future(store.dispatch("SET_K", {"key": "k"}))
        await asyncio.wait_for(inside.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await store._flush_notifications()
        assert calls == ["slow"]

    @pytest.mark.parametrize("cut", ["cancel", "raise"])
    @pytest.mark.parametrize("kind", ["broadcast", "declined"])
    async def test_an_action_that_changes_nothing_is_announced_when_cut_off(self, kind, cut):
        # Announcing waited on a commit, so an action whose reducer changed
        # nothing (a broadcast, or a reducer that declines) went unannounced
        # when its dispatch was cut off after the reducer step. Cancelled, its
        # subscribers are told and its hooks, not yet started, are skipped.
        store = get_store()
        after = asyncio.Event()

        async def audit_after(action, state, next_fn):
            result = await next_fn(action, state)
            if cut == "raise":
                raise ConnectionError("audit write failed")
            after.set()
            await asyncio.Event().wait()
            return result

        async def decline(action, state):
            return state

        await self._setup(store, audit_after)
        store._register_reducer("NOTHING", decline)
        action_type = "PING" if kind == "broadcast" else "NOTHING"
        hooked, told = [], []
        store.on(action_type, lambda action, *rest: hooked.append(action_type))
        store.subscribe(
            "observer",
            lambda state, action=None: told.append(action["type"]),
            action_filter={action_type},
        )
        task = asyncio.ensure_future(store.dispatch(action_type, {}))
        if cut == "cancel":
            await asyncio.wait_for(after.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ConnectionError):
                await task
        await store._flush_notifications()
        assert told == [action_type]
        assert hooked == ([] if cut == "cancel" else [action_type])


class TestHasMiddleware:
    """``StateStore.has_middleware(cls)`` is the public seam for asking
    whether an instance of a middleware class is installed.
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


class TestSetupMiddlewareFunctions:
    """Function middlewares share one class, so they are matched by identity."""

    async def test_every_function_middleware_is_installed_once(self):
        from cascadeui import setup_middleware

        ran = []

        async def first(action, state, next_fn):
            ran.append("first")
            return await next_fn(action, state)

        async def second(action, state, next_fn):
            ran.append("second")
            return await next_fn(action, state)

        store = get_store()
        await setup_middleware(first, second)
        await setup_middleware(first, second)
        await store.dispatch("PING")

        assert [m for m in store._middleware if m in (first, second)] == [first, second]
        assert ran == ["first", "second"]
