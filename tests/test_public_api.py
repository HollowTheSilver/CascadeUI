"""Guards on the package's public import surface.

Every name a subpackage declares public is reachable from the package root,
which is the import path the documentation teaches. Nothing here exercises
behavior: a name absent from ``cascadeui.__all__`` fails these tests even
though the underlying object still imports from its own subpackage.
"""

# // ========================================( Modules )======================================== // #


import importlib

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
