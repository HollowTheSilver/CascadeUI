"""Fan-out persistence middleware tests.

Exercises :class:`~cascadeui.state.middleware.persistence.PersistenceMiddleware`
against an :class:`~cascadeui.persistence.InMemoryBackend`.

Routing tests check that each action type lands in the right namespace
(registry, application) and that bookkeeping actions never produce
writes. Scheduling tests override ``interval`` and ``max_age`` on the
namespace state so debounce/ceiling/backoff behavior can be observed
in milliseconds rather than seconds. Retry tests drive the failure
path by patching the backend's ``row_upsert`` to raise.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import importlib.util
import json
import logging
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands
from discord.ui import ActionRow, TextDisplay
from helpers import RenderableLayoutView, make_interaction, until

from cascadeui.components.base import (
    DynamicPersistentButton,
    StatefulButton,
    _dynamic_button_classes,
)
from cascadeui.persistence import (
    ApplicationPersistence,
    InMemoryBackend,
    PersistenceManager,
    RegistryPersistence,
    SlotPolicy,
)
from cascadeui.persistence.schema import (
    TABLE_APPLICATION_SLOTS,
    TABLE_PERSISTENT_VIEWS,
)
from cascadeui.setup import setup_middleware
from cascadeui.state import slots as _slots_module
from cascadeui.state.middleware import UndoMiddleware
from cascadeui.state.middleware.persistence import PersistenceMiddleware
from cascadeui.state.singleton import get_store
from cascadeui.state.slots import access_slot
from cascadeui.utils.decorators import cascade_reducer
from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.persistent import PersistentLayoutView

# // ========================================( Fixtures )======================================== // #


@pytest.fixture(autouse=True)
def _reset_persistent_slots():
    # _PERSISTENT_SLOTS is a sticky module-level set: once a slot name
    # is marked persistent, future writes to the same name inherit the
    # contract. That is correct production behavior but makes test
    # ordering significant, so snapshot-and-restore around each test.
    snapshot = set(_slots_module._PERSISTENT_SLOTS)
    yield
    _slots_module._PERSISTENT_SLOTS.clear()
    _slots_module._PERSISTENT_SLOTS.update(snapshot)


# A register/unregister or a routed write spawns a registry- or
# application-namespace flush task on the returned middleware. Left
# uncancelled, that task is bound to the test's own event loop, which
# pytest-asyncio closes at teardown -- the task can never resume, and the
# coroutine it awaits is collected as "never awaited" whenever the garbage
# collector next runs, which can land during an unrelated, later test.
# Draining here, once per test, covers every test that reaches
# _make_middleware() regardless of which class calls it.
_pending_test_middleware = []


@pytest.fixture(autouse=True)
async def _drain_persistence_middleware():
    yield
    while _pending_test_middleware:
        await _pending_test_middleware.pop().flush_all()


async def _make_middleware(
    *,
    registry: bool = True,
    application: bool = True,
) -> tuple[PersistenceMiddleware, PersistenceManager, InMemoryBackend]:
    """Construct a manager + middleware + backend wired to the singleton store.

    One shared :class:`InMemoryBackend` across both namespaces keeps
    tests terse; tests that need isolation per namespace can override
    ``ns.backend`` directly on the middleware.
    """
    backend = InMemoryBackend()
    await backend.initialize()
    store = get_store()
    mgr = PersistenceManager(
        store=store,
        registry=RegistryPersistence(backend=backend if registry else None),
        application=ApplicationPersistence(backend=backend if application else None),
    )
    middleware = PersistenceMiddleware(mgr)
    _pending_test_middleware.append(middleware)
    return middleware, mgr, backend


def _library_error(record) -> bool:
    # asyncio logs a pending task an earlier test left behind whenever the
    # collector happens to reach it, so only the library's own records count.
    return record.levelname == "ERROR" and record.name.startswith("cascadeui")


def _library_log(caplog) -> str:
    """The library's own captured messages, one per line."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("cascadeui"))


