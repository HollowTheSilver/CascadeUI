# // ========================================( Modules )======================================== // #


from .backends import InMemoryBackend
from .backends import __all__ as _backends_all
from .config import (
    ApplicationPersistence,
    RegistryPersistence,
    SlotPolicy,
)
from .manager import PersistenceManager
from .migrations import register_kwargs_migrator, register_migrator
from .protocols import Capability, PersistenceBackend

__all__ = [
    "InMemoryBackend",
    "Capability",
    "PersistenceBackend",
    "PersistenceManager",
    "RegistryPersistence",
    "ApplicationPersistence",
    "SlotPolicy",
    "register_kwargs_migrator",
    "register_migrator",
]

# Re-export optional backends that were successfully imported
if "SQLiteBackend" in _backends_all:
    from .backends import SQLiteBackend  # noqa: F401

    __all__.append("SQLiteBackend")
