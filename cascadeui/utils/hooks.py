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

    A ``staticmethod`` or ``classmethod`` wrapping an ``async def`` is
    unwrapped first. Read out of a class body before the descriptor
    protocol runs, the wrapper answers False to both checks while calling
    it still produces a coroutine, and supplying a hook that way is
    ordinary rather than exotic.

    An async generator function is reported too. It is neither a coroutine
    function nor awaitable, so both checks above miss it, and calling one
    returns a truthy object that a refusing seam would consume as an
    answer: a stray ``yield`` in an ``async def`` renders its own repr.
    """
    fn = getattr(fn, "__func__", fn)
    if inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn):
        return True
    call = getattr(fn, "__call__", None)
    return call is not None and (
        inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)
    )


def accepts_second_positional(fn: Any) -> bool:
    """Report whether a control should hand its value to ``fn`` as a second argument.

    Two questions, and both have to answer yes. The callable has to DECLARE
    a second positional parameter, which is how a caller opts in to
    receiving the value, and it has to be callable WITH two arguments,
    which is whether that opt-in can be honored.

    The two can disagree, and the disagreement is not exotic: a
    ``functools.wraps`` adapter advertises the signature of the function it
    wraps, so one that narrows two parameters down to one declares a second
    and cannot take it. Electing the two-argument call on the declaration
    alone fails on the first click, from inside the library, naming the
    wrapped function rather than the wrapper that could not take the
    argument.

    Keyword-only and variadic parameters do not count toward the
    declaration, since neither is a second positional parameter a caller
    can name. A signature that cannot be read reports False, so an
    unintrospectable callable keeps the one-argument contract rather than
    being handed an argument it may not accept.
    """
    if fn is None:
        return False
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    positional = [
        p for p in sig.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) < 2:
        return False
    # The declaration above is the advertisement; this is whether the
    # callable that actually runs can honor it.
    return can_accept_positional(fn, 2) is not False


def can_accept_positional(fn: Any, count: int) -> Optional[bool]:
    """Report whether ``fn`` can be called with ``count`` positional arguments.

    ``None`` means the signature could not be read, and every caller
    treats that as permission: refusing what cannot be inspected would
    reject builtins and some mock objects, which is a worse trade than
    letting a genuinely wrong signature fail at its call site.

    Binding the argument list is the real question, where a parameter
    count is only a proxy for it. Binding refuses a callable whose extra
    parameter is required and accepts one whose extra parameter has a
    default, it accounts for ``*args``, and it counts the bound ``self``
    of a method correctly while catching the unbound function pulled off a
    class body, which is the same mistake wearing a different arity.

    ``follow_wrapped`` is off because the callable that runs is the one
    that must accept the arguments. A ``functools.wraps`` adapter
    advertises the signature of the function it wraps, and a refusal that
    trusts the advertisement checks something other than what is called.
    """
    if fn is None:
        return None
    try:
        sig = inspect.signature(fn, follow_wrapped=False)
    except (ValueError, TypeError):
        return None
    try:
        sig.bind(*([None] * count))
    except TypeError:
        return False
    return True


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
