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

import logging
import os

import pytest

from cascadeui.exceptions import PersistenceSchemaError
from cascadeui.persistence import Capability, InMemoryBackend
from cascadeui.persistence.config import ApplicationPersistence, RegistryPersistence
from cascadeui.persistence.manager import PersistenceManager
from cascadeui.persistence.migrations import physical_table
from cascadeui.persistence.schema import (
    ALL_DDL,
    CURRENT_SCHEMA_VERSIONS,
    TABLE_APPLICATION_SLOTS,
    TABLE_PERSISTENT_VIEWS,
    apply_table_prefix,
)
from cascadeui.state.singleton import get_store

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
    if request.param == "PostgresBackend":
        # Resolve the SYNC postgres_container fixture lazily so the InMemory
        # and SQLite branches never pay the container cost, then build the
        # per-test DB inline: getfixturevalue on the async postgres_dsn
        # fixture raises "Runner.run() cannot be called from a running event
        # loop" under pytest-asyncio.
        from tests._pg_helpers import postgres_test_db

        container = request.getfixturevalue("postgres_container")
        async with postgres_test_db(container) as dsn:
            inst = PostgresBackend(dsn)
            await inst.initialize()
            try:
                yield inst
            finally:
                await inst.close()
        return

    if request.param == "InMemoryBackend":
        inst = InMemoryBackend()
    elif request.param == "SQLiteBackend":
        inst = SQLiteBackend(str(tmp_path / "proto.db"))
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
        await backend.set_schema_version("some_table", 3)
        assert await backend.get_schema_version("some_table") == 3


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
class TestTablePrefix:
    """``table_prefix`` moves every object the backend owns.

    The default names all carry the ``cascadeui_`` prefix; the kwarg keeps
    two CascadeUI deployments sharing one database apart, and covers a
    consumer schema that collides with a library name outright --
    ``CREATE TABLE IF NOT EXISTS`` adopts a same-named table without a word.
    """

    async def _object_names(self, path):
        import sqlite3

        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'"
            )
            return sorted(r[0] for r in rows)
        finally:
            conn.close()

    async def test_default_names_are_unchanged(self, tmp_path):
        """Default parity: a consumer who sets nothing sees what shipped."""
        path = str(tmp_path / "default.db")
        backend = SQLiteBackend(path)
        await backend.initialize()
        await backend.close()

        names = await self._object_names(path)
        assert TABLE_PERSISTENT_VIEWS in names
        assert TABLE_APPLICATION_SLOTS in names
        assert not [n for n in names if n.startswith("pfx_")]

    async def test_prefix_moves_tables_and_indexes(self, tmp_path):
        """Indexes carry their table's name, so a prefix that misses them
        leaves two prefixed databases colliding on the index."""
        path = str(tmp_path / "prefixed.db")
        backend = SQLiteBackend(path, table_prefix="pfx_")
        await backend.initialize()
        await backend.close()

        names = await self._object_names(path)
        assert names, "no objects created"
        assert all(n.startswith("pfx_") for n in names), names
        assert f"pfx_{TABLE_PERSISTENT_VIEWS}" in names

    async def test_rows_round_trip_under_a_prefix(self, tmp_path):
        path = str(tmp_path / "rows.db")
        backend = SQLiteBackend(path, table_prefix="pfx_")
        await backend.initialize()
        await backend.row_upsert(
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
        rows = await backend.row_select(TABLE_APPLICATION_SLOTS)
        await backend.kv_write("ns", "k", b"v")
        value = await backend.kv_read("ns", "k")
        await backend.close()

        assert len(rows) == 1 and rows[0]["payload"] == '{"k":1}'
        assert value == b"v"

    async def test_a_migrator_runs_against_the_prefixed_table(self, tmp_path):
        """Migrators issue raw SQL, which does no prefixing of its own.

        Reading the logical name there targets the unprefixed table: absent
        on a prefixed database, or a consumer's own table of that name --
        the one the prefix was set to stay away from. Latent while one
        migrator exists and every schema bump makes it certain.
        """
        import sqlite3

        from cascadeui.persistence.migrations import get_schema_migrator

        path = str(tmp_path / "migrate.db")
        conn = sqlite3.connect(path)
        conn.execute(f"CREATE TABLE {TABLE_PERSISTENT_VIEWS} (id INTEGER PRIMARY KEY, note TEXT)")
        conn.commit()
        conn.close()

        backend = SQLiteBackend(path, table_prefix="pfx_")
        await backend.initialize()
        await get_schema_migrator(TABLE_PERSISTENT_VIEWS, 1)(backend)
        await backend.close()

        conn = sqlite3.connect(path)
        try:
            consumer = [r[1] for r in conn.execute(f"PRAGMA table_info({TABLE_PERSISTENT_VIEWS})")]
            library = [
                r[1] for r in conn.execute(f"PRAGMA table_info(pfx_{TABLE_PERSISTENT_VIEWS})")
            ]
        finally:
            conn.close()

        assert "first_unreachable_at" not in consumer, "the migrator altered the consumer's table"
        assert "first_unreachable_at" in library

    async def test_a_consumer_table_of_the_same_name_stays_separate(self, tmp_path):
        """The case the kwarg exists for, driven end to end."""
        import sqlite3

        path = str(tmp_path / "shared.db")
        conn = sqlite3.connect(path)
        conn.execute(f"CREATE TABLE {TABLE_PERSISTENT_VIEWS} (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute(f"INSERT INTO {TABLE_PERSISTENT_VIEWS} (note) VALUES ('consumer row')")
        conn.commit()
        conn.close()

        backend = SQLiteBackend(path, table_prefix="pfx_")
        await backend.initialize()
        await backend.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "panel",
                "view_class": "m.C",
                "message_id": 1,
                "channel_id": 2,
                "init_kwargs": "{}",
                "created_at": 0,
                "updated_at": 0,
            },
            ["persistence_key"],
        )
        rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
        await backend.close()

        assert len(rows) == 1 and rows[0]["persistence_key"] == "panel"

        conn = sqlite3.connect(path)
        try:
            kept = conn.execute(f"SELECT note FROM {TABLE_PERSISTENT_VIEWS}").fetchall()
        finally:
            conn.close()
        assert kept == [("consumer row",)], "the consumer's own table was touched"


