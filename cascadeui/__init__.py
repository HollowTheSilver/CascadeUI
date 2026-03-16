"""
CascadeUI - Stateful UI components for discord.py.

Copyright (c) 2024-2025 HollowTheSilver - https://github.com/HollowTheSilver
"""

# // ========================================( Modules )======================================== // #


# Import singleton early to ensure it's available
from .state.singleton import get_store

# Then import other components that might need the store
from .views.base import StatefulView
from .views.specialized import FormView, PaginatedView
from .views.persistent import PersistentView, setup_persistence
from .views.patterns import TabView, WizardView
from .components.base import StatefulButton, StatefulSelect
from .components.composition import CompositeComponent, register_component, get_component
from .components.patterns import (
    ConfirmationButtons, PaginationControls, FormLayout,
    ToggleGroup, ProgressBar,
)
from .components.wrappers import with_loading_state, with_confirmation, with_cooldown
from .state.actions import ActionCreators
from .state.types import StateData
from .state.middleware import DebouncedPersistence, logging_middleware
from .theming.core import Theme, register_theme, get_theme, set_default_theme, get_default_theme
from .theming.themes import default_theme, dark_theme, light_theme
from .utils.tasks import get_task_manager
from .utils.errors import with_error_boundary, with_retry, safe_execute
from .utils.decorators import cascade_reducer, cascade_component
from .devtools import StateInspector, InspectorView, DevToolsCog


# // ========================================( Script )======================================== // #


__version__ = "2.0.0"

# Export public API
__all__ = [
    # Views
    "StatefulView",
    "FormView",
    "PaginatedView",
    "PersistentView",
    "setup_persistence",
    "TabView",
    "WizardView",

    # Components
    "StatefulButton",
    "StatefulSelect",
    "CompositeComponent",
    "ConfirmationButtons",
    "PaginationControls",
    "FormLayout",
    "ToggleGroup",
    "ProgressBar",
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

    # Middleware
    "DebouncedPersistence",
    "logging_middleware",

    # Theming
    "Theme",
    "register_theme",
    "get_theme",
    "set_default_theme",
    "get_default_theme",
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

    # DevTools
    "StateInspector",
    "InspectorView",
    "DevToolsCog",
]
