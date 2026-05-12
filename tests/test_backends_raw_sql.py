"""Raw-SQL escape-hatch tests for SQL-capable backends.

The cross-backend Protocol-conformance suite at ``tests/test_backends.py``
covers the namespace API. This file covers ``Capability.RAW_SQL``:
the four raw-query methods (``execute``, ``fetch``, ``executemany``,
``fetch_one``), the explicit ``transaction()`` context manager including
nested savepoint behavior, the ``placeholder_style`` ClassVar, and the
``InMemoryBackend`` opt-out (the in-memory backend deliberately does not
declare ``Capability.RAW_SQL`` and is excluded from this fixture).
"""

import asyncio

import pytest

from cascadeui.persistence import Capability, InMemoryBackend

# // ========================================( Backend probes )======================================== // #


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


def _sql_backend_ids():
    ids = []
    if sqlite_available:
        ids.append("SQLiteBackend")
    if postgres_available:
        ids.append("PostgresBackend")
    return ids


@pytest.fixture(params=_sql_backend_ids())
async def sql_backend(request, tmp_path):
    """SQL-capable backend instance, parametrized over SQLiteBackend
    and PostgresBackend. Skips PostgresBackend when asyncpg /
    testcontainers / Docker are unavailable.
    """
    if request.param == "SQLiteBackend":
        inst = SQLiteBackend(str(tmp_path / "raw.db"))
    elif request.param == "PostgresBackend":
        dsn = request.getfixturevalue("postgres_dsn")
        inst = PostgresBackend(dsn)
    else:
        pytest.skip(f"Unknown backend: {request.param}")
    await inst.initialize()
    try:
        yield inst
    finally:
        await inst.close()


def _placeholder(backend, n: int) -> str:
    """Return the placeholder string for the n-th positional argument
    according to the backend's PEP 249 paramstyle. Used by tests to
    write portable SQL across the SQL backends.
    """
    if backend.placeholder_style == "qmark":
        return "?"
    if backend.placeholder_style == "numeric":
        return f"${n}"
    raise AssertionError(f"Unsupported placeholder style: {backend.placeholder_style}")


# // ========================================( Capability declaration )======================================== // #


class TestRawSqlCapability:
    """Capability flag and ClassVar declarations on SQL-capable backends."""

    async def test_capability_declared(self, sql_backend):
        assert Capability.RAW_SQL in sql_backend.capabilities

    async def test_placeholder_style_set(self, sql_backend):
        assert sql_backend.placeholder_style in ("qmark", "numeric")

    async def test_placeholder_style_matches_class(self, sql_backend):
        # Class-level attribute is the canonical declaration; instance
        # access proxies to the class.
        assert type(sql_backend).placeholder_style == sql_backend.placeholder_style


# // ========================================( Cross-backend conformance )======================================== // #


