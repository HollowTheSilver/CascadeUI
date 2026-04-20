# // ========================================( Modules )======================================== // #


import logging
from typing import Callable

from ..types import Action, StateData

# // ========================================( Middleware )======================================== // #


class LoggingMiddleware:
    """Middleware that logs every dispatched action.

    Configurable log level (default ``INFO``) lets verbose deployments dial
    down to ``DEBUG`` for full action tracing or up to ``WARNING`` to suppress
    routine traffic without removing the middleware entirely.

    Usage:
        from cascadeui import setup_middleware
        from cascadeui.state.middleware import LoggingMiddleware

        await setup_middleware(LoggingMiddleware())
        await setup_middleware(LoggingMiddleware(level="DEBUG"))
    """

    def __init__(self, level: str = "INFO"):
        self._logger = logging.getLogger("cascadeui.actions")
        self._logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    async def __call__(self, action: Action, state: StateData, next_fn: Callable) -> StateData:
        self._logger.info(
            f"[{action['type']}] source={action.get('source', 'N/A')} "
            f"payload_keys={list(action.get('payload', {}).keys())}"
        )
        return await next_fn(action, state)
