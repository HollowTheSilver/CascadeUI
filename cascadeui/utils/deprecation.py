"""Deprecation warnings attributed to the code that uses the deprecated name.

Every deprecation in the library warns through :func:`warn_deprecated`, so
each one reads the same way and points at the same place: the line of the
caller's own code that reached the library, which is what Python's default
filters decide by. A deprecated name keeps working until the release
:data:`REMOVED_IN` names.
"""

# // ========================================( Modules )======================================== // #


import sys
import warnings

# // ========================================( Constants )======================================== // #


# A deprecated name is removed in the next major release.
REMOVED_IN = "4.0.0"

# // ========================================( Functions )======================================== // #


def _in_library(module_name: str) -> bool:
    # Exact, so a user package named ``cascadeui_panels`` is not skipped.
    return module_name == "cascadeui" or module_name.startswith("cascadeui.")


def _stacklevel_outside_library() -> int:
    """The ``warnings`` stacklevel, for the caller, of the nearest frame outside cascadeui."""
    frame = sys._getframe(2)
    level = 2
    while frame is not None and _in_library(frame.f_globals.get("__name__", "")):
        frame = frame.f_back
        level += 1
    return level


def warn_deprecated(message: str) -> None:
    """Issue a ``DeprecationWarning`` attributed to the first frame outside cascadeui.

    ``message`` names the deprecated thing, what replaces it, and
    :data:`REMOVED_IN`. Python shows a ``DeprecationWarning`` by default
    only for code run as ``__main__`` and under test runners, so a bot sees
    it with ``python -W default::DeprecationWarning`` or in its test suite.
    """
    warnings.warn(message, DeprecationWarning, stacklevel=_stacklevel_outside_library())
