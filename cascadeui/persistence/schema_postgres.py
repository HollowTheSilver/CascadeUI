"""PostgreSQL-flavored DDL for the persistence layer.

Mirrors :mod:`cascadeui.persistence.schema` (SQLite DDL) using
PostgreSQL-native types where the column type differs and
SQLite-equivalent types where cross-backend test compatibility
applies.

Type-mapping decisions:

- ``JSONB`` for columns holding JSON payloads (``init_kwargs``,
  ``payload``). PostgreSQL's binary-decomposed JSON storage indexes
  efficiently and parses once at write rather than per read.
- ``BIGINT`` for snowflake columns (``message_id``, ``channel_id``,
  ``guild_id``, ``user_id``). Discord IDs are 64-bit; signed BIGINT's
  range covers all real-world snowflakes.
- ``BIGINT`` for timestamp columns (``created_at``, ``updated_at``,
  ``expires_at``, ``applied_at``) holding Unix-epoch seconds. SQLiteBackend's
  ``INTEGER`` storage is preserved verbatim so the cross-backend
  Protocol-conformance suite at ``tests/test_backends.py`` runs against
  PostgresBackend without coercion seams. Operators who want timezone-
  aware queries against the persistence schema run conversions in their
  own SQL.
- ``BYTEA`` for the KV ``value`` column (binary payload).

Table names are imported from :mod:`cascadeui.persistence.schema` so
both backends agree on the partitioning vocabulary; only the DDL
strings differ between modules.
"""

# // ========================================( Modules )======================================== // #


from typing import Final

from .schema import (
    TABLE_APPLICATION_SLOTS,
    TABLE_KV,
    TABLE_PERSISTENT_VIEWS,
    TABLE_SCHEMA_META,
)

# // ========================================( DDL -- PostgreSQL )======================================== // #


DDL_SCHEMA_META_PG: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SCHEMA_META} (
    table_name TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    applied_at BIGINT NOT NULL
)
"""


DDL_PERSISTENT_VIEWS_PG: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {TABLE_PERSISTENT_VIEWS} (
    persistence_key TEXT PRIMARY KEY,
    view_class TEXT NOT NULL,
    custom_id TEXT,
    message_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    guild_id BIGINT,
    user_id BIGINT,
    session_id TEXT,
    init_kwargs JSONB NOT NULL,
    kwargs_schema_version INTEGER NOT NULL DEFAULT 1,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
)
"""


DDL_PERSISTENT_VIEWS_INDEX_PG: Final[str] = f"""
CREATE INDEX IF NOT EXISTS idx_persistent_views_message
    ON {TABLE_PERSISTENT_VIEWS}(channel_id, message_id)
"""


DDL_APPLICATION_SLOTS_PG: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {TABLE_APPLICATION_SLOTS} (
    slot_name TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    updated_at BIGINT NOT NULL,
    expires_at BIGINT
)
"""


DDL_APPLICATION_SLOTS_INDEX_PG: Final[str] = f"""
CREATE INDEX IF NOT EXISTS idx_application_slots_expires
    ON {TABLE_APPLICATION_SLOTS}(expires_at)
"""


DDL_KV_PG: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {TABLE_KV} (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value BYTEA NOT NULL,
    PRIMARY KEY (namespace, key)
)
"""


# Ordered list mirroring schema.py's ALL_DDL. PostgresBackend iterates
# this during initialize().
ALL_DDL_PG: Final[tuple[str, ...]] = (
    DDL_SCHEMA_META_PG,
    DDL_PERSISTENT_VIEWS_PG,
    DDL_PERSISTENT_VIEWS_INDEX_PG,
    DDL_APPLICATION_SLOTS_PG,
    DDL_APPLICATION_SLOTS_INDEX_PG,
    DDL_KV_PG,
)


# JSONB columns per table. PostgresBackend's row_upsert / row_select
# coerces between JSON-string (the on-the-wire shape PersistenceManager
# passes) and dict (asyncpg's default decoder shape) for these columns.
# Other columns flow through unchanged.
JSONB_COLUMNS: Final[dict[str, frozenset[str]]] = {
    TABLE_PERSISTENT_VIEWS: frozenset({"init_kwargs"}),
    TABLE_APPLICATION_SLOTS: frozenset({"payload"}),
}
