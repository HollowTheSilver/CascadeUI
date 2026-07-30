"""Tests for computed state and derived values (@computed decorator)."""

import copy

import pytest

from cascadeui.state.computed import ComputedValue, computed
from cascadeui.state.singleton import get_store


class TestComputedValue:
    """ComputedValue caches derived state and invalidates on input change."""

    def test_correct_value_on_first_access(self):
        store = get_store()

        cv = ComputedValue(
            name="total",
            selector=lambda s: s.get("application", {}).get("counts", {}),
            compute_fn=lambda counts: sum(counts.values()) if counts else 0,
        )
        store._register_computed("total", cv)

        store.state["application"]["counts"] = {"a": 1, "b": 2, "c": 3}
        assert store.computed["total"] == 6

    def test_cached_when_input_unchanged(self):
        store = get_store()
        call_count = 0

        def counting_fn(counts):
            nonlocal call_count
            call_count += 1
            return sum(counts.values()) if counts else 0

        cv = ComputedValue(
            name="cached_total",
            selector=lambda s: s.get("application", {}).get("counts", {}),
            compute_fn=counting_fn,
        )
        store._register_computed("cached_total", cv)

        store.state["application"]["counts"] = {"x": 10}

        # First access computes
        assert store.computed["cached_total"] == 10
        assert call_count == 1

        # Second access with same input should use cache
        assert store.computed["cached_total"] == 10
        assert call_count == 1

    def test_recomputes_when_state_changes(self):
        store = get_store()

        cv = ComputedValue(
            name="dynamic",
            selector=lambda s: s.get("application", {}).get("val", 0),
            compute_fn=lambda v: v * 2,
        )
        store._register_computed("dynamic", cv)

        store.state["application"]["val"] = 5
        assert store.computed["dynamic"] == 10

        store.state["application"]["val"] = 7
        assert store.computed["dynamic"] == 14

    def test_decorator_registration(self):
        store = get_store()

        @computed(selector=lambda s: s.get("application", {}).get("items", []))
        def item_count(items):
            return len(items)

        store.state["application"]["items"] = [1, 2, 3]
        assert store.computed["item_count"] == 3

    def test_missing_name_raises_key_error(self):
        store = get_store()

        with pytest.raises(KeyError, match="nonexistent"):
            _ = store.computed["nonexistent"]

    def test_contains_check(self):
        store = get_store()

        cv = ComputedValue(
            name="exists",
            selector=lambda s: None,
            compute_fn=lambda _: None,
        )
        store._register_computed("exists", cv)

        assert "exists" in store.computed
        assert "nope" not in store.computed

    def test_registration_survives_store_reset(self):
        # @computed binds at module import; fresh StateStore instances (e.g.
        # between tests, or any code that replaces the singleton) must re-seed
        # from the module-level registry so previously-decorated values remain
        # accessible as store.computed[name].
        from cascadeui.state import singleton
        from cascadeui.state.computed import _COMPUTED_REGISTRY

        @computed(selector=lambda s: s.get("application", {}).get("reset_marker", 0))
        def reset_marker(val):
            return val + 1

        assert "reset_marker" in _COMPUTED_REGISTRY

        # Simulate a store reset: drop the singleton and read a fresh one.
        # BOTH holders have to go, as conftest documents. StateStore keeps
        # its own ``_instance`` on the class, so clearing only the module
        # global hands back the same object with its ``_computed`` dict
        # already populated -- the reseed loop this test names would never
        # run and the assertion below would pass without it.
        from cascadeui.state.store import StateStore

        stale = get_store()
        StateStore._instance = None
        singleton._store_instance = None
        fresh = get_store()
        assert fresh is not stale

        assert "reset_marker" in fresh.computed
        fresh.state["application"]["reset_marker"] = 10
        assert fresh.computed["reset_marker"] == 11

        # Cleanup: keep the registry clean for neighboring tests.
        _COMPUTED_REGISTRY.pop("reset_marker", None)

    def test_invalidate_forces_recompute(self):
        store = get_store()
        call_count = 0

        def counting_fn(val):
            nonlocal call_count
            call_count += 1
            return val

        cv = ComputedValue(
            name="invalidatable",
            selector=lambda s: s.get("application", {}).get("x", 0),
            compute_fn=counting_fn,
        )
        store._register_computed("invalidatable", cv)

        store.state["application"]["x"] = 42
        assert store.computed["invalidatable"] == 42
        assert call_count == 1

        # Invalidate and access again with same input
        cv.invalidate()
        assert store.computed["invalidatable"] == 42
        assert call_count == 2  # Recomputed