class TestRawSqlConformance:
    """Behavior shared across SQL backends."""

    async def test_execute_ddl_round_trip(self, sql_backend):
        await sql_backend.execute(
            "CREATE TABLE IF NOT EXISTS test_tbl (id INTEGER PRIMARY KEY, name TEXT)"
        )
        ph = _placeholder(sql_backend, 1)
        ph2 = _placeholder(sql_backend, 2)
        await sql_backend.execute(f"INSERT INTO test_tbl VALUES ({ph}, {ph2})", 1, "foo")
        rows = await sql_backend.fetch("SELECT * FROM test_tbl")
        assert len(rows) == 1
        assert rows[0]["name"] == "foo"
        await sql_backend.execute("DROP TABLE test_tbl")

    async def test_fetch_empty_returns_empty_list(self, sql_backend):
        await sql_backend.execute("CREATE TABLE IF NOT EXISTS empty_tbl (id INTEGER PRIMARY KEY)")
        rows = await sql_backend.fetch("SELECT * FROM empty_tbl")
        assert rows == []
        await sql_backend.execute("DROP TABLE empty_tbl")

    async def test_fetch_one_returns_none_for_empty(self, sql_backend):
        await sql_backend.execute("CREATE TABLE IF NOT EXISTS empty_tbl (id INTEGER PRIMARY KEY)")
        ph = _placeholder(sql_backend, 1)
        result = await sql_backend.fetch_one(f"SELECT * FROM empty_tbl WHERE id = {ph}", 999)
        assert result is None
        await sql_backend.execute("DROP TABLE empty_tbl")

    async def test_fetch_one_returns_keyed_row(self, sql_backend):
        """fetch_one against a primary-key query returns the unique match."""
        await sql_backend.execute(
            "CREATE TABLE IF NOT EXISTS first_tbl (id INTEGER PRIMARY KEY, name TEXT)"
        )
        ph1 = _placeholder(sql_backend, 1)
        ph2 = _placeholder(sql_backend, 2)
        await sql_backend.execute(f"INSERT INTO first_tbl VALUES ({ph1}, {ph2})", 1, "alpha")
        await sql_backend.execute(f"INSERT INTO first_tbl VALUES ({ph1}, {ph2})", 2, "beta")
        result = await sql_backend.fetch_one(f"SELECT * FROM first_tbl WHERE id = {ph1}", 1)
        assert result is not None
        assert result["name"] == "alpha"
        await sql_backend.execute("DROP TABLE first_tbl")

    async def test_fetch_one_with_order_by_returns_deterministic_first(self, sql_backend):
        """fetch_one against a multi-match query with explicit ORDER BY
        returns the deterministic first row. Without ORDER BY, "first"
        is implementation-defined; the test pins the contract for the
        ordered case.
        """
        await sql_backend.execute(
            "CREATE TABLE IF NOT EXISTS multi_tbl (id INTEGER PRIMARY KEY, category TEXT, name TEXT)"
        )
        ph1 = _placeholder(sql_backend, 1)
        ph2 = _placeholder(sql_backend, 2)
        ph3 = _placeholder(sql_backend, 3)
        # Three rows with the same category; ORDER BY id should produce them in 10/20/30 order
        await sql_backend.execute(
            f"INSERT INTO multi_tbl VALUES ({ph1}, {ph2}, {ph3})", 30, "shared", "third"
        )
        await sql_backend.execute(
            f"INSERT INTO multi_tbl VALUES ({ph1}, {ph2}, {ph3})", 10, "shared", "first"
        )
        await sql_backend.execute(
            f"INSERT INTO multi_tbl VALUES ({ph1}, {ph2}, {ph3})", 20, "shared", "second"
        )
        result = await sql_backend.fetch_one(
            f"SELECT * FROM multi_tbl WHERE category = {ph1} ORDER BY id LIMIT 1",
            "shared",
        )
        assert result is not None
        assert result["id"] == 10
        assert result["name"] == "first"
        await sql_backend.execute("DROP TABLE multi_tbl")

    async def test_executemany_bulk_insert(self, sql_backend):
        await sql_backend.execute(
            "CREATE TABLE IF NOT EXISTS bulk_tbl (id INTEGER PRIMARY KEY, name TEXT)"
        )
        ph1 = _placeholder(sql_backend, 1)
        ph2 = _placeholder(sql_backend, 2)
        params_list = [(1, "a"), (2, "b"), (3, "c")]
        count = await sql_backend.executemany(
            f"INSERT INTO bulk_tbl VALUES ({ph1}, {ph2})", params_list
        )
        assert count == 3
        rows = await sql_backend.fetch("SELECT * FROM bulk_tbl ORDER BY id")
        assert [r["id"] for r in rows] == [1, 2, 3]
        await sql_backend.execute("DROP TABLE bulk_tbl")

    async def test_executemany_empty_returns_zero(self, sql_backend):
        # Empty params_list is a no-op returning 0 -- behavioral contract
        # documented on the Protocol. Use a real table so the assertion
        # exercises the contract (zero rows inserted on an empty list)
        # rather than coupling to any pre-parse short circuit; if the
        # implementation later validates SQL before checking params_list,
        # this test still passes.
        await sql_backend.execute("CREATE TABLE empty_tbl (id INTEGER, value TEXT)")
        try:
            ph1 = _placeholder(sql_backend, 1)
            ph2 = _placeholder(sql_backend, 2)
            count = await sql_backend.executemany(
                f"INSERT INTO empty_tbl (id, value) VALUES ({ph1}, {ph2})", []
            )
            assert count == 0
            rows = await sql_backend.fetch("SELECT id, value FROM empty_tbl")
            assert rows == []
        finally:
            await sql_backend.execute("DROP TABLE empty_tbl")

    async def test_empty_sql_raises_value_error(self, sql_backend):
        with pytest.raises(ValueError):
            await sql_backend.execute("")
        with pytest.raises(ValueError):
            await sql_backend.fetch("")
        with pytest.raises(ValueError):
            await sql_backend.executemany("", [(1,)])
        with pytest.raises(ValueError):
            await sql_backend.fetch_one("")


