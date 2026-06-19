"""
CascadeUI - Stateful UI components for discord.py.

Copyright (c) 2024-2026 HollowTheSilver - https://github.com/HollowTheSilver
"""

# // ========================================( Modules )======================================== // #


import logging as _logging

_logging.getLogger(__name__).addHandler(_logging.NullHandler())

from .components.base import DynamicPersistentButton, StatefulButton, StatefulSelect
from .components.buttons import (
    DangerButton,
    LinkButton,
    PrimaryButton,
    SecondaryButton,
    SuccessButton,
    ToggleButton,
)
from .components.inputs import Checkbox, CheckboxGroup, FileUpload, Modal, RadioGroup, TextInput
from .components.patterns import (
    ConfirmationButtons,
    EmojiGrid,
    PaginationControls,
    ProgressBar,
    ToggleGroup,
    action_section,
    alert,
    button_grid,
    button_row,
    card,
    confirm_section,
    cycle_button,
    divider,
    emoji_grid,
    file_attachment,
    gallery,
    gap,
    image_section,
    key_value,
    link_section,
    progress_bar,
    stats_card,
    tab_nav,
    toggle_button,
    toggle_section,
)
from .components.selects import (
    ChannelSelect,
    Dropdown,
    MentionableSelect,
    RoleSelect,
    UserSelect,
)
from .components.types import EmojiInput, MediaInput
from .components.v1_composition import CompositeComponent, get_component, register_component
from .components.wrappers import with_confirmation, with_cooldown, with_loading_state
from .devtools import DevToolsCog, InspectorView

# Then import other components that might need the store
from .exceptions import (
    InstanceLimitError,
    PersistenceConfigError,
    PersistenceError,
    PersistenceInitError,
    PersistenceRehydrateError,
    PersistenceSchemaError,
)
from .persistence import (
    ApplicationPersistence,
    Capability,
    InMemoryBackend,
    PersistenceBackend,
    PersistenceManager,
    RegistryPersistence,
    SlotPolicy,
)
from .persistence import __all__ as _persistence_all
from .persistence import (
    register_kwargs_migrator,
    register_migrator,
)
from .setup import setup_middleware
from .state.actions import ActionCreators
from .state.computed import ComputedValue, computed
from .state.middleware import LoggingMiddleware, PersistenceMiddleware, UndoMiddleware

# Import singleton early to ensure it's available
from .state.singleton import get_store
from .state.slots import access_slot, read_slot, slot_property
from .state.store import StateStore
from .state.types import StateData
from .theming.context import get_current_theme
from .theming.core import Theme, get_default_theme, get_theme, register_theme, set_default_theme
from .theming.themes import dark_theme, default_theme, light_theme
from .utils.decorators import cascade_component, cascade_reducer
from .utils.errors import safe_execute, with_error_boundary, with_retry
from .utils.fetch import fetch_as_file
from .utils.logging import setup_logging
from .utils.strings import slugify
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
from .views.layout import DisplayLayoutView, StatefulLayoutView
from .views.patterns import (
    FormLayoutView,
    FormView,
    LeaderboardLayoutView,
    MenuLayoutView,
    MenuView,
    PaginatedLayoutView,
    PaginatedView,
    PersistentLeaderboardLayoutView,
    PersistentRolesLayoutView,
    RolesLayoutView,
    TabLayoutView,
    TabView,
    WizardLayoutView,
    WizardView,
)
from .views.patterns.types import (
    FormField,
    FormSchema,
    RoleCategory,
    WizardSchema,
    WizardStep,
)
from .views.persistent import PersistentLayoutView, PersistentView
from .views.view import StatefulView

# Optional backend -- only present when aiosqlite is installed
if "SQLiteBackend" in _persistence_all:
    from .persistence import SQLiteBackend  # noqa: F401

# // ========================================( Script )======================================== // #


__version__ = "3.3.4"

# Export public API
__all__ = [
    # V1 Views
    "StatefulView",
    "InstanceLimitError",
    # Persistence exceptions
    "PersistenceError",
    "PersistenceConfigError",
    "PersistenceInitError",
    "PersistenceSchemaError",
    "PersistenceRehydrateError",
    # V1 Views (continued)
    "FormView",
    "MenuView",
    "PaginatedView",
    "PersistentView",
    "TabView",
    "WizardView",
    # Persistence
    "PersistenceManager",
    "PersistenceBackend",
    "Capability",
    "InMemoryBackend",
    "RegistryPersistence",
    "ApplicationPersistence",
    "SlotPolicy",
    "register_kwargs_migrator",
    "register_migrator",
    # V2 Layout Views
    "StatefulLayoutView",
    "DisplayLayoutView",
    "PersistentLayoutView",
    "FormLayoutView",
    "LeaderboardLayoutView",
    "MenuLayoutView",
    "PaginatedLayoutView",
    "PersistentLeaderboardLayoutView",
    "PersistentRolesLayoutView",
    "RolesLayoutView",
    "TabLayoutView",
    "WizardLayoutView",
    # Components
    "StatefulButton",
    "StatefulSelect",
    "DynamicPersistentButton",
    "PrimaryButton",
    "SecondaryButton",
    "SuccessButton",
    "DangerButton",
    "LinkButton",
    "ToggleButton",
    "Dropdown",
    "RoleSelect",
    "ChannelSelect",
    "UserSelect",
    "MentionableSelect",
    "CompositeComponent",
    "ConfirmationButtons",
    "PaginationControls",
    "ToggleGroup",
    "ProgressBar",
    "register_component",
    "get_component",
    # Input Components
    "TextInput",
    "Checkbox",
    "CheckboxGroup",
    "RadioGroup",
    "FileUpload",
    "Modal",
    # Component Wrappers
    "with_loading_state",
    "with_confirmation",
    "with_cooldown",
    # Type aliases
    "EmojiInput",
    "MediaInput",
    # V2 Cards & Sections
    "card",
    "action_section",
    "toggle_section",
    "image_section",
    "link_section",
    "confirm_section",
    # V2 Buttons & Rows
    "button_row",
    "cycle_button",
    "toggle_button",
    # V2 Content
    "key_value",
    "alert",
    "stats_card",
    "progress_bar",
    # V2 Separators
    "divider",
    "gap",
    # V2 Navigation
    "tab_nav",
    # V2 Media
    "gallery",
    "file_attachment",
    # V2 Grids
    "EmojiGrid",
    "emoji_grid",
    "button_grid",
    # State
    "get_store",
    "StateStore",
    "ActionCreators",
    "StateData",
    "access_slot",
    "read_slot",
    "slot_property",
    # Middleware
    "LoggingMiddleware",
    "PersistenceMiddleware",
    "UndoMiddleware",
    # Computed State
    "computed",
    "ComputedValue",
    # Typed schemas
    "FormField",
    "FormSchema",
    "RoleCategory",
    "WizardStep",
    "WizardSchema",
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
    "get_current_theme",
    "default_theme",
    "dark_theme",
    "light_theme",
    # Logging
    "setup_logging",
    # Middleware install
    "setup_middleware",
    # Utilities
    "get_task_manager",
    "with_error_boundary",
    "with_retry",
    "safe_execute",
    "cascade_reducer",
    "cascade_component",
    "slugify",
    "fetch_as_file",
    # DevTools
    "InspectorView",
    "DevToolsCog",
]

if "SQLiteBackend" in _persistence_all:
    __all__.append("SQLiteBackend")
