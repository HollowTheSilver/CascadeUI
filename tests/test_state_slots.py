"""Tests for access_slot helper and slot_property descriptor."""

from unittest.mock import MagicMock

import pytest
from helpers import make_interaction as _make_interaction

from cascadeui import access_slot, read_slot, slot_property
from cascadeui.state.singleton import get_store
from cascadeui.views.layout import StatefulLayoutView


class TestAccessSlotHelper:
    """access_slot walks state['application'][name][key] with auto-vivification."""

    def test_creates_application_dict_when_missing(self):
        state = {}
        slot = access_slot(state, "feature")
        assert slot == {}
        assert state["application"]["feature"] is slot

    def test_returns_existing_slot_dict(self):
        state = {"application": {"feature": {"existing": True}}}
        slot = access_slot(state, "feature")
        assert slot == {"existing": True}

    def test_keyed_access_returns_user_dict(self):
        state = {}
        keyed = access_slot(state, "battleship", 12345)
        assert keyed == {}
        assert state["application"]["battleship"][12345] is keyed

    def test_default_factory_seeds_missing_key(self):
        state = {}
        keyed = access_slot(state, "battleship", 999, default_factory=lambda: {"phase": "setup"})
        assert keyed == {"phase": "setup"}

    def test_default_factory_only_runs_on_first_access(self):
        state = {}
        calls = []

        def factory():
            calls.append(1)
            return {"v": 1}

        access_slot(state, "feature", "abc", default_factory=factory)
        access_slot(state, "feature", "abc", default_factory=factory)
        assert len(calls) == 1

    def test_returned_dict_is_mutable(self):
        state = {}
        keyed = access_slot(state, "feature", "k", default_factory=dict)
        keyed["written"] = True
        assert state["application"]["feature"]["k"] == {"written": True}

    def test_works_with_reducer_state_snapshot(self):
        # Reducers receive a deep-copied state. The helper mutates that
        # snapshot freely, which is exactly the contract reducers need.
        snapshot = {"application": {"other": {"keep": "this"}}}
        access_slot(snapshot, "new_feature", 42, default_factory=lambda: {"x": 1})

        assert snapshot["application"]["other"] == {"keep": "this"}
        assert snapshot["application"]["new_feature"][42] == {"x": 1}

    def test_omitting_default_factory_yields_empty_dict(self):
        state = {}
        keyed = access_slot(state, "feature", "k")
        assert keyed == {}


class TestSlotProperty:
    """slot_property descriptor reads from a slot with graceful defaults."""

    async def test_reads_value_from_seeded_slot(self):
        interaction = _make_interaction(user_id=100)

        class GameView(StatefulLayoutView):
            phase = slot_property(
                "phase", slot="bs_test", key=lambda self: self.user_id, default="setup"
            )

            async def seed_initial_state(self, state):
                access_slot(state, "bs_test", self.user_id)["phase"] = "playing"

        view = GameView(interaction=interaction)
        await view.send()

        assert view.phase == "playing"

    async def test_returns_default_when_slot_missing(self):
        # No seed -- slot doesn't exist. Descriptor must return the default
        # rather than raising KeyError.
        interaction = _make_interaction(user_id=200)

        class UnseedeView(StatefulLayoutView):
            phase = slot_property(
                "phase", slot="never_created", key=lambda self: self.user_id, default="idle"
            )

        view = UnseedeView(interaction=interaction)
        await view.send()

        assert view.phase == "idle"

    async def test_returns_default_when_key_missing(self):
        # Slot exists but this user's entry doesn't.
        interaction = _make_interaction(user_id=300)
        store = get_store()
        access_slot(store.state, "shared_slot", 999, default_factory=lambda: {"phase": "x"})

        class OtherUserView(StatefulLayoutView):
            phase = slot_property(
                "phase",
                slot="shared_slot",
                key=lambda self: self.user_id,
                default="default_phase",
            )

        view = OtherUserView(interaction=interaction)
        await view.send()

        assert view.phase == "default_phase"

    async def test_returns_default_when_field_missing(self):
        # Key exists but the named field isn't in it yet.
        interaction = _make_interaction(user_id=400)

        class PartialSeedView(StatefulLayoutView):
            score = slot_property("score", slot="ps", key=lambda self: self.user_id, default=0)

            async def seed_initial_state(self, state):
                access_slot(state, "ps", self.user_id)["other_field"] = "x"

        view = PartialSeedView(interaction=interaction)
        await view.send()

        assert view.score == 0

    async def test_reads_reflect_post_seed_writes(self):
        # Writes to the slot through access_slot are visible immediately
        # via the descriptor -- no caching, no staleness.
        interaction = _make_interaction(user_id=500)
        store = get_store()

        class LiveView(StatefulLayoutView):
            score = slot_property("score", slot="live", key=lambda self: self.user_id, default=0)

            async def seed_initial_state(self, state):
                access_slot(state, "live", self.user_id)["score"] = 10

        view = LiveView(interaction=interaction)
        await view.send()

        assert view.score == 10
        access_slot(store.state, "live", view.user_id)["score"] = 99
        assert view.score == 99

    def test_class_access_returns_descriptor(self):
        # Accessing through the class (no instance) returns the descriptor
        # itself -- standard Python descriptor protocol.
        class V(StatefulLayoutView):
            phase = slot_property("phase", slot="x", key=lambda self: 1, default=None)

        assert isinstance(V.phase, slot_property)

    def test_repr_includes_field_and_slot(self):
        sp = slot_property("phase", slot="bs", key=lambda self: 1, default="setup")
        text = repr(sp)
        assert "phase" in text
        assert "bs" in text


