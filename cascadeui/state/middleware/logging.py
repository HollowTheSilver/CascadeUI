# // ========================================( Modules )======================================== // #


import logging
from typing import Callable

from ..types import Action, StateData

# // ========================================( Middleware )======================================== // #


class LoggingMiddleware:
    """Middleware that logs every dispatched action.

    Each action is logged at the configured ``level`` (default ``INFO``). The
    level sets the *emission* level of the action stream, not a threshold: set
    ``level="DEBUG"`` to keep routine action traffic out of INFO-level logs (it
    surfaces only when DEBUG logging is enabled), or leave it at ``INFO`` to
    include the stream in standard output. Visibility is governed by the
    handlers on the ``cascadeui.actions`` logger and its ``cascadeui`` parent,
    the same as every other library logger -- this middleware does not pin the
    logger's threshold.

    Usage:
        from cascadeui import setup_middleware
        from cascadeui.state.middleware import LoggingMiddleware

        await setup_middleware(LoggingMiddleware())
        await setup_middleware(LoggingMiddleware(level="DEBUG"))
    """

    def __init__(self, level: str = "INFO"):
        self._logger = logging.getLogger("cascadeui.actions")
        self._level = getattr(logging, level.upper(), logging.INFO)

    async def __call__(self, action: Action, state: StateData, next_fn: Callable) -> StateData:
        self._logger.log(
            self._level,
            f"[{action['type']}] source={action.get('source', 'N/A')} "
            f"payload_keys={list(action.get('payload', {}).keys())}",
        )
        return await next_fn(action, state)
