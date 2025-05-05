
# // ========================================( Modules )======================================== // #


import functools
import asyncio
import traceback
from typing import Callable, Any, Coroutine, TypeVar, cast, Optional, Type, Tuple, Union

from .logging import AsyncLogger
logger = AsyncLogger(name="cascadeui.errors", level="DEBUG", path="logs", mode="a")

T = TypeVar('T')


# // ========================================( Classes )======================================== // #


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(self,
                 max_retries: int = 3,
                 backoff_factor: float = 1.0,
                 exceptions_to_retry: Tuple[Type[Exception], ...] = (Exception,),
                 max_backoff: float = 30.0):
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.exceptions_to_retry = exceptions_to_retry
        self.max_backoff = max_backoff


def with_error_boundary(name: str = None):
    """Decorator to create an error boundary around a function."""

    def decorator(func: Callable[..., Coroutine[Any, Any, T]]):
        func_name = name or func.__name__

        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {func_name}: {str(e)}", exc_info=True)

                # For better debugging, include trace
                trace = traceback.format_exc()
                logger.debug(f"Traceback for {func_name}:\n{trace}")

                # Re-raise to allow caller to handle
                raise

        return wrapper

    return decorator


def with_retry(config: Optional[RetryConfig] = None):
    """Decorator to retry a function on failure."""
    retry_config = config or RetryConfig()

    def decorator(func: Callable[..., Coroutine[Any, Any, T]]):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(retry_config.max_retries):
                try:
                    return await func(*args, **kwargs)
                except retry_config.exceptions_to_retry as e:
                    last_exception = e

                    # Calculate backoff time with jitter and max cap
                    base_wait = retry_config.backoff_factor * (2 ** attempt)
                    jitter = 0.1 * base_wait * (asyncio.get_event_loop().time() % 1.0)
                    wait_time = min(base_wait + jitter, retry_config.max_backoff)

                    logger.warning(
                        f"Attempt {attempt + 1}/{retry_config.max_retries} "
                        f"failed for {func.__name__}: {e}. "
                        f"Retrying in {wait_time:.2f}s."
                    )
                    await asyncio.sleep(wait_time)

            # If we get here, all retries failed
            logger.error(
                f"All {retry_config.max_retries} attempts failed "
                f"for {func.__name__}: {last_exception}"
            )
            if last_exception:
                raise last_exception

            # This should never happen, but to satisfy type checker
            raise RuntimeError("Unexpected error in retry logic")

        return wrapper

    return decorator


class ErrorBoundary:
    """Context manager for error boundaries."""

    def __init__(self, name: str, log_level: str = "ERROR"):
        self.name = name
        # Convert string log level to integer
        self.log_level = self._get_log_level(log_level)
        self.logger = AsyncLogger(name="cascadeui.errors", level=log_level, path="logs", mode="a")

    @staticmethod
    def _get_log_level(level_name: str) -> int:
        """Convert string log level to integer constant."""
        import logging
        level_map = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "CRITICAL": logging.CRITICAL
        }
        return level_map.get(level_name.upper(), logging.ERROR)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            self.logger.log(
                self.log_level,
                f"Error in {self.name}: {exc_val}",
                exc_info=True  # Simply use True instead of the tuple
            )
            # Return False to propagate the exception
            return False
        return True


async def safe_execute(coro: Coroutine, fallback: Any = None, log_error: bool = True) -> Any:
    """
    Safely execute a coroutine and return fallback value on error.

    Args:
        coro: Coroutine to execute
        fallback: Value to return if execution fails
        log_error: Whether to log the error

    Returns:
        Result of coroutine or fallback value
    """
    try:
        return await coro
    except Exception as e:
        if log_error:
            logger = AsyncLogger(name="cascadeui.errors", level="ERROR", path="logs", mode="a")
            logger.error(f"Error in safe_execute: {e}", exc_info=True)
        return fallback
