"""
CascadeUI - Python Module

----------------------------

Copyright © HollowTheSilver 2024-2025 - https://github.com/HollowTheSilver

Version: 1.1.0

Description:
- 🐍 A simple Discord ui instance manager to support efficient view and embed chaining.
"""

# // ========================================( Modules )======================================== // #


from .views.base import StatefulView
from .views.specialized import FormView, PaginatedView
from .components.base import StatefulButton, StatefulSelect
from .state.store import get_store
from .state.actions import ActionCreators
from .utils.decorators import cascade_reducer, cascade_component, cascade_persistent
from .utils.logging import AsyncLogger

# Package-level logger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")


# // ========================================( Script )======================================== // #


__version__ = "1.0.0"

# Export public API
__all__ = [
    "StatefulView",
    "FormView",
    "PaginatedView",
    "StatefulButton",
    "StatefulSelect",
    "get_store",
    "ActionCreators",
    "cascade_reducer",
    "cascade_component",
    "cascade_persistent"
]

logger.info(f"CascadeUI v{__version__} initialized")