# // ========================================( Legacy table rename )======================================== // #


def _sql_backend_ids():
    ids = []
    if sqlite_available:
        ids.append("SQLiteBackend")
    if postgres_available:
        ids.append("PostgresBackend")
    return ids


@pytest.fixture(params=_sql_backend_ids())
async def sql_backend(request, tmp_path):
    """SQL-capable backend, initialized and closed around each test.

    The reconciliation under test runs only on backends declaring
    ``Capability.RAW_SQL``, so ``InMemoryBackend`` is not a param here;
    its skip branch has its own test.
    """
    if request.param == "PostgresBackend":
        from tests._pg_helpers import postgres_test_db

        container = request.getfixturevalue("postgres_container")
        async with postgres_test_db(container) as dsn:
            inst = PostgresBackend(dsn)
            await inst.initialize()
            try:
                yield inst
            finally:
                await inst.close()
        return

    inst = SQLiteBackend(str(tmp_path / "legacy.db"))
    await inst.initialize()
    try:
        yield inst
    finally:
        await inst.close()


async def _table_names(backend):
    if backend.placeholder_style == "qmark":
        rows = await backend.fetch(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    else:
        rows = await backend.fetch(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
    return {r["name"] for r in rows}


async def _index_names(backend):
    if backend.placeholder_style == "qmark":
        rows = await backend.fetch(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
    else:
        rows = await backend.fetch(
            "SELECT indexname AS name FROM pg_indexes WHERE schemaname = current_schema()"
        )
    return {r["name"] for r in rows}


async def _rewind_to_legacy(backend):
    """Rewind a freshly initialized database to the pre-rename shape.

    Drops the current tables, recreates the two renamed ones under their
    old names (current column set, old index names), seeds one row each,
    records their versions under the old names, then re-runs the DDL a
    boot against this database would run before ``apply_migrations`` --
    which recreates the current names as the empty shells the
    reconciliation has to see past.
    """
    old_reg = physical_table(backend, "persistent_views")
    old_slots = physical_table(backend, "application_slots")
    await backend.execute(f"DROP TABLE {physical_table(backend, TABLE_PERSISTENT_VIEWS)}")
    await backend.execute(f"DROP TABLE {physical_table(backend, TABLE_APPLICATION_SLOTS)}")

    await backend.execute(f"""
        CREATE TABLE {old_reg} (
            persistence_key TEXT PRIMARY KEY,
            view_class TEXT NOT NULL,
            custom_id TEXT,
            message_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            guild_id BIGINT,
            user_id BIGINT,
            session_id TEXT,
            init_kwargs TEXT NOT NULL,
            kwargs_schema_version BIGINT NOT NULL DEFAULT 1,
            schema_version BIGINT NOT NULL DEFAULT 1,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            first_unreachable_at BIGINT
        )
        """)
    await backend.execute(
        f"CREATE INDEX {physical_table(backend, 'idx_persistent_views_message')} "
        f"ON {old_reg}(channel_id, message_id)"
    )
    await backend.execute(f"""
        CREATE TABLE {old_slots} (
            slot_name TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            schema_version BIGINT NOT NULL DEFAULT 1,
            updated_at BIGINT NOT NULL,
            expires_at BIGINT
        )
        """)
    await backend.execute(
        f"CREATE INDEX {physical_table(backend, 'idx_application_slots_expires')} "
        f"ON {old_slots}(expires_at)"
    )
    await backend.execute(
        f"INSERT INTO {old_reg} (persistence_key, view_class, message_id, channel_id, "
        f"init_kwargs, created_at, updated_at) VALUES ('legacy-panel', 'm.Cls', 1, 2, '{{}}', 0, 0)"
    )
    await backend.execute(
        f"INSERT INTO {old_slots} (slot_name, payload, updated_at) VALUES ('prefs', '{{}}', 0)"
    )
    await backend.set_schema_version("persistent_views", 2)
    await backend.set_schema_version("application_slots", 1)

    if backend.placeholder_style == "qmark":
        boot_ddl = ALL_DDL
    else:
        from cascadeui.persistence.schema_postgres import ALL_DDL_PG

        boot_ddl = ALL_DDL_PG
    for stmt in boot_ddl:
        await backend.execute(apply_table_prefix(stmt, backend.table_prefix))


def _manager_for(backend):
    return PersistenceManager(
        store=get_store(),
        registry=RegistryPersistence(backend=backend),
        application=ApplicationPersistence(backend=backend),
    )


class TestLegacyTableRename:
    """``apply_migrations`` renames a pre-prefix database to the current names.

    The registry and slots tables shipped as ``persistent_views`` and
    ``application_slots``. The reconciliation renames them -- version rows
    and indexes included -- exactly when the old table carries the library's
    columns and the current name holds no rows, and refuses everything else.
    """

    async def test_a_fresh_install_creates_only_the_prefixed_names(self, sql_backend):
        names = await _table_names(sql_backend)
        assert TABLE_PERSISTENT_VIEWS in names
        assert TABLE_APPLICATION_SLOTS in names
        assert "persistent_views" not in names
        assert "application_slots" not in names
        indexes = await _index_names(sql_backend)
        assert "idx_cascadeui_persistent_views_message" in indexes
        assert "idx_cascadeui_application_slots_expires" in indexes

    async def test_a_legacy_database_is_renamed_and_its_version_row_moves(self, sql_backend):
        await _rewind_to_legacy(sql_backend)
        await _manager_for(sql_backend).apply_migrations()

        names = await _table_names(sql_backend)
        assert TABLE_PERSISTENT_VIEWS in names
        assert TABLE_APPLICATION_SLOTS in names
        assert "persistent_views" not in names
        assert "application_slots" not in names

        rows = await sql_backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert [r["persistence_key"] for r in rows] == ["legacy-panel"]
        slots = await sql_backend.row_select(TABLE_APPLICATION_SLOTS)
        assert [r["slot_name"] for r in slots] == ["prefs"]

        # The version record moved with the data; the old key reads unset.
        assert await sql_backend.get_schema_version(TABLE_PERSISTENT_VIEWS) == 2
        assert await sql_backend.get_schema_version(TABLE_APPLICATION_SLOTS) == 1
        assert await sql_backend.get_schema_version("persistent_views") == 0
        assert await sql_backend.get_schema_version("application_slots") == 0

    async def test_the_rename_swaps_the_index_to_the_current_name(self, sql_backend):
        await _rewind_to_legacy(sql_backend)
        await _manager_for(sql_backend).apply_migrations()

        indexes = await _index_names(sql_backend)
        assert "idx_cascadeui_persistent_views_message" in indexes
        assert "idx_cascadeui_application_slots_expires" in indexes
        assert "idx_persistent_views_message" not in indexes
        assert "idx_application_slots_expires" not in indexes

    async def test_a_consumer_table_without_our_columns_is_untouched(self, sql_backend):
        consumer = physical_table(sql_backend, "persistent_views")
        await sql_backend.execute(f"CREATE TABLE {consumer} (id BIGINT PRIMARY KEY, note TEXT)")
        await sql_backend.execute(f"INSERT INTO {consumer} (id, note) VALUES (1, 'mine')")

        await _manager_for(sql_backend).apply_migrations()

        rows = await sql_backend.fetch(f"SELECT id, note FROM {consumer}")
        assert rows == [{"id": 1, "note": "mine"}]
        # The library's own table resolved as a fresh install beside it.
        assert (
            await sql_backend.get_schema_version(TABLE_PERSISTENT_VIEWS)
            == CURRENT_SCHEMA_VERSIONS[TABLE_PERSISTENT_VIEWS]
        )

    async def test_both_tables_with_rows_rename_nothing_and_warn(self, sql_backend, caplog):
        await _rewind_to_legacy(sql_backend)
        # Occupy the current name too: written through the namespace API,
        # this lands in the just-recreated current table.
        await sql_backend.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "new-panel",
                "view_class": "m.New",
                "message_id": 9,
                "channel_id": 9,
                "init_kwargs": "{}",
                "created_at": 0,
                "updated_at": 0,
            },
            ["persistence_key"],
        )

        with caplog.at_level(logging.WARNING):
            await _manager_for(sql_backend).apply_migrations()

        # Neither registry table was touched; the library reads the new one.
        old_reg = physical_table(sql_backend, "persistent_views")
        legacy_rows = await sql_backend.fetch(f"SELECT persistence_key FROM {old_reg}")
        assert [r["persistence_key"] for r in legacy_rows] == ["legacy-panel"]
        rows = await sql_backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert [r["persistence_key"] for r in rows] == ["new-panel"]

        warned = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        # Quoted, because the message interpolates {phys_old!r} and the bare
        # name is a substring of the current one: an assertion on
        # "persistent_views" passes on a warning naming only the new table.
        assert any("'persistent_views'" in m for m in warned), warned

        # The slots table had no such conflict and renamed independently.
        names = await _table_names(sql_backend)
        assert TABLE_APPLICATION_SLOTS in names
        assert "application_slots" not in names

    @pytest.mark.skipif(not sqlite_available, reason="aiosqlite not installed")
    async def test_the_rename_composes_with_table_prefix(self, tmp_path):
        backend = SQLiteBackend(str(tmp_path / "pfx.db"), table_prefix="pfx_")
        await backend.initialize()
        try:
            await _rewind_to_legacy(backend)
            await _manager_for(backend).apply_migrations()

            names = await _table_names(backend)
            assert f"pfx_{TABLE_PERSISTENT_VIEWS}" in names
            assert f"pfx_{TABLE_APPLICATION_SLOTS}" in names
            assert "pfx_persistent_views" not in names
            assert "pfx_application_slots" not in names

            rows = await backend.row_select(TABLE_PERSISTENT_VIEWS)
            assert [r["persistence_key"] for r in rows] == ["legacy-panel"]
            assert await backend.get_schema_version(TABLE_PERSISTENT_VIEWS) == 2

            indexes = await _index_names(backend)
            assert "pfx_idx_cascadeui_persistent_views_message" in indexes
            assert "pfx_idx_persistent_views_message" not in indexes
        finally:
            await backend.close()

    async def test_a_backend_without_raw_sql_skips_reconciliation(self):
        # Regression guard for the new skip branch: passes on any tree where
        # apply_migrations completes on a backend with no raw-SQL surface.
        backend = InMemoryBackend()
        await backend.initialize()
        await _manager_for(backend).apply_migrations()
        assert (
            await backend.get_schema_version(TABLE_PERSISTENT_VIEWS)
            == CURRENT_SCHEMA_VERSIONS[TABLE_PERSISTENT_VIEWS]
        )

    async def test_a_rename_another_process_finished_is_not_a_failure(self, sql_backend):
        """The probes run before the transaction, so on PostgreSQL a second
        booting process can commit the rename in between and this one fails
        on a table that is already gone. Losing that race is the healthy
        outcome; reporting it as a rename failure sends an operator after
        GRANTs. SQLite cannot reach it (one writer at a time), so the
        decision is asserted directly rather than raced.
        """
        mgr = _manager_for(sql_backend)
        already_done = mgr._legacy_rename_already_done

        # Fresh install: the current name exists, the old one never did.
        assert await already_done(sql_backend, "persistent_views", TABLE_PERSISTENT_VIEWS)

        await sql_backend.execute("CREATE TABLE persistent_views (persistence_key TEXT)")
        assert not await already_done(sql_backend, "persistent_views", TABLE_PERSISTENT_VIEWS)

        class _Unreachable:
            placeholder_style = sql_backend.placeholder_style

            async def fetch_one(self, *args, **kwargs):
                raise RuntimeError("connection gone")

        # A diagnostic that cannot run must not swallow the error it explains.
        assert not await already_done(_Unreachable(), "persistent_views", TABLE_PERSISTENT_VIEWS)

    async def test_a_failed_rename_raises_a_schema_error_naming_both_tables(self, sql_backend):
        """The rename runs before the version loop, so on a role that cannot
        rename it is the first DDL to fail and the one an operator sees."""
        await _rewind_to_legacy(sql_backend)
        old_name = physical_table(sql_backend, "persistent_views")
        original = sql_backend.execute

        async def refuse_the_rename(sql, *args, **kwargs):
            if "RENAME TO" in sql:
                raise RuntimeError("must be owner of table persistent_views")
            return await original(sql, *args, **kwargs)

        sql_backend.execute = refuse_the_rename
        try:
            with pytest.raises(PersistenceSchemaError, match="Renaming") as excinfo:
                await _manager_for(sql_backend).apply_migrations()
        finally:
            sql_backend.execute = original

        message = str(excinfo.value)
        assert old_name in message and TABLE_PERSISTENT_VIEWS in message
        assert "ownership" in message
        assert "must be owner of table" in message

        # The message claims the database is unchanged; hold it to that.
        names = await _table_names(sql_backend)
        assert "persistent_views" in names
        rows = await sql_backend.fetch(f"SELECT persistence_key FROM {old_name}")
        assert [r["persistence_key"] for r in rows] == ["legacy-panel"]

    async def test_a_table_that_vanishes_mid_probe_is_not_reported_as_a_consumer_table(
        self, sql_backend, caplog
    ):
        """A concurrent boot renaming the table between the existence check and
        the column probe is the one other failure the broad except sees."""
        await _rewind_to_legacy(sql_backend)
        old_name = physical_table(sql_backend, "persistent_views")
        original = sql_backend.fetch

        async def drop_it_first(sql, *args, **kwargs):
            if sql.startswith("SELECT persistence_key, view_class"):
                sql_backend.fetch = original
                await sql_backend.execute(f"DROP TABLE {old_name}")
            return await original(sql, *args, **kwargs)

        sql_backend.fetch = drop_it_first
        try:
            with caplog.at_level(logging.DEBUG, logger="cascadeui.persistence.manager"):
                await _manager_for(sql_backend).apply_migrations()
        finally:
            sql_backend.fetch = original

        assert "does not carry the library's columns" not in caplog.text
        assert "went away between the existence check" in caplog.text


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


class TestInMemoryBatchAtomicity:
    """The reference backend honors the all-or-nothing batch contract.

    The middleware's retry re-enqueues the whole batch, so a partial
    commit would be re-applied on top of itself. This backend is also
    what a new backend author reads to learn the contract.
    """

    async def test_failed_batch_commits_nothing(self):
        from cascadeui.persistence.backends.memory import InMemoryBackend

        backend = InMemoryBackend()
        await backend.initialize()
        await backend.row_upsert("ns", {"k": "pre", "v": 0}, ["k"])

        with pytest.raises(Exception):
            await backend.row_upsert_many(
                "ns", [{"k": "a", "v": 1}, {"k": "b", "v": 2}, "malformed"], ["k"]
            )

        assert [r["k"] for r in await backend.row_select("ns")] == ["pre"]

    async def test_successful_batch_still_commits(self):
        from cascadeui.persistence.backends.memory import InMemoryBackend

        backend = InMemoryBackend()
        await backend.initialize()
        await backend.row_upsert_many("ns", [{"k": "a"}, {"k": "b"}], ["k"])

        assert sorted(r["k"] for r in await backend.row_select("ns")) == ["a", "b"]