# // ========================================( Persistent slots )======================================== // #


class TestPersistentSlots:
    """``persistent=True`` marks a slot as write-through; PersistenceMiddleware
    writes it to the backend on every change. The default is in-memory only.
    The marker is sticky: once a slot is declared persistent, future writes
    with the same name inherit the contract without needing to re-pass
    the kwarg.
    """

    def setup_method(self):
        # Each test works against a clean slate -- the registry is module
        # level, so any leaked name bleeds into neighboring tests.
        from cascadeui.state.slots import _PERSISTENT_SLOTS

        self._preserved = set(_PERSISTENT_SLOTS)
        _PERSISTENT_SLOTS.clear()

    def teardown_method(self):
        from cascadeui.state.slots import _PERSISTENT_SLOTS

        _PERSISTENT_SLOTS.clear()
        _PERSISTENT_SLOTS.update(self._preserved)

    def test_persistent_flag_registers_name(self):
        from cascadeui.state.slots import is_persistent_slot

        state = {}
        access_slot(state, "user_settings", persistent=True)
        assert is_persistent_slot("user_settings")

    def test_default_is_not_persistent(self):
        from cascadeui.state.slots import is_persistent_slot

        state = {}
        access_slot(state, "ephemeral_cache")
        assert not is_persistent_slot("ephemeral_cache")

    def test_marker_is_sticky_across_calls(self):
        # First call marks it persistent; subsequent calls without the kwarg
        # still see the slot as persistent.
        from cascadeui.state.slots import is_persistent_slot

        state = {}
        access_slot(state, "game_saves", persistent=True)
        access_slot(state, "game_saves", key=42)
        assert is_persistent_slot("game_saves")

    def test_slot_still_works_normally_under_persistent(self):
        # Marking persistent does not change read/write semantics -- the slot
        # is populated and accessible exactly like any other.
        state = {}
        slot = access_slot(state, "user_scores", key="total", persistent=True)
        assert slot == {}
        slot["wins"] = 3
        assert state["application"]["user_scores"]["total"] == {"wins": 3}


# // ========================================( Read slot )======================================== // #


