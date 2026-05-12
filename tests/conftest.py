"""Shared fixtures for CascadeUI tests."""

import os
import uuid

import pytest


@pytest.fixture(autouse=True)
def reset_state_store():
    """Reset the singleton StateStore between tests to prevent bleed.

    The store is cached in two places: StateStore._instance (class-level)
    and singleton._store_instance (module-level). Both must be cleared.
    """
    from cascadeui.state import singleton
    from cascadeui.state.store import StateStore

    StateStore._instance = None
    singleton._store_instance = None

    yield

    StateStore._instance = None
    singleton._store_instance = None


# // ========================================( PostgresBackend fixtures )======================================== // #


# Optional dependency probe. When asyncpg or testcontainers is missing,
# postgres-dependent tests skip cleanly via pytest.mark.skipif rather
# than failing at collection time.
postgres_available = False
try:
    import asyncpg  # noqa: F401
    from testcontainers.postgres import PostgresContainer  # noqa: F401

    postgres_available = True
except ImportError:
    PostgresContainer = None  # type: ignore[assignment,misc]


@pytest.fixture(scope="session")
def postgres_container():
    """Session-scoped PostgreSQL container via testcontainers.

    Skips when asyncpg or testcontainers is unavailable, or when Docker
    is not running. The container starts once per test session and tears
    down at exit. Per-test database isolation comes from the
    ``postgres_dsn`` fixture below, which creates a fresh database name
    against the same container.

    The ``pytest.skip`` at fixture-instantiation time produces a clean
    skip message on pytest 7+ (current minimum). The ``postgres_dsn``
    fixture below has a redundant ``postgres_available`` guard for
    defense-in-depth.
    """
    if not postgres_available:
        pytest.skip("asyncpg or testcontainers not installed")

    # Allow CI / dev environments to override the container image.
    image = os.environ.get("CASCADEUI_TEST_PG_IMAGE", "postgres:16-alpine")
    try:
        container = PostgresContainer(image)
        container.start()
    except Exception as exc:  # Docker unavailable, image pull failed, etc.
        pytest.skip(f"Postgres container could not start: {exc}")

    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def postgres_dsn(postgres_container):
    """Per-test PostgreSQL DSN with an isolated database name.

    Creates a new database for each test and drops it at teardown so
    parallel test runs and rerun-after-failure both see clean state.
    Returns a libpq URL suitable for ``asyncpg.connect()`` and
    ``PostgresBackend(dsn=...)``.
    """
    if not postgres_available:
        pytest.skip("asyncpg or testcontainers not installed")

    import asyncpg

    admin_dsn = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    db_name = f"cascadeui_test_{uuid.uuid4().hex[:12]}"

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()

    # Build the per-test DSN by swapping the database segment of the URL.
    base, _, _existing = admin_dsn.rpartition("/")
    dsn = f"{base}/{db_name}"

    try:
        yield dsn
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            # Force-disconnect any lingering listeners before drop.
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                db_name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await admin.close()
