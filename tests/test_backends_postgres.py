"""PostgresBackend-specific behavior tests.

The cross-backend Protocol-conformance suite at ``tests/test_backends.py``
already exercises ``PostgresBackend`` against the same shape as
``InMemoryBackend`` and ``SQLiteBackend``. This file covers PostgreSQL-
specific behavior that the parametrized suite cannot reach: JSONB column
round-trips, BIGINT snowflake handling, ``LISTEN``/``NOTIFY`` single-
process and multi-process scenarios, pool exhaustion serialization, and
DDL idempotency.

Tests skip cleanly when ``asyncpg`` or ``testcontainers`` is unavailable
or when Docker is not running. The ``postgres_dsn`` fixture in
``tests/conftest.py`` handles container lifecycle and per-test database
isolation.
"""

import asyncio
import json

import pytest

from cascadeui.persistence import Capability
from cascadeui.persistence.schema import (
    TABLE_APPLICATION_SLOTS,
    TABLE_PERSISTENT_VIEWS,
)

# Skip module entirely when asyncpg / testcontainers is unavailable.
postgres_available = False
try:
    import asyncpg
    from testcontainers.postgres import PostgresContainer  # noqa: F401

    from cascadeui.persistence.backends.postgres import (
        CHANNEL_INVALIDATION,
        PostgresBackend,
    )

    postgres_available = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(
    not postgres_available, reason="asyncpg or testcontainers not installed"
)


# // ========================================( Fixture )======================================== // #


@pytest.fixture
async def pg_backend(postgres_dsn):
    """Initialized PostgresBackend wired against the per-test database."""
    backend = PostgresBackend(postgres_dsn)
    await backend.initialize()
    try:
        yield backend
    finally:
        await backend.close()


# // ========================================( Construction )======================================== // #


class TestPostgresBackendConstruction:
    """Defensive input handling at the constructor seam."""

    def test_dsn_must_be_string(self):
        with pytest.raises(TypeError):
            PostgresBackend(dsn=12345)  # type: ignore[arg-type]

    def test_dsn_must_be_non_empty(self):
        with pytest.raises(ValueError):
            PostgresBackend(dsn="")

    def test_pool_kwargs_must_be_dict(self):
        with pytest.raises(TypeError):
            PostgresBackend(dsn="postgresql://localhost", pool_kwargs="bad")  # type: ignore[arg-type]

    def test_capabilities_declared(self):
        # Class-level flag declaration; safe to assert without
        # constructing or connecting.
        caps = PostgresBackend.capabilities
        assert Capability.KV in caps
        assert Capability.RELATIONAL in caps
        assert Capability.TTL_INDEX in caps
        assert Capability.SCHEMA_META in caps

    def test_callback_validation(self):
        # Pure synchronous validation. No connection or live backend
        # needed; constructing against a never-initialized DSN is fine
        # because set_invalidation_callback never touches the pool.
        backend = PostgresBackend(dsn="postgresql://localhost/never_initialized")
        with pytest.raises(TypeError):
            backend.set_invalidation_callback("not callable")  # type: ignore[arg-type]
        # None clears the callback without raising.
        backend.set_invalidation_callback(None)


# // ========================================( PostgreSQL-native behaviors )======================================== // #


class TestPostgresJSONBRoundTrip:
    """JSONB columns round-trip JSON-string payloads bit-stable for
    cross-backend compatibility with SQLiteBackend's TEXT storage.
    """

    async def test_simple_payload_round_trip(self, pg_backend):
        await pg_backend.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "pref",
                "payload": '{"v": 1}',
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            },
            ["slot_name"],
        )
        rows = await pg_backend.row_select(TABLE_APPLICATION_SLOTS)
        # JSONB does not preserve whitespace; restringifying produces
        # canonical form. Test asserts json equivalence, not byte equality.
        assert json.loads(rows[0]["payload"]) == {"v": 1}

    async def test_dict_payload_coerced_to_json(self, pg_backend):
        # Caller passes a dict directly. PostgresBackend's
        # _coerce_jsonb_for_write should serialize it before insert.
        await pg_backend.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "obj",
                "payload": {"nested": {"k": [1, 2, 3]}},
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            },
            ["slot_name"],
        )
        rows = await pg_backend.row_select(TABLE_APPLICATION_SLOTS)
        assert json.loads(rows[0]["payload"]) == {"nested": {"k": [1, 2, 3]}}

    async def test_jsonb_column_has_correct_type(self, pg_backend, postgres_dsn):
        # Direct database query confirming the column type is JSONB,
        # not TEXT. Guards against accidental DDL drift.
        conn = await asyncpg.connect(postgres_dsn)
        try:
            row = await conn.fetchrow(
                """
                SELECT data_type FROM information_schema.columns
                WHERE table_name = $1 AND column_name = $2
                """,
                TABLE_APPLICATION_SLOTS,
                "payload",
            )
        finally:
            await conn.close()
        assert row is not None
        assert row["data_type"] == "jsonb"


