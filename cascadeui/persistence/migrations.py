"""Schema migration registries and runner for the persistence layer.

Two registries cover the two migration surfaces:

- :data:`_MIGRATORS` -- library-owned schema migrators keyed by
  ``(table_name, from_version)``. Run automatically during
  :meth:`PersistenceMiddleware.initialize`.
- :data:`_KWARGS_MIGRATORS` -- user-defined migrators for
  PersistentView ``init_kwargs`` blobs, keyed by
  ``(view_class_qualname, from_version)``. Run lazily during
  registry rehydrate.

Migrators register via :func:`register_migrator` and
:func:`register_kwargs_migrator` decorators. The library ships
zero migrators today -- the infrastructure is in place so future
schema changes have a clear landing spot without another breaking
release.
"""

# // ========================================( Modules )======================================== // #


from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .protocols import PersistenceBackend


# // ========================================( Type aliases )======================================== // #


# A schema migrator rewrites the on-disk shape of one table from
# version N to version N+1. Runs against the backend directly so it
# can move data or rewrite rows in bulk via the backend row API.
#
# Data-level migrations (rewriting rows through row_select + row_upsert)
# are supported today on every backend that implements Capability.RELATIONAL.
#
# DDL-level migrations (ALTER TABLE, ADD COLUMN, DROP INDEX) are NOT
# supported through this Protocol yet -- the backend surface is
# deliberately backend-agnostic and does not expose raw SQL. The first
# library migration that needs DDL will introduce a Capability.RAW_SQL
# flag plus an optional execute(sql) method; backends can opt in without
# breaking KV-only implementations. Until then, schema evolution happens
# by shipping new DDL in schema.py for fresh installs and moving data
# via the row API for existing installs.
Migrator = Callable[["PersistenceBackend"], Awaitable[None]]


# A kwargs migrator rewrites a single PersistentView's stored
# init_kwargs dict from version N to version N+1. Pure function of
# the kwargs payload -- no backend access needed.
KwargsMigrator = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


# // ========================================( Registries )======================================== // #


# Library-owned schema migrators. Key: (table_name, from_version).
# Value: async callable that takes a backend and upgrades the table
# from from_version to from_version + 1.
_MIGRATORS: dict[tuple[str, int], Migrator] = {}


# User-defined init_kwargs migrators. Key: (view_class_qualname,
# from_version). Value: async callable that takes the stored kwargs
# dict and returns the upgraded dict.
_KWARGS_MIGRATORS: dict[tuple[str, int], KwargsMigrator] = {}


# // ========================================( Registration decorators )======================================== // #


def register_migrator(
    table: str, from_version: int
) -> Callable[[Migrator], Migrator]:
    """Register a library schema migrator for ``table``.

    The migrator runs when on-disk ``schema_version == from_version``
    and the library's current version is higher. Migrators are called
    in sequence (``from_version=N`` then ``from_version=N+1``, etc.)
    until the on-disk version matches current.

    Example::

        @register_migrator("persistent_views", 1)
        async def _migrate_persistent_views_1_to_2(backend):
            # ALTER TABLE, rewrite rows, etc.
            ...
    """

    def decorator(fn: Migrator) -> Migrator:
        key = (table, from_version)
        if key in _MIGRATORS:
            raise ValueError(
                f"Migrator already registered for {table} v{from_version}"
            )
        _MIGRATORS[key] = fn
        return fn

    return decorator


def register_kwargs_migrator(
    view_class_qualname: str, from_version: int
) -> Callable[[KwargsMigrator], KwargsMigrator]:
    """Register a user migrator for a PersistentView's ``init_kwargs``.

    When a restored row's ``kwargs_schema_version`` is less than the
    current version AND a matching migrator is registered, the library
    runs the migrator during rehydrate before calling the view's
    ``__init__``. Missing migrator: the row is logged at WARNING and
    skipped for re-attachment (leaving the row on disk for later
    recovery).

    ``view_class_qualname`` must match the stored ``view_class`` column
    exactly -- typically ``f"{module}.{cls.__qualname__}"``.

    Example::

        @register_kwargs_migrator("mybot.views.TicketPanel", from_version=1)
        async def migrate_ticket_panel_1_to_2(kwargs):
            # Rename a kwarg that was renamed in __init__
            kwargs["channel_id"] = kwargs.pop("target_channel_id")
            return kwargs
    """

    def decorator(fn: KwargsMigrator) -> KwargsMigrator:
        key = (view_class_qualname, from_version)
        if key in _KWARGS_MIGRATORS:
            raise ValueError(
                f"Kwargs migrator already registered for "
                f"{view_class_qualname} v{from_version}"
            )
        _KWARGS_MIGRATORS[key] = fn
        return fn

    return decorator


# // ========================================( Lookup helpers )======================================== // #


def get_schema_migrator(table: str, from_version: int) -> Migrator | None:
    """Return the registered schema migrator for a table+version, or
    ``None`` if no migrator handles that step."""
    return _MIGRATORS.get((table, from_version))


def get_kwargs_migrator(
    view_class_qualname: str, from_version: int
) -> KwargsMigrator | None:
    """Return the registered kwargs migrator for a view class+version,
    or ``None`` if no migrator handles that step."""
    return _KWARGS_MIGRATORS.get((view_class_qualname, from_version))
