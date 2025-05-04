
# // ========================================( Modules )======================================== // #


from typing import Dict, Any, Optional, List, Union, Callable

import discord
from discord.ui import Item

from ..state.actions import ActionCreators


# // ========================================( Classes )======================================== // #


class StatefulComponent:
    """Base mixin for components that interact with state."""

    def create_stateful_callback(self, component, original_callback=None):
        """Create a callback that updates state."""
        component_id = getattr(component, "custom_id", None) or str(id(component))

        async def stateful_callback(interaction):
            # Get view from the component itself
            view = component.view

            if not view:
                # If we still can't find the view, log error and call original callback
                from ..utils.logging import AsyncLogger
                logger = AsyncLogger(name="cascadeui.components", level="DEBUG", path="logs", mode="a")
                logger.error(f"Could not find view for component {component_id}")

                # Call original callback if provided
                if original_callback:
                    return await original_callback(interaction)
                return

            # Get component value
            value = None
            if hasattr(component, "value"):
                value = component.value
            elif hasattr(component, "values"):
                value = component.values
            elif isinstance(component, discord.ui.Button):
                value = True

            # Dispatch interaction action
            from ..state.actions import ActionCreators
            payload = ActionCreators.component_interaction(
                component_id=component_id,
                view_id=view.id,
                user_id=interaction.user.id,
                value=value
            )
            await view.dispatch("COMPONENT_INTERACTION", payload)

            # Call original callback if provided
            if original_callback:
                return await original_callback(interaction)

        return stateful_callback


class StatefulButton(discord.ui.Button, StatefulComponent):
    """A button that interacts with state."""

    def __init__(self, *args, callback=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)


class StatefulSelect(discord.ui.Select, StatefulComponent):
    """A select menu that interacts with state."""

    def __init__(self, *args, callback=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)