class TestPostgresBigIntSnowflakes:
    """BIGINT snowflake columns accept full Discord ID range without overflow."""

    async def test_18_digit_snowflake_round_trip(self, pg_backend):
        # Realistic Discord-shape snowflake (18 digits, well within
        # signed BIGINT 2^63-1 = 9223372036854775807).
        big_id = 999999999999999999
        await pg_backend.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "test:1",
                "view_class": "MyView",
                "custom_id": None,
                "message_id": big_id,
                "channel_id": big_id - 1,
                "guild_id": big_id - 2,
                "user_id": big_id - 3,
                "session_id": None,
                "init_kwargs": "{}",
                "kwargs_schema_version": 1,
                "schema_version": 1,
                "created_at": 1700000000,
                "updated_at": 1700000000,
            },
            ["persistence_key"],
        )
        rows = await pg_backend.row_select(TABLE_PERSISTENT_VIEWS)
        assert rows[0]["message_id"] == big_id
        assert rows[0]["channel_id"] == big_id - 1


# // ========================================( LISTEN / NOTIFY )======================================== // #


class TestPostgresListenNotify:
    """Cross-process invalidation broadcasts via LISTEN/NOTIFY."""

    async def test_kv_write_fires_invalidation_callback(self, pg_backend):
        received: list[tuple[str, str]] = []
        callback_done = asyncio.Event()

        def on_invalidate(namespace: str, key: str) -> None:
            received.append((namespace, key))
            callback_done.set()

        pg_backend.set_invalidation_callback(on_invalidate)

        await pg_backend.kv_write("ns1", "k1", b"value")

        # NOTIFY delivery is asynchronous. Wait briefly for the listener
        # to receive and dispatch.
        try:
            await asyncio.wait_for(callback_done.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("Invalidation callback did not fire within 5s")

        assert ("ns1", "k1") in received

    async def test_row_upsert_fires_invalidation(self, pg_backend):
        received: list[tuple[str, str]] = []
        callback_done = asyncio.Event()

        def on_invalidate(namespace: str, key: str) -> None:
            received.append((namespace, key))
            callback_done.set()

        pg_backend.set_invalidation_callback(on_invalidate)

        await pg_backend.row_upsert(
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

        try:
            await asyncio.wait_for(callback_done.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("Invalidation callback did not fire within 5s")

        assert received[0][0] == TABLE_APPLICATION_SLOTS

    async def test_multi_process_invalidation(self, postgres_dsn):
        """Two PostgresBackend instances against the same database
        observe each other's writes via LISTEN/NOTIFY.

        Simulates the multi-process bot scenario: worker A writes,
        worker B's invalidation callback fires.
        """
        backend_a = PostgresBackend(postgres_dsn)
        backend_b = PostgresBackend(postgres_dsn)
        await backend_a.initialize()
        try:
            await backend_b.initialize()
            try:
                received: list[tuple[str, str]] = []
                done = asyncio.Event()

                def on_b_invalidate(namespace: str, key: str) -> None:
                    received.append((namespace, key))
                    done.set()

                backend_b.set_invalidation_callback(on_b_invalidate)

                # backend_b.initialize() awaits add_listener, which
                # commits LISTEN before returning. backend_a's write
                # therefore fires NOTIFY after backend_b's LISTEN commit
                # point, so backend_b receives the invalidation.
                await backend_a.kv_write("ns_x", "k_x", b"payload")

                try:
                    await asyncio.wait_for(done.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pytest.fail("Cross-process invalidation did not propagate within 5s")

                assert ("ns_x", "k_x") in received
            finally:
                await backend_b.close()
        finally:
            await backend_a.close()


# // ========================================( Operational behaviors )======================================== // #


class TestPostgresOperational:
    """Pool, DDL idempotency, and connection-lifecycle edge cases."""

    async def test_initialize_idempotent(self, postgres_dsn):
        """Calling initialize() twice on the same backend is a no-op
        on the second call -- pool is reused, DDL re-runs are safe.
        """
        backend = PostgresBackend(postgres_dsn)
        await backend.initialize()
        try:
            first_pool = backend._pool
            await backend.initialize()  # second call should be no-op
            assert backend._pool is first_pool
        finally:
            await backend.close()

    async def test_close_idempotent(self, postgres_dsn):
        """Calling close() multiple times is safe."""
        backend = PostgresBackend(postgres_dsn)
        await backend.initialize()
        await backend.close()
        # Second close should not raise.
        await backend.close()

    async def test_method_before_initialize_raises(self):
        backend = PostgresBackend(dsn="postgresql://localhost/never_initialized")
        with pytest.raises(RuntimeError, match="initialize"):
            await backend.kv_read("ns", "k")

    async def test_pool_serializes_under_min_size_one(self, postgres_dsn):
        """Pool with max_size=1 serializes concurrent reads. No
        exception, just sequential execution.
        """
        backend = PostgresBackend(
            postgres_dsn,
            pool_kwargs={"min_size": 1, "max_size": 1, "statement_cache_size": 0},
        )
        await backend.initialize()
        try:
            await backend.kv_write("ns", "a", b"1")
            await backend.kv_write("ns", "b", b"2")

            # Concurrent reads should both succeed without pool errors.
            results = await asyncio.gather(
                backend.kv_read("ns", "a"),
                backend.kv_read("ns", "b"),
            )
            assert b"1" in results
            assert b"2" in results
        finally:
            await backend.close()


class TestPostgresPersistenceAcrossRestarts:
    """Data survives backend close/reopen against the same database."""

    async def test_data_survives_reopen(self, postgres_dsn):
        a = PostgresBackend(postgres_dsn)
        await a.initialize()
        await a.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "prefs",
                "payload": '{"k": 1}',
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            },
            ["slot_name"],
        )
        await a.close()

        b = PostgresBackend(postgres_dsn)
        await b.initialize()
        try:
            rows = await b.row_select(TABLE_APPLICATION_SLOTS)
            assert len(rows) == 1
            assert json.loads(rows[0]["payload"]) == {"k": 1}
        finally:
            await b.close()

    async def test_schema_version_survives_reopen(self, postgres_dsn):
        a = PostgresBackend(postgres_dsn)
        await a.initialize()
        await a.set_schema_version(TABLE_PERSISTENT_VIEWS, 2)
        await a.close()

        b = PostgresBackend(postgres_dsn)
        await b.initialize()
        try:
            assert await b.get_schema_version(TABLE_PERSISTENT_VIEWS) == 2
        finally:
            await b.close()


# // ========================================( Internal helpers )======================================== // #


class TestPostgresInternalHelpers:
    """Direct unit tests on module-level helpers."""

    def test_parse_command_tag_count_with_count(self):
        from cascadeui.persistence.backends.postgres import _parse_command_tag_count

        assert _parse_command_tag_count("DELETE 3") == 3
        assert _parse_command_tag_count("UPDATE 1") == 1
        assert _parse_command_tag_count("INSERT 0 5") == 5

    def test_parse_command_tag_count_empty(self):
        from cascadeui.persistence.backends.postgres import _parse_command_tag_count

        assert _parse_command_tag_count("") == 0

    def test_parse_command_tag_count_no_number(self):
        from cascadeui.persistence.backends.postgres import _parse_command_tag_count

        assert _parse_command_tag_count("BEGIN") == 0

    def test_quote_ident_doubles_embedded_quotes(self):
        from cascadeui.persistence.backends.postgres import _quote_ident

        assert _quote_ident("plain") == '"plain"'
        assert _quote_ident('with"quote') == '"with""quote"'

    def test_escape_like_escapes_wildcards(self):
        from cascadeui.persistence.backends.postgres import _escape_like

        assert _escape_like("plain") == "plain"
        assert _escape_like("100%") == "100\\%"
        assert _escape_like("a_b") == "a\\_b"
        assert _escape_like("c\\d") == "c\\\\d"

    def test_channel_constant(self):
        # Hardcoded channel name; bumping it would require a coordinated
        # cross-version migration, so the test pins it.
        assert CHANNEL_INVALIDATION == "cascadeui_invalidation"
