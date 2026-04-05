"""
CascadeUI - Stateful UI components for discord.py.

Copyright (c) 2024-2026 HollowTheSilver - https://github.com/HollowTheSilver
"""

# // ========================================( Modules )======================================== // #


from .components.base import StatefulButton, StatefulSelect
from .components.inputs import Modal, TextInput
from .components.v1_composition import CompositeComponent, get_component, register_component
from .components.v1_patterns import (
    ConfirmationButtons,
    FormLayout,
    PaginationControls,
    ProgressBar,
    ToggleGroup,
)
from .components.v2_patterns import (
    action_section,
    alert,
    card,
    divider,
    gallery,
    gap,
    image_section,
    key_value,
    toggle_section,
)
from .components.wrappers import with_confirmation, with_cooldown, with_loading_state
from .devtools import DevToolsCog, InspectorView, StateInspector
from .state.actions import ActionCreators
from .state.computed import ComputedValue, computed
from .state.middleware import DebouncedPersistence, logging_middleware

# Import singleton early to ensure it's available
from .state.singleton import get_store
from .state.types import StateData
from .state.undo import UndoMiddleware
from .theming.core import Theme, get_default_theme, get_theme, register_theme, set_default_theme
from .theming.themes import dark_theme, default_theme, light_theme
from .utils.decorators import cascade_component, cascade_reducer
from .utils.errors import safe_execute, with_error_boundary, with_retry
from .utils.helpers import slugify
from .utils.tasks import get_task_manager
from .validation import (
    ValidationResult,
    choices,
    max_length,
    max_value,
    min_length,
    min_value,
    regex,
    validate_field,
    validate_fields,
)

# Then import other components that might need the store
from .views.base import SessionLimitError, StatefulView
from .views.layout import StatefulLayoutView
from .views.layout_patterns import TabLayoutView, WizardLayoutView
from .views.layout_specialized import FormLayoutView, PaginatedLayoutView
from .views.patterns import TabView, WizardView
from .views.persistent import PersistentLayoutView, PersistentView, setup_persistence
from .views.specialized import FormView, PaginatedView

# // ========================================( Script )======================================== // #


__version__ = "2.1.0"

# Export public API
__all__ = [
    # V1 Views
    "StatefulView",
    "SessionLimitError",
    "FormView",
    "PaginatedView",
    "PersistentView",
    "setup_persistence",
    "TabView",
    "WizardView",
    # V2 Layout Views
    "StatefulLayoutView",
    "PersistentLayoutView",
    "FormLayoutView",
    "PaginatedLayoutView",
    "TabLayoutView",
    "WizardLayoutView",
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
    # Input Components
    "TextInput",
    "Modal",
    # Component Wrappers
    "with_loading_state",
    "with_confirmation",
    "with_cooldown",
    # V2 Patterns
    "card",
    "action_section",
    "toggle_section",
    "image_section",
    "key_value",
    "alert",
    "divider",
    "gap",
    "gallery",
    # State
    "get_store",
    "ActionCreators",
    "StateData",
    # Middleware
    "DebouncedPersistence",
    "logging_middleware",
    "UndoMiddleware",
    # Computed State
    "computed",
    "ComputedValue",
    # Validation
    "ValidationResult",
    "validate_field",
    "validate_fields",
    "min_length",
    "max_length",
    "regex",
    "choices",
    "min_value",
    "max_value",
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
    "slugify",
    # DevTools
    "StateInspector",
    "InspectorView",
    "DevToolsCog",
]
