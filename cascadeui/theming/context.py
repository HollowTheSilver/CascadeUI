# // ========================================( Modules )======================================== // #


import contextlib
import contextvars
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .core import Theme

# // ========================================( Theme Context )======================================== // #


_current_theme: contextvars.ContextVar[Optional["Theme"]] = contextvars.ContextVar(
    "cascadeui_theme", default=None
)


def get_current_theme() -> Optional["Theme"]:
    """Return the theme active in the current execution context.

    Inside a ``build_ui()`` method, this returns the view's theme
    (set automatically by the ``__init_subclass__`` wrapper). Outside
    a view context, returns ``None``.

    Builder functions like ``card()`` and ``stats_card()`` call this
    as a fallback when no explicit ``color=`` argument is passed.
    """
    return _current_theme.get()


def set_current_theme(theme: Optional["Theme"]) -> contextvars.Token:
    """Set the active theme for the current execution context.

    Returns a token for resetting the context variable to its
    previous value via ``_current_theme.reset(token)``.
    """
    return _current_theme.set(theme)


@contextlib.contextmanager
def theme_context(theme: Optional["Theme"]):
    """Establish ``theme`` as the ambient theme for the enclosed block.

    The single seam every library render path uses to make builder
    fallbacks (``card()``, ``stats_card()``, ``divider()``) resolve the
    view's theme: the ``build_ui`` / ``on_load`` wrappers and the
    pattern-internal rebuild seams all enter this context before
    invoking builder code. Restores the previous ambient theme on exit,
    exception or not.
    """
    token = _current_theme.set(theme)
    try:
        yield theme
    finally:
        _current_theme.reset(token)
