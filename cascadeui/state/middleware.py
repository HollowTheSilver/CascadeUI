
# // ========================================( Modules )======================================== // #


import asyncio
from typing import Optional, Set, Callable

from .types import Action, StateData
from ..utils.logging import AsyncLogger

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")


# // ========================================( Middleware )======================================== // #


class DebouncedPersistence:
    """Middleware that batches state writes to disk.

    Instead of writing on every dispatch, this buffers changes and flushes
    at most once per `interval` seconds. Lifecycle actions (VIEW_DESTROYED
    by default) trigger an immediate flush to prevent data loss.

    Usage:
        from cascadeui import get_store
        from cascadeui.state.middleware import DebouncedPersistence

        store = get_store()
        persistence = DebouncedPersistence(store, interval=2.0)
        store.add_middleware(persistence)
    """

    def __init__(self, store, interval: float = 2.0,
                 flush_actions: Optional[Set[str]] = None):
        self._store = store
        self._interval = interval
        self._flush_actions = flush_actions or {"VIEW_DESTROYED"}
        self._dirty = False
        self._timer: Optional[asyncio.TimerHandle] = None
        self._write_lock = asyncio.Lock()

    async def __call__(self, action: Action, state: StateData,
                       next_fn: Callable) -> StateData:
        """Process the action through the chain, then handle persistence."""
        result = await next_fn(action, state)

        # Only persist if persistence is configured
        if not self._store.persistence_enabled or not self._store.persistence_backend:
            return result

        if action["type"] in self._flush_actions:
            # Lifecycle event: flush immediately
            self._cancel_timer()
            await self._flush()
        else:
            # Normal action: mark dirty and start/reset debounce timer
            self._dirty = True
            self._reset_timer()

        return result

    def _reset_timer(self):
        """Reset the debounce timer."""
        self._cancel_timer()
        try:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(
                self._interval,
                lambda: asyncio.ensure_future(self._flush())
            )
        except RuntimeError:
            pass

    def _cancel_timer(self):
        """Cancel the pending debounce timer if active."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def _flush(self):
        """Write current state to disk if dirty."""
        if not self._dirty:
            return

        async with self._write_lock:
            if not self._dirty:
                return
            self._dirty = False

            try:
                await self._store.persistence_backend.save_state(self._store.state)
                logger.debug("Debounced persistence: state flushed to disk")
            except Exception as e:
                logger.error(f"Debounced persistence: flush failed: {e}")
                # Re-mark dirty so the next timer retry catches it
                self._dirty = True

    async def flush_now(self):
        """Force an immediate flush. Useful for shutdown hooks."""
        self._cancel_timer()
        self._dirty = True
        await self._flush()


def logging_middleware():
    """Middleware that logs every dispatched action at INFO level.

    Usage:
        store.add_middleware(logging_middleware())
    """
    action_logger = AsyncLogger(name="cascadeui.actions", level="INFO", path="logs", mode="a")

    async def middleware(action: Action, state: StateData,
                        next_fn: Callable) -> StateData:
        action_logger.info(
            f"[{action['type']}] source={action.get('source', 'N/A')} "
            f"payload_keys={list(action.get('payload', {}).keys())}"
        )
        return await next_fn(action, state)

    return middleware
