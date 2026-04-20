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


from typing import Any, Optional

from .state.singleton import get_store

# // ========================================( Helper )======================================== // #


async def setup_middleware(*middlewares: Any, store: Optional[Any] = None) -> None:
    """Install middleware into the store's dispatch chain in order.

    For each middleware, the helper does two things:

    1. **Install, once.** If the store already holds a middleware of
       the same class, the install step is skipped. This keeps repeat
       ``setup_middleware`` calls from double-registering.
    2. **Initialize, always.** If the middleware defines an
       ``async initialize(store)`` method, it is awaited. Middlewares
       that need backend init, migrations, or blocking rehydrate run
       that work here. Initialize implementations must be idempotent
       so the always-await policy is safe.

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
        if not store.has_middleware(type(middleware)):
            store._add_middleware(middleware)

        initialize = getattr(middleware, "initialize", None)
        if initialize is not None:
            await initialize(store)
