"""
CascadeUI - Python Module

----------------------------

Copyright © HollowTheSilver 2024-2025 - https://github.com/HollowTheSilver

Version: 1.1.0

Description:
- 🐍 A simple Discord ui instance manager to support efficient view and embed chaining.
"""

# // ========================================( Modules )======================================== // #


# First, import all types
from .types import UIManagerObj, UISessionObj, CascadeViewObj

# Import the logger
from .utils.logger import AsyncLogger

# Next, import all classes
from .manager import UIManager
from .view import CascadeView
from .session import UISession
from .views.paginated import PaginatedCascadeView


# // ========================================( Script )======================================== // #


# Create a package-level logger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")

# CRITICAL: Create the singleton UIManager instance and resolve circular dependencies
manager_instance = UIManager()

# CRITICAL: Set the manager attribute on CascadeView to break the circular dependency
CascadeView.manager = manager_instance

# Expose these classes at the package level
__all__ = [
    'UIManager',
    'CascadeView',
    'UISession',
    'PaginatedCascadeView',
    'AsyncLogger'
]

__version__ = '1.1.0'

logger.info(f"CascadeUI v{__version__} initialized")
