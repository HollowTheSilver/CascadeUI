# // ========================================( Modules )======================================== // #


import inspect
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# // ========================================( Functions )======================================== // #


async def await_maybe(result: Any) -> Any:
    """Resolve a call result that may or may not be awaitable.

    User-supplied callables arrive in both shapes. A hook documented as
    ``Callable`` is as likely to be written ``def`` as ``async def``, and
    awaiting the return of a synchronous one raises ``TypeError: 'list'
    object can't be awaited`` from inside library code, naming neither the
    hook nor the requirement.

    Takes the result rather than the callable, because the check has to
    happen after the call. ``inspect.iscoroutinefunction`` answers only
    whether the object is itself a coroutine function, so it is blind to an
    instance whose ``__call__`` is async, to a ``functools.partial`` around
    one, and to any plain function that returns a coroutine. It does see a
    ``partial`` wrapping an ``async def``, on every supported interpreter.
    All of them return an awaitable, which is what this resolves.
    """
    if inspect.isawaitable(result):
        return await result
    return result


def is_async_callable(fn: Any) -> bool:
    """Report whether calling ``fn`` produces a coroutine.

    The counterpart to :func:`await_maybe`, for the seams that must
    *refuse* an async callable rather than resolve one: a check that runs
    inline and cannot await has no way to use the answer, so it has to
    say so where the callable is supplied.

    ``inspect.iscoroutinefunction`` alone answers only whether the object
    is itself a coroutine function, so it is blind to an instance whose
    ``__call__`` is async. It does see a ``functools.partial`` around an
    ``async def``, on every supported interpreter. Both shapes are
    checked here so a caller cannot pass the one the narrow check misses.
    """
    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(fn, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


async def call_hook_safe(
    hook, *args, owner: str = "", log: Optional[logging.Logger] = None
) -> None:
    """Run a fire-and-forget user hook, logging any exception.

    Post-event hooks fire after the state they report has already changed
    but before the render that shows it. An override that raises must not
    take the render down with it, or the cursor advances while the display
    stays put: the page index moves and the page never turns.

    Reached from two layers: the view patterns call it through
    ``_StatefulMixin._call_hook_safe``, and the V2 composites call it
    directly, since ``components -> views`` is an import the package does
    not make.

    Args:
        hook: The bound hook to run.
        *args: Positional arguments for the hook.
        owner: Class name for the log line, so a raising override names the
            surface it came from.
        log: Logger to report through, so failures stay filterable by
            subsystem rather than surfacing under ``cascadeui.utils``.
    """
    try:
        await await_maybe(hook(*args))
    except Exception as exc:
        name = getattr(hook, "__name__", repr(hook))
        where = f" in {owner}" if owner else ""
        (log or logger).warning(f"{name} raised{where}: {exc}")
