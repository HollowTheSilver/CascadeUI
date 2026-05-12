# // ========================================( Modules )======================================== // #


import logging

from .memory import InMemoryBackend

__all__ = ["InMemoryBackend"]

_logger = logging.getLogger(__name__)

# Optional backend -- imported lazily to avoid a hard dependency on aiosqlite
try:
    from .sqlite import SQLiteBackend

    __all__.append("SQLiteBackend")
except ImportError as _e:
    if "aiosqlite" not in str(_e):
        _logger.warning(f"Failed to import SQLiteBackend: {_e}")

# Optional backend -- imported lazily to avoid a hard dependency on asyncpg
try:
    from .postgres import PostgresBackend

    __all__.append("PostgresBackend")
except ImportError as _e:
    if "asyncpg" not in str(_e):
        _logger.warning(f"Failed to import PostgresBackend: {_e}")
