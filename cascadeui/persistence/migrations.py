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
:func:`register_kwargs_migrator` decorators. The library's own
migrators live at the bottom of this module, so importing it
registers them before :meth:`PersistenceManager.apply_migrations`
looks any of them up.
"""

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .protocols import Capability
from .schema import LEGACY_TABLE_RENAMES, TABLE_PERSISTENT_VIEWS

# // ========================================( Modules )======================================== // #


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
# DDL-level migrations (ALTER TABLE, ADD COLUMN, DROP INDEX) go through
# the backend's raw-SQL surface (Capability.RAW_SQL). A backend that
# declares Capability.OPEN_ROWS stores rows as open mappings, so a
# column-level change alters nothing on disk -- skip on that flag, for
# that reason, never on the absence of RAW_SQL (which only says the
# escape hatch is missing, not that skipping is safe):
#
#     if Capability.OPEN_ROWS in backend.capabilities:
#         return  # open rows already read a missing column as None
#     async with backend.transaction():
#         await backend.execute("ALTER TABLE ...")
#
# apply_migrations refuses to run a migrator on a backend declaring
# neither flag, so the DDL branch can assume the raw-SQL surface exists.
#
# Group the statements inside one transaction() block: apply_migrations
# records the new version in a separate call after the migrator returns,
# so a migrator that fails partway leaves the old version recorded and
# will be re-run against a half-migrated schema otherwise.
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


# Old table name -> current name, for the registration guard below. A
# migrator keyed on a pre-rename name would never match a lookup: the
# pending step raises "No migrator registered" naming only the current
# table, with nothing pointing at the stale key as the cause.
_RENAMED_TABLES: dict[str, str] = {
    legacy.old_name: current for current, legacy in LEGACY_TABLE_RENAMES.items()
}


# // ========================================( Registration decorators )======================================== // #


def register_migrator(table: str, from_version: int) -> Callable[[Migrator], Migrator]:
    """Register a library schema migrator for ``table``.

    The migrator runs when on-disk ``schema_version == from_version``
    and the library's current version is higher. Migrators are called
    in sequence (``from_version=N`` then ``from_version=N+1``, etc.)
    until the on-disk version matches current.

    The row API resolves a namespace to its table, so ``row_select`` and
    ``row_upsert`` need nothing extra. Raw SQL names its own table, and the
    logical name is not the physical one under a ``table_prefix``: resolve
    it through :func:`physical_table` first, or the statement runs against
    the unprefixed table, which on a prefixed deployment is either absent
    or a consumer's own.

    The pre-rename table names (``persistent_views``,
    ``application_slots``) are refused with a ``ValueError`` naming the
    current name; a migrator keyed on one would never be looked up.

    Example::

        @register_migrator("cascadeui_persistent_views", 1)
        async def _migrate_persistent_views_1_to_2(backend):
            table = physical_table(backend, "cascadeui_persistent_views")
            await backend.execute(f"ALTER TABLE {table} ADD COLUMN ...")
    """

    def decorator(fn: Migrator) -> Migrator:
        current = _RENAMED_TABLES.get(table)
        if current is not None:
            raise ValueError(
                f"Table {table!r} was renamed to {current!r}; key the migrator "
                f"on the current name -- one keyed on {table!r} is never looked up."
            )
        key = (table, from_version)
        if key in _MIGRATORS:
            raise ValueError(f"Migrator already registered for {table} v{from_version}")
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
                f"Kwargs migrator already registered for " f"{view_class_qualname} v{from_version}"
            )
        _KWARGS_MIGRATORS[key] = fn
        return fn

    return decorator


# // ========================================( Lookup helpers )======================================== // #


def get_schema_migrator(table: str, from_version: int) -> Migrator | None:
    """Return the registered schema migrator for a table+version, or
    ``None`` if no migrator handles that step."""
    return _MIGRATORS.get((table, from_version))


def get_kwargs_migrator(view_class_qualname: str, from_version: int) -> KwargsMigrator | None:
    """Return the registered kwargs migrator for a view class+version,
    or ``None`` if no migrator handles that step."""
    return _KWARGS_MIGRATORS.get((view_class_qualname, from_version))


# // ========================================( Library migrators )======================================== // #


def physical_table(backend: Any, table: str) -> str:
    """Resolve a logical table name to the one this backend actually reads.

    Migrators are keyed and versioned by the logical name, and issue their
    SQL through the raw surface, which does no prefixing of its own. A
    backend configured with ``table_prefix`` therefore has to be asked, or
    the statement runs against the unprefixed name: absent on that database,
    or worse, a consumer's own table of the same name, which is exactly what
    the prefix was set to stay away from.
    """
    return f"{getattr(backend, 'table_prefix', '')}{table}"


@register_migrator(TABLE_PERSISTENT_VIEWS, 1)
async def _persistent_views_1_to_2(backend: Any) -> None:
    """Add ``first_unreachable_at`` to the registry table.

    A row the reattach pass cannot fetch is kept rather than pruned,
    because a permission blip at boot must not delete a live panel. The
    stamp is what lets an operator tell a blip from a channel the bot
    will never see again, so the column is nullable with no default:
    ``NULL`` means reachable, or never yet observed otherwise.

    Open-row backends need no DDL: a key that was never written already
    reads as ``NULL``, which is exactly what the nullable column means.
    """
    # apply_migrations rejects a backend declaring neither OPEN_ROWS nor
    # RAW_SQL before this runs, so falling through the skip means the
    # raw-SQL surface exists.
    if Capability.OPEN_ROWS in backend.capabilities:
        return

    # apply_migrations records the new version only after this returns, so a
    # crash in that window re-runs the migrator against a table that already
    # has the column -- and SQLite has no ADD COLUMN IF NOT EXISTS. Probing
    # first is what makes the re-run a no-op instead of a duplicate-column
    # error. The except is broad because the driver raises its own vendor
    # type unwrapped; anything that is not a missing column resurfaces on the
    # ALTER below with its real message intact.
    table = physical_table(backend, TABLE_PERSISTENT_VIEWS)
    try:
        await backend.fetch(f"SELECT first_unreachable_at FROM {table} LIMIT 1")
        return
    except Exception:
        pass

    # PostgreSQL supports IF NOT EXISTS on ADD COLUMN and SQLite does not.
    # Using it where it exists closes the two-process boot race: both can pass
    # the probe above, and without it the process that loses the ALTER fails
    # its whole startup over a column the winner just added. SQLite keeps the
    # bare form, where the probe is the only guard available and a single-file
    # database makes the race far less reachable.
    guard = "" if backend.placeholder_style == "qmark" else "IF NOT EXISTS "
    await backend.execute(f"ALTER TABLE {table} ADD COLUMN {guard}first_unreachable_at BIGINT")