async def _drain(middleware: PersistenceMiddleware) -> None:
    """Await all pending flush tasks so the backend reflects in-flight writes."""
    tasks = [t for t in middleware._tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _register_fake_view(store, **attrs):
    """Register a stand-in live view through the store's own registration seam.

    ``get_active_view`` answers from the key index ``_register_view``
    maintains, so a view written straight into ``_active_views`` is invisible
    to it. ``instance_scope=None`` keeps the stand-in out of the instance index.
    """
    view = SimpleNamespace(
        id="fake-id",
        instance_scope=None,
        user_id=None,
        guild_id=None,
        _instance_root_class="FakeView",
        _participants=set(),
        is_finished=lambda: False,
        **attrs,
    )
    store._register_view(view)
    return view


# // ========================================( Routing: bookkeeping skip )======================================== // #


class TestMiddlewareBookkeepingSkip:
    """Bookkeeping actions must never produce backend writes."""

    @pytest.mark.parametrize(
        "action_type",
        [
            "SESSION_CREATED",
            "VIEW_CREATED",
            "VIEW_DESTROYED",
            "NAVIGATION_PUSH",
            "BATCH_COMPLETE",
            "APPLICATION_SLOTS_PRUNED",
        ],
    )
    async def test_bookkeeping_produces_no_write(self, action_type):
        middleware, mgr, backend = await _make_middleware()

        async def passthrough(action, state):
            return state

        await middleware(
            {"type": action_type, "payload": {}},
            middleware._store.state,
            passthrough,
        )
        await _drain(middleware)

        for ns in (middleware._ns_registry, middleware._ns_application):
            assert not ns.dirty_rows
            assert not ns.deleted_keys


# // ========================================( Routing: application )======================================== // #


class TestMiddlewareRoutesApplication:
    """Application slot changes drive application-namespace writes."""

    async def test_slot_change_queues_upsert(self):
        middleware, mgr, backend = await _make_middleware()
        # Tight debounce so the test finishes in milliseconds.
        middleware._ns_application.interval = 0.01
        middleware._ns_application.max_age = 0.02

        store = middleware._store
        # Opt the slot in; the middleware only scans persistent slots.
        _slots_module._PERSISTENT_SLOTS.add("prefs")

        # Seed state_before, then set an application slot and run the
        # middleware with a next_fn that returns the mutated state.
        state_before = store.state

        async def mutate_app(action, state):
            new = dict(state)
            new["application"] = {**state.get("application", {}), "prefs": {"theme": "dark"}}
            store.state = new
            return new

        await middleware({"type": "SET_PREFS", "payload": {}}, state_before, mutate_app)

        assert "prefs" in middleware._ns_application.dirty_rows
        row = middleware._ns_application.dirty_rows["prefs"]
        assert row["slot_name"] == "prefs"
        assert json.loads(row["payload"]) == {"theme": "dark"}

        # Let the scheduled task fire and reach the backend.
        await asyncio.sleep(0.05)
        await _drain(middleware)

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert len(rows) == 1
        assert rows[0]["slot_name"] == "prefs"

    async def _write_slot(self, value):
        """Route one write of ``value`` into the persistent slot ``scores``."""
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store
        _slots_module._PERSISTENT_SLOTS.add("scores")

        async def mutate_app(action, state):
            new = dict(state)
            new["application"] = {**state.get("application", {}), "scores": value}
            store.state = new
            return new

        await middleware({"type": "SET_SCORES", "payload": {}}, store.state, mutate_app)
        # The key check runs when the write takes the row.
        await middleware.flush_all()
        return middleware

    async def test_int_key_logs_an_error_and_still_saves(self, caplog):
        """An int key reads back as a string after a restart, so it is named.

        The row is still queued: refusing it would drop every other entry
        in the slot as well.
        """
        middleware = await self._write_slot({42: {"wins": 3}, "7": {"wins": 1}})

        errors = [r.getMessage() for r in caplog.records if _library_error(r)]
        assert len(errors) == 1
        assert "'scores'" in errors[0]
        assert "42" in errors[0] and "'42'" in errors[0]
        assert "str(user_id)" in errors[0]
        rows = await middleware._ns_application.backend.row_select(TABLE_APPLICATION_SLOTS)
        assert json.loads(rows[0]["payload"]) == {"42": {"wins": 3}, "7": {"wins": 1}}

    async def test_a_slot_holding_a_plain_number_is_written(self):
        """The key check walked the number as a container and raised inside
        the write, after the rows had been taken, so the write was lost."""
        middleware = await self._write_slot(5)

        rows = await middleware._ns_application.backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [5]

    async def test_nested_non_string_key_is_named_with_its_path(self, caplog):
        await self._write_slot({"guild": {"members": [{"ok": 1}, {(1, 2): "x"}]}})

        errors = [r.getMessage() for r in caplog.records if _library_error(r)]
        # A tuple key is not JSON-serializable at all, so the slot is not
        # saved and the serialization error fires instead; a bool key is
        # serializable.
        assert len(errors) == 1
        assert "'scores' was not saved" in errors[0] and ".id" in errors[0]

        caplog.clear()
        await self._write_slot({"guild": {"members": [{"ok": 1}, {True: "x"}]}})
        errors = [r.getMessage() for r in caplog.records if _library_error(r)]
        assert len(errors) == 1
        assert "scores['guild']['members'][1]" in errors[0]
        assert "'true'" in errors[0]

    async def test_the_same_key_note_is_logged_once(self, caplog):
        """A slot written on every change logged the same ERROR each time."""
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store
        _slots_module._PERSISTENT_SLOTS.add("scores")

        for wins in (3, 4):

            async def mutate_app(action, state, wins=wins):
                new = dict(state)
                new["application"] = {**state.get("application", {}), "scores": {42: wins}}
                store.state = new
                return new

            await middleware({"type": "SET_SCORES", "payload": {}}, store.state, mutate_app)
            await middleware.flush_all()

        errors = [r.getMessage() for r in caplog.records if _library_error(r)]
        assert len(errors) == 1

    async def test_a_slot_changed_many_times_is_checked_once_per_write(self, monkeypatch):
        # The check walks the whole slot, and one scoped slot holds every
        # user's data, so walking it at every commit made each scoped write
        # cost more the more users the bot had.
        from cascadeui.state.middleware import persistence as persistence_module

        middleware, mgr, backend = await _make_middleware()
        store = middleware._store
        _slots_module._PERSISTENT_SLOTS.add("scores")
        checked = []
        real = persistence_module._key_coercion_note
        monkeypatch.setattr(
            persistence_module,
            "_key_coercion_note",
            lambda value, root: checked.append(root) or real(value, root),
        )

        for wins in range(5):

            async def mutate_app(action, state, wins=wins):
                new = dict(state)
                new["application"] = {**state.get("application", {}), "scores": {"a": wins}}
                store.state = new
                return new

            await middleware({"type": "SET_SCORES", "payload": {}}, store.state, mutate_app)
        await middleware.flush_all()
        assert checked == ["scores"]

    async def test_each_write_checks_only_the_slots_it_takes(self, monkeypatch):
        from cascadeui.state.middleware import persistence as persistence_module

        middleware, mgr, backend = await _make_middleware()
        store = middleware._store
        checked = []
        real = persistence_module._key_coercion_note
        monkeypatch.setattr(
            persistence_module,
            "_key_coercion_note",
            lambda value, root: checked.append(root) or real(value, root),
        )
        for slot in ("first", "second"):
            _slots_module._PERSISTENT_SLOTS.add(slot)

            async def mutate_app(action, state, slot=slot):
                new = {**state, "application": {**state.get("application", {}), slot: {"a": 1}}}
                store.state = new
                return new

            await middleware({"type": "SET_SLOT", "payload": {}}, store.state, mutate_app)
            await middleware.flush_all()
            # A value left here would be serialized again at every later write.
            assert not middleware._ns_application.unchecked
        assert checked == ["first", "second"]

    async def test_a_value_changed_in_place_after_it_was_queued_is_still_written(self):
        # The key check read the live value, which seed_initial_state or a
        # raw reducer can change in place after it was queued: a key JSON
        # refuses made it raise after the flush had taken the rows, so every
        # slot in that write was lost. A cycle took the same guard, and would
        # hang this test rather than fail it, so the tuple key stands for both.
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store
        for slot in ("p", "q"):
            _slots_module._PERSISTENT_SLOTS.add(slot)
        p_value = {"a": 1}

        async def mutate_app(action, state):
            application = {**state.get("application", {}), "p": p_value, "q": {"b": 1}}
            new = {**state, "application": application}
            store.state = new
            return new

        await middleware({"type": "SET_BOTH", "payload": {}}, store.state, mutate_app)
        p_value[(1, 2)] = "x"
        await middleware.flush_all()
        rows = {
            row["slot_name"]: json.loads(row["payload"])
            for row in await backend.row_select(TABLE_APPLICATION_SLOTS)
        }
        assert rows == {"p": {"a": 1}, "q": {"b": 1}}

    async def test_a_slot_removed_before_its_write_is_not_checked(self, caplog):
        # Only a value the write takes is checked; this one is never written.
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store
        _slots_module._PERSISTENT_SLOTS.add("scores")

        for value in ({42: 1}, None):

            async def mutate_app(action, state, value=value):
                application = dict(state.get("application", {}))
                if value is None:
                    application.pop("scores", None)
                else:
                    application["scores"] = value
                new = {**state, "application": application}
                store.state = new
                return new

            await middleware({"type": "SET_SCORES", "payload": {}}, store.state, mutate_app)
        await middleware.flush_all()
        assert not [r for r in caplog.records if _library_error(r)]

    async def test_a_key_colliding_with_its_string_form_is_named(self, caplog):
        """Both are saved under one JSON key, and a restart keeps one value."""
        await self._write_slot({42: "int", "42": "str"})

        errors = [r.getMessage() for r in caplog.records if _library_error(r)]
        assert len(errors) == 1
        assert "keeps only one" in errors[0]

    async def test_string_keys_log_nothing(self, caplog):
        middleware = await self._write_slot({"42": {"wins": 3}, "nested": {"a": [{"b": 1}]}})

        assert not [r for r in caplog.records if _library_error(r)]
        rows = await middleware._ns_application.backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [row["slot_name"] for row in rows] == ["scores"]

    async def test_config_declared_policy_slot_queues_upsert(self):
        """A persistent SlotPolicy is enough on its own to reach the backend.

        Deliberately omits the ``_PERSISTENT_SLOTS.add`` its sibling tests
        perform: proving that manual step is unnecessary is the point. The
        policy registered the TTL and nothing else, so the opt-in scan
        skipped the slot and this write went nowhere.
        """
        backend = InMemoryBackend()
        await backend.initialize()
        store = get_store()
        mgr = PersistenceManager(
            store=store,
            registry=RegistryPersistence(backend=backend),
            application=ApplicationPersistence(
                backend=backend,
                slots={"user_preferences": SlotPolicy(persistent=True)},
            ),
        )
        middleware = PersistenceMiddleware(mgr)
        middleware._ns_application.interval = 0.01
        middleware._ns_application.max_age = 0.02

        state_before = store.state

        async def mutate_app(action, state):
            new = dict(state)
            new["application"] = {
                **state.get("application", {}),
                "user_preferences": {"theme": "dark"},
            }
            store.state = new
            return new

        await middleware({"type": "SET_PREFS", "payload": {}}, state_before, mutate_app)

        assert "user_preferences" in middleware._ns_application.dirty_rows

        await asyncio.sleep(0.05)
        await _drain(middleware)

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [r["slot_name"] for r in rows] == ["user_preferences"]

    async def test_slot_set_to_none_queues_delete(self):
        middleware, mgr, backend = await _make_middleware()
        middleware._ns_application.interval = 0.01
        store = middleware._store
        _slots_module._PERSISTENT_SLOTS.add("prefs")

        # Prime: slot present.
        store.state = {
            "application": {"prefs": {"theme": "dark"}},
            "sessions": {},
        }

        async def clear_slot(action, state):
            new = dict(state)
            new["application"] = {**state["application"], "prefs": None}
            store.state = new
            return new

        await middleware(
            {"type": "CLEAR_PREFS", "payload": {}},
            store.state,
            clear_slot,
        )

        assert "prefs" in middleware._ns_application.deleted_keys

    async def test_unmarked_slot_never_queued(self):
        # Under opt-in polarity, slots without ``persistent=True`` stay
        # in memory. This test exercises the default -- no marker, no
        # queue.
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store

        store.state = {"application": {}, "sessions": {}}
        access_slot(store.state, "scratch", "x", default_factory=dict)

        async def bump(action, state):
            new = dict(state)
            app = dict(state["application"])
            app["scratch"] = {"x": {"n": 1}}
            new["application"] = app
            store.state = new
            return new

        await middleware({"type": "BUMP_SCRATCH", "payload": {}}, store.state, bump)

        assert "scratch" not in middleware._ns_application.dirty_rows

    async def test_persistent_slot_queues_upsert(self):
        # The positive counterpart: a slot declared persistent=True
        # drives the positive-filter scan and produces a dirty row.
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store

        store.state = {"application": {}, "sessions": {}}
        access_slot(store.state, "user_prefs", "42", default_factory=dict, persistent=True)

        async def write(action, state):
            new = dict(state)
            app = dict(state["application"])
            app["user_prefs"] = {"42": {"theme": "dark"}}
            new["application"] = app
            store.state = new
            return new

        await middleware({"type": "UPDATE_PREFS", "payload": {}}, store.state, write)

        assert "user_prefs" in middleware._ns_application.dirty_rows


# // ========================================( Routing: unchanged slots )======================================== // #


def _copying_reducer(action_type, change):
    """Register ``change`` behind the real ``@cascade_reducer``, which copies the whole state."""
    if action_type not in get_store()._custom_reducers:
        cascade_reducer(action_type)(change)


def _touch_ui(action, state):
    state["application"]["ui"] = action["payload"]["n"]
    return state


def _bump_visits(action, state):
    state["application"]["visits"]["count"] += 1
    return state


class TestUnchangedSlotsAreNotWrittenAgain:
    """A ``@cascade_reducer`` copies every slot, and only slots whose stored
    form changed are written. Before, any such action rewrote every
    persistent slot and pushed each ``ttl_days`` expiry back."""

    async def _middleware(self, backend=None, **kwargs):
        backend = backend or InMemoryBackend()
        writes = []
        real = backend.row_upsert_many

        async def counting(table, rows, key_columns):
            writes.append(sorted(row["slot_name"] for row in rows))
            return await real(table, rows, key_columns)

        backend.row_upsert_many = counting
        middleware = PersistenceMiddleware(backend=backend, **kwargs)
        _pending_test_middleware.append(middleware)
        await setup_middleware(middleware)
        return middleware, backend, writes

    async def _stored(self, backend, slot):
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        return next((row for row in rows if row["slot_name"] == slot), None)

    async def test_an_action_that_leaves_saved_slots_alone_writes_nothing(self):
        middleware, backend, writes = await self._middleware()
        _slots_module._PERSISTENT_SLOTS.update({"visits", "stats"})
        await _change_slot(middleware, "visits", {"count": 6})
        await _change_slot(middleware, "stats", {"games": 2})
        await middleware.flush_all()
        assert writes == [["stats", "visits"]]
        writes.clear()

        _copying_reducer("TOUCH_UI_ONLY", _touch_ui)
        await get_store().dispatch("TOUCH_UI_ONLY", {"n": 1})
        assert not middleware._ns_application.dirty_rows
        await middleware.flush_all()
        assert writes == []

        _copying_reducer("BUMP_VISITS", _bump_visits)
        await get_store().dispatch("BUMP_VISITS", {})
        await middleware.flush_all()
        assert writes == [["visits"]]
        assert json.loads((await self._stored(backend, "visits"))["payload"]) == {"count": 7}

    async def test_a_ttl_slot_keeps_its_expiry_through_unrelated_actions(self, monkeypatch):
        clock = [1_800_000_000.0]
        monkeypatch.setattr("cascadeui.state.middleware.persistence.time.time", lambda: clock[0])
        backend = InMemoryBackend()
        middleware, backend, writes = await self._middleware(
            backend,
            application=ApplicationPersistence(
                backend=backend, slots={"cache": SlotPolicy(persistent=True, ttl_days=7)}
            ),
        )
        _slots_module._PERSISTENT_SLOTS.add("cache")
        await _change_slot(middleware, "cache", {"q": "dragons"})
        await middleware.flush_all()
        expires = (await self._stored(backend, "cache"))["expires_at"]
        assert expires == int(clock[0]) + 7 * 86400

        clock[0] += 3 * 86400
        _copying_reducer("TOUCH_UI_TTL", _touch_ui)
        await get_store().dispatch("TOUCH_UI_TTL", {"n": 1})
        await middleware.flush_all()
        assert (await self._stored(backend, "cache"))["expires_at"] == expires

    async def test_slots_loaded_at_startup_are_not_written_again(self):
        backend = InMemoryBackend()
        await backend.initialize()
        for slot, value in (("visits", {"count": 6}), ("stats", {"games": 2})):
            await backend.row_upsert(
                TABLE_APPLICATION_SLOTS,
                {
                    "slot_name": slot,
                    "payload": json.dumps(value),
                    "schema_version": 1,
                    "updated_at": 1,
                    "expires_at": None,
                },
                ["slot_name"],
            )
        _slots_module._PERSISTENT_SLOTS.update({"visits", "stats"})
        middleware, backend, writes = await self._middleware(backend)
        assert get_store().state["application"]["visits"] == {"count": 6}

        _copying_reducer("TOUCH_UI_BOOT", _touch_ui)
        await get_store().dispatch("TOUCH_UI_BOOT", {"n": 1})
        await middleware.flush_all()
        assert writes == []

    async def test_rows_a_failed_write_left_go_with_the_next_action(self):
        """Retries that ran out leave their rows queued with no flush scheduled.
        The next action took them along when every copied slot was queued; an
        action that queues nothing has to as well."""
        middleware, backend, writes = await self._middleware()
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        ns.task.cancel()

        real = backend.row_upsert_many

        async def failing(table, rows, key_columns):
            raise RuntimeError("database down")

        backend.row_upsert_many = failing
        ns.retry_count = PersistenceMiddleware.MAX_RETRIES - 1
        await middleware._flush(ns)
        assert "prefs" in ns.dirty_rows and ns.retry_count == 0
        backend.row_upsert_many = real

        ns.interval = ns.max_age = 0.01
        _copying_reducer("TOUCH_UI_RETRY", _touch_ui)
        await get_store().dispatch("TOUCH_UI_RETRY", {"n": 1})
        await until(lambda: not ns.dirty_rows, timeout=2)
        await _drain(middleware)
        assert json.loads((await self._stored(backend, "prefs"))["payload"]) == {"theme": "dark"}

    async def test_a_deleted_slot_set_back_to_its_old_value_is_written(self):
        middleware, backend, writes = await self._middleware()
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        await _change_slot(middleware, "prefs", None)
        await middleware.flush_all()
        assert await self._stored(backend, "prefs") is None

        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        assert json.loads((await self._stored(backend, "prefs"))["payload"]) == {"theme": "dark"}

    async def test_a_slot_changed_and_changed_back_before_a_save_ends_with_the_last_value(self):
        middleware, backend, writes = await self._middleware()
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()

        await _change_slot(middleware, "prefs", {"theme": "light"})
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        assert json.loads((await self._stored(backend, "prefs"))["payload"]) == {"theme": "dark"}

    async def test_the_no_policy_line_is_logged_once_per_slot(self, caplog):
        caplog.set_level(logging.DEBUG, logger="cascadeui.persistence.manager")
        middleware, backend, writes = await self._middleware()
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        for theme in ("dark", "light", "dark"):
            await _change_slot(middleware, "prefs", {"theme": theme})
            await middleware.flush_all()

        lines = [
            r for r in caplog.records if "No slot policy registered for 'prefs'" in r.getMessage()
        ]
        assert len(lines) == 1


class TestPrunedSlotsLeaveTheRunningBot:
    """Expiry and prunes remove a slot from memory, from the writes waiting,
    and from the record of what is stored, as well as from the database."""

    _clock = 1_800_000_000.0

    async def _middleware(self, monkeypatch, backend=None, ttl=("cache",)):
        clock = [self._clock]
        monkeypatch.setattr("cascadeui.state.middleware.persistence.time.time", lambda: clock[0])
        backend = backend or InMemoryBackend()
        policies = {name: SlotPolicy(persistent=True, ttl_days=7) for name in ttl}
        middleware = PersistenceMiddleware(
            backend=backend,
            application=ApplicationPersistence(backend=backend, slots=policies),
        )
        _pending_test_middleware.append(middleware)
        await setup_middleware(middleware)
        return middleware, backend, clock

    async def _stored(self, backend, slot):
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        return next((row for row in rows if row["slot_name"] == slot), None)

    async def _seed(self, backend, slot, value, expires_at):
        await backend.initialize()
        await backend.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": slot,
                "payload": json.dumps(value),
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": expires_at,
            },
            ["slot_name"],
        )

    @pytest.mark.parametrize("prune", ["sweep", "older_than_days"])
    async def test_an_expired_slot_leaves_memory_and_is_written_again_when_set(
        self, monkeypatch, prune
    ):
        middleware, backend, clock = await self._middleware(monkeypatch)
        manager = middleware._manager
        _slots_module._PERSISTENT_SLOTS.add("cache")
        await _change_slot(middleware, "cache", {"q": "dragons"})
        await middleware.flush_all()
        pruned = []
        get_store().on("APPLICATION_SLOTS_PRUNED", lambda action, state: pruned.append(action))

        clock[0] += 10 * 86400
        if prune == "sweep":
            await manager._sweep_expired_slots()
        else:
            await manager.prune_application(older_than_days=1)
        assert "cache" not in get_store().state["application"]
        assert await self._stored(backend, "cache") is None
        assert pruned[0]["payload"]["slots"] == ["cache"]
        await manager._sweep_expired_slots()
        assert len(pruned) == 1

        # Set again to the value it expired with: the record of what is stored
        # went with it, so the write is not skipped as a repeat.
        await _change_slot(middleware, "cache", {"q": "dragons"})
        await middleware.flush_all()
        assert json.loads((await self._stored(backend, "cache"))["payload"]) == {"q": "dragons"}

    async def test_a_slot_changed_since_keeps_its_new_expiry(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch)
        _slots_module._PERSISTENT_SLOTS.add("cache")
        await _change_slot(middleware, "cache", {"q": "dragons"})
        await middleware.flush_all()
        clock[0] += 5 * 86400
        await _change_slot(middleware, "cache", {"q": "wyverns"})
        await middleware.flush_all()

        clock[0] += 3 * 86400
        await middleware._manager._sweep_expired_slots()
        assert get_store().state["application"]["cache"] == {"q": "wyverns"}
        assert await self._stored(backend, "cache") is not None

    async def test_a_slot_loaded_at_startup_leaves_memory_when_it_expires(self, monkeypatch):
        backend = InMemoryBackend()
        await self._seed(backend, "cache", {"q": "dragons"}, int(self._clock) + 86400)
        _slots_module._PERSISTENT_SLOTS.add("cache")
        middleware, backend, clock = await self._middleware(monkeypatch, backend)
        assert get_store().state["application"]["cache"] == {"q": "dragons"}

        clock[0] += 2 * 86400
        await middleware._manager._sweep_expired_slots()
        assert "cache" not in get_store().state["application"]

    async def test_a_slot_whose_ttl_was_removed_stays_once_written_again(self, monkeypatch):
        backend = InMemoryBackend()
        await self._seed(backend, "cache", {"q": "dragons"}, int(self._clock) + 86400)
        _slots_module._PERSISTENT_SLOTS.add("cache")
        middleware, backend, clock = await self._middleware(monkeypatch, backend, ttl=())
        await _change_slot(middleware, "cache", {"q": "wyverns"})
        await middleware.flush_all()
        assert (await self._stored(backend, "cache"))["expires_at"] is None

        clock[0] += 2 * 86400
        await middleware._manager._sweep_expired_slots()
        assert get_store().state["application"]["cache"] == {"q": "wyverns"}

    async def test_a_deleted_slot_is_not_reported_by_a_later_sweep(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch)
        _slots_module._PERSISTENT_SLOTS.add("cache")
        await _change_slot(middleware, "cache", {"q": "dragons"})
        await middleware.flush_all()
        await _change_slot(middleware, "cache", None)
        await middleware.flush_all()
        pruned = []
        get_store().on("APPLICATION_SLOTS_PRUNED", lambda action, state: pruned.append(action))

        clock[0] += 10 * 86400
        await middleware._manager._sweep_expired_slots()
        assert pruned == []

    async def test_pruning_a_slot_drops_the_write_waiting_for_it(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        await _change_slot(middleware, "prefs", {"theme": "light"})
        assert "prefs" in ns.dirty_rows

        await middleware._manager.prune_application(slot="prefs")
        await middleware.flush_all()
        assert await self._stored(backend, "prefs") is None
        assert "prefs" not in get_store().state["application"]

        await _change_slot(middleware, "prefs", {"theme": "light"})
        await middleware.flush_all()
        assert json.loads((await self._stored(backend, "prefs"))["payload"]) == {"theme": "light"}

    async def test_pruning_a_slot_waits_for_a_write_already_running(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        ns.task.cancel()

        started, release = asyncio.Event(), asyncio.Event()
        real = backend.row_upsert_many

        async def slow(table, rows, key_columns):
            started.set()
            await release.wait()
            return await real(table, rows, key_columns)

        backend.row_upsert_many = slow
        try:
            flush = asyncio.create_task(middleware.flush_all())
            await asyncio.wait_for(started.wait(), 5)
            prune = asyncio.create_task(middleware._manager.prune_application(slot="prefs"))
            await asyncio.sleep(0.05)
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(flush, prune), 5)
        assert await self._stored(backend, "prefs") is None

    async def test_rehydrate_run_again_keeps_a_change_still_waiting(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        await _change_slot(middleware, "prefs", {"theme": "light"})

        await middleware._manager.rehydrate()
        assert get_store().state["application"]["prefs"] == {"theme": "light"}
        await middleware.flush_all()
        assert json.loads((await self._stored(backend, "prefs"))["payload"]) == {"theme": "light"}

        # The record names the waiting value, not the reloaded row, so setting
        # the slot back to the older value is written.
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        assert json.loads((await self._stored(backend, "prefs"))["payload"]) == {"theme": "dark"}

    async def test_rehydrate_run_again_keeps_a_delete_still_waiting(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        await _change_slot(middleware, "prefs", None)

        await middleware._manager.rehydrate()
        assert get_store().state["application"].get("prefs") is None

    async def test_rehydrate_run_again_keeps_the_expiry_of_a_change_still_waiting(
        self, monkeypatch
    ):
        middleware, backend, clock = await self._middleware(monkeypatch)
        _slots_module._PERSISTENT_SLOTS.add("cache")
        await _change_slot(middleware, "cache", {"q": "dragons"})
        await middleware.flush_all()
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        clock[0] += 5 * 86400
        await _change_slot(middleware, "cache", {"q": "wyverns"})

        await middleware._manager.rehydrate()
        await middleware.flush_all()
        clock[0] += 3 * 86400
        await middleware._manager._sweep_expired_slots()
        assert get_store().state["application"]["cache"] == {"q": "wyverns"}

    async def test_rehydrate_run_again_waits_for_a_write_in_flight(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        await _change_slot(middleware, "prefs", {"theme": "light"})
        ns.task.cancel()

        started, release = asyncio.Event(), asyncio.Event()
        real = backend.row_upsert_many

        async def slow(table, rows, key_columns):
            started.set()
            await release.wait()
            return await real(table, rows, key_columns)

        backend.row_upsert_many = slow
        flush = asyncio.create_task(middleware._run_flush(ns, 0))
        try:
            await asyncio.wait_for(started.wait(), 5)
            reload = asyncio.create_task(middleware._manager.rehydrate())
            await asyncio.sleep(0.05)
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(flush, reload), 5)
        assert get_store().state["application"]["prefs"] == {"theme": "light"}
        assert json.loads((await self._stored(backend, "prefs"))["payload"]) == {"theme": "light"}

    async def test_a_slot_set_again_during_a_prune_ends_the_same_in_memory_and_on_disk(
        self, monkeypatch
    ):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        _slots_module._PERSISTENT_SLOTS.update({"prefs", "other"})
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        await _change_slot(middleware, "other", {"n": 1})
        ns.task.cancel()

        started, release = asyncio.Event(), asyncio.Event()
        real = backend.row_upsert_many
        calls = []

        async def slow_first(table, rows, key_columns):
            calls.append(rows)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return await real(table, rows, key_columns)

        backend.row_upsert_many = slow_first
        tasks = [asyncio.create_task(middleware._run_flush(ns, 0))]
        try:
            await asyncio.wait_for(started.wait(), 5)
            # A flush queued on the lock, then the prune behind it, then the
            # slot set again before either runs.
            await _change_slot(middleware, "other", {"n": 2})
            ns.task.cancel()
            tasks.append(asyncio.create_task(middleware._run_flush(ns, 0)))
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(middleware._manager.prune_application(slot="prefs")))
            await asyncio.sleep(0.05)
            await _change_slot(middleware, "prefs", {"theme": "light"})
            ns.task.cancel()
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        await middleware.flush_all()
        row = await self._stored(backend, "prefs")
        on_disk = json.loads(row["payload"]) if row else None
        assert on_disk == get_store().state["application"].get("prefs")

    async def test_a_write_queued_behind_a_prune_cannot_land_before_the_slot_leaves_memory(
        self, monkeypatch
    ):
        """The prune drops the slot while it still holds the write lock: with a
        reducer holding the state turn, the drop waits, and a flush queued
        behind the prune would otherwise write the slot back in between."""
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        await _change_slot(middleware, "prefs", {"theme": "light"})
        ns.task.cancel()

        entered, gate = asyncio.Event(), asyncio.Event()

        async def hold_the_turn(action, state):
            entered.set()
            await gate.wait()
            return state

        if "HOLD_TURN_PRUNE" not in get_store()._custom_reducers:
            cascade_reducer("HOLD_TURN_PRUNE")(hold_the_turn)
        tasks = [asyncio.create_task(get_store().dispatch("HOLD_TURN_PRUNE", {}))]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            tasks.append(asyncio.create_task(middleware._manager.prune_application(slot="prefs")))
            await asyncio.sleep(0.05)
            tasks.append(asyncio.create_task(middleware._run_flush(ns, 0)))
            await asyncio.sleep(0.05)
        finally:
            gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        await middleware.flush_all()
        row = await self._stored(backend, "prefs")
        on_disk = json.loads(row["payload"]) if row else None
        assert on_disk == get_store().state["application"].get("prefs")

    async def test_a_prune_during_a_failing_write_stays_pruned(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        middleware._BACKOFF_BASE = 0.01
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        await _change_slot(middleware, "prefs", {"theme": "light"})
        ns.task.cancel()

        started, release = asyncio.Event(), asyncio.Event()
        real = backend.row_upsert_many
        calls = []

        async def failing_once(table, rows, key_columns):
            calls.append(rows)
            if len(calls) == 1:
                started.set()
                await release.wait()
                raise RuntimeError("database blip")
            return await real(table, rows, key_columns)

        backend.row_upsert_many = failing_once
        tasks = [asyncio.create_task(middleware._run_flush(ns, 0))]
        try:
            await asyncio.wait_for(started.wait(), 5)
            tasks.append(asyncio.create_task(middleware._manager.prune_application(slot="prefs")))
            await asyncio.sleep(0.05)
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        await _drain(middleware)
        assert await self._stored(backend, "prefs") is None
        assert get_store().state["application"].get("prefs") is None

    async def test_a_prune_whose_delete_raises_leaves_the_slot_and_its_waiting_write(
        self, monkeypatch
    ):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        ns = middleware._ns_application
        ns.interval = ns.max_age = 60.0
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await middleware.flush_all()
        await _change_slot(middleware, "prefs", {"theme": "light"})

        async def failing_delete(table, where):
            raise RuntimeError("database down")

        backend.row_delete = failing_delete
        try:
            with pytest.raises(RuntimeError, match="database down"):
                await middleware._manager.prune_application(slot="prefs")
        finally:
            del backend.row_delete
        assert get_store().state["application"]["prefs"] == {"theme": "light"}
        assert "prefs" in ns.dirty_rows

    async def test_a_prune_of_a_slot_not_in_memory_names_no_slot(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        pruned = []
        get_store().on("APPLICATION_SLOTS_PRUNED", lambda action, state: pruned.append(action))
        await middleware._manager.prune_application(slot="never_existed")
        assert pruned[0]["payload"]["slots"] == []

    @pytest.mark.parametrize("old_ttl, new_ttl", [(7, 30), (7, None), (None, 7)])
    async def test_a_row_written_under_another_ttl_takes_the_current_policy(
        self, monkeypatch, old_ttl, new_ttl
    ):
        clock = [self._clock]
        monkeypatch.setattr("cascadeui.state.middleware.persistence.time.time", lambda: clock[0])
        backend = InMemoryBackend()
        await backend.initialize()
        await backend.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "cache",
                "payload": json.dumps({"q": "dragons"}),
                "schema_version": 1,
                "updated_at": int(clock[0]),
                "expires_at": int(clock[0]) + old_ttl * 86400 if old_ttl else None,
            },
            ["slot_name"],
        )
        middleware = PersistenceMiddleware(
            backend=backend,
            application=ApplicationPersistence(
                backend=backend,
                slots={"cache": SlotPolicy(persistent=True, ttl_days=new_ttl)},
            ),
        )
        _pending_test_middleware.append(middleware)
        await setup_middleware(middleware)
        _slots_module._PERSISTENT_SLOTS.add("cache")

        clock[0] += 86400
        _copying_reducer("TOUCH_UI_POLICY", _touch_ui)
        await get_store().dispatch("TOUCH_UI_POLICY", {"n": 1})
        await middleware.flush_all()
        expected = int(clock[0]) + new_ttl * 86400 if new_ttl else None
        assert (await self._stored(backend, "cache"))["expires_at"] == expected

    async def test_a_policy_registered_at_runtime_reaches_the_row_at_the_next_action(
        self, monkeypatch
    ):
        middleware, backend, clock = await self._middleware(monkeypatch, ttl=())
        _slots_module._PERSISTENT_SLOTS.add("cache")
        await _change_slot(middleware, "cache", {"q": "dragons"})
        await middleware.flush_all()
        assert (await self._stored(backend, "cache"))["expires_at"] is None

        middleware._manager.register_slot_policy("cache", SlotPolicy(persistent=True, ttl_days=7))
        _copying_reducer("TOUCH_UI_RUNTIME", _touch_ui)
        await get_store().dispatch("TOUCH_UI_RUNTIME", {"n": 1})
        await middleware.flush_all()
        assert (await self._stored(backend, "cache"))["expires_at"] == int(clock[0]) + 7 * 86400

    async def test_rehydrate_run_again_drops_a_slot_that_expired(self, monkeypatch):
        middleware, backend, clock = await self._middleware(monkeypatch)
        _slots_module._PERSISTENT_SLOTS.add("cache")
        await _change_slot(middleware, "cache", {"q": "dragons"})
        await middleware.flush_all()

        clock[0] += 10 * 86400
        await middleware._manager.rehydrate()
        assert "cache" not in get_store().state["application"]


# // ========================================( Routing: registry )======================================== // #


class TestMiddlewareRoutesRegistry:
    """PERSISTENT_VIEW_REGISTERED/UNREGISTERED drive registry writes."""

    async def test_register_without_live_view_skips(self, caplog):
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store

        async def pseudo_reducer(action, state):
            new = dict(state)
            store.state = new
            return new

        await middleware(
            {
                "type": "PERSISTENT_VIEW_REGISTERED",
                "payload": {
                    "persistence_key": "GhostView:msg:999",
                    "class_name": "GhostView",
                    "message_id": 999,
                    "channel_id": 1,
                    "guild_id": None,
                    "user_id": None,
                },
            },
            store.state,
            pseudo_reducer,
        )
        # No live view, no row queued.
        assert "GhostView:msg:999" not in middleware._ns_registry.dirty_rows

    async def test_register_with_live_view_queues_row(self):
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store

        # Stub a live view holding the persistence_key.
        _register_fake_view(
            store,
            _persistence_key="TicketPanel:msg:42",
            _init_kwargs={"channel_id": 5},
            kwargs_schema_version=1,
            session_id="TicketPanel:global",
        )

        async def passthrough(action, state):
            return state

        # Real reducers mutate state, so the identity short-circuit
        # wouldn't fire in production. Simulate that by returning a
        # fresh dict from next_fn.
        async def pseudo_reducer(action, state):
            new = dict(state)
            store.state = new
            return new

        try:
            await middleware(
                {
                    "type": "PERSISTENT_VIEW_REGISTERED",
                    "payload": {
                        "persistence_key": "TicketPanel:msg:42",
                        "class_name": "TicketPanel",
                        "message_id": 42,
                        "channel_id": 5,
                        "guild_id": 7,
                        "user_id": None,
                    },
                },
                store.state,
                pseudo_reducer,
            )
            # Registry uses immediate flush.
            await _drain(middleware)
        finally:
            store._unregister_view("fake-id")

        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert len(rows) == 1
        assert rows[0]["persistence_key"] == "TicketPanel:msg:42"
        assert json.loads(rows[0]["init_kwargs"]) == {"channel_id": 5}

    async def test_capture_strips_non_persistable_kwargs(self):
        # ``persistence_key`` and ``theme`` are captured into
        # ``_init_kwargs`` by ``__init_subclass__`` for push/pop
        # reconstruction symmetry, but neither belongs in the registry
        # row body: ``persistence_key`` rides its own column, and
        # ``theme`` is a live ``Theme`` object that has no JSON shape.
        # The middleware drops both at write time so the row never
        # carries a stale or unparseable copy.
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store

        fake_theme = SimpleNamespace(name="dark", accent_colour=0x123456)
        _register_fake_view(
            store,
            _persistence_key="TicketPanel:msg:99",
            _init_kwargs={
                "channel_id": 5,
                "persistence_key": "TicketPanel:msg:99",
                "theme": fake_theme,
            },
            kwargs_schema_version=1,
            session_id="TicketPanel:global",
        )

        async def pseudo_reducer(action, state):
            new = dict(state)
            store.state = new
            return new

        try:
            await middleware(
                {
                    "type": "PERSISTENT_VIEW_REGISTERED",
                    "payload": {
                        "persistence_key": "TicketPanel:msg:99",
                        "class_name": "TicketPanel",
                        "message_id": 99,
                        "channel_id": 5,
                        "guild_id": None,
                        "user_id": None,
                    },
                },
                store.state,
                pseudo_reducer,
            )
            await _drain(middleware)
        finally:
            store._unregister_view("fake-id")

        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert len(rows) == 1
        # Only the legitimate kwarg survives. The row column carries
        # persistence_key and theme rebuilds from the class default.
        assert json.loads(rows[0]["init_kwargs"]) == {"channel_id": 5}

    async def test_capture_skips_non_json_kwargs(self, caplog):
        # Without the ``default=str`` fallback, a non-JSON kwarg now
        # surfaces as a ``TypeError`` inside the capture path. The
        # middleware logs the failure and declines the row rather than
        # writing a stringified placeholder that the reattach path
        # cannot consume.
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store

        class _Unserializable:
            pass

        _register_fake_view(
            store,
            _persistence_key="TicketPanel:msg:50",
            _init_kwargs={"weird": _Unserializable()},
            kwargs_schema_version=1,
            session_id="TicketPanel:global",
        )

        async def pseudo_reducer(action, state):
            new = dict(state)
            store.state = new
            return new

        try:
            await middleware(
                {
                    "type": "PERSISTENT_VIEW_REGISTERED",
                    "payload": {
                        "persistence_key": "TicketPanel:msg:50",
                        "class_name": "TicketPanel",
                        "message_id": 50,
                        "channel_id": 5,
                        "guild_id": None,
                        "user_id": None,
                    },
                },
                store.state,
                pseudo_reducer,
            )
            await _drain(middleware)
        finally:
            store._unregister_view("fake-id")

        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert rows == []
        # An absent row is also what an unfound view produces, so the
        # declined-row error is what proves the capture ran and refused.
        assert "weird=_Unserializable" in caplog.text

    async def test_capture_declines_kwargs_nested_too_deep_to_save(self, caplog):
        # json.dumps raises RecursionError, which the decline did not catch,
        # so the registration's dispatch raised instead of declining the row.
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store
        deep = []
        for _ in range(100000):
            deep = [deep]
        _register_fake_view(
            store,
            _persistence_key="TicketPanel:msg:51",
            _init_kwargs={"tree": deep},
            kwargs_schema_version=1,
            session_id="TicketPanel:global",
        )

        async def pseudo_reducer(action, state):
            new = dict(state)
            store.state = new
            return new

        try:
            await middleware(
                {
                    "type": "PERSISTENT_VIEW_REGISTERED",
                    "payload": {
                        "persistence_key": "TicketPanel:msg:51",
                        "class_name": "TicketPanel",
                        "message_id": 51,
                        "channel_id": 5,
                        "guild_id": None,
                        "user_id": None,
                    },
                },
                store.state,
                pseudo_reducer,
            )
            await _drain(middleware)
        finally:
            store._unregister_view("fake-id")

        assert await backend.row_select(TABLE_PERSISTENT_VIEWS) == []
        assert "tree=list" in _library_log(caplog)

    async def test_capture_walks_kwargs_nested_past_the_recursion_limit(self, caplog):
        # From Python 3.12 json.dumps writes a value this deep, and the key
        # check after it recursed and raised, so the row was never written.
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store
        deep = {}
        for _ in range(sys.getrecursionlimit() + 100):
            deep = {"x": deep}
        _register_fake_view(
            store,
            _persistence_key="TicketPanel:msg:52",
            _init_kwargs={"tree": deep},
            kwargs_schema_version=1,
            session_id="TicketPanel:global",
        )

        async def pseudo_reducer(action, state):
            new = dict(state)
            store.state = new
            return new

        try:
            await middleware(
                {
                    "type": "PERSISTENT_VIEW_REGISTERED",
                    "payload": {
                        "persistence_key": "TicketPanel:msg:52",
                        "class_name": "TicketPanel",
                        "message_id": 52,
                        "channel_id": 5,
                        "guild_id": None,
                        "user_id": None,
                    },
                },
                store.state,
                pseudo_reducer,
            )
            await _drain(middleware)
        finally:
            store._unregister_view("fake-id")

        # Written where json.dumps encodes this depth, declined by name where not.
        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert len(rows) == 1 or "tree=dict" in _library_log(caplog)

    async def test_capture_names_a_non_string_kwarg_key_and_still_saves(self, caplog):
        """An int key in a kwarg restores as a string, so the row names it.

        The row is still written: declining it would leave the panel dead on
        the next restart instead of restored with one mismatched key.
        """
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store
        _register_fake_view(
            store,
            _persistence_key="TicketPanel:msg:51",
            _init_kwargs={"labels": {"a": "x"}, "scores": {42: 3}},
            kwargs_schema_version=1,
            session_id="TicketPanel:global",
        )

        async def pseudo_reducer(action, state):
            new = dict(state)
            store.state = new
            return new

        try:
            await middleware(
                {
                    "type": "PERSISTENT_VIEW_REGISTERED",
                    "payload": {
                        "persistence_key": "TicketPanel:msg:51",
                        "class_name": "TicketPanel",
                        "message_id": 51,
                        "channel_id": 5,
                        "guild_id": None,
                        "user_id": None,
                    },
                },
                store.state,
                pseudo_reducer,
            )
            await _drain(middleware)
        finally:
            store._unregister_view("fake-id")

        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert len(rows) == 1
        assert json.loads(rows[0]["init_kwargs"]) == {"labels": {"a": "x"}, "scores": {"42": 3}}
        errors = [r.getMessage() for r in caplog.records if _library_error(r)]
        assert len(errors) == 1
        assert "'TicketPanel:msg:51'" in errors[0]
        assert "kwargs['scores']" in errors[0]
        assert "'42'" in errors[0]

    async def test_unregister_queues_delete(self):
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store

        async def pseudo_reducer(action, state):
            new = dict(state)
            store.state = new
            return new

        await middleware(
            {
                "type": "PERSISTENT_VIEW_UNREGISTERED",
                "payload": {"persistence_key": "TicketPanel:msg:42"},
            },
            store.state,
            pseudo_reducer,
        )
        await _drain(middleware)

        # Backend remains empty (nothing to delete), but the routing
        # ran without error and the delete-keys buffer was cleared
        # by the immediate flush.
        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert rows == []


# // ========================================( Debounce and max-age )======================================== // #


class TestMiddlewareDebounce:
    """Per-namespace debounce windows and max-age ceilings."""

    async def test_registry_immediate_flush(self):
        middleware, mgr, backend = await _make_middleware()
        assert middleware._ns_registry.interval == 0.0
        assert middleware._ns_application.interval == 2.0

    async def test_application_debounce_coalesces(self):
        middleware, mgr, backend = await _make_middleware()
        middleware._ns_application.interval = 0.05
        middleware._ns_application.max_age = 1.0
        store = middleware._store
        store.state = {"application": {}, "sessions": {}}
        _slots_module._PERSISTENT_SLOTS.add("counter")

        async def write(value):
            async def fn(action, state):
                new = dict(state)
                app = dict(state.get("application") or {})
                app["counter"] = {"n": value}
                new["application"] = app
                store.state = new
                return new

            return fn

        # Three rapid writes collapse into one upsert because the
        # debounce window keeps resetting.
        for n in (1, 2, 3):
            await middleware(
                {"type": "SET_COUNTER", "payload": {}},
                store.state,
                await write(n),
            )

        await asyncio.sleep(0.1)
        await _drain(middleware)

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"]) == {"n": 3}

    async def test_max_age_ceiling_fires_under_steady_traffic(self):
        # Traffic every 10ms keeps resetting a 10s idle window, so the idle
        # flush cannot fire while it lasts; only the ceiling can write before
        # the traffic stops.
        middleware, mgr, backend = await _make_middleware()
        middleware._ns_application.interval = 10.0
        middleware._ns_application.max_age = 0.05
        store = middleware._store
        store.state = {"application": {}, "sessions": {}}
        _slots_module._PERSISTENT_SLOTS.add("ticker")

        def write(n):
            async def fn(action, state):
                new = dict(state)
                app = dict(state.get("application") or {})
                app["ticker"] = {"n": n}
                new["application"] = app
                store.state = new
                return new

            return fn

        # Traffic is measured on the clock the ceiling reads, not counted in
        # ticks: that clock advances in ~16ms steps on Windows before 3.13,
        # so a fixed number of 10ms sleeps can end before any time has passed.
        loop = asyncio.get_running_loop()
        start = loop.time()
        written_during_traffic = False
        n = 0
        while loop.time() - start < 2.0:
            await middleware({"type": "TICK", "payload": {}}, store.state, write(n))
            n += 1
            await asyncio.sleep(0.01)
            if await backend.row_select(TABLE_APPLICATION_SLOTS):
                written_during_traffic = True
                break
        await middleware.close()

        assert written_during_traffic

    async def test_a_change_during_a_write_does_not_cancel_it(self):
        # Each change cancelled the flush it replaced, one inside its backend
        # write included, so under steady traffic slower than the backend no
        # write ever finished.
        started = asyncio.Event()
        release = asyncio.Event()
        outcomes = []

        class _Held(InMemoryBackend):
            async def row_upsert_many(self, *args, **kwargs):
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    outcomes.append("cancelled")
                    raise
                outcomes.append("written")
                return await super().row_upsert_many(*args, **kwargs)

        backend = _Held()
        middleware = PersistenceMiddleware(backend=backend)
        await setup_middleware(middleware)
        ns = middleware._ns_application
        ns.interval = 0.01
        ns.max_age = 60.0
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        try:
            await _change_slot(middleware, "prefs", {"theme": "dark"})
            await asyncio.wait_for(started.wait(), 2)
            await _change_slot(middleware, "prefs", {"theme": "light"})
            await asyncio.sleep(0.05)
        finally:
            release.set()
        await asyncio.wait_for(_drain(middleware), 5)

        assert outcomes[0] == "written"
        assert "cancelled" not in outcomes
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "light"}]

    @staticmethod
    def _held_backend(outcomes, started, release, *, fail_first=False, hang_first=False):
        class _Held(InMemoryBackend):
            async def row_upsert_many(self, *args, **kwargs):
                first = not outcomes
                started.set()
                try:
                    if hang_first and first:
                        await asyncio.Event().wait()
                    await release.wait()
                except asyncio.CancelledError:
                    outcomes.append("cancelled")
                    raise
                if fail_first and first:
                    outcomes.append("failed")
                    raise RuntimeError("simulated backend failure")
                outcomes.append("written")
                return await super().row_upsert_many(*args, **kwargs)

        return _Held()

    @staticmethod
    def _live(middleware):
        return sum(not t.done() for t in middleware._tasks)

    async def test_changes_during_a_slow_write_queue_one_flush(self):
        # Every change past the max-age ceiling spawned a flush that queued on
        # the write lock, one per change for as long as the write lasted.
        outcomes, started, release = [], asyncio.Event(), asyncio.Event()
        backend = self._held_backend(outcomes, started, release)
        middleware = PersistenceMiddleware(backend=backend)
        await setup_middleware(middleware)
        ns = middleware._ns_application
        ns.interval = 0.01
        ns.max_age = 0.01
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        try:
            await _change_slot(middleware, "prefs", {"n": 0})
            await asyncio.wait_for(started.wait(), 2)
            # Measured on the ceiling's clock, which advances in ~16ms steps on
            # Windows before 3.13: a tick count could end before the ceiling.
            loop = asyncio.get_running_loop()
            start = loop.time()
            n = 0
            while loop.time() - start < 0.2:
                n += 1
                await _change_slot(middleware, "prefs", {"n": n})
                await asyncio.sleep(0.005)
            assert self._live(middleware) <= 2
        finally:
            release.set()
        await asyncio.wait_for(_drain(middleware), 5)

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"n": n}]

    async def test_a_write_that_never_returns_is_retried(self, monkeypatch):
        # Nothing cancelled a write in progress, so one that never returned
        # held every later write behind it for the life of the process.
        monkeypatch.setattr(PersistenceMiddleware, "_WRITE_TIMEOUT", 0.05)
        monkeypatch.setattr(PersistenceMiddleware, "_BACKOFF_BASE", 0.01)
        outcomes, started, release = [], asyncio.Event(), asyncio.Event()
        release.set()
        backend = self._held_backend(outcomes, started, release, hang_first=True)
        middleware = PersistenceMiddleware(backend=backend)
        await setup_middleware(middleware)
        middleware._ns_application.interval = 0.01
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})

        await _until(lambda: "written" in outcomes)

        assert outcomes[0] == "cancelled"
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "dark"}]
        await middleware.close()

    async def test_a_final_write_that_never_returns_does_not_hold_the_close(self, monkeypatch):
        # The close's final flush waited on the write with no bound, ahead of
        # the backend close bound that exists for exactly this.
        monkeypatch.setattr(PersistenceMiddleware, "_WRITE_TIMEOUT", 0.05)
        outcomes, started = [], asyncio.Event()
        never = asyncio.Event()
        backend = self._held_backend(outcomes, started, never)
        middleware = PersistenceMiddleware(backend=backend)
        await setup_middleware(middleware)
        middleware._ns_application.interval = 60.0
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "dark"})

        close = asyncio.ensure_future(middleware.close())
        done, _ = await asyncio.wait({close}, timeout=2)
        if not done:
            never.set()
            await close
        assert close in done
        assert outcomes == ["cancelled"]
        assert "prefs" in middleware._ns_application.dirty_rows

    async def test_a_retry_replaces_a_flush_scheduled_during_the_write(self, monkeypatch):
        # A change during the write scheduled a flush, and the failure's retry
        # overwrote the reference to it, so that flush ran on its own schedule
        # and later changes could not reach it.
        monkeypatch.setattr(PersistenceMiddleware, "_BACKOFF_BASE", 60.0)
        outcomes, started, release = [], asyncio.Event(), asyncio.Event()
        backend = self._held_backend(outcomes, started, release, fail_first=True)
        middleware = PersistenceMiddleware(backend=backend)
        await setup_middleware(middleware)
        ns = middleware._ns_application
        ns.interval = 30.0
        ns.max_age = 60.0
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        try:
            await _change_slot(middleware, "prefs", {"n": 0})
            # Start the write now rather than after the 30s idle window.
            first = ns.task
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            middleware._spawn(middleware._run_flush(ns, wait=0.0))
            await asyncio.wait_for(started.wait(), 2)
            await _change_slot(middleware, "prefs", {"n": 1})
            during = ns.task
        finally:
            release.set()
        # A flush cancelled in its sleep returns rather than raising, so done
        # before its 30s window is the sign it was replaced.
        await _until(lambda: outcomes == ["failed"] and during.done())

        assert outcomes == ["failed"]
        assert ns.task is not during and not ns.task.done()
        await middleware.close()


