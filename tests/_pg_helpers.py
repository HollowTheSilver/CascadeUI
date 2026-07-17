"""Shared PostgreSQL per-test database helper.

The create/drop of an isolated per-test database lives here so both the
``postgres_dsn`` fixture and the parametrized backend fixtures build the same
DSN from one implementation.

The parametrized backend fixtures resolve the sync ``postgres_container``
fixture lazily (so their InMemory / SQLite branches never pay the container
cost) and enter this context manager inline. They cannot request the async
``postgres_dsn`` fixture through ``request.getfixturevalue``: under
pytest-asyncio, resolving an async fixture that way runs its setup coroutine
via ``asyncio.run`` inside the already-running test loop and raises
``RuntimeError: Runner.run() cannot be called from a running event loop``.
Resolving the sync container fixture is safe, and the async DB work then
happens inside the fixture's own coroutine.
"""

import contextlib
import uuid


@contextlib.asynccontextmanager
async def postgres_test_db(container):
    """Yield a DSN for a fresh per-test database on ``container``, dropped after.

    Creates a uniquely named database so parallel runs and rerun-after-failure
    both see clean state, and drops it on exit after terminating any lingering
    connections.
    """
    import asyncpg

    admin_dsn = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    db_name = f"cascadeui_test_{uuid.uuid4().hex[:12]}"

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()

    base, _, _existing = admin_dsn.rpartition("/")
    dsn = f"{base}/{db_name}"
    try:
        yield dsn
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                db_name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await admin.close()
