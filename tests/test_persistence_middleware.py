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
import json
from types import SimpleNamespace

import pytest

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
from cascadeui.state import slots as _slots_module
from cascadeui.state.middleware.persistence import PersistenceMiddleware
from cascadeui.state.singleton import get_store
from cascadeui.state.slots import access_slot

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
    return middleware, mgr, backend


async def _drain(middleware: PersistenceMiddleware) -> None:
    """Await all pending flush tasks so the backend reflects in-flight writes."""
    tasks = [t for t in middleware._tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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

        # Stub a live view in _active_views matching the persistence_key.
        fake_view = SimpleNamespace(
            _persistence_key="TicketPanel:msg:42",
            _init_kwargs={"channel_id": 5},
            kwargs_schema_version=1,
            session_id="TicketPanel:global",
        )
        store._active_views["fake-id"] = fake_view

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
            store._active_views.pop("fake-id", None)

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
        fake_view = SimpleNamespace(
            _persistence_key="TicketPanel:msg:99",
            _init_kwargs={
                "channel_id": 5,
                "persistence_key": "TicketPanel:msg:99",
                "theme": fake_theme,
            },
            kwargs_schema_version=1,
            session_id="TicketPanel:global",
        )
        store._active_views["fake-id"] = fake_view

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
            store._active_views.pop("fake-id", None)

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

        fake_view = SimpleNamespace(
            _persistence_key="TicketPanel:msg:50",
            _init_kwargs={"weird": _Unserializable()},
            kwargs_schema_version=1,
            session_id="TicketPanel:global",
        )
        store._active_views["fake-id"] = fake_view

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
            store._active_views.pop("fake-id", None)

        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert rows == []

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
        middleware, mgr, backend = await _make_middleware()
        # Keep resetting the idle window every 20ms, but the ceiling
        # caps total wait at 80ms.
        middleware._ns_application.interval = 0.05
        middleware._ns_application.max_age = 0.08
        store = middleware._store
        store.state = {"application": {}, "sessions": {}}
        _slots_module._PERSISTENT_SLOTS.add("ticker")

        async def write(n):
            async def fn(action, state):
                new = dict(state)
                app = dict(state.get("application") or {})
                app["ticker"] = {"n": n}
                new["application"] = app
                store.state = new
                return new

            return fn

        # Dispatch every 20ms; the idle window never elapses but the
        # ceiling eventually clamps wait to 0 and a flush fires.
        start = asyncio.get_running_loop().time()
        for n in range(10):
            await middleware({"type": "TICK", "payload": {}}, store.state, await write(n))
            await asyncio.sleep(0.02)

        await _drain(middleware)
        elapsed = asyncio.get_running_loop().time() - start
        # Should have flushed within the 0.2s dispatch window plus
        # ceiling overhead; 0.5s ceiling is generous.
        assert elapsed < 0.5

        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert len(rows) == 1


# // ========================================( Retry backoff )======================================== // #


class TestMiddlewareRetryBackoff:
    """Flush failures increment retry_count and reschedule with backoff."""

    async def test_failure_reenqueues_and_increments_retry(self):
        middleware, mgr, backend = await _make_middleware()
        middleware._ns_application.interval = 0.01

        calls = {"count": 0}
        original = backend.row_upsert

        async def failing(table, row, key_columns):
            calls["count"] += 1
            raise RuntimeError("simulated backend failure")

        backend.row_upsert = failing  # type: ignore[method-assign]
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
        backend.row_upsert = original  # type: ignore[method-assign]
        middleware._ns_application.dirty_rows.clear()
        middleware._ns_application.deleted_keys.clear()
        await middleware.close()

    async def test_max_retries_resets_counter(self):
        middleware, mgr, backend = await _make_middleware()
        ns = middleware._ns_application

        async def failing(table, row, key_columns):
            raise RuntimeError("boom")

        backend.row_upsert = failing  # type: ignore[method-assign]
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

        async def failing(table, row, key_columns):
            raise RuntimeError("disk full")

        backend.row_upsert = failing  # type: ignore[method-assign]
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
