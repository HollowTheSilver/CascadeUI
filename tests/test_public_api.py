"""Guards on the package's public import surface.

Every name a subpackage declares public is reachable from the package root,
which is the import path the documentation teaches. Nothing here exercises
behavior: a name absent from ``cascadeui.__all__`` fails these tests even
though the underlying object still imports from its own subpackage.
"""

# // ========================================( Modules )======================================== // #


import ast
import importlib
import os
import pkgutil

import pytest

import cascadeui

# // ========================================( Constants )======================================== // #


# Subpackages whose ``__all__`` the root re-exports in full.
SUBPACKAGES = [
    "cascadeui.components",
    "cascadeui.persistence",
    "cascadeui.state",
    "cascadeui.theming",
    "cascadeui.utils",
    "cascadeui.views",
]


# // ========================================( Class )======================================== // #


class TestPublicExportSurface:
    """The root re-exports every subpackage's public names."""

    def test_root_all_has_no_dangling_names(self):
        missing = [name for name in cascadeui.__all__ if not hasattr(cascadeui, name)]
        assert missing == [], f"__all__ names the root does not define: {missing}"

    @pytest.mark.parametrize("module_name", SUBPACKAGES)
    def test_subpackage_all_has_no_dangling_names(self, module_name):
        module = importlib.import_module(module_name)
        missing = [name for name in module.__all__ if not hasattr(module, name)]
        assert missing == [], f"{module_name}.__all__ names it does not define: {missing}"

    @pytest.mark.parametrize("module_name", SUBPACKAGES)
    def test_subpackage_public_names_reach_the_root(self, module_name):
        # A name in a subpackage's __all__ is public, and the documented import
        # path for a public name is `from cascadeui import X`. A subpackage
        # whose names stop short of the root is reachable only by an import
        # path nothing teaches.
        module = importlib.import_module(module_name)
        unreachable = sorted(set(module.__all__) - set(cascadeui.__all__))
        assert unreachable == [], (
            f"{module_name}.__all__ declares these public, but they are absent "
            f"from cascadeui.__all__: {unreachable}"
        )

    def test_root_all_is_free_of_duplicates(self):
        seen = sorted({n for n in cascadeui.__all__ if cascadeui.__all__.count(n) > 1})
        assert seen == [], f"duplicated in __all__: {seen}"


# // ========================================( Class )======================================== // #


class TestSnowflakeHelpersAreImportable:
    """The coercion family imports from the root alongside every other utility."""

    def test_import_from_root(self):
        from cascadeui import coerce_snowflake_id, coerce_snowflake_id_set, is_snowflake

        assert is_snowflake(1239295935075582032) is True
        assert is_snowflake(42) is False
        assert coerce_snowflake_id(42) == 42
        assert coerce_snowflake_id_set([1, 2]) == {1, 2}

    def test_import_from_utils_still_works(self):
        # Subpackage-level imports stay supported alongside the root re-export.
        from cascadeui import is_snowflake as root_is_snowflake
        from cascadeui.utils import is_snowflake as utils_is_snowflake

        assert utils_is_snowflake is root_is_snowflake


# // ========================================( Class )======================================== // #


# Modules whose entire public surface is internal machinery. A name added to one
# of these is still internal (a reducer, a DDL string), so the module is skipped
# wholesale rather than listing every name it will ever hold.
_INTERNAL_MODULES = frozenset(
    {
        "cascadeui.state.reducers",  # reduce_* -- registered, never called by users
        "cascadeui.persistence.schema",  # SQL DDL string constants
        "cascadeui.persistence.schema_postgres",  # PostgreSQL DDL string constants
    }
)

# Public-looking names deliberately kept OUT of the root API. Every name a
# library module defines must be either in ``cascadeui.__all__`` or here; a new
# name that is neither fails ``test_no_accidental_internal_public`` and forces a
# deliberate export-or-internal decision at the point the name is added.
_INTERNAL_NAMES = frozenset(
    {
        "logger",  # the module-level logging.getLogger(__name__) idiom
        # Internal typing surface (cascadeui/state/types.py + siblings)
        "ComponentId",
        "GuildId",
        "SessionId",
        "UserId",
        "ViewId",
        "Timestamp",
        "HookFn",
        "MiddlewareFn",
        "ReducerFn",
        "SelectorFn",
        "SubscriberFn",
        "T",
        "ActionPayload",
        "AxisLabels",
        "CellKey",
        # DevTools @computed registrations (read via store.computed[...])
        "application_keys",
        "state_size_bytes",
        "total_sessions",
        "total_views",
        # Persistence internals (register_* helpers ARE exported; these are not)
        "NAMESPACE_APPLICATION",
        "NAMESPACE_REGISTRY",
        "KwargsMigrator",
        "Migrator",
        "get_kwargs_migrator",
        "get_schema_migrator",
        "is_persistent_slot",
        # Postgres backend LISTEN/NOTIFY channel name; only surfaces when asyncpg is installed
        "CHANNEL_INVALIDATION",
        "BatchContext",
        # Theming context managers (get_current_theme is exported; these are not)
        "set_current_theme",
        "theme_context",
        # Logging internals (setup_logging + ColorScheme/FormatTemplate/JSONFormatter export)
        "COLOR_SCHEMES",
        "FORMAT_TEMPLATES",
        "ColoredStreamFormatter",
        "FileFormatter",
        # Misc internal
        "is_viewstore_trace_enabled",
        "TaskManager",
        "MAX_TEXT_FIELDS",
    }
)


def _defined_public_names(source: str) -> set:
    """Top-level names a module DEFINES (not imports), minus underscore-prefixed.

    Uses the AST rather than ``dir(module)`` so module-level constants and type
    aliases (which carry no ``__module__``) are captured alongside classes and
    functions, and re-exported / imported names are excluded.
    """
    tree = ast.parse(source)
    imported: set = set()
    defined: set = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    return {n for n in defined if not n.startswith("_") and n not in imported}


class TestNoAccidentalInternalPublic:
    """Every public name a module defines is exported or explicitly internal.

    Closes the residual gap the ``__all__``-subset tests cannot see: a public
    name that reaches NO ``__all__`` list. That is the shape that shipped an
    unreachable-exports defect. A new public class, function, or constant
    must be added to ``cascadeui.__all__`` (public) or ``_INTERNAL_NAMES``
    (internal), or this test fails at the point the name is introduced.
    """

    def test_no_accidental_internal_public(self):
        offenders: dict = {}
        for info in pkgutil.walk_packages(cascadeui.__path__, cascadeui.__name__ + "."):
            name = info.name
            if name in _INTERNAL_MODULES:
                continue
            if name.rsplit(".", 1)[-1].startswith("_"):
                continue  # underscore module -- internal by path convention
            try:
                module = importlib.import_module(name)
            except Exception:
                continue
            path = getattr(module, "__file__", None)
            if not path or not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                public = _defined_public_names(handle.read())
            leaked = sorted(public - set(cascadeui.__all__) - _INTERNAL_NAMES)
            if leaked:
                offenders[name] = leaked
        assert not offenders, (
            "Public names that reach neither cascadeui.__all__ nor _INTERNAL_NAMES "
            "-- export each from the package root or add it to _INTERNAL_NAMES: "
            f"{offenders}"
        )