# // ========================================( Retry backoff )======================================== // #


class TestMiddlewareRetryBackoff:
    """Flush failures increment retry_count and reschedule with backoff."""

    async def test_failure_reenqueues_and_increments_retry(self):
        middleware, mgr, backend = await _make_middleware()
        middleware._ns_application.interval = 0.01

        calls = {"count": 0}
        original = backend.row_upsert_many

        async def failing(table, rows, key_columns):
            calls["count"] += 1
            raise RuntimeError("simulated backend failure")

        # _flush prefers row_upsert_many, so that is the seam to fail on.
        backend.row_upsert_many = failing  # type: ignore[method-assign]
        store = middleware._store
        store.state = {"application": {}, "sessions": {}}
        _slots_module._PERSISTENT_SLOTS.add("pref")

        async def write(action, state):
            new = dict(state)
            new["application"] = {**state["application"], "pref": {"k": 1}}
            store.state = new
            return new

        await middleware({"type": "WRITE", "payload": {}}, store.state, write)

        # Let the first flush fail and bump retry_count.
        await asyncio.sleep(0.05)
        assert middleware._ns_application.retry_count >= 1
        assert "pref" in middleware._ns_application.dirty_rows

        # Restore backend and close middleware so any scheduled backoff
        # retry drains cleanly (close cancels pending tasks under the
        # write lock, then flushes the now-empty namespaces).
        backend.row_upsert_many = original  # type: ignore[method-assign]
        middleware._ns_application.dirty_rows.clear()
        middleware._ns_application.deleted_keys.clear()
        await middleware.close()

    async def test_max_retries_resets_counter(self):
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        async def failing(table, rows, key_columns):
            raise RuntimeError("boom")

        backend.row_upsert_many = failing  # type: ignore[method-assign]
        ns.dirty_rows["pref"] = {
            "slot_name": "pref",
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }
        ns.retry_count = PersistenceMiddleware.MAX_RETRIES - 1

        await middleware._flush(ns)
        # Reaching MAX_RETRIES resets the counter and stops rescheduling.
        assert ns.retry_count == 0
        await middleware.close()

    async def test_changes_during_a_backoff_wait_for_the_retry(self):
        """A change replaces the pending retry with its own flush, which used to
        run after the debounce window, so steady traffic retried a failing
        backend every window instead of backing off."""
        middleware, mgr, backend = await _make_middleware()
        await setup_middleware(middleware)
        middleware._BACKOFF_BASE = 30.0
        ns = middleware._ns_application
        ns.interval = ns.max_age = 0.01
        _slots_module._PERSISTENT_SLOTS.add("pref")
        attempts = []

        async def failing(table, rows, key_columns):
            attempts.append(rows)
            raise RuntimeError("database down")

        real = backend.row_upsert_many
        backend.row_upsert_many = failing
        try:
            await _change_slot(middleware, "pref", {"n": 0})
            await until(lambda: attempts, timeout=5)
            loop = asyncio.get_running_loop()
            end = loop.time() + 0.3
            n = 0
            while loop.time() < end:
                n += 1
                await _change_slot(middleware, "pref", {"n": n})
                await asyncio.sleep(0.02)
            assert len(attempts) == 1
            assert "pref" in ns.dirty_rows
        finally:
            backend.row_upsert_many = real
            ns.dirty_rows.clear()
            await middleware.close()

    async def test_a_write_that_lands_clears_the_retry_delay(self):
        """A flush queued behind a failed one can land while the retry still
        sleeps; a change after it is written at once, not after that retry."""
        middleware, mgr, backend = await _make_middleware()
        middleware._BACKOFF_BASE = 30.0
        ns = middleware._ns_registry
        real = backend.row_upsert_many

        async def failing(table, rows, key_columns):
            raise RuntimeError("database blip")

        def row(key):
            return {"persistence_key": key, "view_class": "Panel", "message_id": "1"}

        try:
            backend.row_upsert_many = failing
            ns.dirty_rows["a"] = row("a")
            await middleware._flush(ns)
            backend.row_upsert_many = real
            await middleware._flush(ns)

            ns.dirty_rows["c"] = row("c")
            middleware._schedule(ns)
            await until(lambda: "c" not in ns.dirty_rows, timeout=2)
        finally:
            backend.row_upsert_many = real
            await middleware.close()

    async def test_a_registry_change_during_a_backoff_waits_for_the_retry(self):
        middleware, mgr, backend = await _make_middleware()
        middleware._BACKOFF_BASE = 30.0
        ns = middleware._ns_registry
        attempts = []

        async def failing(table, rows, key_columns):
            attempts.append(rows)
            raise RuntimeError("database down")

        def row(key):
            return {"persistence_key": key, "view_class": "Panel", "message_id": "1"}

        real = backend.row_upsert_many
        backend.row_upsert_many = failing
        try:
            ns.dirty_rows["a"] = row("a")
            await middleware._flush(ns)
            assert len(attempts) == 1 and ns.task is not None

            ns.dirty_rows["b"] = row("b")
            middleware._schedule(ns)
            await asyncio.sleep(0.05)
            assert len(attempts) == 1
            assert set(ns.dirty_rows) == {"a", "b"}
        finally:
            backend.row_upsert_many = real
            ns.dirty_rows.clear()
            await middleware.close()

    async def test_retry_reenqueue_preserves_write_over_pending_delete(self):
        """A write that lands during a failed delete-flush must not be
        resurrected as a delete on the retry re-enqueue.

        Regression: the re-enqueue re-added every snapshotted delete
        unconditionally, so a re-register racing a failed unregister flush left
        the key in BOTH buffers and the next flush upsert-then-deleted it --
        dropping the newer write (a re-registered persistent view failing to
        reattach after restart).
        """
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application
        key = "pref"
        row = {
            "slot_name": key,
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }

        # Snapshot state: a delete (unregister) is pending, no dirty row yet.
        ns.deleted_keys.add(key)
        # Sit at MAX-1 so this failure hits the cap and returns without leaving
        # a scheduled backoff task past the assertions.
        ns.retry_count = PersistenceMiddleware.MAX_RETRIES - 1

        async def failing_delete(table, where):
            # A concurrent write of the same key arrives during the delete's
            # await: routing sets the dirty row and discards it from deletes.
            ns.dirty_rows[key] = row
            ns.deleted_keys.discard(key)
            raise RuntimeError("backend down mid-delete")

        backend.row_delete = failing_delete  # type: ignore[method-assign]

        await middleware._flush(ns)

        # The newer write survives and is NOT also queued for deletion.
        assert key in ns.dirty_rows
        assert key not in ns.deleted_keys

        ns.dirty_rows.clear()
        ns.deleted_keys.clear()
        await middleware.close()

    async def test_retry_reenqueue_preserves_delete_over_pending_write(self):
        """Mirror of the write-over-delete case: a delete (unregister) that
        lands during a failed write-flush must not be resurrected as a write on
        the retry re-enqueue.

        The re-enqueue guard is symmetric; the sibling test above fails a delete
        flush and covers the delete-side branch, so this one fails a write flush
        to reach the write-side branch (``if key not in ns.deleted_keys``) that
        the empty ``rows`` snapshot in the sibling never executes.
        """
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application
        key = "pref"
        row = {
            "slot_name": key,
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }

        # Snapshot state: a write (register) is pending, no delete yet.
        ns.dirty_rows[key] = row
        # Sit at MAX-1 so this failure hits the cap and returns without leaving
        # a scheduled backoff task past the assertions.
        ns.retry_count = PersistenceMiddleware.MAX_RETRIES - 1

        async def failing_upsert_many(table, rows, key_columns):
            # A concurrent unregister of the same key arrives during the write's
            # await: routing sets the delete and discards the dirty row.
            ns.deleted_keys.add(key)
            ns.dirty_rows.pop(key, None)
            raise RuntimeError("backend down mid-write")

        backend.row_upsert_many = failing_upsert_many  # type: ignore[method-assign]

        await middleware._flush(ns)

        # The newer delete survives and the stale write is NOT resurrected.
        assert key in ns.deleted_keys
        assert key not in ns.dirty_rows

        ns.dirty_rows.clear()
        ns.deleted_keys.clear()
        await middleware.close()


