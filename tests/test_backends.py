"""Protocol conformance tests for persistence backends.

Exercises :class:`~cascadeui.persistence.protocols.PersistenceBackend`
against every library-shipped implementation. One parametrized class
runs the full Protocol surface against :class:`InMemoryBackend` and
:class:`SQLiteBackend`, so any future backend author can drop their
class into the ``BACKENDS`` fixture list and see the same coverage.

Three contract items have their own classes because they are subtle
enough to warrant explicit coverage: copy-on-store (row_upsert must
defensively copy inputs), NULL-safe TTL prune (row_delete_where_lt
must not sweep rows whose column is missing/None), and scan-snapshot
safety (kv_scan must not RuntimeError when the caller writes mid-iter).
"""

import os

import pytest

from cascadeui.persistence import Capability, InMemoryBackend
from cascadeui.persistence.schema import (
    TABLE_APPLICATION_SLOTS,
    TABLE_PERSISTENT_VIEWS,
)

# // ========================================( Backend fixtures )======================================== // #


sqlite_available = False
try:
    import aiosqlite  # noqa: F401

    from cascadeui.persistence.backends.sqlite import SQLiteBackend

    sqlite_available = True
except ImportError:
    SQLiteBackend = None  # type: ignore[assignment]


postgres_available = False
try:
    import asyncpg  # noqa: F401
    from testcontainers.postgres import PostgresContainer  # noqa: F401

    from cascadeui.persistence.backends.postgres import PostgresBackend

    postgres_available = True
except ImportError:
    PostgresBackend = None  # type: ignore[assignment]


def _backend_ids():
    ids = ["InMemoryBackend"]
    if sqlite_available:
        ids.append("SQLiteBackend")
    if postgres_available:
        ids.append("PostgresBackend")
    return ids


@pytest.fixture(params=_backend_ids())
async def backend(request, tmp_path):
    """Protocol-parametrized backend instance, initialized and closed
    around each test. Skips SQLiteBackend when aiosqlite is unavailable
    and PostgresBackend when asyncpg / testcontainers / Docker are
    unavailable.
    """
    if request.param == "InMemoryBackend":
        inst = InMemoryBackend()
    elif request.param == "SQLiteBackend":
        inst = SQLiteBackend(str(tmp_path / "proto.db"))
    elif request.param == "PostgresBackend":
        # Postgres path defers fixture lookup so the InMemory and SQLite
        # branches do not pay the testcontainers spin-up cost when the
        # Postgres branch is skipped.
        dsn = request.getfixturevalue("postgres_dsn")
        inst = PostgresBackend(dsn)
    else:
        pytest.skip(f"Unknown backend: {request.param}")
    await inst.initialize()
    try:
        yield inst
    finally:
        await inst.close()


# // ========================================( Protocol conformance )======================================== // #