class TestReadSlot:
    """``read_slot`` is the pure-read counterpart to ``access_slot``. Walks the
    same path without mutating state or touching the persistent registry --
    safe to call from selectors and @computed, which receive live state by
    reference.
    """

    def setup_method(self):
        from cascadeui.state.slots import _PERSISTENT_SLOTS

        self._preserved = set(_PERSISTENT_SLOTS)
        _PERSISTENT_SLOTS.clear()

    def teardown_method(self):
        from cascadeui.state.slots import _PERSISTENT_SLOTS

        _PERSISTENT_SLOTS.clear()
        _PERSISTENT_SLOTS.update(self._preserved)

    def test_returns_slot_dict_when_key_omitted(self):
        state = {"application": {"feature": {"a": 1, "b": 2}}}
        slot = read_slot(state, "feature")
        assert slot == {"a": 1, "b": 2}

    def test_returns_keyed_value(self):
        state = {"application": {"bs": {42: {"phase": "setup"}}}}
        value = read_slot(state, "bs", 42)
        assert value == {"phase": "setup"}

    def test_returns_default_when_application_missing(self):
        assert read_slot({}, "feature", default="fallback") == {}
        assert read_slot({}, "feature", 1, default="fallback") == "fallback"

    def test_returns_default_when_slot_missing(self):
        state = {"application": {}}
        assert read_slot(state, "nope") == {}
        assert read_slot(state, "nope", "k", default=None) is None

    def test_returns_default_when_key_missing(self):
        state = {"application": {"feature": {}}}
        assert read_slot(state, "feature", "missing", default=7) == 7

    def test_does_not_mutate_state(self):
        # The whole point of read_slot: safe to call from selectors that
        # receive live store state by reference.
        state = {"existing": "ignore"}
        read_slot(state, "feature")
        read_slot(state, "feature", "k", default="x")
        assert state == {"existing": "ignore"}

    def test_does_not_register_persistent_slot(self):
        from cascadeui.state.slots import _PERSISTENT_SLOTS

        state = {"application": {"feature": {}}}
        read_slot(state, "feature")
        read_slot(state, "feature", 1)
        assert _PERSISTENT_SLOTS == set()

    def test_idempotent_across_calls(self):
        state = {"application": {"counter": {"v": 5}}}
        first = read_slot(state, "counter")
        second = read_slot(state, "counter")
        assert first == second == {"v": 5}
        # Same state shape both times -- no walk-time side effects.
        assert state == {"application": {"counter": {"v": 5}}}

    def test_safe_on_store_state_reference(self):
        # Calling read_slot on live store state must not create keys. This
        # is what makes the helper selector-safe.
        store = get_store()
        read_slot(store.state, "absent_slot")
        read_slot(store.state, "absent_slot", 999, default=None)
        assert "absent_slot" not in store.state.get("application", {})

    def test_walks_two_level_path(self):
        state = {"application": {"visits": {42: {"count": 7}}}}
        assert read_slot(state, "visits", 42, "count") == 7

    def test_walks_three_level_path(self):
        state = {
            "application": {
                "stats": {100: {"combat": {"wins": 12}}},
            }
        }
        assert read_slot(state, "stats", 100, "combat", "wins") == 12

    def test_walks_five_level_path(self):
        state = {
            "application": {
                "stats": {
                    "guild1": {
                        "user1": {
                            "combat": {"wins": 99},
                        }
                    }
                }
            }
        }
        assert read_slot(state, "stats", "guild1", "user1", "combat", "wins") == 99

    def test_returns_default_on_missing_intermediate(self):
        state = {"application": {"stats": {100: {"combat": {}}}}}
        assert read_slot(state, "stats", 100, "combat", "wins", default=0) == 0
        assert read_slot(state, "stats", 100, "missing", "wins", default=0) == 0

    def test_returns_default_when_intermediate_is_not_dict(self):
        state = {"application": {"flags": {42: "enabled"}}}
        # "enabled" is not a dict -- walking past it must fall back.
        assert read_slot(state, "flags", 42, "nested", default="fallback") == "fallback"

    def test_distinguishes_literal_none_from_missing(self):
        # A stored None is a valid value; a missing key returns the default.
        state = {"application": {"flags": {42: {"active": None}}}}
        assert read_slot(state, "flags", 42, "active", default="fallback") is None
        assert read_slot(state, "flags", 42, "absent", default="fallback") == "fallback"


# // ========================================( persistent_slots attribute )======================================== // #


class TestPersistentSlotsAttribute:
    """``persistent_slots`` on a view class is the declarative form of
    ``access_slot(..., persistent=True)``. Every name listed there is
    registered with the module-level set at class definition time, so
    the middleware flushes subsequent writes without a seeding hook.
    """

    def setup_method(self):
        from cascadeui.state.slots import _PERSISTENT_SLOTS

        self._preserved = set(_PERSISTENT_SLOTS)
        _PERSISTENT_SLOTS.clear()

    def teardown_method(self):
        from cascadeui.state.slots import _PERSISTENT_SLOTS

        _PERSISTENT_SLOTS.clear()
        _PERSISTENT_SLOTS.update(self._preserved)

    def test_tuple_attribute_registers_names(self):
        from cascadeui.state.slots import is_persistent_slot

        class _V(StatefulLayoutView):
            persistent_slots = ("visits", "badges")

        assert is_persistent_slot("visits")
        assert is_persistent_slot("badges")

    def test_list_attribute_accepted(self):
        from cascadeui.state.slots import is_persistent_slot

        class _V(StatefulLayoutView):
            persistent_slots = ["prefs"]

        assert is_persistent_slot("prefs")

    def test_set_attribute_accepted(self):
        from cascadeui.state.slots import is_persistent_slot

        class _V(StatefulLayoutView):
            persistent_slots = {"scores"}

        assert is_persistent_slot("scores")

    def test_empty_default_is_noop(self):
        from cascadeui.state.slots import _PERSISTENT_SLOTS

        class _V(StatefulLayoutView):
            pass

        assert _PERSISTENT_SLOTS == set()

    def test_rejects_non_collection(self):
        with pytest.raises(TypeError, match="persistent_slots"):

            class _V(StatefulLayoutView):
                persistent_slots = "not_a_collection"

    def test_rejects_non_string_entry(self):
        with pytest.raises(TypeError, match="persistent_slots entries"):

            class _V(StatefulLayoutView):
                persistent_slots = ("valid", 42)

    def test_reducer_write_is_persistent_without_hook(self):
        # The whole point of the attribute: writing to the slot from a
        # plain reducer inherits the persistence contract without any
        # seed_initial_state call.
        from cascadeui.state.slots import is_persistent_slot

        class _V(StatefulLayoutView):
            persistent_slots = ("notes",)

        state = {}
        slot = access_slot(state, "notes", key=1)
        slot["text"] = "hello"
        assert is_persistent_slot("notes")
        assert state["application"]["notes"][1]["text"] == "hello"