# // ========================================( Observability hooks )======================================== // #


class TestMiddlewareFlushCancellation:
    """A cancel landing mid-write returns the batch to the buffers.

    ``_flush`` drains ``dirty_rows``/``deleted_keys`` into a local
    snapshot before awaiting the backend. ``CancelledError`` is a
    ``BaseException``, so the retry path's ``except Exception`` never
    saw it and the snapshot went nowhere. ``flush_all`` opens exactly
    that window at shutdown: it cancels in-flight flush tasks and then
    drains, so the rows lost were the ones ``close`` exists to persist.
    """

    def _dirty_row(self, slot="pref"):
        return {
            "slot_name": slot,
            "payload": '{"k": 1}',
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }

    async def test_cancel_mid_write_returns_the_batch(self):
        middleware, _mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        started = asyncio.Event()
        original = backend.row_upsert_many

        async def hangs(table, rows, key_columns):
            started.set()
            await asyncio.sleep(3600)

        backend.row_upsert_many = hangs  # type: ignore[method-assign]
        ns.dirty_rows["pref"] = self._dirty_row()

        task = asyncio.create_task(middleware._flush(ns))
        await started.wait()
        assert ns.dirty_rows == {}, "drained into the snapshot, as designed"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert "pref" in ns.dirty_rows

        # close() drains, so the hanging stand-in has to go first or the
        # requeued row parks the shutdown on it.
        backend.row_upsert_many = original  # type: ignore[method-assign]
        await middleware.close()

    async def test_shutdown_persists_a_batch_whose_flush_was_cancelled(self):
        """The end-to-end shape: ``close`` cancels the in-flight flush,
        then drains, and the row reaches the backend."""
        middleware, _mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        started = asyncio.Event()
        hang = True
        written = []
        original = backend.row_upsert_many

        async def maybe_hangs(table, rows, key_columns):
            if hang:
                started.set()
                await asyncio.sleep(3600)
            written.extend(rows)
            return await original(table, rows, key_columns)

        backend.row_upsert_many = maybe_hangs  # type: ignore[method-assign]
        ns.dirty_rows["pref"] = self._dirty_row()
        ns.task = asyncio.ensure_future(middleware._flush(ns))
        middleware._tasks.add(ns.task)
        await started.wait()

        hang = False
        await middleware.close()

        assert [r["slot_name"] for r in written] == ["pref"]


class TestMiddlewareBatchedFlush:
    """_flush prefers row_upsert_many, falling back to per-row row_upsert."""

    def _dirty(self, ns, names):
        for n in names:
            ns.dirty_rows[n] = {
                "slot_name": n,
                "payload": "{}",
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            }

    async def test_flush_prefers_row_upsert_many(self):
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        calls = {"many": 0, "rows": 0}
        original = backend.row_upsert_many

        async def spy(table, rows, key_columns):
            calls["many"] += 1
            calls["rows"] = len(rows)
            await original(table, rows, key_columns)

        backend.row_upsert_many = spy  # type: ignore[method-assign]
        self._dirty(ns, ("a", "b"))
        await middleware._flush(ns)

        assert calls["many"] == 1  # one batched call, not two per-row calls
        assert calls["rows"] == 2

    async def test_flush_falls_back_to_per_row_upsert(self):
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        # Shadow the batch method with None so getattr finds no callable --
        # mimics a custom backend that never implemented row_upsert_many.
        backend.row_upsert_many = None  # type: ignore[assignment]

        calls = {"single": 0}
        original = backend.row_upsert

        async def spy(table, row, key_columns):
            calls["single"] += 1
            await original(table, row, key_columns)

        backend.row_upsert = spy  # type: ignore[method-assign]
        self._dirty(ns, ("a", "b"))
        await middleware._flush(ns)

        assert calls["single"] == 2  # one row_upsert per dirty row

    async def test_batched_failure_re_enqueues_whole_batch(self):
        # Raise from row_upsert_many itself (not via InMemoryBackend's
        # delegation to row_upsert) so the batched failure path has direct
        # coverage of the retry re-enqueue.
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        async def failing(table, rows, key_columns):
            raise RuntimeError("disk full")

        backend.row_upsert_many = failing  # type: ignore[method-assign]
        self._dirty(ns, ("a", "b"))
        await middleware._flush(ns)

        # The whole batch re-enqueues and the retry counter advances.
        assert ns.retry_count == 1
        assert set(ns.dirty_rows) == {"a", "b"}

        # Clear the buffer and close so the scheduled backoff task drains.
        ns.dirty_rows.clear()
        ns.deleted_keys.clear()
        await middleware.close()


class TestMiddlewareObservabilityHooks:
    """on_flush and on_error fire with the documented argument shapes."""

    async def test_a_hook_registered_during_a_flush_first_runs_on_the_next(self):
        # The hooks ran from the list registration appends to, so one that
        # registered a hook each time it ran never let the flush finish.
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application
        calls = []

        async def spreading(namespace, upserts, deletes):
            calls.append("spreading")
            mgr.register_hook("on_flush", spreading)
            # Suspends, so a regression that loops fails at the timeout below
            # instead of spinning without ever yielding to it.
            await asyncio.sleep(0)

        mgr.register_hook("on_flush", spreading)
        for flush in range(2):
            ns.dirty_rows[f"s{flush}"] = {
                "slot_name": f"s{flush}",
                "payload": "{}",
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            }
            await asyncio.wait_for(middleware._flush(ns), 5)
        assert calls == ["spreading", "spreading", "spreading"]

    async def test_on_flush_receives_namespace_and_counts(self):
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        events: list[tuple] = []

        async def observer(namespace, upserts, deletes):
            events.append((namespace, upserts, deletes))

        mgr.register_hook("on_flush", observer)

        ns.dirty_rows["a"] = {
            "slot_name": "a",
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }
        ns.deleted_keys.add("b")

        await middleware._flush(ns)

        assert events == [("application", 1, 1)]

    async def test_on_error_receives_namespace_and_exception(self):
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        errors: list[tuple] = []

        async def observer(namespace, exc):
            errors.append((namespace, exc))

        mgr.register_hook("on_error", observer)

        async def failing(table, rows, key_columns):
            raise RuntimeError("disk full")

        backend.row_upsert_many = failing  # type: ignore[method-assign]
        ns.dirty_rows["a"] = {
            "slot_name": "a",
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }

        await middleware._flush(ns)
        assert len(errors) == 1
        ns_name, exc = errors[0]
        assert ns_name == "application"
        assert isinstance(exc, RuntimeError)

        ns.dirty_rows.clear()

    async def test_hook_exception_is_swallowed(self):
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        async def bad_observer(namespace, upserts, deletes):
            raise ValueError("observer exploded")

        mgr.register_hook("on_flush", bad_observer)
        ns.dirty_rows["a"] = {
            "slot_name": "a",
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }

        # Flush should complete despite the bad observer.
        await middleware._flush(ns)
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert len(rows) == 1


# // ========================================( Shutdown )======================================== // #


class TestMiddlewareShutdown:
    """flush_all drains pending tasks; close blocks subsequent dispatches."""

    async def test_flush_all_cancels_and_drains(self):
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application
        ns.interval = 5.0  # Long enough that the scheduled task never fires naturally.
        store = middleware._store
        store.state = {"application": {"pref": {"v": 1}}, "sessions": {}}

        ns.dirty_rows["pref"] = {
            "slot_name": "pref",
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }
        # Schedule a task that would normally wait 5s.
        middleware._schedule(ns)

        await middleware.flush_all()

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert len(rows) == 1

    async def test_close_blocks_further_routing(self):
        middleware, mgr, backend = await _make_middleware()
        await middleware.close()

        async def write(action, state):
            new = dict(state)
            new["application"] = {**state.get("application", {}), "pref": {"v": 1}}
            middleware._store.state = new
            return new

        await middleware(
            {"type": "SET", "payload": {}},
            middleware._store.state,
            write,
        )
        # Closed middleware is a no-op after reducer chain; no rows queued.
        assert not middleware._ns_application.dirty_rows


class _RecordingBackend(InMemoryBackend):
    """An in-memory backend that records when it is closed."""

    def __init__(self, order=None):
        super().__init__()
        self.closed = False
        self.opened = 0
        self.order = order if order is not None else []

    async def initialize(self) -> None:
        self.closed = False
        self.opened += 1

    async def close(self) -> None:
        # Yields, as a real backend's close does, so two closes can overlap.
        await asyncio.sleep(0)
        self.closed = True
        self.order.append("persistence")


def _real_bot(cls=commands.Bot):
    # Never logged in: constructing a bot and closing it touches no network.
    return cls(command_prefix="!", intents=discord.Intents.none())


async def _set_slot(action, state):
    payload = action["payload"]
    application = {**state.get("application", {}), payload["slot"]: payload["value"]}
    return {**state, "application": application}


async def _change_slot(middleware, slot, value):
    """Change the application slot ``slot`` with a dispatch, as an application's reducer does."""
    store = middleware._store
    if "SET_SLOT" not in store._custom_reducers:
        store._register_reducer("SET_SLOT", _set_slot)
    await store.dispatch("SET_SLOT", {"slot": slot, "value": value})


async def _route_batched_write(middleware, value=None):
    """Queue one application write that stays batched until something flushes it."""
    ns = middleware._ns_application
    ns.interval = 60.0
    ns.max_age = 60.0
    _slots_module._PERSISTENT_SLOTS.add("prefs")
    await _change_slot(middleware, "prefs", value or {"theme": "dark"})
    assert "prefs" in ns.dirty_rows


def _warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and r.name.startswith("cascadeui")
    ]


async def _until(condition, timeout=2):
    # Bounded, so a regression that never meets the condition fails the test
    # instead of hanging the suite.
    await until(condition, timeout)