class TestComputedMemoAgainstInPlaceMutation:
    """The memo holds a copy of its input, not a reference to it.

    A selector returns a slice of live state. Storing that slice by
    reference made the change check compare the slice against itself, so
    a slot mutated in place -- which ``access_slot`` does by design, on
    the live state ``seed_initial_state`` hands it -- left the cached
    result standing. A later correct replacement did not rescue it: the
    aliased reference already equalled the new value while the cached
    result predated it, so the stale answer was permanent.
    """

    def test_in_place_mutation_invalidates(self):
        cv = ComputedValue(
            name="votes",
            selector=lambda s: s.get("application", {}).get("votes", {}),
            compute_fn=lambda v: sum(v.values()),
        )
        state = {"application": {"votes": {"a": 1}}}

        assert cv.get(state) == 1
        state["application"]["votes"]["a"] = 100
        assert cv.get(state) == 100

    def test_replacement_after_in_place_mutation_invalidates(self):
        """The half that made it permanent rather than merely stale."""
        cv = ComputedValue(
            name="votes",
            selector=lambda s: s.get("application", {}).get("votes", {}),
            compute_fn=lambda v: sum(v.values()),
        )
        state = {"application": {"votes": {"a": 1}}}

        cv.get(state)
        state["application"]["votes"]["a"] = 100
        state["application"]["votes"] = {"a": 100}
        assert cv.get(state) == 100

    def test_unchanged_input_still_hits_the_cache(self):
        calls = []

        cv = ComputedValue(
            name="counted",
            selector=lambda s: s.get("application", {}).get("x", 0),
            compute_fn=lambda v: calls.append(v) or v,
        )
        state = {"application": {"x": 7}}

        assert [cv.get(state) for _ in range(4)] == [7, 7, 7, 7]
        assert len(calls) == 1

    def test_uncopyable_input_recomputes_rather_than_trusting_a_reference(self):
        class Uncopyable:
            def __deepcopy__(self, memo):
                raise TypeError("cannot copy")

            def __eq__(self, other):
                return True

        calls = []
        payload = Uncopyable()
        cv = ComputedValue(
            name="uncopyable",
            selector=lambda s: payload,
            compute_fn=lambda v: calls.append(1) or "value",
        )

        assert cv.get({}) == "value"
        assert cv.get({}) == "value"
        assert len(calls) == 2, "an input it cannot copy is an input it cannot verify"


class TestComputedSelectorValidation:
    """``@computed`` refuses a selector it cannot call.

    The selector runs inside the memo comparison, which is synchronous.
    An async one handed back a coroutine that the compute function then
    used as data, failing downstream with a message naming neither the
    decorator nor the argument. A non-callable was not checked at all.
    """

    def test_async_selector_rejected(self):
        async def selector(state):
            return state

        with pytest.raises(TypeError, match="must be synchronous"):
            computed(selector=selector)(lambda value: value)

    def test_async_dunder_call_selector_rejected(self):
        class AsyncCallable:
            async def __call__(self, state):
                return state

        with pytest.raises(TypeError, match="must be synchronous"):
            computed(selector=AsyncCallable())(lambda value: value)

    def test_non_callable_selector_rejected(self):
        with pytest.raises(TypeError, match="must be a callable"):
            computed(selector="application")(lambda value: value)

    def test_synchronous_selector_accepted(self):
        store = get_store()

        @computed(selector=lambda s: s.get("application", {}).get("accepted", 0))
        def accepted_total(value):
            return value + 1

        store.state["application"]["accepted"] = 4
        assert store.computed["accepted_total"] == 5
