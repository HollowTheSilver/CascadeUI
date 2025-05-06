"""
CascadeUI - Python Module

----------------------------

Copyright © HollowTheSilver 2024-2025 - https://github.com/HollowTheSilver

Version: 1.1.0

Description:
- 🐍 A simple Discord ui instance manager to support efficient view and embed chaining.
"""

# // ========================================( Modules )======================================== // #


# Import singleton early to ensure it's available
from .state.singleton import get_store

# Then import other components that might need the store
from .views.base import StatefulView
from .views.specialized import FormView, PaginatedView
from .components.base import StatefulButton, StatefulSelect
from .components.composition import CompositeComponent, register_component, get_component
from .components.patterns import ConfirmationButtons, PaginationControls, FormLayout
from .components.wrappers import with_loading_state, with_confirmation, with_cooldown
from .state.actions import ActionCreators
from .state.types import StateData
from .theming.core import Theme, register_theme, get_theme, set_current_theme, get_current_theme
from .theming.themes import default_theme, dark_theme, light_theme
from .utils.tasks import get_task_manager
from .utils.errors import with_error_boundary, with_retry, safe_execute
from .utils.decorators import cascade_reducer, cascade_component, cascade_persistent


# // ========================================( Script )======================================== // #


__version__ = "1.1.0"

# Export public API
__all__ = [
    # Views
    "StatefulView",
    "FormView",
    "PaginatedView",

    # Components
    "StatefulButton",
    "StatefulSelect",
    "CompositeComponent",
    "ConfirmationButtons",
    "PaginationControls",
    "FormLayout",
    "register_component",
    "get_component",

    # Component Wrappers
    "with_loading_state",
    "with_confirmation",
    "with_cooldown",

    # State
    "get_store",
    "ActionCreators",
    "StateData",

    # Theming
    "Theme",
    "register_theme",
    "get_theme",
    "set_current_theme",
    "get_current_theme",
    "default_theme",
    "dark_theme",
    "light_theme",

    # Utilities
    "get_task_manager",
    "with_error_boundary",
    "with_retry",
    "safe_execute",
    "cascade_reducer",
    "cascade_component",
    "cascade_persistent"
]

"""logger.info(f"CascadeUI v{__version__} initialized")"""