class TestPersistenceClosesWithTheBot:
    """A bot passed as ``bot=`` closes persistence when it closes."""

    async def test_leaving_the_bot_writes_what_was_batched_and_closes_the_backend(self):
        # ``async with bot`` is what ``bot.run()`` runs, and leaving it is
        # the bot's shutdown.
        backend = _RecordingBackend()
        bot = _real_bot()
        async with bot:
            middleware = PersistenceMiddleware(backend=backend, bot=bot)
            await setup_middleware(middleware)
            await _route_batched_write(middleware)
            assert await backend.row_select(TABLE_APPLICATION_SLOTS) == []

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "dark"}]
        assert backend.closed
        assert middleware._manager._closed

    async def test_a_write_a_cog_makes_while_it_unloads_is_written(self):
        # commands.Bot.close() unloads cogs before it closes the connection,
        # and persistence closes after all of it.
        backend = _RecordingBackend()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)

        class _SavesOnUnload(commands.Cog):
            async def cog_unload(self):
                await _route_batched_write(middleware)

        async with bot:
            await setup_middleware(middleware)
            await bot.add_cog(_SavesOnUnload())

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "dark"}]

    async def test_a_close_override_runs_first_and_persistence_closes_when_it_raises(self):
        order = []

        class _Bot(commands.Bot):
            async def close(self):
                order.append("bot")
                raise RuntimeError("the bot's own close failed")

        backend = _RecordingBackend(order)
        bot = _real_bot(_Bot)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        await setup_middleware(middleware)
        await _route_batched_write(middleware)

        with pytest.raises(RuntimeError, match="the bot's own close failed"):
            await bot.close()

        assert order == ["bot", "persistence"]
        assert len(await backend.row_select(TABLE_APPLICATION_SLOTS)) == 1

    async def test_a_prebuilt_manager_closes_with_the_bot(self):
        backend = _RecordingBackend()
        bot = _real_bot()
        manager = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=backend),
            application=ApplicationPersistence(backend=backend),
            bot=bot,
        )
        middleware = PersistenceMiddleware(manager)
        await setup_middleware(middleware)
        await _route_batched_write(middleware)

        await bot.close()

        assert len(await backend.row_select(TABLE_APPLICATION_SLOTS)) == 1
        assert backend.closed

    async def test_a_bot_started_again_in_the_same_process_saves_again(self):
        # bot.clear() re-opens a closed bot, and setup_hook runs again on the
        # next login; that setup_middleware() call reopens persistence.
        backend = _RecordingBackend()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await _route_batched_write(middleware, {"theme": "dark"})

        bot.clear()
        async with bot:
            await setup_middleware(middleware)
            assert not backend.closed
            await _route_batched_write(middleware, {"theme": "light"})

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "light"}]
        assert backend.opened == 2
        assert backend.closed

    async def test_a_restarted_bot_whose_setup_hook_builds_a_new_middleware_saves_again(self):
        # The documented setup_hook constructs PersistenceMiddleware every time
        # it runs, so the restart hands setup_middleware() a second instance.
        backends = []

        class _Bot(commands.Bot):
            async def setup_hook(self):
                backends.append(_RecordingBackend())
                await setup_middleware(PersistenceMiddleware(backend=backends[-1], bot=self))

        bot = _real_bot(_Bot)
        store = get_store()
        async with bot:
            await bot.setup_hook()  # login() runs it; nothing logs in here
            installed = next(m for m in store._middleware if isinstance(m, PersistenceMiddleware))
            await _route_batched_write(installed, {"theme": "dark"})

        bot.clear()
        async with bot:
            await bot.setup_hook()
            assert store.persistence_manager is installed._manager
            await _route_batched_write(installed, {"theme": "light"})

        rows = await backends[0].row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "light"}]
        assert backends[1].opened == 0

    async def test_two_closes_at_once_close_persistence_once(self):
        # A signal handler and a shutdown command can both close the bot.
        backend = _RecordingBackend()
        bot = _real_bot()
        async with bot:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=bot))
            await asyncio.gather(bot.close(), bot.close())
        assert backend.order == ["persistence"]

    async def test_a_close_cancelled_mid_write_still_writes_and_closes(self):
        # asyncio.run cancels a close started by another task once the task
        # owning ``async with bot`` returns, then waits for it to finish.
        writing = asyncio.Event()
        release = asyncio.Event()

        class _SlowWrite(_RecordingBackend):
            async def row_upsert_many(self, *args, **kwargs):
                writing.set()
                await release.wait()
                return await super().row_upsert_many(*args, **kwargs)

        backend = _SlowWrite()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        await setup_middleware(middleware)
        await _route_batched_write(middleware)

        closing = asyncio.create_task(bot.close())
        try:
            await asyncio.wait_for(writing.wait(), 2)
            closing.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 5)

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "dark"}]
        assert backend.closed
        assert middleware._manager._closed

    async def test_a_restart_with_a_new_bot_object_closes_with_the_new_bot(self):
        # A supervisor that builds a new bot per run passes the new one in a
        # new middleware; the installed middleware takes it over.
        class _Bot(commands.Bot):
            async def setup_hook(self):
                await setup_middleware(PersistenceMiddleware(backend=_RecordingBackend(), bot=self))

        store = get_store()
        first = _real_bot(_Bot)
        async with first:
            await first.setup_hook()
        second = _real_bot(_Bot)
        async with second:
            await second.setup_hook()
            manager = store.persistence_manager
            assert manager._bot is second
            assert "on_raw_message_delete" in second.extra_events
            assert not manager._closed

        assert manager._closed
        # The bot persistence left behind no longer closes it.
        await manager._reopen()
        await first.close()
        assert not manager._closed

    async def test_a_restart_that_passes_a_built_manager_hands_over_its_bot(self):
        # The new bot was read only from the arguments a replacement was built
        # with, so one built around its own manager left persistence on the
        # bot that had closed.
        store = get_store()
        first = _real_bot()
        async with first:
            await setup_middleware(PersistenceMiddleware(backend=_RecordingBackend(), bot=first))
        second = _real_bot()
        backend = InMemoryBackend()
        built = PersistenceManager(
            store=store,
            registry=RegistryPersistence(backend=backend),
            application=ApplicationPersistence(backend=backend),
            bot=second,
        )
        async with second:
            await setup_middleware(PersistenceMiddleware(built))
            assert store.persistence_manager._bot is second

    async def test_a_change_while_closed_is_written_when_persistence_reopens(self):
        backend = _RecordingBackend()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)

        # An exit or a timeout between the bot's close and its restart. No
        # flush runs against the closed backend, however short the debounce.
        ns = middleware._ns_application
        ns.interval = 0.01
        ns.max_age = 0.01
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        await _change_slot(middleware, "prefs", {"theme": "held"})
        await asyncio.sleep(0.05)
        assert await backend.row_select(TABLE_APPLICATION_SLOTS) == []

        bot.clear()
        async with bot:
            await setup_middleware(middleware)
            await asyncio.sleep(0.05)
            await _drain(middleware)
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
            assert [json.loads(row["payload"]) for row in rows] == [{"theme": "held"}]

    async def test_a_reopen_waits_for_a_close_still_running_in_another_task(self):
        # A restart command closes from its own task while the main task
        # clears the bot and starts it again.
        release = asyncio.Event()

        class _SlowClose(_RecordingBackend):
            async def close(self):
                await release.wait()
                await super().close()

        backend = _SlowClose()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            closing = asyncio.create_task(bot.close())
            try:
                await _until(lambda: middleware._closed)

                bot.clear()
                reopening = asyncio.create_task(setup_middleware(middleware))
                await asyncio.sleep(0.01)
                release.set()
                await asyncio.gather(closing, reopening)
            finally:
                release.set()

            assert not middleware._manager._closed
            assert not middleware._closed
            assert backend.opened == 2

    async def test_rows_a_failed_final_flush_kept_are_written_on_reopen(self):
        class _Flaky(_RecordingBackend):
            down = True

            async def row_upsert_many(self, *args, **kwargs):
                if self.down:
                    raise RuntimeError("database down")
                return await super().row_upsert_many(*args, **kwargs)

        backend = _Flaky()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await _route_batched_write(middleware)
        assert "prefs" in middleware._ns_application.dirty_rows

        backend.down = False
        bot.clear()
        async with bot:
            middleware._ns_application.interval = 0.01
            middleware._ns_application.max_age = 0.01
            await setup_middleware(middleware)
            await asyncio.sleep(0.05)
            await _drain(middleware)
            assert len(await backend.row_select(TABLE_APPLICATION_SLOTS)) == 1

    async def test_a_failed_final_flush_schedules_no_retry_after_close(self):
        # A retry would run against the backend the close shuts, and report
        # it as never initialized.
        class _Down(_RecordingBackend):
            async def row_upsert_many(self, *args, **kwargs):
                raise RuntimeError("database down")

            async def row_upsert(self, *args, **kwargs):
                raise RuntimeError("database down")

        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=_Down(), bot=bot)
        await setup_middleware(middleware)
        await _route_batched_write(middleware)

        await bot.close()

        assert [task for task in middleware._tasks if not task.done()] == []
        assert "prefs" in middleware._ns_application.dirty_rows

    async def test_a_change_while_closed_warns_once_per_close(self, caplog):
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        backend = _RecordingBackend()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)

        await _change_slot(middleware, "prefs", {"theme": "dark"})
        await _change_slot(middleware, "prefs", {"theme": "light"})
        warned = _warnings(caplog)
        assert len(warned) == 1
        assert "'prefs'" in warned[0] and "setup_middleware()" in warned[0]
        assert await backend.row_select(TABLE_APPLICATION_SLOTS) == []

        # Reopened and closed again, the next unsaved change is reported too.
        bot.clear()
        async with bot:
            await setup_middleware(middleware)
        caplog.clear()
        await _change_slot(middleware, "prefs", {"theme": "blue"})
        assert len(_warnings(caplog)) == 1

    async def test_a_panel_registered_while_closed_is_held_and_warns(self, caplog):
        # Only application slots were reported: a panel sent while persistence
        # was closed had its row held with nothing said, and a process ending
        # before the reopen lost it.
        class _Panel(PersistentLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(TextDisplay("panel"))

        middleware = PersistenceMiddleware(backend=InMemoryBackend())
        await setup_middleware(middleware)
        await middleware._store.persistence_manager.close()
        panel = _Panel(interaction=make_interaction(), persistence_key="held-panel")
        panel.interaction.original_response.return_value.guild = MagicMock(id=9)

        await panel.send()

        assert "held-panel" in middleware._ns_registry.dirty_rows
        held = [m for m in _warnings(caplog) if m.startswith("Persistence is closed")]
        assert len(held) == 1 and "'held-panel'" in held[0]

    async def test_a_change_nothing_saves_does_not_warn_while_closed(self, caplog):
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await _change_slot(middleware, "prefs", {"theme": "dark"})

        await _change_slot(middleware, "not_persistent", {"n": 1})
        # A @cascade_reducer hands back a copy of every slot: a new object,
        # the same stored value.
        await _change_slot(middleware, "prefs", {"theme": "dark"})
        assert _warnings(caplog) == []

    async def test_an_unchanged_slot_with_int_and_str_keys_does_not_warn_while_closed(self, caplog):
        # Compared with sort_keys, which refuses a dict mixing int and str
        # keys, an unchanged copy read as a change and was reported as held,
        # and serialized with it, the slot was not saved at all.
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await _change_slot(middleware, "prefs", {1: "x", "b": 2})

        await _change_slot(middleware, "prefs", {1: "x", "b": 2})
        assert _warnings(caplog) == []
        # The int key draws its own note (saved anyway); refusing the dict is
        # a different error.
        refused = [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui") and "was not saved" in r.getMessage()
        ]
        assert refused == []

    @pytest.mark.parametrize("kind", ["not_json", "too_deep"])
    async def test_a_value_the_router_declines_is_not_reported_as_held(self, caplog, kind):
        # The warning compared the value again on its own and said a change
        # was held while the error beside it said the slot was not saved.
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=bot)
        async with bot:
            await setup_middleware(middleware)
        if kind == "not_json":
            value = {"member": object()}
        else:
            value = []
            for _ in range(100000):
                value = [value]

        await _change_slot(middleware, "prefs", value)
        assert "prefs" not in middleware._ns_application.dirty_rows
        assert _warnings(caplog) == []

    async def test_a_slot_changed_from_a_value_too_deep_to_save_is_held(self, caplog):
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=bot)
        async with bot:
            await setup_middleware(middleware)
        deep = []
        for _ in range(100000):
            deep = [deep]
        await _change_slot(middleware, "prefs", deep)

        await _change_slot(middleware, "prefs", {"theme": "dark"})
        assert "prefs" in middleware._ns_application.dirty_rows
        warned = _warnings(caplog)
        assert len(warned) == 1 and "'prefs'" in warned[0]

    async def test_a_change_made_in_place_while_closed_is_reported_as_held(self, caplog):
        # Made in place, the change looks the same before and after the action
        # that picks it up; the warning goes with what that action queued.
        _slots_module._PERSISTENT_SLOTS.add("prefs")
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await _change_slot(middleware, "prefs", {"v": 1})

        access_slot(get_store().state, "prefs")["v"] = 3
        _copying_reducer("TOUCH_UI_CLOSED", _touch_ui)
        await get_store().dispatch("TOUCH_UI_CLOSED", {"n": 1})
        assert "prefs" in middleware._ns_application.dirty_rows
        warned = _warnings(caplog)
        assert len(warned) == 1 and "'prefs'" in warned[0]

    async def test_a_startup_that_fails_after_the_backends_open_still_closes_them(
        self, monkeypatch
    ):
        # A database written by a newer version fails migrations once the
        # backends are open; the bot's close is what shuts them.
        from cascadeui.exceptions import PersistenceSchemaError

        async def newer_schema(self):
            raise PersistenceSchemaError("written by a newer CascadeUI")

        monkeypatch.setattr(PersistenceManager, "apply_migrations", newer_schema)
        backend = _RecordingBackend()
        bot = _real_bot()
        async with bot:
            with pytest.raises(PersistenceSchemaError):
                await setup_middleware(PersistenceMiddleware(backend=backend, bot=bot))
            assert backend.opened == 1
        assert backend.closed

    async def test_rows_a_flush_took_survive_a_cancel_in_the_error_hook(self):
        # flush_all cancels in-flight flushes on every close; one parked in
        # the on_error hook had not put its rows back yet.
        class _Flaky(_RecordingBackend):
            down = True

            async def row_upsert_many(self, *args, **kwargs):
                if self.down:
                    raise RuntimeError("database down")
                return await super().row_upsert_many(*args, **kwargs)

        backend = _Flaky()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        await setup_middleware(middleware)
        in_hook = asyncio.Event()

        async def on_error(namespace, exc):
            in_hook.set()
            await asyncio.sleep(60)

        middleware._manager.register_hook("on_error", on_error)
        await _route_batched_write(middleware)
        middleware._ns_application.interval = 0.01
        middleware._ns_application.max_age = 0.01
        middleware._schedule(middleware._ns_application)
        await asyncio.wait_for(in_hook.wait(), 2)

        backend.down = False
        await bot.close()
        assert len(await backend.row_select(TABLE_APPLICATION_SLOTS)) == 1

    async def test_a_close_that_outlasts_a_restart_leaves_persistence_open(self):
        # A bot close() override still awaiting after super().close() (a pool
        # of its own) when the restart's setup_hook runs: the restart leaves
        # the reopen to the close, which reopens once it has shut persistence
        # and finds the bot running again.
        class _Bot(commands.Bot):
            async def close(self):
                await super().close()
                await asyncio.sleep(0.05)

        backend = _RecordingBackend()
        bot = _real_bot(_Bot)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            closing = asyncio.create_task(bot.close())
            await _until(bot.is_closed)
            bot.clear()

            await setup_middleware(middleware)
            await closing

            assert not middleware._manager._closed
            assert not middleware._closed
            assert backend.opened == 2

    async def test_a_new_bot_adopted_during_the_old_bots_close_closes_persistence(self):
        """A restart that builds a new bot object while the old bot's close
        still ran: the reopen returned for that close before wiring the new
        bot, and the old close left persistence to the bot it now follows, so
        the new bot's close never closed it."""

        class _Bot(commands.Bot):
            async def close(self):
                await super().close()
                await asyncio.sleep(0.05)

        backend = _RecordingBackend()
        first = _real_bot(_Bot)
        middleware = PersistenceMiddleware(backend=backend, bot=first)
        second = _real_bot(_Bot)
        async with first, second:
            await setup_middleware(middleware)
            closing = asyncio.ensure_future(first.close())
            await _until(first.is_closed)

            await setup_middleware(PersistenceMiddleware(backend=_RecordingBackend(), bot=second))
            await closing

            manager = middleware._manager
            assert manager._bot is second and not manager._closed
            await second.close()
            assert manager._closed and backend.closed

    async def test_two_reopens_at_once_open_the_backends_once(self):
        """Two reopens landing together (a reconnect and a close handing
        persistence back) both found it closed and opened every backend twice;
        on SQLite the first connection was left open with nothing to close it."""

        class _SlowOpen(_RecordingBackend):
            async def initialize(self):
                await asyncio.sleep(0.01)
                await super().initialize()

        backend = _SlowOpen()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await middleware._manager.close()

            await asyncio.gather(middleware._manager._reopen(), middleware._manager._reopen())

            assert backend.opened == 2

    async def test_a_new_bot_adopted_while_persistence_closes_reopens_it(self):
        """The old bot's close was still closing persistence when a restart
        adopted a new bot object. The reopen was left to that close, which
        reopened only for the bot it was closing, so persistence stayed
        closed under the running bot."""
        entered = asyncio.Event()
        release = asyncio.Event()

        class _SlowClose(_RecordingBackend):
            async def close(self):
                entered.set()
                await release.wait()
                await super().close()

        backend = _SlowClose()
        first = _real_bot()
        second = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=first)
        try:
            async with first, second:
                await setup_middleware(middleware)
                closing = asyncio.ensure_future(first.close())
                await asyncio.wait_for(entered.wait(), timeout=2)

                await setup_middleware(
                    PersistenceMiddleware(backend=_RecordingBackend(), bot=second)
                )
                release.set()
                await closing

                assert not middleware._manager._closed and backend.opened == 2
        finally:
            release.set()

    async def test_a_close_cut_off_before_the_bot_closed_does_not_reopen(self):
        """A close cancelled before discord.py's own close began, as the
        program's teardown cancels one: the bot still read as running, so the
        close reopened persistence, and a reopened SQLite connection kept the
        process from exiting."""

        class _PoolBot(commands.Bot):
            async def close(self):
                await asyncio.sleep(60)
                await super().close()

        backend = _RecordingBackend()
        bot = _real_bot(_PoolBot)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        await setup_middleware(middleware)
        closing = asyncio.ensure_future(bot.close())
        await asyncio.sleep(0.01)

        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert middleware._manager._closed
        assert backend.opened == 1

    async def test_a_restart_after_the_bot_closed_again_leaves_persistence_closed(self):
        """A SIGTERM while a restart's setup_hook loaded cogs closed the bot,
        and the setup_middleware() after it reopened the backends with nothing
        left to close them: a SQLite bot hung until it was killed."""
        backend = _RecordingBackend()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await bot.close()
            bot.clear()
            await bot.close()

            await setup_middleware(middleware)

            assert middleware._manager._closed
            assert backend.opened == 1

    async def test_a_restart_during_a_close_already_running_leaves_persistence_closed(self):
        """A shutdown during a restart, in a bot whose close() closes a pool of
        its own first: setup_middleware() reopened persistence under it, and
        the close then skipped it as belonging to the restart."""

        class _PoolBot(commands.Bot):
            async def close(self):
                await asyncio.sleep(0.05)
                await super().close()

        backend = _RecordingBackend()
        bot = _real_bot(_PoolBot)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await bot.close()
            bot.clear()
            shutdown = asyncio.ensure_future(bot.close())
            await asyncio.sleep(0.01)

            await setup_middleware(middleware)
            await shutdown

            assert middleware._manager._closed and backend.closed
            assert backend.opened == 1

    async def test_a_backend_close_that_never_returns_does_not_hold_the_bot(
        self, monkeypatch, caplog
    ):
        # The backend's close runs inside the bot's close, so one that never
        # returned kept the bot from exiting.
        import cascadeui.persistence.manager as manager_module

        monkeypatch.setattr(manager_module, "_BACKEND_CLOSE_SECONDS", 0.05)
        stuck = asyncio.Event()

        class _HungClose(_RecordingBackend):
            async def close(self):
                await stuck.wait()

        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=_HungClose(), bot=bot)
        try:
            async with bot:
                await setup_middleware(middleware)
                closing = asyncio.create_task(bot.close())
                done, _ = await asyncio.wait({closing}, timeout=2)
                assert closing in done
        finally:
            stuck.set()

        assert middleware._manager._closed
        assert any("_HungClose" in m and "timed out" in m for m in _warnings(caplog))

    async def test_a_close_cut_off_before_it_finished_is_reopened(self):
        # _closed is set only at the end of a close; one cancelled part way,
        # with the middleware closed and a backend half shut, still reopens.
        closing_backend = asyncio.Event()

        class _Hangs(_RecordingBackend):
            async def close(self):
                closing_backend.set()
                await asyncio.sleep(60)

        backend = _Hangs()
        middleware = PersistenceMiddleware(backend=backend)
        await setup_middleware(middleware)
        manager = middleware._manager
        closing = asyncio.create_task(manager.close())
        await asyncio.wait_for(closing_backend.wait(), 2)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        assert middleware._closed and not manager._closed

        await setup_middleware(middleware)
        assert backend.opened == 2
        assert not middleware._closed

    async def test_a_cancelled_close_is_not_swallowed_while_it_waits_on_a_task(self):
        # Waiting on a task it cancelled, close() took a cancel of its own
        # caller as that task's and finished as if nothing had happened.
        middleware = PersistenceMiddleware(backend=InMemoryBackend())
        await setup_middleware(middleware)
        manager = middleware._manager

        async def slow_to_stop():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)

        restore = asyncio.create_task(slow_to_stop())
        manager._post_ready_restore_tasks.add(restore)
        await asyncio.sleep(0)
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0.05)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

    def test_a_bot_run_twice_through_asyncio_run_keeps_saving(self, tmp_path):
        # Each asyncio.run is a new loop, and a lock a waiter touched in the
        # first refuses the second.
        from cascadeui.persistence import SQLiteBackend

        backend = SQLiteBackend(str(tmp_path / "loops.db"))
        middleware = PersistenceMiddleware(backend=backend)
        _slots_module._PERSISTENT_SLOTS.add("prefs")

        async def walk():
            await asyncio.sleep(0.01)

        async def run(theme):
            await setup_middleware(middleware)
            manager = middleware._manager
            await _change_slot(middleware, "prefs", {"theme": theme})
            # Every lock waits once in each loop.
            await asyncio.gather(
                middleware.flush_all(),
                middleware.flush_all(),
                backend.kv_write("ns", "a", theme.encode()),
                backend.kv_write("ns", "b", theme.encode()),
                manager._serialized_walk("first", walk),
                manager._serialized_walk("second", walk),
            )
            await asyncio.gather(manager.close(), manager.close())

        asyncio.run(run("dark"))
        asyncio.run(run("light"))

        async def read():
            reader = SQLiteBackend(str(tmp_path / "loops.db"))
            await reader.initialize()
            try:
                rows = await reader.row_select(TABLE_APPLICATION_SLOTS)
                return [json.loads(row["payload"]) for row in rows], await reader.kv_read("ns", "b")
            finally:
                await reader.close()

        assert asyncio.run(read()) == ([{"theme": "light"}], b"light")

    async def test_initializing_again_wraps_the_close_once(self):
        # setup_middleware always re-runs initialize(); a second wrap would
        # close persistence twice over a close that already closed it.
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=bot)
        await setup_middleware(middleware)
        wrapped = bot.close
        await setup_middleware(middleware)
        assert bot.close is wrapped

    @pytest.mark.skipif(
        importlib.util.find_spec("aiosqlite") is None, reason="aiosqlite not installed"
    )
    async def test_a_bot_process_on_sqlite_exits_and_keeps_its_writes(self, tmp_path):
        # SQLite runs on a worker thread that holds the interpreter open
        # until the backend closes, so the process not exiting is the
        # failure this measures, and the timeout is what reports it.
        db = tmp_path / "shutdown.db"
        script = (
            "import asyncio, sys\n"
            "import discord\n"
            "from discord.ext import commands\n"
            "from cascadeui import PersistenceMiddleware, SQLiteBackend, access_slot,"
            " cascade_reducer, get_store, setup_middleware\n"
            "@cascade_reducer('WRITE')\n"
            "async def write(action, state):\n"
            "    access_slot(state, 'probe', 'k', persistent=True)['value'] = 42\n"
            "    return state\n"
            "async def main():\n"
            "    bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())\n"
            "    async with bot:\n"
            "        backend = SQLiteBackend(sys.argv[1])\n"
            "        await setup_middleware(PersistenceMiddleware(backend=backend, bot=bot))\n"
            "        await get_store().dispatch('WRITE', {})\n"
            "asyncio.run(main())\n"
        )
        subprocess.run(
            [sys.executable, "-c", script, str(db)], check=True, timeout=60, capture_output=True
        )

        from cascadeui.persistence import SQLiteBackend

        backend = SQLiteBackend(str(db))
        await backend.initialize()
        try:
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        finally:
            await backend.close()
        assert [json.loads(row["payload"]) for row in rows] == [{"k": {"value": 42}}]

    @pytest.mark.skipif(
        importlib.util.find_spec("aiosqlite") is None, reason="aiosqlite not installed"
    )
    async def test_a_close_from_another_task_finishes_before_the_process_exits(self, tmp_path):
        # A shutdown command closes the bot from its own task. The task that
        # owns ``async with bot`` returns as soon as the bot has closed, and
        # asyncio.run then cancels the command task mid-way through closing
        # persistence.
        db = tmp_path / "command.db"
        script = (
            "import asyncio, sys\n"
            "import discord\n"
            "from discord.ext import commands\n"
            "from cascadeui import PersistenceMiddleware, SQLiteBackend, access_slot,"
            " cascade_reducer, get_store, setup_middleware\n"
            "@cascade_reducer('WRITE')\n"
            "async def write(action, state):\n"
            "    access_slot(state, 'probe', 'k', persistent=True)['value'] = 42\n"
            "    return state\n"
            "async def main():\n"
            "    bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())\n"
            "    async with bot:\n"
            "        backend = SQLiteBackend(sys.argv[1])\n"
            "        await setup_middleware(PersistenceMiddleware(backend=backend, bot=bot))\n"
            "        await get_store().dispatch('WRITE', {})\n"
            "        asyncio.get_running_loop().call_later(\n"
            "            0.05, lambda: asyncio.ensure_future(bot.close())\n"
            "        )\n"
            "        while not bot.is_closed():\n"
            "            await asyncio.sleep(0.01)\n"
            "asyncio.run(main())\n"
        )
        subprocess.run(
            [sys.executable, "-c", script, str(db)], check=True, timeout=60, capture_output=True
        )

        from cascadeui.persistence import SQLiteBackend

        backend = SQLiteBackend(str(db))
        await backend.initialize()
        try:
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        finally:
            await backend.close()
        assert [json.loads(row["payload"]) for row in rows] == [{"k": {"value": 42}}]

    @pytest.mark.skipif(
        importlib.util.find_spec("aiosqlite") is None, reason="aiosqlite not installed"
    )
    async def test_a_bot_process_restarted_in_place_on_sqlite_saves_and_exits(self, tmp_path):
        # The second run reconnects SQLite through setup_middleware(), and its
        # close must release the new worker thread too.
        db = tmp_path / "restart.db"
        script = (
            "import asyncio, sys\n"
            "import discord\n"
            "from discord.ext import commands\n"
            "from cascadeui import PersistenceMiddleware, SQLiteBackend, access_slot,"
            " cascade_reducer, get_store, setup_middleware\n"
            "@cascade_reducer('WRITE')\n"
            "async def write(action, state):\n"
            "    access_slot(state, 'probe', 'k', persistent=True)['value'] = action['payload']['v']\n"
            "    return state\n"
            "async def main():\n"
            "    bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())\n"
            "    middleware = PersistenceMiddleware(backend=SQLiteBackend(sys.argv[1]), bot=bot)\n"
            "    async with bot:\n"
            "        await setup_middleware(middleware)\n"
            "        await get_store().dispatch('WRITE', {'v': 1})\n"
            "    bot.clear()\n"
            "    async with bot:\n"
            "        await setup_middleware(middleware)\n"
            "        await get_store().dispatch('WRITE', {'v': 2})\n"
            "asyncio.run(main())\n"
        )
        subprocess.run(
            [sys.executable, "-c", script, str(db)], check=True, timeout=60, capture_output=True
        )

        from cascadeui.persistence import SQLiteBackend

        backend = SQLiteBackend(str(db))
        await backend.initialize()
        try:
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        finally:
            await backend.close()
        assert [json.loads(row["payload"]) for row in rows] == [{"k": {"value": 2}}]