class TestBackendProtocolConformance:
    """Every library backend passes the full Protocol surface."""

    async def test_declares_capability_flag(self, backend):
        assert isinstance(backend.capabilities, Capability)
        # KV and SCHEMA_META are the baseline every shipped backend carries.
        assert Capability.KV in backend.capabilities
        assert Capability.SCHEMA_META in backend.capabilities

    async def test_kv_write_then_read_round_trips_bytes(self, backend):
        await backend.kv_write("testns", "k1", b"payload")
        value = await backend.kv_read("testns", "k1")
        assert value == b"payload"

    async def test_kv_read_missing_returns_none(self, backend):
        assert await backend.kv_read("testns", "absent") is None

    async def test_kv_delete_is_silent_on_missing(self, backend):
        # No raise, no return value convention.
        await backend.kv_delete("testns", "ghost")
        assert await backend.kv_read("testns", "ghost") is None

    async def test_kv_scan_filters_by_prefix(self, backend):
        await backend.kv_write("testns", "user:1", b"a")
        await backend.kv_write("testns", "user:2", b"b")
        await backend.kv_write("testns", "guild:7", b"c")
        seen = {k: v async for k, v in backend.kv_scan("testns", prefix="user:")}
        assert seen == {"user:1": b"a", "user:2": b"b"}

    async def test_kv_scan_empty_prefix_yields_all(self, backend):
        await backend.kv_write("ns2", "a", b"1")
        await backend.kv_write("ns2", "b", b"2")
        keys = [k async for k, _ in backend.kv_scan("ns2")]
        assert set(keys) == {"a", "b"}

    async def test_row_upsert_insert(self, backend):
        await backend.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "pref",
                "payload": "{}",
                "schema_version": 1,
                "updated_at": 100,
                "expires_at": None,
            },
            ["slot_name"],
        )
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert len(rows) == 1
        assert rows[0]["slot_name"] == "pref"

    async def test_row_upsert_updates_on_conflict(self, backend):
        base = {
            "slot_name": "pref",
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 100,
            "expires_at": None,
        }
        await backend.row_upsert(TABLE_APPLICATION_SLOTS, base, ["slot_name"])
        await backend.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {**base, "payload": '{"v": 2}', "updated_at": 200},
            ["slot_name"],
        )
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert len(rows) == 1
        assert rows[0]["payload"] == '{"v": 2}'
        assert rows[0]["updated_at"] == 200

    async def test_row_upsert_many_inserts_batch(self, backend):
        rows = [
            {
                "slot_name": n,
                "payload": "{}",
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            }
            for n in ("a", "b", "c")
        ]
        await backend.row_upsert_many(TABLE_APPLICATION_SLOTS, rows, ["slot_name"])
        got = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert {r["slot_name"] for r in got} == {"a", "b", "c"}

    async def test_row_upsert_many_mixes_insert_and_update(self, backend):
        base = {
            "slot_name": "pref",
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }
        await backend.row_upsert(TABLE_APPLICATION_SLOTS, base, ["slot_name"])
        await backend.row_upsert_many(
            TABLE_APPLICATION_SLOTS,
            [
                {**base, "payload": '{"v": 2}', "updated_at": 2},  # conflict -> update
                {**base, "slot_name": "new"},  # insert
            ],
            ["slot_name"],
        )
        got = {r["slot_name"]: r for r in await backend.row_select(TABLE_APPLICATION_SLOTS)}
        assert got["pref"]["payload"] == '{"v": 2}'
        assert got["pref"]["updated_at"] == 2
        assert "new" in got

    async def test_row_upsert_many_empty_is_noop(self, backend):
        await backend.row_upsert_many(TABLE_APPLICATION_SLOTS, [], ["slot_name"])
        assert await backend.row_select(TABLE_APPLICATION_SLOTS) == []

    async def test_row_upsert_many_copy_on_store(self, backend):
        # Mutating the caller's dict after the batch must not change the row.
        row = {
            "slot_name": "z",
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }
        await backend.row_upsert_many(TABLE_APPLICATION_SLOTS, [row], ["slot_name"])
        row["payload"] = "MUTATED"
        got = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert got[0]["payload"] == "{}"

    async def test_row_select_where_filters(self, backend):
        for name, payload in [("a", "{}"), ("b", "{}"), ("c", "{}")]:
            await backend.row_upsert(
                TABLE_APPLICATION_SLOTS,
                {
                    "slot_name": name,
                    "payload": payload,
                    "schema_version": 1,
                    "updated_at": 1,
                    "expires_at": None,
                },
                ["slot_name"],
            )
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS, {"slot_name": "b"})
        assert len(rows) == 1
        assert rows[0]["slot_name"] == "b"

    async def test_row_select_empty_returns_all(self, backend):
        for name in ("x", "y"):
            await backend.row_upsert(
                TABLE_APPLICATION_SLOTS,
                {
                    "slot_name": name,
                    "payload": "{}",
                    "schema_version": 1,
                    "updated_at": 1,
                    "expires_at": None,
                },
                ["slot_name"],
            )
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert {r["slot_name"] for r in rows} == {"x", "y"}

    async def test_row_delete_returns_count(self, backend):
        for name in ("a", "b", "c"):
            await backend.row_upsert(
                TABLE_APPLICATION_SLOTS,
                {
                    "slot_name": name,
                    "payload": "{}",
                    "schema_version": 1,
                    "updated_at": 1,
                    "expires_at": None,
                },
                ["slot_name"],
            )
        deleted = await backend.row_delete(TABLE_APPLICATION_SLOTS, {"slot_name": "b"})
        assert deleted == 1
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert {r["slot_name"] for r in rows} == {"a", "c"}

    async def test_row_delete_nonexistent_returns_zero(self, backend):
        deleted = await backend.row_delete(TABLE_APPLICATION_SLOTS, {"slot_name": "never"})
        assert deleted == 0

    async def test_row_delete_where_lt_ttl_prune(self, backend):
        # Three rows with different expires_at. Prune cutoff at 150
        # should delete only the row expiring at 100.
        for name, exp in (("old", 100), ("mid", 200), ("new", 300)):
            await backend.row_upsert(
                TABLE_APPLICATION_SLOTS,
                {
                    "slot_name": name,
                    "payload": "{}",
                    "schema_version": 1,
                    "updated_at": 1,
                    "expires_at": exp,
                },
                ["slot_name"],
            )
        deleted = await backend.row_delete_where_lt(TABLE_APPLICATION_SLOTS, "expires_at", 150)
        assert deleted == 1
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert {r["slot_name"] for r in rows} == {"mid", "new"}

    async def test_schema_version_fresh_returns_zero(self, backend):
        # Unknown table name: 0 is the Protocol contract for "never set".
        assert await backend.get_schema_version("never_touched") == 0

    async def test_schema_version_round_trip(self, backend):
        await backend.set_schema_version("application_slots", 3)
        assert await backend.get_schema_version("application_slots") == 3


# // ========================================( Contract items )======================================== // #