# // ========================================( Transaction primitive )======================================== // #


class TestRawSqlTransactions:
    """Transaction context manager: commit on clean exit, rollback on
    exception, savepoints on nested entries.
    """

    async def test_commit_on_clean_exit(self, sql_backend):
        await sql_backend.execute(
            "CREATE TABLE IF NOT EXISTS txn_commit_tbl (id INTEGER PRIMARY KEY)"
        )
        ph = _placeholder(sql_backend, 1)
        async with sql_backend.transaction():
            await sql_backend.execute(f"INSERT INTO txn_commit_tbl VALUES ({ph})", 1)
            await sql_backend.execute(f"INSERT INTO txn_commit_tbl VALUES ({ph})", 2)
        rows = await sql_backend.fetch("SELECT * FROM txn_commit_tbl ORDER BY id")
        assert [r["id"] for r in rows] == [1, 2]
        await sql_backend.execute("DROP TABLE txn_commit_tbl")

    async def test_rollback_on_exception(self, sql_backend):
        await sql_backend.execute(
            "CREATE TABLE IF NOT EXISTS txn_rollback_tbl (id INTEGER PRIMARY KEY)"
        )
        ph = _placeholder(sql_backend, 1)
        with pytest.raises(RuntimeError):
            async with sql_backend.transaction():
                await sql_backend.execute(f"INSERT INTO txn_rollback_tbl VALUES ({ph})", 1)
                raise RuntimeError("simulated failure")
        rows = await sql_backend.fetch("SELECT * FROM txn_rollback_tbl")
        assert rows == []
        await sql_backend.execute("DROP TABLE txn_rollback_tbl")

    async def test_nested_transaction_savepoint_isolation(self, sql_backend):
        """Inner transaction failure rolls back to savepoint; outer
        transaction continues and commits successfully.
        """
        await sql_backend.execute(
            "CREATE TABLE IF NOT EXISTS txn_nested_tbl (id INTEGER PRIMARY KEY)"
        )
        ph = _placeholder(sql_backend, 1)
        async with sql_backend.transaction():
            await sql_backend.execute(f"INSERT INTO txn_nested_tbl VALUES ({ph})", 1)
            try:
                async with sql_backend.transaction():
                    await sql_backend.execute(f"INSERT INTO txn_nested_tbl VALUES ({ph})", 2)
                    raise RuntimeError("inner failure")
            except RuntimeError:
                pass
            await sql_backend.execute(f"INSERT INTO txn_nested_tbl VALUES ({ph})", 3)
        rows = await sql_backend.fetch("SELECT id FROM txn_nested_tbl ORDER BY id")
        ids = [r["id"] for r in rows]
        assert 1 in ids
        assert 2 not in ids  # savepoint rolled back
        assert 3 in ids  # outer continued
        await sql_backend.execute("DROP TABLE txn_nested_tbl")

    async def test_nested_commit_then_outer_rollback(self, sql_backend):
        """Inner transaction commits to savepoint; outer rollback
        discards both inner and outer writes.
        """
        await sql_backend.execute(
            "CREATE TABLE IF NOT EXISTS txn_outer_tbl (id INTEGER PRIMARY KEY)"
        )
        ph = _placeholder(sql_backend, 1)
        with pytest.raises(RuntimeError):
            async with sql_backend.transaction():
                await sql_backend.execute(f"INSERT INTO txn_outer_tbl VALUES ({ph})", 1)
                async with sql_backend.transaction():
                    await sql_backend.execute(f"INSERT INTO txn_outer_tbl VALUES ({ph})", 2)
                # Inner committed to savepoint; outer continues but raises
                raise RuntimeError("outer failure")
        rows = await sql_backend.fetch("SELECT id FROM txn_outer_tbl")
        assert rows == []  # both rolled back
        await sql_backend.execute("DROP TABLE txn_outer_tbl")

    async def test_transaction_read_your_own_writes(self, sql_backend):
        """Mid-transaction reads observe writes already issued in the
        same transaction (read-your-own-writes), and the writes remain
        visible after commit.
        """
        await sql_backend.execute(
            "CREATE TABLE IF NOT EXISTS atomic_tbl (id INTEGER PRIMARY KEY, name TEXT)"
        )
        ph1 = _placeholder(sql_backend, 1)
        ph2 = _placeholder(sql_backend, 2)
        async with sql_backend.transaction():
            await sql_backend.execute(f"INSERT INTO atomic_tbl VALUES ({ph1}, {ph2})", 1, "first")
            # Mid-transaction read sees the write
            rows = await sql_backend.fetch("SELECT * FROM atomic_tbl")
            assert len(rows) == 1
        # Post-commit
        rows = await sql_backend.fetch("SELECT * FROM atomic_tbl")
        assert len(rows) == 1
        await sql_backend.execute("DROP TABLE atomic_tbl")