class TestPersistenceClosesOnSigterm:
    """systemd, ``docker stop``, and process managers stop a bot with SIGTERM,
    which discord.py leaves at its default: the process died at once, with
    the batched writes, and persistence never closed."""

    @staticmethod
    def _signals(monkeypatch, disposition=signal.SIG_DFL, supported=True):
        """Record the SIGTERM handler instead of installing it on the process."""
        state = SimpleNamespace(disposition=disposition, handler=None, removed=False, raised=[])
        getsignal = signal.getsignal
        monkeypatch.setattr(
            signal,
            "getsignal",
            lambda sig: state.disposition if sig == signal.SIGTERM else getsignal(sig),
        )
        monkeypatch.setattr(signal, "raise_signal", state.raised.append)

        def add_signal_handler(sig, callback, *args):
            if not supported:
                raise NotImplementedError
            assert sig == signal.SIGTERM
            state.handler = callback
            state.disposition = "installed"

        def remove_signal_handler(sig):
            assert sig == signal.SIGTERM
            state.removed = True
            state.disposition = signal.SIG_DFL
            return True

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", add_signal_handler)
        monkeypatch.setattr(loop, "remove_signal_handler", remove_signal_handler)
        return state

    async def test_sigterm_closes_the_bot_and_writes_what_was_batched(self, monkeypatch):
        signals = self._signals(monkeypatch)
        backend = _RecordingBackend()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        await setup_middleware(middleware)
        await _route_batched_write(middleware)

        signals.handler()
        await middleware._sigterm_close

        assert bot.is_closed()
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "dark"}]
        assert backend.closed

    async def test_the_first_sigterm_puts_the_default_back(self, monkeypatch):
        """The handler stayed for the loop's life, so a program running other
        work beside the bot no longer ended on SIGTERM at all."""
        signals = self._signals(monkeypatch)
        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=_real_bot())
        await setup_middleware(middleware)

        signals.handler()

        assert signals.removed
        assert signals.disposition is signal.SIG_DFL
        await middleware._sigterm_close

    async def test_a_handler_the_application_set_first_keeps_sigterm(self, monkeypatch):
        signals = self._signals(monkeypatch, disposition=lambda *args: None)

        await setup_middleware(PersistenceMiddleware(backend=InMemoryBackend(), bot=_real_bot()))

        assert signals.handler is None

    async def test_a_handler_the_application_sets_later_takes_over(self, monkeypatch):
        """asyncio still calls its own handler after signal.signal() replaces
        the process's, so both would run."""
        signals = self._signals(monkeypatch)
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=bot)
        await setup_middleware(middleware)

        signals.disposition = lambda *args: None
        signals.handler()

        assert middleware._sigterm_close is None
        assert not bot.is_closed()
        await bot.close()

    async def test_a_loop_without_signal_handling_installs_nothing(self, monkeypatch):
        """Windows, or a loop not on the main thread."""
        signals = self._signals(monkeypatch, supported=False)
        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=_real_bot())

        await setup_middleware(middleware)

        assert signals.handler is None
        assert middleware._sigterm_disposition is None

    async def test_without_a_bot_sigterm_is_left_alone(self, monkeypatch):
        """With no bot to close, the caller closes persistence."""
        signals = self._signals(monkeypatch)

        await setup_middleware(PersistenceMiddleware(backend=InMemoryBackend()))

        assert signals.handler is None

    _SCRIPT = (
        "import asyncio, os, signal, sys\n"
        "import discord\n"
        "from discord.ext import commands\n"
        "from cascadeui import PersistenceMiddleware, SQLiteBackend, access_slot,"
        " cascade_reducer, get_store, setup_middleware\n"
        "@cascade_reducer('WRITE')\n"
        "async def write(action, state):\n"
        "    access_slot(state, 'probe', 'k', persistent=True)['value'] = action['payload']['v']\n"
        "    return state\n"
        # connect() stands in for the gateway, as start() runs it: the main
        # task sits in it until the bot closes.
        "class Bot(commands.Bot):\n"
        "    async def connect(self, *, reconnect=True):\n"
        "        while not self.is_closed():\n"
        "            await asyncio.sleep(0.01)\n"
        "bot = Bot(command_prefix='!', intents=discord.Intents.none())\n"
        "middleware = PersistenceMiddleware(backend=SQLiteBackend(sys.argv[1]), bot=bot)\n"
        "async def run(v, stop):\n"
        "    async with bot:\n"
        "        await setup_middleware(middleware)\n"
        "        await get_store().dispatch('WRITE', {'v': v})\n"
        "        if stop:\n"
        "            asyncio.get_running_loop().call_later(\n"
        "                0.05, os.kill, os.getpid(), signal.SIGTERM\n"
        "            )\n"
        "            await bot.connect()\n"
    )

    @staticmethod
    async def _saved(db):
        from cascadeui.persistence import SQLiteBackend

        backend = SQLiteBackend(str(db))
        await backend.initialize()
        try:
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        finally:
            await backend.close()
        return [json.loads(row["payload"]) for row in rows]

    @pytest.mark.skipif(sys.platform == "win32", reason="an event loop cannot handle SIGTERM")
    @pytest.mark.skipif(
        importlib.util.find_spec("aiosqlite") is None, reason="aiosqlite not installed"
    )
    async def test_a_bot_process_stopped_with_sigterm_keeps_its_writes(self, tmp_path):
        # The process dying of the signal is the failure: check=True reports it.
        db = tmp_path / "sigterm.db"
        script = self._SCRIPT + "asyncio.run(run(42, True))\n"
        result = subprocess.run(
            [sys.executable, "-c", script, str(db)], timeout=60, capture_output=True, text=True
        )

        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr, result.stderr
        assert await self._saved(db) == [{"k": {"value": 42}}]

    @pytest.mark.skipif(sys.platform == "win32", reason="an event loop cannot handle SIGTERM")
    @pytest.mark.skipif(
        importlib.util.find_spec("aiosqlite") is None, reason="aiosqlite not installed"
    )
    async def test_a_bot_run_again_on_a_new_loop_still_closes_on_sigterm(self, tmp_path):
        # The first loop's handler goes when that loop closes, and the second
        # run finds the bot's close already wrapped.
        db = tmp_path / "sigterm_restart.db"
        script = (
            self._SCRIPT
            + "asyncio.run(run(1, False))\n"
            + "bot.clear()\n"
            + "asyncio.run(run(2, True))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(db)], timeout=60, capture_output=True, text=True
        )

        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr, result.stderr
        assert await self._saved(db) == [{"k": {"value": 2}}]


class TestPersistenceFollowsTheBotsConnection:
    """discord.py closes the client itself on some gateway drops, and a bot
    reconnects through ``clear()`` and ``connect()`` without running
    ``setup_hook``; a close from another task still has persistence to close
    when ``connect()`` returns."""

    class _OfflineBot(commands.Bot):
        """A bot whose connect() stands in for the gateway: it returns once the bot closes."""

        async def connect(self, *, reconnect=True):
            while not self.is_closed():
                await asyncio.sleep(0.005)

    async def test_a_bot_that_reconnects_without_setup_hook_reopens_persistence(self):
        backend = _RecordingBackend()
        bot = _real_bot(self._OfflineBot)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            # What connect() does itself on a close code it cannot resume from.
            await bot.close()
            assert middleware._manager._closed

            bot.clear()
            connecting = asyncio.ensure_future(bot.connect())
            await _until(lambda: not middleware._manager._closed)
            await _route_batched_write(middleware)
            await middleware.flush_all()
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
            assert [json.loads(row["payload"]) for row in rows] == [{"theme": "dark"}]
            await bot.close()
            await connecting

    async def test_connect_returns_after_a_close_from_another_task_finishes(self):
        """The program ends when connect() returns, and asyncio.run then cut
        off the close still running in the other task: a SQLite worker thread
        outlived the loop and printed a traceback on every such stop."""

        class _SlowClose(_RecordingBackend):
            async def close(self):
                await asyncio.sleep(0.2)
                await super().close()

        backend = _SlowClose()
        bot = _real_bot(self._OfflineBot)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            asyncio.get_running_loop().call_later(0.02, lambda: asyncio.ensure_future(bot.close()))
            await bot.connect()
            assert backend.closed

    async def test_sigterm_is_not_handled_while_the_backends_open(self, monkeypatch):
        """A close while the backends were opening found nothing to close, and
        the backend then opened with nothing left to close it: the process hung
        until the process manager killed it."""
        signals = TestPersistenceClosesOnSigterm._signals(monkeypatch)
        seen = []

        class _Opening(_RecordingBackend):
            async def initialize(self):
                seen.append(signals.handler)
                await super().initialize()

        await setup_middleware(PersistenceMiddleware(backend=_Opening(), bot=_real_bot()))

        assert seen == [None]
        assert signals.handler is not None

    async def test_a_connect_after_the_bot_closed_leaves_persistence_closed(self):
        """A SIGTERM while setup_hook ran closed the bot and persistence, then
        start() reached connect(), which reopened the backends with nothing
        left to close them: a SQLite bot hung until it was killed."""
        backend = _RecordingBackend()
        bot = _real_bot(self._OfflineBot)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await bot.close()

            await bot.connect()

            assert middleware._manager._closed
            assert backend.opened == 1

    async def test_a_close_already_running_when_the_bot_reconnects_still_closes_persistence(self):
        """A shutdown during a reconnect's backoff, in a bot whose close()
        closes a pool of its own first: the reconnect reopened persistence
        under it, and the close then skipped it as belonging to a restart."""

        class _PoolBot(self._OfflineBot):
            async def close(self):
                await asyncio.sleep(0.05)
                await super().close()

        backend = _RecordingBackend()
        bot = _real_bot(_PoolBot)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await bot.close()
            bot.clear()
            shutdown = asyncio.ensure_future(bot.close())
            await asyncio.sleep(0.01)

            await bot.connect()
            await shutdown

            assert bot.is_closed()
            assert middleware._manager._closed and backend.closed
            assert backend.opened == 1

    async def test_a_bot_reconnects_when_persistence_cannot_reopen(self, caplog):
        """A database still restarting made connect() raise before it reached
        the gateway, so a reconnect loop that catches discord errors ended."""

        class _Restarting(_RecordingBackend):
            async def initialize(self):
                await super().initialize()
                if self.opened > 1:
                    raise OSError("connection refused")

        bot = _real_bot(self._OfflineBot)
        middleware = PersistenceMiddleware(backend=_Restarting(), bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await bot.close()
            bot.clear()
            asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(bot.close()))

            with caplog.at_level("ERROR", logger="cascadeui"):
                await bot.connect()

            assert middleware._manager._closed
            assert any("Could not reopen persistence" in r.getMessage() for r in caplog.records)

    async def test_a_bot_reconnected_after_sigterm_ends_the_process(self, monkeypatch):
        """A reconnect after the SIGTERM close ran on until the process manager
        killed it, and lost what it had batched by then."""
        signals = TestPersistenceClosesOnSigterm._signals(monkeypatch)
        bot = _real_bot(self._OfflineBot)
        middleware = PersistenceMiddleware(backend=_RecordingBackend(), bot=bot)
        async with bot:
            await setup_middleware(middleware)
            signals.handler()
            await middleware._sigterm_close
            bot.clear()

            connecting = asyncio.ensure_future(bot.connect())
            await _until(lambda: signals.raised)

            assert signals.raised == [signal.SIGTERM]
            assert middleware._manager._closed
            assert signals.disposition is signal.SIG_DFL
            await bot.close()
            await connecting

    async def test_a_change_held_since_the_sigterm_is_written_before_the_end(self, monkeypatch):
        signals = TestPersistenceClosesOnSigterm._signals(monkeypatch)
        backend = _RecordingBackend()
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            signals.handler()
            await middleware._sigterm_close
            await _route_batched_write(middleware)
            bot.clear()

            await setup_middleware(middleware)

            assert signals.raised == [signal.SIGTERM]
            assert middleware._manager._closed
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "dark"}]

    async def test_a_restart_after_sigterm_is_left_to_a_handler_the_application_installed(
        self, monkeypatch
    ):
        signals = TestPersistenceClosesOnSigterm._signals(monkeypatch)
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=_RecordingBackend(), bot=bot)
        async with bot:
            await setup_middleware(middleware)
            signals.handler()
            await middleware._sigterm_close
            signals.disposition = lambda *args: None
            bot.clear()

            await setup_middleware(middleware)

            assert signals.raised == []
            assert not middleware._manager._closed

    async def test_a_restart_does_not_wait_on_a_close_that_waits_for_ready(self):
        """A shutdown during a restart's setup_hook, in a bot whose close()
        waits for READY first (a goodbye message): the reopen waited for that
        close, and READY comes only after setup_hook returns, so the restart
        hung until the process was killed."""

        class _Goodbye(self._OfflineBot):
            async def close(self):
                await self.wait_until_ready()
                await super().close()

            async def connect(self, *, reconnect=True):
                self._ready.set()
                await super().connect(reconnect=reconnect)

        backend = _RecordingBackend()
        bot = _real_bot(_Goodbye)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        async with bot:
            await setup_middleware(middleware)
            first = asyncio.ensure_future(bot.connect())
            await asyncio.sleep(0.02)
            await bot.close()
            await first
            bot.clear()
            shutdown = asyncio.ensure_future(bot.close())
            await asyncio.sleep(0)
            try:
                await asyncio.wait_for(setup_middleware(middleware), timeout=3)
                await asyncio.wait_for(bot.connect(), timeout=3)
                await shutdown
            finally:
                # A hung restart fails here instead of hanging the teardown too.
                bot._ready.set()

            assert bot.is_closed()
            assert middleware._manager._closed and backend.opened == 1

    async def test_a_sigterm_after_the_bot_closed_ends_the_program(self, monkeypatch):
        """With the bot closed there is nothing for the handler to close, so a
        program still running other work swallowed the SIGTERM."""
        signals = TestPersistenceClosesOnSigterm._signals(monkeypatch)
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=_RecordingBackend(), bot=bot)
        await setup_middleware(middleware)
        await bot.close()

        signals.handler()

        assert signals.removed and signals.raised == [signal.SIGTERM]
        assert middleware._sigterm_close is None

    async def test_a_sigterm_during_a_close_already_running_lets_it_write(self, monkeypatch):
        """is_closed() is true from the start of a close, so a SIGTERM during a
        shutdown command's close ended the process before the batch was
        written. Joined by calling close() again, it ran the bot's own close()
        override a second time."""
        signals = TestPersistenceClosesOnSigterm._signals(monkeypatch)
        release = asyncio.Event()
        entered = []

        class _PoolBot(commands.Bot):
            async def close(self):
                entered.append(1)
                await super().close()
                await release.wait()

        backend = _RecordingBackend()
        bot = _real_bot(_PoolBot)
        middleware = PersistenceMiddleware(backend=backend, bot=bot)
        await setup_middleware(middleware)
        await _route_batched_write(middleware)
        closing = asyncio.ensure_future(bot.close())
        try:
            await _until(bot.is_closed)

            signals.handler()
            release.set()
            await closing
            await middleware._sigterm_close
        finally:
            release.set()

        assert signals.raised == []
        assert entered == [1]
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [json.loads(row["payload"]) for row in rows] == [{"theme": "dark"}]

    async def test_the_bots_close_leaves_a_handler_the_application_installed(self, monkeypatch):
        """asyncio gives every handler it installs the same process
        disposition, so removing the library's at close removed the application's."""
        signals = TestPersistenceClosesOnSigterm._signals(monkeypatch)
        bot = _real_bot()
        await setup_middleware(PersistenceMiddleware(backend=_RecordingBackend(), bot=bot))

        def application(*args):
            pass

        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, application)
        await bot.close()

        assert not signals.removed and signals.handler is application

    async def test_a_restart_after_a_normal_close_handles_sigterm_again(self, monkeypatch):
        signals = TestPersistenceClosesOnSigterm._signals(monkeypatch)
        bot = _real_bot()
        middleware = PersistenceMiddleware(backend=_RecordingBackend(), bot=bot)
        async with bot:
            await setup_middleware(middleware)
            await bot.close()
            bot.clear()

            await setup_middleware(middleware)

            assert signals.disposition == "installed"

    @pytest.mark.skipif(sys.platform == "win32", reason="an event loop cannot handle SIGTERM")
    @pytest.mark.skipif(
        importlib.util.find_spec("aiosqlite") is None, reason="aiosqlite not installed"
    )
    async def test_a_second_sigterm_ends_a_program_still_running_other_work(self, tmp_path):
        # The first SIGTERM closes the bot and writes the batch; the service
        # beside it keeps the program alive until the second.
        db = tmp_path / "second.db"
        script = TestPersistenceClosesOnSigterm._SCRIPT + (
            "async def both():\n"
            "    async def service():\n"
            "        while True:\n"
            "            await asyncio.sleep(0.05)\n"
            "    serving = asyncio.ensure_future(service())\n"
            "    await run(7, True)\n"
            "    os.kill(os.getpid(), signal.SIGTERM)\n"
            "    await serving\n"
            "asyncio.run(both())\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(db)], timeout=60, capture_output=True, text=True
        )

        assert result.returncode == -signal.SIGTERM, result.stderr
        assert await TestPersistenceClosesOnSigterm._saved(db) == [{"k": {"value": 7}}]

    @pytest.mark.skipif(sys.platform == "win32", reason="an event loop cannot handle SIGTERM")
    @pytest.mark.skipif(
        importlib.util.find_spec("aiosqlite") is None, reason="aiosqlite not installed"
    )
    async def test_a_sigterm_after_the_bot_closed_ends_a_program_still_running(self, tmp_path):
        # The bot closes on its own (a shutdown command) and the service beside
        # it runs on; the SIGTERM a process manager sends then has no bot to
        # close, and the handler still installed must not swallow it.
        db = tmp_path / "after_close.db"
        script = TestPersistenceClosesOnSigterm._SCRIPT + (
            "async def both():\n"
            "    async def service():\n"
            "        while True:\n"
            "            await asyncio.sleep(0.05)\n"
            "    serving = asyncio.ensure_future(service())\n"
            "    await run(3, False)\n"
            "    os.kill(os.getpid(), signal.SIGTERM)\n"
            "    await serving\n"
            "asyncio.run(both())\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(db)], timeout=20, capture_output=True, text=True
        )

        assert result.returncode == -signal.SIGTERM, result.stderr
        assert await TestPersistenceClosesOnSigterm._saved(db) == [{"k": {"value": 3}}]

    @pytest.mark.skipif(sys.platform == "win32", reason="an event loop cannot handle SIGTERM")
    @pytest.mark.skipif(
        importlib.util.find_spec("aiosqlite") is None, reason="aiosqlite not installed"
    )
    async def test_a_program_that_starts_the_bot_again_after_sigterm_ends(self, tmp_path):
        # A retry loop around the bot starts it again after the SIGTERM close;
        # the change made in between is held, and written before the end.
        db = tmp_path / "restart.db"
        script = TestPersistenceClosesOnSigterm._SCRIPT + (
            "async def retry():\n"
            "    await run(5, True)\n"
            "    await get_store().dispatch('WRITE', {'v': 6})\n"
            "    bot.clear()\n"
            "    await run(7, False)\n"
            "    print('still running')\n"
            "asyncio.run(retry())\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(db)], timeout=20, capture_output=True, text=True
        )

        assert result.returncode == -signal.SIGTERM, result.stderr
        assert "still running" not in result.stdout
        assert await TestPersistenceClosesOnSigterm._saved(db) == [{"k": {"value": 6}}]


# // ========================================( Views a restart leaves behind )======================================== // #


