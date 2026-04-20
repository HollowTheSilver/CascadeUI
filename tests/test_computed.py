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
        singleton._store_instance = None
        fresh = get_store()

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