class TestBackendCopyOnStore:
    """row_upsert must not retain references to caller-owned dicts.

    A backend that stores the reference would leak later mutations
    into its storage, producing ghost writes on the next select.
    """

    async def test_input_mutation_does_not_affect_storage(self, tmp_path):
        backend = InMemoryBackend()
        await backend.initialize()
        row = {
            "slot_name": "pref",
            "payload": '{"v": 1}',
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }
        await backend.row_upsert(TABLE_APPLICATION_SLOTS, row, ["slot_name"])
        row["payload"] = '{"v": 999}'  # caller mutation after store
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert rows[0]["payload"] == '{"v": 1}'

    async def test_returned_rows_are_independent(self, tmp_path):
        backend = InMemoryBackend()
        await backend.initialize()
        await backend.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "pref",
                "payload": "{}",
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            },
            ["slot_name"],
        )
        first = await backend.row_select(TABLE_APPLICATION_SLOTS)
        first[0]["payload"] = "mutated"
        second = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert second[0]["payload"] == "{}"


class TestBackendNullSafeTTLPrune:
    """row_delete_where_lt must not sweep NULL/missing rows.

    SQL treats ``NULL < value`` as NULL (never true), so a TTL prune
    naturally preserves rows without an expiration. The in-memory
    backend mirrors that explicitly so the contract holds across
    implementations.
    """

    async def test_null_expires_at_survives_prune(self):
        backend = InMemoryBackend()
        await backend.initialize()
        await backend.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "forever",
                "payload": "{}",
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            },
            ["slot_name"],
        )
        await backend.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "expiring",
                "payload": "{}",
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": 100,
            },
            ["slot_name"],
        )
        deleted = await backend.row_delete_where_lt(TABLE_APPLICATION_SLOTS, "expires_at", 10_000)
        assert deleted == 1
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        assert rows[0]["slot_name"] == "forever"


class TestBackendScanSnapshotSafety:
    """kv_scan must not raise RuntimeError when callers write mid-iter.

    Snapshotting the keys up-front lets callers safely rewrite the
    namespace during iteration, which the rehydrate path occasionally
    does when migrating row shapes.
    """

    async def test_concurrent_write_during_scan(self):
        backend = InMemoryBackend()
        await backend.initialize()
        for i in range(5):
            await backend.kv_write("ns", f"k{i}", b"v")

        seen = []
        async for key, _ in backend.kv_scan("ns"):
            seen.append(key)
            # Mid-iteration write must not RuntimeError.
            await backend.kv_write("ns", f"new-{key}", b"x")

        assert len(seen) == 5


# // ========================================( SQLiteBackend persistence )======================================== // #


@pytest.mark.skipif(not sqlite_available, reason="aiosqlite not installed")
class TestSQLiteBackendPersistence:
    """SQLiteBackend persists across instances for the same file."""

    async def test_data_survives_reopen(self, tmp_path):
        path = str(tmp_path / "survive.db")
        a = SQLiteBackend(path)
        await a.initialize()
        await a.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "prefs",
                "payload": '{"k":1}',
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            },
            ["slot_name"],
        )
        await a.close()

        b = SQLiteBackend(path)
        await b.initialize()
        rows = await b.row_select(TABLE_APPLICATION_SLOTS)
        await b.close()
        assert len(rows) == 1
        assert rows[0]["payload"] == '{"k":1}'


@pytest.mark.skipif(not sqlite_available, reason="aiosqlite not installed")
class TestSQLiteBatchAtomicity:
    """row_upsert_many rolls back the whole batch on a mid-batch failure."""

    async def test_partial_batch_does_not_leak(self, tmp_path):
        backend = SQLiteBackend(str(tmp_path / "atomic.db"))
        await backend.initialize()
        good = {
            "slot_name": "A",
            "payload": "{}",
            "schema_version": 1,
            "updated_at": 1,
            "expires_at": None,
        }
        # A second column-signature group carrying a non-existent column fails
        # the second executemany; the first group must not stay committed.
        bad = {**good, "slot_name": "B", "NOPE": 1}
        with pytest.raises(Exception):
            await backend.row_upsert_many(TABLE_APPLICATION_SLOTS, [good, bad], ["slot_name"])

        assert await backend.row_select(TABLE_APPLICATION_SLOTS) == []  # atomic rollback

        # The connection recovers for the next write after the rollback.
        await backend.row_upsert(TABLE_APPLICATION_SLOTS, good, ["slot_name"])
        assert len(await backend.row_select(TABLE_APPLICATION_SLOTS)) == 1
        await backend.close()

    async def test_schema_version_survives_reopen(self, tmp_path):
        path = str(tmp_path / "schemav.db")
        a = SQLiteBackend(path)
        await a.initialize()
        await a.set_schema_version(TABLE_PERSISTENT_VIEWS, 2)
        await a.close()

        b = SQLiteBackend(path)
        await b.initialize()
        v = await b.get_schema_version(TABLE_PERSISTENT_VIEWS)
        await b.close()
        assert v == 2
