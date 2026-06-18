"""Tests for the StateStore dispatch, subscribe, and reducer system."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from cascadeui.state.singleton import get_store
from cascadeui.state.store import StateStore


class TestStateStoreSingleton:
    """StateStore is a singleton with a well-defined initial state shape."""

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

    def test_build_initial_state_returns_canonical_shape(self):
        shape = StateStore._build_initial_state()
        assert shape == {
            "sessions": {},
            "views": {},
            "components": {},
            "application": {},
        }

    def test_build_initial_state_returns_fresh_dict(self):
        a = StateStore._build_initial_state()
        b = StateStore._build_initial_state()
        assert a == b
        assert a is not b
        a["sessions"]["x"] = 1
        assert "x" not in b["sessions"]

    def test_init_uses_build_initial_state(self):
        store = get_store()
        # Reset to a known shape, verify it matches the helper output.
        store.state = StateStore._build_initial_state()
        assert store.state == StateStore._build_initial_state()


class TestGetActiveViews:
    """Public read-only accessor for the active view registry."""

    def test_returns_mapping_view_reflecting_registry(self):
        store = get_store()
        store._active_views.clear()
        view = object()
        store._active_views["v1"] = view
        mapping = store.get_active_views()
        assert mapping["v1"] is view
        assert len(mapping) == 1

    def test_is_live_not_snapshot(self):
        store = get_store()
        store._active_views.clear()
        mapping = store.get_active_views()
        assert len(mapping) == 0
        store._active_views["v1"] = object()
        assert "v1" in mapping

    def test_rejects_mutation(self):
        store = get_store()
        store._active_views.clear()
        mapping = store.get_active_views()
        with pytest.raises(TypeError):
            mapping["v1"] = object()


class TestDestroyView:
    """Atomic teardown orders state removal before the active-registry removal.

    A ghost is the divergence ``view_id in state["views"] and view_id not in
    _active_views`` -- what the inspector paints red. _destroy_view clears the
    active entry only after VIEW_DESTROYED confirms the state removal, so a
    failed dispatch can never produce that divergence.
    """

    def _seed(self, store, vid):
        """Put a view in both registries the way a live send would."""
        store._active_views.clear()
        store.subscribers = {}
        store.state = StateStore._build_initial_state()
        store.state["views"][vid] = {"view_type": "FakeView", "user_id": "u1"}
        # _unregister_view reads these; instance_scope="user" + user_id=None
        # yields a None scope key, so no _instance_index entry is touched.
        store._active_views[vid] = SimpleNamespace(
            _instance_root_class="FakeView",
            instance_scope="user",
            user_id=None,
            guild_id=None,
            _participants=set(),
        )

    async def test_happy_path_removes_from_both_registries(self):
        store = get_store()
        saved_mw = store._middleware
        store._middleware = []
        try:
            self._seed(store, "v-happy")
            result = await store._destroy_view("v-happy")
            assert result is True
            assert "v-happy" not in store.state["views"]
            assert "v-happy" not in store._active_views
        finally:
            store._middleware = saved_mw

    async def test_middleware_failure_retains_active_entry_no_ghost(self):
        store = get_store()
        saved_mw = store._middleware

        async def failing_mw(action, state, next_fn):
            if action["type"] == "VIEW_DESTROYED":
                raise RuntimeError("middleware failed before reducer")
            return await next_fn(action, state)

        store._middleware = [failing_mw]
        try:
            self._seed(store, "v-mw")
            result = await store._destroy_view("v-mw")
            assert result is False
            # Both registries retained -> present in both -> NOT a ghost.
            assert "v-mw" in store.state["views"]
            assert "v-mw" in store._active_views
        finally:
            store._middleware = saved_mw

    async def test_state_not_removed_retains_active_entry_no_ghost(self):
        store = get_store()
        saved_mw = store._middleware

        # Short-circuits without calling next_fn: dispatch returns normally but
        # the reducer never runs, so state["views"] is unchanged. This models
        # the swallowed-reducer branch where dispatch does not raise yet the
        # state removal silently did not happen.
        async def swallow_mw(action, state, next_fn):
            if action["type"] == "VIEW_DESTROYED":
                return state
            return await next_fn(action, state)

        store._middleware = [swallow_mw]
        try:
            self._seed(store, "v-swallow")
            result = await store._destroy_view("v-swallow")
            assert result is False
            assert "v-swallow" in store.state["views"]
            assert "v-swallow" in store._active_views
        finally:
            store._middleware = saved_mw

    async def test_cancelled_dispatch_after_state_removal_clears_active(self):
        store = get_store()
        saved_mw = store._middleware

        # Reducer runs (state entry removed), then the dispatch is cancelled
        # before _destroy_view reaches its active-registry removal. CancelledError
        # is a BaseException, so it bypasses the except Exception guard; the
        # finally clause must still clear the active entry to avoid stranding
        # the view in _active_views (the reverse of a ghost).
        async def cancel_after_reducer(action, state, next_fn):
            result = await next_fn(action, state)
            if action["type"] == "VIEW_DESTROYED":
                raise asyncio.CancelledError()
            return result

        store._middleware = [cancel_after_reducer]
        try:
            self._seed(store, "v-cancel")
            with pytest.raises(asyncio.CancelledError):
                await store._destroy_view("v-cancel")
            assert "v-cancel" not in store.state["views"]
            assert "v-cancel" not in store._active_views
        finally:
            store._middleware = saved_mw

    async def test_idempotent_under_double_teardown(self):
        store = get_store()
        saved_mw = store._middleware
        store._middleware = []
        try:
            self._seed(store, "v-double")
            assert await store._destroy_view("v-double") is True
            # Second call: view already gone from state -> not in views -> the
            # idempotent _unregister_view runs as a no-op and it returns True.
            assert await store._destroy_view("v-double") is True
            assert "v-double" not in store.state["views"]
            assert "v-double" not in store._active_views
        finally:
            store._middleware = saved_mw


class TestDispatch:
    """Dispatch routes actions through registered reducers and records history."""

    async def test_dispatch_with_registered_reducer(self):
        store = get_store()

        async def my_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["test"] = action["payload"]["value"]
            return new

        store._register_reducer("TEST_ACTION", my_reducer)
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
    """Subscriber registration, notification, action filtering, and unsubscribe."""

    async def test_subscriber_receives_notification(self):
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("test-sub", handler)
        await store.dispatch("PING", {})
        await store._flush_notifications()

        assert "PING" in received

    async def test_action_filter_blocks_unmatched(self):
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("filtered-sub", handler, action_filter={"WANTED"})
        await store.dispatch("UNWANTED", {})
        await store.dispatch("WANTED", {})
        await store._flush_notifications()

        assert received == ["WANTED"]

    async def test_unsubscribe_stops_notifications(self):
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("temp-sub", handler)
        await store.dispatch("BEFORE", {})
        await store._flush_notifications()
        store._unsubscribe("temp-sub")
        await store.dispatch("AFTER", {})
        await store._flush_notifications()

        assert received == ["BEFORE"]

    async def test_subscriber_with_no_filter_gets_everything(self):
        store = get_store()
        received = []

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("all-sub", handler, action_filter=None)
        await store.dispatch("A", {})
        await store.dispatch("B", {})
        await store._flush_notifications()

        assert received == ["A", "B"]

    async def test_cross_view_subscribers_do_not_inherit_current_interaction(self):
        """Cross-view subscribers run in tasks scheduled while ``_CURRENT_INTERACTION``
        is scoped to ``None`` -- only the acting subscriber (awaited inline)
        sees the live value. Protects the ``refresh()`` fast path from
        accidentally firing on cross-view subscribers via contextvar
        inheritance through ``asyncio.create_task``.
        """
        from cascadeui.state.store import _CURRENT_INTERACTION

        store = get_store()
        sentinel = object()
        seen_by_acting: list = []
        seen_by_cross_view: list = []

        async def acting_handler(state, action):
            seen_by_acting.append(_CURRENT_INTERACTION.get())

        async def cross_handler(state, action):
            seen_by_cross_view.append(_CURRENT_INTERACTION.get())

        store.subscribe("acting-view", acting_handler)
        store.subscribe("cross-view", cross_handler)

        # Simulate ``stateful_callback`` having bound the contextvar to a
        # live interaction before the acting dispatch.
        token = _CURRENT_INTERACTION.set(sentinel)
        try:
            await store.dispatch("ACTING_ACTION", {}, source_id="acting-view")
        finally:
            _CURRENT_INTERACTION.reset(token)

        await store._flush_notifications()

        assert seen_by_acting == [sentinel]
        assert seen_by_cross_view == [None]


class TestSelectors:
    """Selector-based notification skips dispatches where the selected value is unchanged."""

    async def test_selector_skips_unchanged(self):
        """Subscriber with selector should NOT be notified when selected value stays the same."""
        store = get_store()
        received = []

        async def counter_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["counter"] = action["payload"]["value"]
            return new

        store._register_reducer("SET_COUNTER", counter_reducer)

        async def handler(state, action):
            received.append(action["type"])

        selector = lambda state: state.get("application", {}).get("counter")
        store.subscribe("selector-sub", handler, selector=selector)

        await store.dispatch("SET_COUNTER", {"value": 5})
        await store._flush_notifications()
        assert len(received) == 1

        # Same value again -- should be skipped
        await store.dispatch("SET_COUNTER", {"value": 5})
        await store._flush_notifications()
        assert len(received) == 1

    async def test_selector_notifies_on_change(self):
        """Subscriber with selector should be notified when selected value changes."""
        store = get_store()
        received = []

        async def counter_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["counter"] = action["payload"]["value"]
            return new

        store._register_reducer("SET_COUNTER", counter_reducer)

        async def handler(state, action):
            received.append(state["application"]["counter"])

        selector = lambda state: state.get("application", {}).get("counter")
        store.subscribe("selector-sub", handler, selector=selector)

        await store.dispatch("SET_COUNTER", {"value": 1})
        await store.dispatch("SET_COUNTER", {"value": 2})
        await store.dispatch("SET_COUNTER", {"value": 3})
        await store._flush_notifications()

        assert received == [1, 2, 3]

    async def test_selector_with_action_filter(self):
        """Selector and action filter work together — both must pass."""
        store = get_store()
        received = []

        async def reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["val"] = action["payload"]["val"]
            return new

        store._register_reducer("A", reducer)
        store._register_reducer("B", reducer)

        async def handler(state, action):
            received.append(action["type"])

        selector = lambda state: state.get("application", {}).get("val")
        store.subscribe("combo-sub", handler, action_filter={"A"}, selector=selector)

        # B is filtered by action_filter
        await store.dispatch("B", {"val": 10})
        await store._flush_notifications()
        assert len(received) == 0

        # A passes action filter, selector value is new
        await store.dispatch("A", {"val": 10})
        await store._flush_notifications()
        assert len(received) == 1

        # A passes action filter, but selector value hasn't changed
        await store.dispatch("A", {"val": 10})
        await store._flush_notifications()
        assert len(received) == 1

    async def test_no_selector_always_notifies(self):
        """Without a selector, subscriber receives every matching action."""
        store = get_store()
        received = []

        async def noop_reducer(action, state):
            return copy.deepcopy(state)

        store._register_reducer("NOOP", noop_reducer)

        async def handler(state, action):
            received.append(action["type"])

        store.subscribe("no-sel-sub", handler)

        await store.dispatch("NOOP", {})
        await store.dispatch("NOOP", {})
        await store.dispatch("NOOP", {})
        await store._flush_notifications()

        assert len(received) == 3

    async def test_unsubscribe_cleans_up_selector_memo(self):
        """Unsubscribing should remove memoized selector values."""
        store = get_store()

        selector = lambda state: state.get("application", {}).get("x")
        store.subscribe("memo-sub", lambda s, a: None, selector=selector)

        # Force a value into the memo
        store._last_selected["memo-sub"] = 42
        store._unsubscribe("memo-sub")

        assert "memo-sub" not in store._last_selected


class TestReducers:
    """Reducer registration, override, and unregistration on the store."""

    async def test_register_and_unregister_reducer(self):
        store = get_store()

        async def temp_reducer(action, state):
            new = copy.deepcopy(state)
            new["application"]["temp"] = True
            return new

        store._register_reducer("TEMP", temp_reducer)
        assert "TEMP" in store.reducers

        store._unregister_reducer("TEMP")
        assert "TEMP" not in store.reducers

    async def test_custom_reducer_overrides_core(self):
        store = get_store()
        store._load_core_reducers()

        async def custom_view_created(action, state):
            new = copy.deepcopy(state)
            new["application"]["custom_fired"] = True
            return new

        store._register_reducer("VIEW_CREATED", custom_view_created)
        await store.dispatch("VIEW_CREATED", {"view_id": "test"})

        assert store.state["application"].get("custom_fired") is True

    async def test_register_reducer_warns_on_overwrite(self):
        from unittest.mock import patch

        store = get_store()

        async def first(action, state):
            return state

        async def second(action, state):
            return state

        store._register_reducer("DUPE_TEST", first)

        import cascadeui.state.store as store_mod

        with patch.object(store_mod.logger, "warning") as mock_warn:
            store._register_reducer("DUPE_TEST", second)

        mock_warn.assert_called_once()
        assert "Overwriting" in mock_warn.call_args[0][0]
        assert store.reducers["DUPE_TEST"] is second


class TestCascadeReducerCollision:
    """The @cascade_reducer decorator refuses built-in action names at decoration time."""

    def test_collision_raises_value_error(self):
        from cascadeui.utils import cascade_reducer

        with pytest.raises(ValueError, match="built-in action"):

            @cascade_reducer("VIEW_CREATED")
            async def _shadow(action, state):
                return state

    def test_collision_message_names_action(self):
        from cascadeui.utils import cascade_reducer

        with pytest.raises(ValueError, match="NAVIGATION_PUSH"):

            @cascade_reducer("NAVIGATION_PUSH")
            async def _shadow(action, state):
                return state

    def test_collision_message_points_to_middleware(self):
        from cascadeui.utils import cascade_reducer

        with pytest.raises(ValueError, match="middleware"):

            @cascade_reducer("UNDO")
            async def _shadow(action, state):
                return state

    def test_collision_fires_at_decoration_not_call(self):
        # The raise happens at the @cascade_reducer("X") line, not when the
        # decorated function is later called. Confirms the decorator body
        # itself catches the collision.
        from cascadeui.utils import cascade_reducer

        async def _candidate(action, state):
            return state

        with pytest.raises(ValueError):
            cascade_reducer("VIEW_DESTROYED")(_candidate)

    def test_custom_action_still_registers(self):
        from cascadeui.utils import cascade_reducer

        store = get_store()

        @cascade_reducer("MY_CUSTOM_ACTION_FOR_TEST")
        async def _handler(action, state):
            return state

        assert "MY_CUSTOM_ACTION_FOR_TEST" in store.reducers
        store._unregister_reducer("MY_CUSTOM_ACTION_FOR_TEST")

    def test_every_core_reducer_is_reserved(self):
        # Drift guard: every action that has a built-in reducer must also be
        # reserved against collision. Adding a reducer without reserving it
        # would let a user @cascade_reducer shadow the library reducer.
        from cascadeui.state.reducers import _BUILTIN_REDUCER_ACTIONS

        store = get_store()
        store._load_core_reducers()
        assert set(store._core_reducers.keys()) <= _BUILTIN_REDUCER_ACTIONS

    def test_dispatch_only_actions_are_reserved_without_reducer(self):
        # Contract: library-owned dispatch-only actions (prune signals) are
        # reserved against @cascade_reducer collision but intentionally have
        # no reducer. The frozenset is a superset of _core_reducers, not an
        # equality relation.
        from cascadeui.state.reducers import _BUILTIN_REDUCER_ACTIONS

        store = get_store()
        store._load_core_reducers()
        dispatch_only = _BUILTIN_REDUCER_ACTIONS - set(store._core_reducers.keys())
        assert dispatch_only == {
            "APPLICATION_SLOTS_PRUNED",
            "REGISTRY_PRUNED",
        }

    def test_register_reducer_direct_api_still_allows_override(self):
        # The collision guard is on the decorator only. Direct register_reducer
        # is the escape hatch for advanced cases (tests, custom middleware
        # replacement). Confirms the lower-level API is unaffected.
        store = get_store()
        store._load_core_reducers()

        async def _override(action, state):
            return state

        store._register_reducer("VIEW_UPDATED", _override)
        assert store.reducers["VIEW_UPDATED"] is _override
        store._unregister_reducer("VIEW_UPDATED")


class TestInspectorPurgedStaleReducer:
    """INSPECTOR_PURGED_STALE keeps the inspector's own entries and drops everything else."""

    async def test_keeps_inspector_entries_drops_stale_components(self):
        from cascadeui.state.reducers import reduce_inspector_purged_stale

        state = {
            "components": {
                "c-own": {"view_id": "inspector-1", "interactions": []},
                "c-stale-1": {"view_id": "other-view", "interactions": []},
                "c-stale-2": {"view_id": "gone", "interactions": []},
            },
        }
        action = {"type": "INSPECTOR_PURGED_STALE", "payload": {"inspector_id": "inspector-1"}}

        result = await reduce_inspector_purged_stale(action, state)

        assert set(result["components"].keys()) == {"c-own"}

    async def test_keeps_inspector_modal_drops_others(self):
        from cascadeui.state.reducers import reduce_inspector_purged_stale

        state = {
            "modals": {
                "inspector-1": {"submissions": []},
                "other-view": {"submissions": []},
            },
        }
        action = {"type": "INSPECTOR_PURGED_STALE", "payload": {"inspector_id": "inspector-1"}}

        result = await reduce_inspector_purged_stale(action, state)

        assert set(result["modals"].keys()) == {"inspector-1"}

    async def test_drops_components_key_when_no_inspector_entries(self):
        from cascadeui.state.reducers import reduce_inspector_purged_stale

        state = {
            "components": {
                "c-stale": {"view_id": "other", "interactions": []},
            },
        }
        action = {"type": "INSPECTOR_PURGED_STALE", "payload": {"inspector_id": "inspector-1"}}

        result = await reduce_inspector_purged_stale(action, state)

        assert "components" not in result

    async def test_missing_inspector_id_returns_state_unchanged(self):
        from cascadeui.state.reducers import reduce_inspector_purged_stale

        state = {"components": {"c-stale": {"view_id": "other"}}}
        action = {"type": "INSPECTOR_PURGED_STALE", "payload": {}}

        result = await reduce_inspector_purged_stale(action, state)

        assert result is state