# // ========================================( PostgresBackend-specific )======================================== // #


@pytest.mark.skipif(not postgres_available, reason="asyncpg or testcontainers not installed")
class TestRawSqlPostgresSpecific:
    """ContextVar isolation across asyncio tasks for PostgresBackend.

    SQLite tracks transaction depth on the backend instance (singleton
    connection); PostgreSQL routes through a per-Task ContextVar that
    must isolate concurrent transactions correctly.
    """

    async def test_concurrent_transactions_isolate(self, postgres_dsn):
        """Two asyncio Tasks each running a transaction must not see
        each other's uncommitted writes (Read Committed default).
        """
        backend = PostgresBackend(postgres_dsn, pool_kwargs={"min_size": 2, "max_size": 4})
        await backend.initialize()
        try:
            await backend.execute(
                "CREATE TABLE IF NOT EXISTS iso_tbl (id INTEGER PRIMARY KEY, val TEXT)"
            )

            barrier = asyncio.Event()
            results: list[list[dict]] = []

            async def task_a():
                async with backend.transaction():
                    await backend.execute("INSERT INTO iso_tbl VALUES ($1, $2)", 1, "from_a")
                    # Hold the transaction open
                    await barrier.wait()
                # Commit happens here

            async def task_b():
                # Wait briefly so task_a starts first
                await asyncio.sleep(0.1)
                async with backend.transaction():
                    # Should not see task_a's uncommitted write
                    rows = await backend.fetch("SELECT * FROM iso_tbl")
                    results.append(rows)
                barrier.set()

            await asyncio.gather(task_a(), task_b())

            # Final state: task_a committed
            rows = await backend.fetch("SELECT * FROM iso_tbl")
            assert len(rows) == 1
            assert rows[0]["val"] == "from_a"

            # Task_b's mid-flight read saw 0 rows (Read Committed isolation)
            assert len(results) == 1
            assert results[0] == []

            await backend.execute("DROP TABLE iso_tbl")
        finally:
            await backend.close()


# // ========================================( InMemoryBackend opt-out )======================================== // #


class TestRawSqlOptOut:
    """InMemoryBackend deliberately does not declare Capability.RAW_SQL.
    Code paths that require raw SQL must check the capability flag first;
    accessing the raw-SQL methods on an in-memory backend raises
    NotImplementedError with a directed error message pointing the
    caller at SQLiteBackend or PostgresBackend.
    """

    async def test_capability_not_declared(self):
        backend = InMemoryBackend()
        assert Capability.RAW_SQL not in backend.capabilities

    async def test_placeholder_style_is_n_a(self):
        backend = InMemoryBackend()
        assert backend.placeholder_style == "n/a"

    async def test_execute_raises_not_implemented(self):
        backend = InMemoryBackend()
        await backend.initialize()
        try:
            with pytest.raises(NotImplementedError):
                await backend.execute("SELECT 1")
        finally:
            await backend.close()

    async def test_fetch_raises_not_implemented(self):
        backend = InMemoryBackend()
        await backend.initialize()
        try:
            with pytest.raises(NotImplementedError):
                await backend.fetch("SELECT 1")
        finally:
            await backend.close()

    async def test_fetch_one_raises_not_implemented(self):
        backend = InMemoryBackend()
        await backend.initialize()
        try:
            with pytest.raises(NotImplementedError):
                await backend.fetch_one("SELECT 1")
        finally:
            await backend.close()

    async def test_executemany_raises_not_implemented(self):
        backend = InMemoryBackend()
        await backend.initialize()
        try:
            with pytest.raises(NotImplementedError):
                await backend.executemany("INSERT INTO t VALUES (?)", [(1,)])
        finally:
            await backend.close()

    async def test_transaction_raises_not_implemented(self):
        backend = InMemoryBackend()
        await backend.initialize()
        try:
            with pytest.raises(NotImplementedError):
                backend.transaction()
        finally:
            await backend.close()
