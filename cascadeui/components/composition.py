
# // ========================================( Modules )======================================== // #


from typing import List, Dict, Any, Optional, Callable, Union, Type
import discord
from discord import Interaction

from .base import StatefulComponent, StatefulButton
from ..state.actions import ActionCreators

# Component registry for pre-built components
_component_registry = {}


# // ========================================( Scripts )======================================== // #


def register_component(name: str, component_class: Type) -> None:
    """Register a component in the global registry."""
    _component_registry[name] = component_class


def get_component(name: str) -> Optional[Type]:
    """Get a component class from the registry."""
    return _component_registry.get(name)


class CompositeComponent:
    """Base class for composite components."""

    def __init__(self) -> None:
        self.components = []

    def add_component(self, component) -> 'CompositeComponent':
        """Add a component to this composite."""
        self.components.append(component)
        return self

    def create_discord_components(self) -> List:
        """Create Discord UI components for all child components."""
        result = []
        for component in self.components:
            if hasattr(component, "create_discord_components"):
                # Handle composite components
                result.extend(component.create_discord_components())
            else:
                # Handle direct Discord components
                result.append(component)
        return result

    def add_to_view(self, view, row=None) -> Any:
        """Add all components to a view."""
        for component in self.create_discord_components():
            view.add_item(component)
        return view
