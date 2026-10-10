"""Top-level install helpers.

:func:`setup_middleware` is the canonical way to register middleware
with the global store. It treats every middleware uniformly: install
into the dispatch chain (guarded against duplicates by class), then
run the middleware's own ``initialize(store)`` method when one is
defined. Middlewares that need async startup (backend initialization,
migrations, blocking rehydrate) own that work themselves -- the
helper simply awaits each in declaration order.

The helper is middleware-agnostic. It knows nothing about persistence,
logging, or undo specifics; each middleware class encapsulates its own
setup. This keeps the install graph linear and the surface uniform.

Usage in ``setup_hook``::

    async def setup_hook(self):
        setup_logging(level="DEBUG")
        await setup_middleware(
            PersistenceMiddleware(backend=SQLiteBackend("data.db"), bot=self),
            UndoMiddleware(),
        )
"""

# // ========================================( Modules )======================================== // #


import functools
import inspect
import logging
from typing import Any, Optional

from .state.singleton import get_store

logger = logging.getLogger(__name__)

# // ========================================( Helper )======================================== // #


def _same_middleware(installed: Any, middleware: Any) -> bool:
    if inspect.isroutine(middleware) or isinstance(middleware, functools.partial):
        return installed == middleware
    return isinstance(installed, type(middleware))


async def setup_middleware(*middlewares: Any, store: Optional[Any] = None) -> None:
    """Install middleware into the store's dispatch chain in order.

    For each middleware, the helper does two things:

    1. **Install, once.** If the store already holds a middleware of
       the same class, the install step is skipped. This keeps repeat
       ``setup_middleware`` calls from double-registering. A plain
       function or method middleware is matched by equality instead (the
       same function, or the same method of the same object), since every
       function shares one class.
    2. **Initialize, always.** If the installed middleware defines an
       ``async initialize(store)`` method, it is awaited. Middlewares
       that need backend init, migrations, or blocking rehydrate run
       that work here. Initialize implementations must be idempotent
       so the always-await policy is safe.

    A repeat call that passes a new instance of a class already installed
    initializes the installed one, and the new instance is not used.
    discord.py cannot log a closed bot in again, so a restart in the same
    process builds a new bot, and its ``setup_hook`` makes exactly that
    call: the installed ``PersistenceMiddleware`` takes the new bot from
    the new instance and reopens the persistence the old bot's close shut.

    Parameters
    ----------
    *middlewares
        Middleware instances in the order they should appear in the
        dispatch chain. Variadic positional so the call site reads
        like a declarative chain definition.
    store
        Optional explicit store. Defaults to the global singleton.

    Notes
    -----
    Idempotency is a contract on each middleware's ``initialize``, not
    on this helper. The helper always awaits initialize so that a
    direct ``store._add_middleware(...)`` call followed by
    ``setup_middleware`` still produces a fully-initialized middleware.
    """
    if store is None:
        store = get_store()

    for middleware in middlewares:
        installed = next((m for m in store._middleware if _same_middleware(m, middleware)), None)
        if installed is None:
            store._add_middleware(middleware)
            installed = middleware
        elif installed is not middleware:
            # Initializing an instance the chain never runs would build a
            # second pipeline beside the installed one.
            logger.debug(
                f"{type(middleware).__name__} is already installed; initializing "
                "the installed instance instead of the new one"
            )
            adopt = getattr(installed, "_adopt", None)
            if adopt is not None:
                adopt(middleware)

        initialize = getattr(installed, "initialize", None)
        if initialize is not None:
            await initialize(store)
