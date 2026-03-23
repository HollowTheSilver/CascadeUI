# // ========================================( Modules )======================================== // #


import logging

from .migration import migrate_storage
from .serialization import StateSerializer
from .storage import FileStorageBackend, StorageBackend

__all__ = [
    "StateSerializer",
    "StorageBackend",
    "FileStorageBackend",
    "migrate_storage",
]

_logger = logging.getLogger(__name__)

# Optional backends — imported lazily to avoid hard dependency on aiosqlite/redis
try:
    from .sqlite import SQLiteBackend

    __all__.append("SQLiteBackend")
except ImportError as _e:
    if "aiosqlite" not in str(_e):
        _logger.warning(f"Failed to import SQLiteBackend: {_e}")

try:
    from .redis import RedisBackend

    __all__.append("RedisBackend")
except ImportError as _e:
    if "redis" not in str(_e):
        _logger.warning(f"Failed to import RedisBackend: {_e}")
