"""Schema version declarations and table DDL for the persistence layer.

:data:`CURRENT_SCHEMA_VERSIONS` is the single source of truth for
what the library expects on disk. Migrators registered in
:mod:`cascadeui.persistence.migrations` close the gap between
on-disk versions and current versions when
:class:`~cascadeui.state.middleware.PersistenceMiddleware` initializes.

DDL constants are SQLite-flavored. Backends that target a different
engine can reuse the table names and column shapes but should not
import the SQL strings directly -- they are tied to SQLite syntax
(nullable primary-key columns, ``INTEGER`` affinity for timestamps).
"""

# // ========================================( Modules )======================================== // #


from typing import Final

# // ========================================( Versions )======================================== // #


CURRENT_SCHEMA_VERSIONS: Final[dict[str, int]] = {
    "persistent_views": 1,
    "application_slots": 1,
    "cascadeui_schema": 1,
    "cascadeui_kv": 1,
}


# // ========================================( Table Names )======================================== // #


# Exposed as constants so migrators, backends, and tests agree on one
# source of truth.

TABLE_PERSISTENT_VIEWS: Final[str] = "persistent_views"
TABLE_APPLICATION_SLOTS: Final[str] = "application_slots"
TABLE_SCHEMA_META: Final[str] = "cascadeui_schema"
TABLE_KV: Final[str] = "cascadeui_kv"


# // ========================================( DDL -- SQLite )======================================== // #


DDL_PERSISTENT_VIEWS: Final[
    str
] = """
CREATE TABLE IF NOT EXISTS persistent_views (
    persistence_key TEXT PRIMARY KEY,
    view_class TEXT NOT NULL,
    custom_id TEXT,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    guild_id INTEGER,
    user_id INTEGER,
    session_id TEXT,
    init_kwargs TEXT NOT NULL,
    kwargs_schema_version INTEGER NOT NULL DEFAULT 1,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)
"""


DDL_PERSISTENT_VIEWS_INDEX: Final[
    str
] = """
CREATE INDEX IF NOT EXISTS idx_persistent_views_message
    ON persistent_views(channel_id, message_id)
"""


DDL_APPLICATION_SLOTS: Final[
    str
] = """
CREATE TABLE IF NOT EXISTS application_slots (
    slot_name TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER
)
"""


DDL_APPLICATION_SLOTS_INDEX: Final[
    str
] = """
CREATE INDEX IF NOT EXISTS idx_application_slots_expires
    ON application_slots(expires_at)
"""


DDL_SCHEMA_META: Final[
    str
] = """
CREATE TABLE IF NOT EXISTS cascadeui_schema (
    table_name TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    applied_at INTEGER NOT NULL
)
"""


# Generic key-value surface used by the KV Protocol methods. Namespaced so
# multiple logical stores share one physical table. Value is BLOB so callers
# can stash arbitrary bytes (serialized JSON, pickle, msgpack) without the
# backend caring about the payload shape.
DDL_KV: Final[
    str
] = """
CREATE TABLE IF NOT EXISTS cascadeui_kv (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value BLOB NOT NULL,
    PRIMARY KEY (namespace, key)
)
"""


# Ordered list of every DDL statement needed to bring a fresh database
# up to current schema. Backends iterate this list during
# initialize().
ALL_DDL: Final[tuple[str, ...]] = (
    DDL_SCHEMA_META,
    DDL_PERSISTENT_VIEWS,
    DDL_PERSISTENT_VIEWS_INDEX,
    DDL_APPLICATION_SLOTS,
    DDL_APPLICATION_SLOTS_INDEX,
    DDL_KV,
)
