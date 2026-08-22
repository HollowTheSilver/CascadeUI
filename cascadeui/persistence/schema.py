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


import re
from typing import Final, NamedTuple

# // ========================================( Versions )======================================== // #


CURRENT_SCHEMA_VERSIONS: Final[dict[str, int]] = {
    "cascadeui_persistent_views": 2,
    "cascadeui_application_slots": 1,
    "cascadeui_schema": 1,
    "cascadeui_kv": 1,
}


# // ========================================( Table Names )======================================== // #


# Exposed as constants so migrators, backends, and tests agree on one
# source of truth.

TABLE_PERSISTENT_VIEWS: Final[str] = "cascadeui_persistent_views"
TABLE_APPLICATION_SLOTS: Final[str] = "cascadeui_application_slots"
TABLE_SCHEMA_META: Final[str] = "cascadeui_schema"
TABLE_KV: Final[str] = "cascadeui_kv"

ALL_TABLES: Final[tuple[str, ...]] = (
    TABLE_PERSISTENT_VIEWS,
    TABLE_APPLICATION_SLOTS,
    TABLE_SCHEMA_META,
    TABLE_KV,
)

# Index names carry their table's name, so a prefix has to reach them too:
# two databases sharing a schema under different prefixes would otherwise
# collide on the index while their tables stayed apart.
_INDEX_PREFIX: Final[str] = "idx_"


# fullmatch rather than an anchored match: "$" also matches just before a
# trailing newline, so a prefix read from an env var or a config line
# would pass here and split into two SQL tokens mid-DDL, failing with a
# syntax error naming neither the prefix nor the newline.
_SAFE_PREFIX = re.compile(r"[a-z_][a-z0-9_]*")


def validate_table_prefix(prefix: str, owner: str) -> None:
    """Reject a table prefix that cannot be interpolated as an identifier.

    The prefix reaches SQL two ways that do not agree on quoting: the DDL
    and the migrator resolve names by text substitution, unquoted, while a
    backend's own row and key-value paths quote what they build. PostgreSQL
    folds an unquoted identifier to lower case and preserves a quoted one,
    so a prefix carrying a capital would create ``Bot_cascadeui_kv`` on one
    path and address ``bot_cascadeui_kv`` on the other, splitting one
    deployment's data across two tables that both look right in isolation.

    Lower case, digits, and underscores are what survive both paths
    identically. Everything else is refused here, where the deployment
    names its prefix, rather than at a divergence discovered later.
    """
    if not isinstance(prefix, str):
        raise TypeError(
            f"{owner} table_prefix must be a str, got {type(prefix).__name__}: {prefix!r}"
        )
    if prefix and not _SAFE_PREFIX.fullmatch(prefix):
        raise ValueError(
            f"{owner} table_prefix {prefix!r} is not a usable identifier prefix.\n"
            f"  Fix: use lower-case letters, digits, and underscores, starting "
            f"with a letter or underscore (for example 'staging_'). A capital "
            f"or a quote resolves differently in the DDL than in the row path, "
            f"so one deployment would address two sets of tables."
        )


def apply_table_prefix(sql: str, prefix: str) -> str:
    """Rewrite every library table and index name in ``sql`` under ``prefix``.

    Word-boundary matched, so ``idx_cascadeui_persistent_views_message`` is
    not caught by the ``cascadeui_persistent_views`` pass (an underscore is a
    word character, so no boundary sits before the table name there); the
    index pass renames it on its own. An empty prefix returns ``sql``
    unchanged, which is the default path and does no work.
    """
    if not prefix:
        return sql
    for name in ALL_TABLES:
        sql = re.sub(rf"\b{re.escape(name)}\b", f"{prefix}{name}", sql)
    return re.sub(rf"\b{_INDEX_PREFIX}", f"{prefix}{_INDEX_PREFIX}", sql)


# // ========================================( DDL -- SQLite )======================================== // #


DDL_PERSISTENT_VIEWS: Final[str] = """
CREATE TABLE IF NOT EXISTS cascadeui_persistent_views (
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
    updated_at INTEGER NOT NULL,
    first_unreachable_at INTEGER
)
"""


DDL_PERSISTENT_VIEWS_INDEX: Final[str] = """
CREATE INDEX IF NOT EXISTS idx_cascadeui_persistent_views_message
    ON cascadeui_persistent_views(channel_id, message_id)
"""


DDL_APPLICATION_SLOTS: Final[str] = """
CREATE TABLE IF NOT EXISTS cascadeui_application_slots (
    slot_name TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER
)
"""


DDL_APPLICATION_SLOTS_INDEX: Final[str] = """
CREATE INDEX IF NOT EXISTS idx_cascadeui_application_slots_expires
    ON cascadeui_application_slots(expires_at)
"""


DDL_SCHEMA_META: Final[str] = """
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
DDL_KV: Final[str] = """
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


# // ========================================( Legacy Names )======================================== // #


class LegacyTableRename(NamedTuple):
    """One table's pre-rename identity, consumed by
    :meth:`~cascadeui.persistence.manager.PersistenceManager.apply_migrations`
    when it reconciles a database created under the old names.

    Attributes
    ----------
    old_name
        The unprefixed name the table shipped under.
    signature_columns
        Columns that identify the old table as library-owned. A
        consumer's same-named table without them is left alone.
    old_index
        The index the old DDL created. A rename keeps the index under
        its old name on both engines, so reconciliation drops it and
        recreates from ``index_ddl`` under the current name.
    index_ddl
        The current ``CREATE INDEX IF NOT EXISTS`` statement. The
        SQLite text is valid PostgreSQL verbatim (the two schema
        modules differ only in column types, which an index statement
        never names), so one string serves both engines.
    """

    old_name: str
    signature_columns: tuple[str, ...]
    old_index: str
    index_ddl: str


# The registry and slots tables shipped without the ``cascadeui_``
# prefix. Keyed by current name so the reconciliation loop and the
# migration loop walk the same vocabulary.
LEGACY_TABLE_RENAMES: Final[dict[str, LegacyTableRename]] = {
    TABLE_PERSISTENT_VIEWS: LegacyTableRename(
        old_name="persistent_views",
        signature_columns=("persistence_key", "view_class"),
        old_index="idx_persistent_views_message",
        index_ddl=DDL_PERSISTENT_VIEWS_INDEX,
    ),
    TABLE_APPLICATION_SLOTS: LegacyTableRename(
        old_name="application_slots",
        signature_columns=("slot_name", "payload"),
        old_index="idx_application_slots_expires",
        index_ddl=DDL_APPLICATION_SLOTS_INDEX,
    ),
}