class TestARestartReleasesTheViewsLeftBehind:
    """discord.py cannot log a closed bot in again, so a restart in the same
    process builds a new bot object. Every view the old bot left answered
    nothing, held its instance slot, and rendered through a closed client,
    and a persistent panel stayed dead until the process restarted."""

    class _Timed(RenderableLayoutView):
        instance_limit = 1
        instance_policy = "reject"
        timeout = 60

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.timed_out = False
            self.go = StatefulButton(label="Go", custom_id="go", callback=self._go)
            self.add_item(ActionRow(self.go))

        async def _go(self, interaction):
            await asyncio.sleep(0.01)

        async def on_timeout(self):
            self.timed_out = True
            await super().on_timeout()

    class _Panel(PersistentLayoutView):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.add_item(
                ActionRow(StatefulButton(label="Hit", custom_id="panel_hit", callback=self._hit))
            )

        async def _hit(self, interaction):
            pass

    class _SlowBindPanel(_Panel):
        bound: list = []
        reaches_discord = False

        async def on_bind(self, bot):
            type(self).bound.append(self.persistence_key)
            await asyncio.sleep(0.02)
            if type(self).reaches_discord and bot.is_closed():
                # As a bind that fetches through the bot does.
                raise RuntimeError("Session is closed")

    @staticmethod
    async def _sent(view, bot, message_id, *, stored=True):
        """Send the view through ``bot`` and store it there, as a real send does."""
        response = view.interaction.original_response.return_value
        response.id = message_id
        response.guild = MagicMock(id=9)
        if bot is not None:
            view.interaction.client = bot
        await view.send()
        if stored and bot is not None:
            bot._connection.store_view(view, message_id)
        return view

    @staticmethod
    async def _fetch(manager, row, removed, unreachable):
        message_id = int(row["message_id"])
        return (MagicMock(id=5), MagicMock(id=message_id, channel=MagicMock(id=5)))

    async def _boot_row(self, backend):
        """A row for panel ``p`` on message 222, as an earlier run left it."""
        await backend.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "p",
                "view_class": self._Panel._class_session_key(),
                "custom_id": None,
                "message_id": 222,
                "channel_id": 5,
                "guild_id": None,
                "user_id": None,
                "session_id": None,
                "init_kwargs": json.dumps({"persistence_key": "p"}),
                "kwargs_schema_version": 1,
                "schema_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
            ["persistence_key"],
        )

    def _timed(self, user_id=1):
        return self._Timed(interaction=make_interaction(user_id=user_id, guild_id=9))

    def _panel(self, key="p"):
        return self._Panel(interaction=make_interaction(user_id=1, guild_id=9), persistence_key=key)

    @pytest.mark.parametrize("policy", ["reject", "replace"])
    async def test_the_same_view_opens_again_through_the_new_bot(self, policy, caplog):
        # Released only by the new bot's first send, after that send had
        # counted it: reject refused the reopen, and replace exited the old
        # view through the closed client.
        class _One(self._Timed):
            instance_policy = policy

        store = get_store()
        old, new = _real_bot(), _real_bot()
        async with old:
            left = await self._sent(
                _One(interaction=make_interaction(user_id=1, guild_id=9)), old, 111
            )
            message = left._message
        edits = message.edit.await_count
        with caplog.at_level("WARNING", logger="cascadeui"):
            async with new:
                again = await self._sent(
                    _One(interaction=make_interaction(user_id=1, guild_id=9)), new, 222
                )
                assert again._message is not None
                assert again.id in store.subscribers
        assert message.edit.await_count == edits
        message.delete.assert_not_awaited()
        assert not [r for r in caplog.records if r.name.startswith("cascadeui")]

    async def test_a_released_views_timer_never_fires(self):
        # Left running after its bot closed, it froze the message through the
        # closed client.
        class _Short(self._Timed):
            timeout = 0.2

        old = _real_bot()
        async with old:
            left = await self._sent(
                _Short(interaction=make_interaction(user_id=1, guild_id=9)), old, 111
            )
        await asyncio.sleep(0.4)
        assert not left.timed_out
        assert left.id not in get_store().subscribers
        assert await left.refresh() == "no_message"

    async def test_a_released_view_leaves_nothing_holding_it(self):
        # Its timer and its Continue-button arming stayed pending, each holding
        # the view until it fired, so every restart kept the views it released.
        class _Handoff(self._Timed):
            enable_undo = True
            auto_refresh_ephemeral = True
            refresh_warning_seconds = 899

        store = get_store()
        old = _real_bot()
        async with old:
            view = _Handoff(interaction=make_interaction(user_id=1, guild_id=9))
            response = view.interaction.original_response.return_value
            response.id = 111
            view.interaction.client = old
            await view.send(ephemeral=True)
            # As a real send does: discord.py starts the view's timer here.
            old._connection.store_view(view, 111)
            assert view.id in store._undo_enabled_views
            # The Continue-button arming waits on a loop timer, not a task.
            assert view.task_manager._timers.get(view.id)

        def holding(task):
            frame = task.get_coro().cr_frame
            return frame is not None and frame.f_locals.get("self") is view

        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() if not t.done() and holding(t)]
        assert not view.task_manager._timers.get(view.id)
        assert view.id not in store._undo_enabled_views

    async def test_a_views_own_task_that_closes_the_bot_runs_on(self):
        # Cancelled with the rest of the view's tasks, it was cut off inside
        # the close it had started.
        old = _real_bot()
        after = []
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=_RecordingBackend(), bot=old))
            view = await self._sent(self._timed(), old, 111)

            async def shut_down():
                await old.close()
                after.append("closed")

            task = view.create_task(shut_down())
            await until(task.done)
        assert after == ["closed"]

    async def test_code_waiting_on_a_view_ends_when_its_bot_closes(self):
        # Woken as by stop(), it went on as if the view had finished, and
        # replied through the closed bot.
        old = _real_bot()
        after = []
        async with old:
            view = await self._sent(self._timed(), old, 111)

            async def command():
                await view.wait()
                after.append("went on")

            waiting = asyncio.ensure_future(command())
            await asyncio.sleep(0)
        await until(waiting.done)
        assert waiting.cancelled()
        assert after == []

    async def test_a_closed_bots_views_leave_the_state_with_no_dispatch(self):
        # Their removal was replayed as VIEW_DESTROYED through every hook and
        # middleware when the next bot came up, inside its setup_hook, where a
        # hook waiting for the bot to be ready held the boot forever. A
        # process restart dispatches nothing for the old process's views.
        store = get_store()
        seen = []

        async def audit(action, state, next_fn):
            seen.append(action["type"])
            return await next_fn(action, state)

        store._add_middleware(audit)
        old, new = _real_bot(), _real_bot()
        async with old:
            left = await self._sent(self._timed(), old, 111)
            session_id = left.session_id
            seen.clear()
        assert left.id not in store.state["views"]
        assert left.id not in store._active_views
        assert session_id not in store.state["sessions"]
        async with new:
            await self._sent(self._timed(user_id=2), new, 222)
        assert "VIEW_DESTROYED" not in seen

    async def test_a_slot_written_while_panels_are_restored_is_saved(self, monkeypatch):
        # Nothing was routed until the restore had finished, so a persistent
        # slot a restored panel's on_bind() wrote stayed in memory and was
        # gone after the next restart.
        class _Binding(self._Panel):
            persistent_slots = ("bound_panels",)

            async def on_bind(self, bot):
                await get_store().dispatch("PANEL_BOUND", {"key": self.persistence_key})

        async def bound(action, state):
            application = {**state["application"], "bound_panels": [action["payload"]["key"]]}
            return {**state, "application": application}

        store = get_store()
        store._register_reducer("PANEL_BOUND", bound)
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        backend = InMemoryBackend()
        await backend.initialize()
        await backend.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "b",
                "view_class": _Binding._class_session_key(),
                "custom_id": None,
                "message_id": 222,
                "channel_id": 5,
                "guild_id": None,
                "user_id": None,
                "session_id": None,
                "init_kwargs": json.dumps({"persistence_key": "b"}),
                "kwargs_schema_version": 1,
                "schema_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
            ["persistence_key"],
        )
        old = _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            assert store.state["application"]["bound_panels"] == ["b"]

        saved = await backend.row_select(TABLE_APPLICATION_SLOTS, {"slot_name": "bound_panels"})
        assert len(saved) == 1

    async def test_a_send_cut_as_its_bot_closes_leaves_no_empty_session(self):
        # Its VIEW_CREATED had not landed when the bot closed, so the view was
        # no member of the session its send made, and the release left that
        # session in the state with no member.
        store = get_store()
        holding, release = asyncio.Event(), asyncio.Event()

        async def hold_view_created(action, state, next_fn):
            if action["type"] == "VIEW_CREATED":
                holding.set()
                await release.wait()
            return await next_fn(action, state)

        store._add_middleware(hold_view_created)
        old = _real_bot()
        view = self._timed()
        view.interaction.client = old
        try:
            async with old:
                sending = asyncio.ensure_future(view.send())
                await asyncio.wait_for(holding.wait(), 5)
                session_id = view.session_id
                assert session_id in store.state["sessions"]
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
        finally:
            release.set()
            store._remove_middleware(hold_view_created)
        await store._flush_notifications()

        assert view.id not in store.state["views"]
        assert session_id not in store.state["sessions"]

    async def test_a_view_discord_py_never_stored_is_released(self):
        # Nothing clickable, sent as a followup: discord.py never stored it,
        # and a release keyed on its view store left it holding its slot.
        class _Card(RenderableLayoutView):
            instance_limit = 1
            instance_policy = "reject"

        store = get_store()
        old = _real_bot()
        async with old:
            card = await self._sent(
                _Card(interaction=make_interaction(user_id=1, guild_id=9)), old, 111, stored=False
            )
            assert card._view_store() is None
        assert card.id not in store.subscribers
        assert _Card.check_instance_available(user_id=1, guild_id=9)

    async def test_a_view_sent_to_a_channel_is_released(self):
        # Sent with a channel as its context, it named no interaction client
        # or context.bot, so its bot was never wired and nothing released it.
        bot = _real_bot()
        async with bot:
            channel = MagicMock(spec=["send", "_state", "id"], id=5, _state=bot._connection)
            channel.send = AsyncMock(return_value=MagicMock(id=111, guild=MagicMock(id=9)))
            view = self._Timed(context=channel, user_id=1, guild_id=9)
            await view.send()
            assert view._message is not None
        assert view.id not in get_store().subscribers

    async def test_a_close_that_raises_before_closing_the_bot_releases_nothing(self):
        class _Failing(commands.Bot):
            async def close(self):
                raise RuntimeError("the database would not close")

        store = get_store()
        bot = _real_bot(_Failing)
        await bot._async_setup_hook()
        view = await self._sent(self._timed(), bot, 111)
        try:
            with pytest.raises(RuntimeError, match="would not close"):
                await bot.close()
            assert not bot.is_closed()
            assert view.id in store.subscribers
            assert view._message is not None
        finally:
            view.stop()
            await commands.Bot.close(bot)

    async def test_the_release_reads_the_registry_once(self):
        # Scanned once per view with a registration, closing a bot with
        # thousands of panels took seconds, ahead of persistence's final write.
        class _Counting(dict):
            def items(self):
                self.reads += 1
                return super().items()

        store = get_store()
        old = _real_bot()
        async with old:
            for i in range(5):
                await self._sent(self._panel(f"p{i}"), old, 200 + i)
            mirror = _Counting(store.state["persistent_views"])
            mirror.reads = 0
            store.state = {**store.state, "persistent_views": mirror}
        assert mirror.reads == 1

    async def test_a_view_being_sent_when_its_bot_closes_is_released(self):
        # Its bot was wired only at the end of its first send, so a view whose
        # send the close cut short kept its slot and refused the reopen.
        store = get_store()
        old = _real_bot()
        release = asyncio.Event()
        fetching = asyncio.Event()
        async with old:
            view = self._timed()
            view.interaction.client = old
            message = MagicMock(spec=discord.InteractionMessage)
            message.id = 111
            message.guild = MagicMock(id=9)
            message.channel = MagicMock(id=5)

            async def fetch_message(message_id):
                fetching.set()
                await release.wait()
                raise RuntimeError("Session is closed")

            message.channel.fetch_message = fetch_message
            view.interaction.original_response.return_value = message
            sending = asyncio.ensure_future(view.send())
            try:
                await asyncio.wait_for(fetching.wait(), 5)
                await old.close()
            finally:
                release.set()
                await asyncio.gather(sending, return_exceptions=True)
        assert view.id not in store.subscribers
        assert self._Timed.check_instance_available(user_id=1, guild_id=9)

    async def test_the_views_of_a_bot_still_running_are_left_alone(self):
        store = get_store()
        first, second = _real_bot(), _real_bot()
        async with first:
            running = await self._sent(self._timed(), first, 111)
            async with second:
                other = await self._sent(self._timed(user_id=2), second, 222)
            assert other.id not in store.subscribers
            assert running.id in store.subscribers
            assert running._message is not None

    async def test_a_persistent_panel_comes_back_in_the_new_bots_setup_hook(self, monkeypatch):
        # Restored once the new bot connected, it was missing in on_ready,
        # where code that posts a missing panel posted a second one.
        class _RestartButton(DynamicPersistentButton, template=r"restartdyn:(?P<n>[0-9]+)"):
            def __init__(self, *, n: int):
                super().__init__(discord.ui.Button(label="d", custom_id=f"restartdyn:{n}"))

        key = f"{_RestartButton.__module__}.{_RestartButton.__qualname__}"
        store = get_store()
        backend = _RecordingBackend()
        events = []

        def record(kind):
            async def hook(action, state):
                events.append((kind, action["payload"]["view_id"]))

            return hook

        store.on("view_created", record("created"))
        store.on("view_destroyed", record("destroyed"))
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        try:
            async with old:
                await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
                left = await self._sent(self._panel(), old, 222)
            async with new:
                await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
                restored = store.get_active_view(persistence_key="p")
                assert restored not in (None, left)
                assert new._connection._view_store._synced_message_views[222] is restored
                assert _RestartButton in new._connection._view_store._dynamic_items.values()
                assert ("destroyed", left.id) not in events
                assert events.count(("created", restored.id)) == 1
        finally:
            _dynamic_button_classes.pop(key, None)

    async def test_a_panel_restored_at_boot_comes_back_through_the_new_bot(self, monkeypatch):
        # Reattach skips a key it restored on an earlier pass, so a panel the
        # boot restored would otherwise stay dead after the restart.
        store = get_store()
        backend = _RecordingBackend()
        await self._boot_row(backend)
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            booted = store.get_active_view(persistence_key="p")
            assert booted is not None
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            restored = store.get_active_view(persistence_key="p")
            assert restored not in (None, booted)
            assert new._connection._view_store._synced_message_views[222] is restored

    async def test_a_row_still_pending_from_boot_is_retried_by_the_restart(self, monkeypatch):
        # As a process restart retries it at boot.
        store = get_store()
        backend = _RecordingBackend()
        await self._boot_row(backend)
        reachable = False

        async def fetch(manager, row, removed, unreachable):
            if not reachable:
                unreachable.append(row["persistence_key"])
                return None
            return await self._fetch(manager, row, removed, unreachable)

        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            assert store.get_active_view(persistence_key="p") is None
        reachable = True
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            assert store.get_active_view(persistence_key="p") is not None

    async def test_a_restored_panel_starts_a_session_of_its_own(self, monkeypatch):
        # It rejoins the session its row names. When the old views were still
        # leaving, that session still existed, and the restored panel took over
        # its shared_data, which a process restart would have dropped.
        store = get_store()
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            for i in range(5):
                await self._sent(self._timed(user_id=100 + i), old, 1000 + i)
            left = await self._sent(self._panel(), old, 222)
            await left.update_session(cart=["sword"])
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            restored = store.get_active_view(persistence_key="p")
            session = store.state["sessions"][restored.session_id]
            assert session["members"] == [restored.id]
            assert not session.get("shared_data")

    @pytest.mark.parametrize("method", ["undo", "redo"])
    async def test_a_released_view_cannot_undo_or_redo(self, method):
        # Its row and stacks stay until a new bot is in use, and undo() or
        # redo() changed the state after its bot had closed.
        class _Undoable(self._Timed):
            enable_undo = True

        store = get_store()
        await setup_middleware(UndoMiddleware())
        store._register_reducer("SCORE", self._scorer())
        old = _real_bot()
        async with old:
            view = await self._sent(
                _Undoable(interaction=make_interaction(user_id=1, guild_id=9)), old, 111
            )
            for score in (1, 2, 3):
                await view.dispatch("SCORE", {"score": score})
            await view.undo()
        await getattr(view, method)()
        assert store.state["application"]["score"] == 2

    async def test_a_batch_open_across_a_restart_leaves_the_restored_panel_no_step(
        self, monkeypatch
    ):
        # The entry followed the released panel to the one restored on its
        # message, whose first Undo then reverted a change from before the
        # restart, which a process restart forgets.
        class _Undoable(self._Panel):
            enable_undo = True

        store = get_store()
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        await setup_middleware(UndoMiddleware())
        store._register_reducer("SCORE", self._scorer())
        started, gate = asyncio.Event(), asyncio.Event()
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            left = await self._sent(
                _Undoable(interaction=make_interaction(user_id=1, guild_id=9), persistence_key="p"),
                old,
                222,
            )

            async def batched():
                async with store.batch(source_id=left.id):
                    await left.dispatch("SCORE", {"score": 1})
                    started.set()
                    await gate.wait()

            task = asyncio.ensure_future(batched())
            await asyncio.wait_for(started.wait(), 2)
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            restored = store.get_active_view(persistence_key="p")
            assert left.current_view is restored
            gate.set()
            await asyncio.wait_for(task, 2)
            assert not store.state["views"][restored.id].get("undo_stack")

    @staticmethod
    def _scorer():
        async def reducer(action, state):
            return {
                **state,
                "application": {**state["application"], "score": action["payload"]["score"]},
            }

        return reducer

    async def test_code_holding_the_old_panel_reaches_the_restored_one(self, monkeypatch):
        # Its exit() removed the registration of the panel restored on the
        # same message, which then stayed dead after the next restart.
        store = get_store()
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            left = await self._sent(self._panel(), old, 222)
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            restored = store.get_active_view(persistence_key="p")
            assert left.current_view is restored
            await left.exit()
            assert "p" in store.state["persistent_views"]
            assert not restored.is_finished()

    async def test_code_holding_a_panel_that_pushed_reaches_the_restored_one(self, monkeypatch):
        # The screen on the panel carries its registration without its key, and
        # a second release at the close read that registration after the first
        # had cleared it, so the panel's current_view stopped at the old screen.
        store = get_store()
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            panel = await self._sent(self._panel(), old, 222)
            click = make_interaction(user_id=1, guild_id=9, message=MagicMock(id=222))
            click.client = old
            screen = await panel.push(RenderableLayoutView, click)
            assert panel.current_view is screen
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            restored = store.get_active_view(persistence_key="p")
            assert restored not in (None, panel, screen)
            assert panel.current_view is restored

    async def test_an_exit_on_a_released_panel_before_the_restore_keeps_its_registration(
        self, monkeypatch
    ):
        # The released panel still carried the message id its registration
        # names, so its exit() removed the registration and the panel on
        # that message was never restored.
        store = get_store()
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            left = await self._sent(self._panel(), old, 222)
        await left.exit()
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            assert store.get_active_view(persistence_key="p") not in (None, left)

    async def test_a_panel_with_a_push_in_flight_at_the_close_comes_back(self, monkeypatch):
        # The arriving view carried the panel's registration and was never
        # released, so the key read as live and the panel stayed dead.
        gate = asyncio.Event()

        class _Slow(RenderableLayoutView):
            async def on_load(self):
                await gate.wait()

        store = get_store()
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            panel = await self._sent(self._panel(), old, 222)
            click = make_interaction(user_id=1, guild_id=9, message=MagicMock(id=222))
            click.client = old
            pushing = asyncio.ensure_future(panel.push(_Slow, click))
            try:
                await until(lambda: panel._away_for_navigation)
            except BaseException:
                gate.set()
                await asyncio.gather(pushing, return_exceptions=True)
                raise
        gate.set()
        await asyncio.gather(pushing, return_exceptions=True)
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            restored = store.get_active_view(persistence_key="p")
            assert restored is not None
            assert new._connection._view_store._synced_message_views[222] is restored

    async def test_a_restored_panel_pushed_from_code_when_its_bot_closes_comes_back(
        self, monkeypatch
    ):
        # A panel restored at startup has no interaction, so the screen a push
        # from code was bringing in named no bot. It was never released and
        # kept the panel's key held.
        gate = asyncio.Event()

        class _Slow(RenderableLayoutView):
            async def on_load(self):
                await gate.wait()

        store = get_store()
        backend = _RecordingBackend()
        await self._boot_row(backend)
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            booted = store.get_active_view(persistence_key="p")
            assert booted.interaction is None
            # The edit the push ends with goes through the closed client.
            booted._message.edit = AsyncMock(side_effect=RuntimeError("Session is closed"))
            pushing = asyncio.ensure_future(booted.push(_Slow))
            await until(lambda: booted._away_for_navigation)
        try:
            async with new:
                await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
                restored = store.get_active_view(persistence_key="p")
                assert isinstance(restored, self._Panel) and restored is not booted
        finally:
            gate.set()
            await asyncio.gather(pushing, return_exceptions=True)

    async def test_a_screen_with_nothing_to_click_pushed_from_code_is_released(self, monkeypatch):
        # discord.py never stores a view with nothing to click, and one pushed
        # from code onto a panel restored at startup has no interaction. It
        # named no bot, outlived its bot's close, and held the panel's key, so
        # the restart skipped the panel.
        class _Card(StatefulLayoutView):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.add_item(TextDisplay("results"))

        async def fetch(manager, row, removed, unreachable):
            channel, message = await self._fetch(manager, row, removed, unreachable)
            # As a real fetch: the message names the client it came through.
            message._state = manager._bot._connection
            return channel, message

        store = get_store()
        backend = _RecordingBackend()
        await self._boot_row(backend)
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            booted = store.get_active_view(persistence_key="p")
            booted._message.edit = AsyncMock()
            card = await booted.push(_Card)
            assert not card._torn_down()
        assert card.id not in store.subscribers
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            restored = store.get_active_view(persistence_key="p")
            assert isinstance(restored, self._Panel) and restored._client() is new

    async def test_panels_come_back_after_a_bot_closes_during_its_own_restore(
        self, monkeypatch, caplog
    ):
        # A bot closed while its setup_hook restored the panels went on
        # binding the rest through the closed client, each logged as an error
        # and reported failed, and remembered as restored one its close had
        # released, so the next bot skipped it.
        async def fetch(manager, row, removed, unreachable):
            if manager._bot.is_closed():
                raise RuntimeError("Session is closed")
            return await self._fetch(manager, row, removed, unreachable)

        store = get_store()
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", fetch)
        monkeypatch.setattr(self._SlowBindPanel, "bound", [])
        first, second, third = _real_bot(), _real_bot(), _real_bot()
        async with first:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=first))
            for i in range(3):
                panel = self._SlowBindPanel(
                    interaction=make_interaction(user_id=1, guild_id=9), persistence_key=f"p{i}"
                )
                await self._sent(panel, first, 200 + i)
        self._SlowBindPanel.bound.clear()
        async with second:
            booting = asyncio.ensure_future(
                setup_middleware(PersistenceMiddleware(backend=backend, bot=second))
            )
            await until(lambda: self._SlowBindPanel.bound)
            with caplog.at_level("ERROR", logger="cascadeui"):
                await second.close()
                await asyncio.wait_for(booting, 5)
            assert not [r for r in caplog.records if r.name.startswith("cascadeui")]
            assert store.persistence_manager.last_reattach_summary["failed"] == []
            # A bot that closed never becomes ready, so a render waiting for it
            # held the released panels until the next close.
            assert not store.persistence_manager._post_ready_restore_tasks
        assert not [v for v in store._active_views.values() if v._client() is second]
        async with third:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=third))
            for i in range(3):
                restored = store.get_active_view(persistence_key=f"p{i}")
                assert restored is not None and restored._client() is third

    async def test_a_bind_that_fails_because_its_bot_closed_leaves_the_panel_pending(
        self, monkeypatch, caplog
    ):
        # It was logged as an error, reported failed, and its rollback
        # dispatched VIEW_DESTROYED for a view the close had already released.
        async def fetch(manager, row, removed, unreachable):
            return await self._fetch(manager, row, removed, unreachable)

        store = get_store()
        backend = _RecordingBackend()
        destroyed = []

        async def record(action, state):
            destroyed.append(action["payload"]["view_id"])

        store.on("view_destroyed", record)
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", fetch)
        monkeypatch.setattr(self._SlowBindPanel, "bound", [])
        monkeypatch.setattr(self._SlowBindPanel, "reaches_discord", True)
        first, second, third = _real_bot(), _real_bot(), _real_bot()
        async with first:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=first))
            for i in range(2):
                panel = self._SlowBindPanel(
                    interaction=make_interaction(user_id=1, guild_id=9), persistence_key=f"p{i}"
                )
                await self._sent(panel, first, 200 + i)
        self._SlowBindPanel.bound.clear()
        destroyed.clear()
        async with second:
            booting = asyncio.ensure_future(
                setup_middleware(PersistenceMiddleware(backend=backend, bot=second))
            )
            await until(lambda: self._SlowBindPanel.bound)
            with caplog.at_level("ERROR", logger="cascadeui"):
                await second.close()
                await asyncio.wait_for(booting, 5)
            assert not [r for r in caplog.records if r.name.startswith("cascadeui")]
            assert store.persistence_manager.last_reattach_summary["failed"] == []
        assert destroyed == []
        async with third:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=third))
            for i in range(2):
                restored = store.get_active_view(persistence_key=f"p{i}")
                assert restored is not None and restored._client() is third

    async def test_rows_written_back_after_the_close_leave_before_the_next_bot_restores(
        self, monkeypatch
    ):
        # A dispatch suspended in the chain at the close committed a state
        # read before it, which put the old views back; the panel restored
        # into its old session then found them there and kept their
        # shared_data.
        store = get_store()
        entered, gate = asyncio.Event(), asyncio.Event()

        async def hold(action, state, next_fn):
            if action["type"] == "SLOW":
                state = {**state}
                entered.set()
                await gate.wait()
            return await next_fn(action, state)

        async def slow(action, state):
            return {**state, "application": {**state.get("application", {}), "slow": True}}

        store._register_reducer("SLOW", slow)
        store._add_middleware(hold)
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            panel = await self._sent(self._panel(), old, 222)
            await panel.update_session(secret="old")
            left = await self._sent(self._timed(user_id=2), old, 111)
            pending = asyncio.ensure_future(store.dispatch("SLOW", {}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
            except BaseException:
                gate.set()
                await asyncio.gather(pending, return_exceptions=True)
                raise
        gate.set()
        await pending
        assert left.id in store.state["views"]
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            assert left.id not in store.state["views"]
            assert panel.id not in store.state["views"]
            restored = store.get_active_view(persistence_key="p")
            session = store.state["sessions"][restored.session_id]
            assert session["members"] == [restored.id]
            assert "secret" not in (session.get("shared_data") or {})
            assert store.state["application"]["slow"] is True

    async def test_an_interaction_recorded_for_a_released_view_leaves_no_entry(self):
        # Its entry outlived the view, since the view's row was already gone.
        store = get_store()
        old, new = _real_bot(), _real_bot()
        async with old:
            left = await self._sent(self._timed(), old, 111)
        await store.dispatch("MODAL_SUBMITTED", {"view_id": left.id, "values": {}})
        await store.dispatch(
            "COMPONENT_INTERACTION", {"component_id": "c1", "view_id": left.id, "user_id": 1}
        )
        assert left.id in store.state.get("modals", {})
        assert "c1" in store.state.get("components", {})
        async with new:
            await self._sent(self._timed(user_id=2), new, 222)
            assert left.id not in store.state.get("modals", {})
            assert "c1" not in store.state.get("components", {})

    async def test_destroying_a_released_view_dispatches_nothing(self):
        # A navigation that was in flight at the close settled by destroying
        # its view, and VIEW_DESTROYED reached the hooks for it.
        store = get_store()
        destroyed = []

        async def record(action, state):
            destroyed.append(action["payload"]["view_id"])

        store.on("view_destroyed", record)
        old = _real_bot()
        async with old:
            left = await self._sent(self._timed(), old, 111)
        assert await store._destroy_view(left.id) is True
        assert destroyed == []

    async def test_a_view_sent_again_as_its_bot_closes_logs_nothing(self, caplog):
        # Closing the message it left went out through the closed client,
        # and the failure was logged as an error saying the view was still
        # registered.
        old = _real_bot()
        gate, in_edit = asyncio.Event(), asyncio.Event()
        with caplog.at_level("WARNING", logger="cascadeui"):
            async with old:
                view = await self._sent(self._timed(), old, 111)
                left = view._message
                calls = []

                async def edit(*args, **kwargs):
                    calls.append(kwargs)
                    if len(calls) == 1:
                        in_edit.set()
                        await gate.wait()
                        return left
                    if old.is_closed():
                        raise RuntimeError("Session is closed")
                    return left

                left.edit = AsyncMock(side_effect=edit)
                left.delete = AsyncMock(side_effect=RuntimeError("Session is closed"))
                view.go.label = "changed"
                render = asyncio.ensure_future(view.refresh())
                await asyncio.wait_for(in_edit.wait(), 2)
                again = make_interaction(user_id=1, guild_id=9)
                again.client = old
                again.original_response.return_value.id = 222
                view.interaction = again
                resend = asyncio.ensure_future(view.send())
                try:
                    await until(lambda: view._message is not left)
                except BaseException:
                    gate.set()
                    await asyncio.gather(render, resend, return_exceptions=True)
                    raise
                await old.close()
                gate.set()
                await asyncio.wait_for(resend, 5)
                await render
        assert not [r for r in caplog.records if r.name.startswith("cascadeui")]

    async def test_a_panel_sent_again_as_its_bot_closes_comes_back(self, monkeypatch):
        # A process restart during the re-send keeps the row, which still names
        # the message the panel was sent from, so the new bot restores it there.
        store = get_store()
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        gate, entered = asyncio.Event(), asyncio.Event()
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            view = await self._sent(self._panel(), old, 111)

            async def hold(action, state):
                entered.set()
                await gate.wait()

            store.on("view_updated", hold)
            again = make_interaction(user_id=1, guild_id=9)
            again.client = old
            again.original_response.return_value.id = 222
            again.original_response.return_value.guild = None
            view.interaction = again
            resend = asyncio.ensure_future(view.send())
            await asyncio.wait_for(entered.wait(), 2)
            store.off("view_updated", hold)
        gate.set()
        await asyncio.wait_for(resend, 5)
        async with new:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
            restored = store.get_active_view(persistence_key="p")

            assert restored is not None and restored is not view
            assert restored._message.id == 111

    async def test_a_registration_failing_after_the_close_keeps_the_restore_row(self, caplog):
        # The failed registration put back the id the close had cleared, so
        # code still holding the old panel removed the row with a stale exit()
        # before the new bot could restore the panel from it.
        store = get_store()
        caplog.set_level("ERROR", logger="cascadeui")
        old = _real_bot()
        view = await self._sent(self._panel(), old, 111)
        entered, release = asyncio.Event(), asyncio.Event()

        async def failing(action, state, next_fn):
            if action["type"] == "PERSISTENT_VIEW_REGISTERED":
                entered.set()
                await release.wait()
                raise RuntimeError("audit pool closed")
            return await next_fn(action, state)

        store._add_middleware(failing)
        try:
            again = make_interaction(user_id=1, guild_id=9)
            again.client = old
            again.original_response.return_value.id = 222
            again.original_response.return_value.guild = None
            view.interaction = again
            resend = asyncio.ensure_future(view.send())
            await asyncio.wait_for(entered.wait(), 2)
            await old.close()
            release.set()
            await asyncio.wait_for(resend, 5)
        finally:
            release.set()
            store._middleware.remove(failing)
        await view.exit()

        assert store.state["persistent_views"]["p"]["message_id"] == "111"
        errors = [r.getMessage() for r in caplog.records if r.name.startswith("cascadeui")]
        assert any("its registration still names message 111" in m for m in errors)

    async def test_a_bot_closed_while_its_restore_fetches_reports_no_failure(
        self, monkeypatch, caplog
    ):
        # Every fetch through the closed client raised, so a shutdown during
        # the boot logged each row as an error and reported it failed.
        gate = asyncio.Event()
        started = []

        async def fetch(manager, row, removed, unreachable):
            started.append(row["persistence_key"])
            await gate.wait()
            if manager._bot.is_closed():
                raise RuntimeError("Session is closed")
            return await self._fetch(manager, row, removed, unreachable)

        store = get_store()
        backend = _RecordingBackend()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", fetch)
        first, second, third = _real_bot(), _real_bot(), _real_bot()
        async with first:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=first))
            for i in range(3):
                await self._sent(self._panel(f"p{i}"), first, 200 + i)
        async with second:
            booting = asyncio.ensure_future(
                setup_middleware(PersistenceMiddleware(backend=backend, bot=second))
            )
            try:
                await until(lambda: started)
                with caplog.at_level("ERROR", logger="cascadeui"):
                    closing = asyncio.ensure_future(second.close())
                    await until(second.is_closed)
                    gate.set()
                    await asyncio.wait_for(asyncio.gather(closing, booting), 5)
            finally:
                gate.set()
            assert not [r for r in caplog.records if r.name.startswith("cascadeui")]
            assert store.persistence_manager.last_reattach_summary["failed"] == []
        async with third:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=third))
            for i in range(3):
                restored = store.get_active_view(persistence_key=f"p{i}")
                assert restored is not None and restored._client() is third

    async def test_panels_come_back_when_the_new_bot_starts_while_the_old_one_closes(
        self, monkeypatch
    ):
        # A close from another task still writing when the new bot's
        # setup_hook ran restored the panels itself once it finished, and it
        # did that before the old bot's views were released, so it read the
        # panels as live and skipped them.
        release = asyncio.Event()

        class _SlowClose(_RecordingBackend):
            async def close(self):
                await release.wait()
                await super().close()

        store = get_store()
        backend = _SlowClose()
        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", self._fetch)
        old, new = _real_bot(), _real_bot()
        async with old:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=old))
            left = await self._sent(self._panel(), old, 222)
            closing = asyncio.ensure_future(old.close())
            try:
                await until(old.is_closed)
                async with new:
                    await setup_middleware(PersistenceMiddleware(backend=backend, bot=new))
                    release.set()
                    await closing
                    restored = store.get_active_view(persistence_key="p")
                    assert restored not in (None, left)
            finally:
                release.set()
                await asyncio.gather(closing, return_exceptions=True)

    async def test_a_panel_sent_while_a_re_drive_fetches_is_not_restored_beside_it(
        self, monkeypatch
    ):
        # Keys a live panel holds were read once, as the pass began, so the
        # send's key was still pending when the pass restored its row.
        store = get_store()
        backend = _RecordingBackend()
        await self._boot_row(backend)
        gate = None

        async def fetch(manager, row, removed, unreachable):
            if gate is None:
                unreachable.append(row["persistence_key"])
                return None
            await gate.wait()
            return await self._fetch(manager, row, removed, unreachable)

        monkeypatch.setattr(PersistenceManager, "_fetch_restore_message", fetch)
        bot = _real_bot()
        async with bot:
            await setup_middleware(PersistenceMiddleware(backend=backend, bot=bot))
            assert store.get_active_view(persistence_key="p") is None
            gate = asyncio.Event()
            redrive = asyncio.ensure_future(store.persistence_manager.reattach())
            try:
                await asyncio.sleep(0.01)
                sent = await self._sent(self._panel(), bot, 333)
                gate.set()
                await redrive
                live = [v for v in store._views_for_key("p") if not v.is_finished()]
                assert live == [sent]
            finally:
                gate.set()
                await asyncio.gather(redrive, return_exceptions=True)

    async def test_a_send_through_a_plain_client_finishes(self):
        # discord.Client has no listen(): installing the deletion listeners
        # raised in the send's last stage, so its parent was never attached.
        client = discord.Client(intents=discord.Intents.none())
        async with client:
            parent = await self._sent(self._timed(), None, 111, stored=False)
            child = self._Timed(interaction=make_interaction(user_id=2, guild_id=9), parent=parent)
            await self._sent(child, client, 222)
            assert child in parent._attached_children
        assert child.id not in get_store().subscribers

    async def test_persistence_takes_a_plain_client(self):
        client = discord.Client(intents=discord.Intents.none())
        async with client:
            await setup_middleware(PersistenceMiddleware(backend=_RecordingBackend(), bot=client))
            assert get_store().persistence_manager._bot is client

    async def test_a_reattach_answers_stale_clicks_on_the_store_it_restores_onto(self):
        # A bot's clear() replaces its view store, and a reattach restores the
        # panels onto the new one before any send has wrapped it, so a click
        # from an earlier render went unanswered there.
        from discord.ui.view import ViewStore

        client = discord.Client(intents=discord.Intents.none())
        async with client:
            await setup_middleware(PersistenceMiddleware(backend=_RecordingBackend(), bot=client))
            replaced = ViewStore(client._connection)
            client._connection._view_store = replaced

            await get_store().persistence_manager.reattach()

            assert "dispatch_view" in vars(replaced)

    def test_views_are_released_when_a_loop_closes_without_asyncio_run(self, caplog):
        # run_until_complete() and then loop.close() leaves discord.py's timer
        # pending on a closed loop, and a release after that raised "Event
        # loop is closed" and left the view registered.
        old, new = _real_bot(), _real_bot()
        held = {}

        async def first():
            async with old:
                held["view"] = await self._sent(self._timed(), old, 111)

        async def second():
            async with new:
                await self._sent(self._timed(user_id=2), new, 222)
                await until(lambda: held["view"].id not in get_store()._active_views)

        with caplog.at_level("ERROR", logger="cascadeui"):
            for step in (first, second):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(step())
                finally:
                    loop.close()

        assert self._Timed.check_instance_available(user_id=1, guild_id=9)
        assert not [r for r in caplog.records if r.name.startswith("cascadeui")]

    def test_a_reload_cut_off_at_the_old_loops_end_is_released(self, caplog):
        # asyncio.run cancels the tasks alive when its main task returns, once.
        # A reload cut off there releases its turn, and a render it owed was
        # replayed on a new task still pending when the loop closed.
        class _Loading(self._Timed):
            subscribed_actions = {"TICK"}
            blocked = None

            async def on_load(self):
                if type(self).blocked is not None:
                    await type(self).blocked.wait()

            def build_ui(self):
                # A new tree each render, so a replayed render sends its edit.
                self.renders = getattr(self, "renders", 0) + 1
                self.clear_items()
                self.add_item(TextDisplay(f"render {self.renders}"))
                self.add_item(ActionRow(self.go))

        old, new = _real_bot(), _real_bot()
        held = {}

        async def first():
            async with old:
                view = await self._sent(
                    _Loading(interaction=make_interaction(user_id=1, guild_id=9)), old, 111
                )

                async def slow_edit(**kwargs):
                    await asyncio.sleep(30)

                view._message.edit = AsyncMock(side_effect=slow_edit)
                _Loading.blocked = asyncio.Event()
                asyncio.ensure_future(view.reload())
                await until(lambda: view._reload_task is not None)
                await get_store().dispatch("TICK", {})
                await get_store()._flush_notifications()
                held["view"] = view

        async def second():
            _Loading.blocked = None
            async with new:
                await self._sent(self._timed(user_id=2), new, 222)
                await until(lambda: held["view"].id not in get_store()._active_views)

        with caplog.at_level("ERROR", logger="cascadeui"):
            asyncio.run(first())
            asyncio.run(second())

        assert not [r for r in caplog.records if r.name.startswith("cascadeui")]