class TestPerfSampling:
    """Opt-in profiling records per-dispatch timings to a bounded ring buffer."""

    async def test_disabled_by_default(self):
        store = get_store()
        store.clear_perf()
        store.disable_perf()
        await store.dispatch("PERF_NOOP_1")
        assert len(store._perf_samples) == 0

    async def test_enabled_records_sample(self):
        store = get_store()
        store.clear_perf()
        store.enable_perf()
        try:
            await store.dispatch("PERF_NOOP_2")
            assert len(store._perf_samples) == 1
            sample = store._perf_samples[-1]
            assert sample["action"] == "PERF_NOOP_2"
            assert sample["total_ms"] >= 0
            assert sample["reducer_ms"] >= 0
            assert sample["middleware_ms"] >= 0
            assert sample["notify_ms"] >= 0
            assert "timestamp" in sample
            assert "subscribers" in sample
        finally:
            store.disable_perf()

    async def test_ring_buffer_caps_at_100(self):
        store = get_store()
        store.clear_perf()
        store.enable_perf()
        try:
            for i in range(120):
                await store.dispatch("PERF_FLOOD")
            assert len(store._perf_samples) == 100
        finally:
            store.disable_perf()

    def test_clear_wipes_both_buffers(self):
        store = get_store()
        store._perf_samples.append({"action": "X"})
        store._refresh_samples.append({"view_class": "Y"})
        store.clear_perf()
        assert len(store._perf_samples) == 0
        assert len(store._refresh_samples) == 0

    def test_clear_wipes_edit_stack(self):
        """A stale in-progress dispatch's counter cannot leak across clears."""
        store = get_store()
        store._perf_edit_stack.append(5)
        store.clear_perf()
        assert store._perf_edit_stack == []

    async def test_sample_records_edits_field(self):
        """Every profiled dispatch records an ``edits`` count, finalized to
        an int once ``_flush_notifications()`` drains any in-flight
        subscriber tasks that might still be incrementing the counter.
        """
        store = get_store()
        store.clear_perf()
        store.enable_perf()
        try:
            await store.dispatch("PERF_EDIT_BARE")
            await store._flush_notifications()
            sample = store._perf_samples[-1]
            assert sample["edits"] == 0
        finally:
            store.disable_perf()

    async def test_edits_increment_from_refresh(self):
        """A subscriber that mutates the current dispatch's edit counter
        via the contextvar -- the same path ``refresh()`` uses -- bumps
        the sample's ``edits`` field after ``_flush_notifications()``.
        """
        store = get_store()
        store.clear_perf()
        store.enable_perf()

        async def fake_subscriber(state, action):
            store._record_edit()

        store.subscribe("tester", fake_subscriber)
        try:
            await store.dispatch("PERF_EDIT_ONE")
            await store._flush_notifications()
            sample = store._perf_samples[-1]
            assert sample["edits"] == 1
        finally:
            store._unsubscribe("tester")
            store.disable_perf()

    async def test_edits_stack_nests_correctly(self):
        """Contextvar-based edit attribution keeps nested dispatches
        isolated: the OUTER sample's counter stays pinned even when an
        INNER dispatch fires inside a subscriber and bumps its own.
        """
        store = get_store()
        store.clear_perf()
        store.enable_perf()

        async def nested_subscriber(state, action):
            if action["type"] != "OUTER":
                return
            # Bump the outer dispatch's counter via contextvar
            store._record_edit()
            # Fire an inner dispatch that itself edits. The inner
            # dispatch sets its own contextvar for the duration of the
            # inner body and restores this task's counter on exit.
            await store.dispatch("INNER")

        async def inner_edit_subscriber(state, action):
            if action["type"] != "INNER":
                return
            store._record_edit()

        store.subscribe("nest", nested_subscriber)
        store.subscribe("inner", inner_edit_subscriber)
        try:
            await store.dispatch("OUTER")
            await store._flush_notifications()
            samples_by_action = {s["action"]: s for s in store._perf_samples}
            assert samples_by_action["OUTER"]["edits"] == 1
            assert samples_by_action["INNER"]["edits"] == 1
        finally:
            store._unsubscribe("nest")
            store._unsubscribe("inner")

    async def test_sample_splits_middleware_from_reducer(self):
        """A slow middleware inflates ``middleware_ms`` without polluting
        ``reducer_ms``. Uses a no-op reducer (dispatch-only action) so the
        reducer branch exits fast, then asserts middleware dominates the
        chain total.
        """
        store = get_store()
        store.clear_perf()

        async def slow_middleware(action, state, next_fn):
            if action["type"] == "PERF_SLOW_MW":
                await asyncio.sleep(0.02)  # 20ms
            return await next_fn(action, state)

        store._add_middleware(slow_middleware)
        store.enable_perf()
        try:
            await store.dispatch("PERF_SLOW_MW")
            sample = store._perf_samples[-1]
            # The 20ms sleep lands in middleware, not reducer.
            assert sample["middleware_ms"] >= 15.0
            assert sample["reducer_ms"] < 5.0
        finally:
            store.disable_perf()
            store._remove_middleware(slow_middleware)

    async def test_sample_splits_reducer_from_middleware(self):
        """A slow reducer inflates ``reducer_ms`` without polluting
        ``middleware_ms``. The registered reducer awaits a small sleep;
        the middleware chain is empty so ``middleware_ms`` reflects only
        the chain-wrap overhead, which should stay close to zero.
        """
        store = get_store()
        store.clear_perf()

        async def slow_reducer(action, state):
            await asyncio.sleep(0.02)
            return state

        store._register_reducer("PERF_SLOW_REDUCER", slow_reducer)
        store.enable_perf()
        try:
            await store.dispatch("PERF_SLOW_REDUCER")
            sample = store._perf_samples[-1]
            assert sample["reducer_ms"] >= 15.0
            assert sample["middleware_ms"] < 5.0
        finally:
            store.disable_perf()
            store._unregister_reducer("PERF_SLOW_REDUCER")

    def test_clear_wipes_reducer_stack(self):
        """A stale in-progress dispatch's reducer timing cannot leak
        across clears."""
        store = get_store()
        store._perf_reducer_stack.append(12.5)
        store.clear_perf()
        assert store._perf_reducer_stack == []

    async def test_notify_sample_per_subscriber(self):
        """Each subscriber touched by a dispatch produces exactly one
        ``_notify_samples`` entry with subscriber_id, action, and ms.
        """
        store = get_store()
        store.clear_perf()

        async def s1(state, action):
            pass

        async def s2(state, action):
            pass

        store.subscribe("sub_alpha", s1, action_filter={"PERF_NOTIFY"})
        store.subscribe("sub_beta", s2, action_filter={"PERF_NOTIFY"})
        store.enable_perf()
        try:
            await store.dispatch("PERF_NOTIFY")
            await store._flush_notifications()
            # Only the two subscribed handlers fired; pre-existing
            # subscribers with other filters produce no samples.
            samples = [s for s in store._notify_samples if s["action"] == "PERF_NOTIFY"]
            ids = {s["subscriber_id"] for s in samples}
            assert ids == {"sub_alpha", "sub_beta"}
            for s in samples:
                assert s["ms"] >= 0
                assert s["action"] == "PERF_NOTIFY"
        finally:
            store.disable_perf()
            store._unsubscribe("sub_alpha")
            store._unsubscribe("sub_beta")

    async def test_slow_subscriber_isolated_in_samples(self):
        """A slow subscriber's ms reading reflects its own wall time,
        not the fan-out total.  A fast sibling records a small ms.
        """
        store = get_store()
        store.clear_perf()

        async def slow_sub(state, action):
            await asyncio.sleep(0.02)

        async def fast_sub(state, action):
            pass

        store.subscribe("slow", slow_sub, action_filter={"PERF_NOTIFY_MIX"})
        store.subscribe("fast", fast_sub, action_filter={"PERF_NOTIFY_MIX"})
        store.enable_perf()
        try:
            await store.dispatch("PERF_NOTIFY_MIX")
            await store._flush_notifications()
            by_id = {
                s["subscriber_id"]: s["ms"]
                for s in store._notify_samples
                if s["action"] == "PERF_NOTIFY_MIX"
            }
            assert by_id["slow"] >= 15.0
            assert by_id["fast"] < 5.0
        finally:
            store.disable_perf()
            store._unsubscribe("slow")
            store._unsubscribe("fast")

    async def test_crashing_subscriber_still_records_sample(self):
        """A subscriber that raises still contributes a sample -- its
        cost counts even when the callback fails.  The ``finally`` guard
        in ``_safe_notify`` makes this invariant.
        """
        store = get_store()
        store.clear_perf()

        async def crashing_sub(state, action):
            raise RuntimeError("deliberate")

        store.subscribe("crash", crashing_sub, action_filter={"PERF_NOTIFY_ERR"})
        store.enable_perf()
        try:
            await store.dispatch("PERF_NOTIFY_ERR")
            await store._flush_notifications()
            samples = [
                s
                for s in store._notify_samples
                if s["action"] == "PERF_NOTIFY_ERR" and s["subscriber_id"] == "crash"
            ]
            assert len(samples) == 1
            assert samples[0]["ms"] >= 0
        finally:
            store.disable_perf()
            store._unsubscribe("crash")

    def test_clear_wipes_notify_samples(self):
        """Clearing perf drains the per-subscriber ring as well."""
        store = get_store()
        store._notify_samples.append({"subscriber_id": "x", "action": "Y", "ms": 1.0})
        store.clear_perf()
        assert len(store._notify_samples) == 0