# // ========================================( State identity short-circuit )======================================== // #


class TestMiddlewareStateIdentity:
    """Unchanged state (next_fn returns the same object) produces no writes."""

    async def test_state_identity_skip(self):
        middleware, mgr, backend = await _make_middleware()
        store = middleware._store

        async def identity(action, state):
            # Return the exact object the store already holds.
            return store.state

        await middleware(
            {"type": "NOOP", "payload": {}},
            store.state,
            identity,
        )
        assert not middleware._ns_application.dirty_rows
        assert not middleware._ns_registry.dirty_rows


class TestUndoIsSaved:
    """UNDO and REDO restore application slots, persistent ones included, and
    were skipped as bookkeeping, so the value a user undid came back after a
    restart."""

    async def test_each_undo_and_redo_is_saved(self):
        store = get_store()
        backend = InMemoryBackend()
        persistence = PersistenceMiddleware(backend=backend)
        _pending_test_middleware.append(persistence)
        await setup_middleware(UndoMiddleware(), persistence)
        access_slot(store.state, "prefs", persistent=True)

        async def set_theme(action, state):
            application = {
                **state.get("application", {}),
                "prefs": {"theme": action["payload"]["v"]},
            }
            return {**state, "application": application}

        store._register_reducer("SET_THEME", set_theme)
        await store.dispatch("SET_THEME", {"v": "light"})
        await store.dispatch("SESSION_CREATED", {"session_id": "s"})
        await store.dispatch("VIEW_CREATED", {"view_id": "a", "session_id": "s"})
        store._undo_enabled_views["a"] = 20
        await store.dispatch("SET_THEME", {"v": "dark"}, source_id="a")
        for method, expected in (("UNDO", "light"), ("REDO", "dark")):
            await store.dispatch(method, {"view_id": "a", "session_id": "s"}, source_id="a")
            assert store.state["application"]["prefs"] == {"theme": expected}
            await persistence.flush_all()
            rows = await backend.row_select(TABLE_APPLICATION_SLOTS, {"slot_name": "prefs"})
            assert json.loads(rows[0]["payload"]) == {"theme": expected}, method


class TestRoutesOnlyWhatCommitted:
    """Routing read the store before and after the whole chain, so a commit
    another dispatch made while this one waited counted as this action's: a
    registration a middleware refused, or one whose dispatch was cancelled,
    still wrote a registry row, and the panel came back after a restart."""

    @pytest.mark.parametrize("mode", ["raise", "cancel", "drop"])
    async def test_a_registration_that_never_committed_writes_no_row(self, mode):
        store = get_store()
        backend = InMemoryBackend()
        persistence = PersistenceMiddleware(backend=backend)
        _pending_test_middleware.append(persistence)
        gate = asyncio.Event()
        waiting = asyncio.Event()

        async def gatekeeper(action, state, next_fn):
            if action["type"] == "PERSISTENT_VIEW_REGISTERED":
                waiting.set()
                await gate.wait()
                if mode == "raise":
                    raise PermissionError("registration refused")
                if mode == "drop":
                    return state
            return await next_fn(action, state)

        async def set_j(action, state):
            application = {**state.get("application", {}), "slot": {"j": action["payload"]["v"]}}
            return {**state, "application": application}

        await setup_middleware(persistence, gatekeeper)
        _slots_module._PERSISTENT_SLOTS.add("slot")
        store._register_reducer("SET_J", set_j)
        _register_fake_view(
            store,
            _persistence_key="k1",
            _registry_message_id="555",
            _init_kwargs={"a": 1},
            kwargs_schema_version=1,
            session_id="X:global",
        )
        payload = {
            "persistence_key": "k1",
            "class_name": "X",
            "message_id": "555",
            "channel_id": "1",
        }
        registering = asyncio.ensure_future(store.dispatch("PERSISTENT_VIEW_REGISTERED", payload))
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            await store.dispatch("SET_J", {"v": 2})
            if mode == "cancel":
                registering.cancel()
        finally:
            gate.set()
            await asyncio.gather(registering, return_exceptions=True)
        await persistence.flush_all()
        assert "k1" not in store.state.get("persistent_views", {})
        assert await backend.row_select(TABLE_PERSISTENT_VIEWS, {"persistence_key": "k1"}) == []
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS, {"slot_name": "slot"})
        assert json.loads(rows[0]["payload"]) == {"j": 2}

    async def test_a_value_nested_too_deep_to_save_is_declined(self, caplog):
        # json.dumps raised RecursionError, which the decline did not catch,
        # so dispatch() raised for a value that could simply not be saved.
        store = get_store()
        backend = InMemoryBackend()
        persistence = PersistenceMiddleware(backend=backend)
        _pending_test_middleware.append(persistence)
        await setup_middleware(persistence)
        _slots_module._PERSISTENT_SLOTS.add("deep")
        deep = []
        for _ in range(100000):
            deep = [deep]

        async def set_deep(action, state):
            return {**state, "application": {**state.get("application", {}), "deep": deep}}

        store._register_reducer("SET_DEEP", set_deep)
        with caplog.at_level("ERROR", logger="cascadeui"):
            await store.dispatch("SET_DEEP", {})
        assert store.state["application"]["deep"] is deep
        assert any("'deep' was not saved" in r.getMessage() for r in caplog.records)

    async def test_a_value_nested_past_the_recursion_limit_leaves_its_siblings_saved(self, caplog):
        # json.dumps writes this depth from Python 3.12, and the key check
        # after it recursed and raised inside the commit, dropping the slot
        # and every slot routed after it in the same commit.
        store = get_store()
        backend = InMemoryBackend()
        persistence = PersistenceMiddleware(backend=backend)
        _pending_test_middleware.append(persistence)
        await setup_middleware(persistence)
        names = [f"s{index}" for index in range(8)]
        for name in ("deep", *names):
            _slots_module._PERSISTENT_SLOTS.add(name)
        deep = {}
        for _ in range(sys.getrecursionlimit() + 100):
            deep = {"x": deep}

        async def set_all(action, state):
            application = {**state.get("application", {}), "deep": deep}
            application.update({name: {"v": 1} for name in names})
            return {**state, "application": application}

        store._register_reducer("SET_ALL", set_all)
        with caplog.at_level("ERROR", logger="cascadeui"):
            await store.dispatch("SET_ALL", {})
        await persistence.flush_all()
        rows = {row["slot_name"] for row in await backend.row_select(TABLE_APPLICATION_SLOTS)}
        assert set(names) <= rows
        assert "deep" in rows or "'deep' was not saved" in _library_log(caplog)
        assert "Commit callback failed" not in _library_log(caplog)
