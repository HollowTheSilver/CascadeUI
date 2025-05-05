
# // ========================================( Modules )======================================== // #


import asyncio
import uuid
from typing import Dict, Any, Optional, List, Union, Callable

import discord
from discord import Interaction
from discord.ui import View, Item

# Import from singleton instead of store directly
from ..state.singleton import get_store
from ..state.actions import ActionCreators

# Add logger
from ..utils.logging import AsyncLogger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")


# // ========================================( Classes )======================================== // #


class StatefulView(View):
    """Base class for all stateful UI views."""

    def __init__(self, *args, **kwargs):
        # Extract custom arguments
        self.state_store = kwargs.pop("state_store", None) or get_store()
        self.session_id = kwargs.pop("session_id", None)
        self.user_id = kwargs.pop("user_id", None)
        self.context = kwargs.pop("context", None)
        self.interaction = kwargs.pop("interaction", None)

        # Initialize standard View class
        super().__init__(*args, **kwargs)

        # Unique identifier for this view instance
        self.id = str(uuid.uuid4())

        # Message reference
        self._message = None

        # Setup from context or interaction if provided
        if self.interaction is None and self.context is not None:
            if hasattr(self.context, "interaction") and self.context.interaction:
                self.interaction = self.context.interaction

            if self.user_id is None and hasattr(self.context, "author"):
                self.user_id = self.context.author.id

        if self.interaction is not None and self.user_id is None:
            self.user_id = self.interaction.user.id

        # If no session ID, create one based on user
        if self.session_id is None and self.user_id is not None:
            self.session_id = f"user_{self.user_id}"

        # Subscribe to state updates
        self.state_store.subscribe(self.id, self._on_state_changed)

        # Register this view in the state
        self._register_view()

        # Schedule initialization based on context
        if self.interaction:
            asyncio.create_task(self._init_from_interaction())
        elif self.context and hasattr(self.context, "send"):
            asyncio.create_task(self._init_from_context())

    def _register_view(self):
        """Register this view in the state store."""
        # First, ensure session exists
        if self.session_id:
            payload = ActionCreators.session_created(
                session_id=self.session_id,
                user_id=self.user_id
            )
            asyncio.create_task(self.state_store.dispatch("SESSION_CREATED", payload))

        # Then register the view
        payload = ActionCreators.view_created(
            view_id=self.id,
            view_type=self.__class__.__name__,
            user_id=self.user_id,
            session_id=self.session_id
        )
        asyncio.create_task(self.state_store.dispatch("VIEW_CREATED", payload))

    async def _on_state_changed(self, state, action):
        """React to state changes."""
        # Log notification
        logger.debug(f"View '{self.id}' received state update for action '{action['type']}'")

        # Default implementation - update UI if needed
        await self.update_from_state(state)

    async def update_from_state(self, state):
        """Update this view based on current state.

        Override this in subclasses to update specific UI elements.
        """
        pass

    async def dispatch(self, action_type, payload=None):
        """Dispatch an action to the state store."""
        return await self.state_store.dispatch(action_type, payload, source_id=self.id)

    async def _init_from_interaction(self):
        """Initialize this view from an interaction."""
        if not self.interaction:
            return

        try:
            # Defer if not already responded
            if not (hasattr(self.interaction, "response") and self.interaction.response.is_done()):
                try:
                    await self.interaction.response.defer(ephemeral=getattr(self, "ephemeral", False))
                except discord.errors.HTTPException:
                    pass  # Already acknowledged

            # Create or update message
            response = await self.interaction.original_response()
            await response.edit(view=self)
            self._message = response

            # Update state with message info
            payload = ActionCreators.view_updated(
                view_id=self.id,
                message_id=str(response.id),
                channel_id=str(response.channel.id) if response.channel else None
            )
            await self.dispatch("VIEW_UPDATED", payload)

        except Exception as e:
            logger.error(f"Error initializing view from interaction: {e}")

    async def _init_from_context(self):
        """Initialize this view from a command context."""
        if not self.context or not hasattr(self.context, "send"):
            return

        try:
            # Send new message
            message = await self.context.send(view=self)
            self._message = message

            # Update state with message info
            payload = ActionCreators.view_updated(
                view_id=self.id,
                message_id=str(message.id),
                channel_id=str(message.channel.id) if message.channel else None
            )
            await self.dispatch("VIEW_UPDATED", payload)

        except Exception as e:
            logger.error(f"Error initializing view from context: {e}")

    @property
    def message(self):
        """Get the message associated with this view."""
        return self._message

    @message.setter
    def message(self, value):
        """Set the message associated with this view."""
        self._message = value

        # Update state with new message info
        if value:
            payload = ActionCreators.view_updated(
                view_id=self.id,
                message_id=str(value.id),
                channel_id=str(value.channel.id) if value.channel else None
            )
            asyncio.create_task(self.dispatch("VIEW_UPDATED", payload))

    async def transition_to(self, view_class, interaction=None, **kwargs):
        """Transition from this view to a new view."""
        # Get current interaction or use provided one
        current_interaction = interaction or self.interaction

        # Dispatch navigation action
        await self.dispatch("NAVIGATION", ActionCreators.navigation(
            destination=view_class.__name__,
            **kwargs
        ))

        # Include state store and session in new view
        if "state_store" not in kwargs:
            kwargs["state_store"] = self.state_store

        if "session_id" not in kwargs:
            kwargs["session_id"] = self.session_id

        # Create new view
        new_view = view_class(interaction=current_interaction, **kwargs)

        # Clean up this view
        self.stop()

        return new_view

    async def exit(self, delete_message=False):
        """
        Cleanly exit and clean up this view.

        Args:
            delete_message: Whether to delete the message or just remove the view
        """
        # Stop this view
        self.stop()

        # Clean up the message
        if self._message:
            try:
                if delete_message:
                    await self._message.delete()
                else:
                    await self._message.edit(view=None)
            except Exception as e:
                logger.error(f"Error cleaning up message: {e}")

        # Dispatch view destroyed action
        await self.dispatch("VIEW_DESTROYED", ActionCreators.view_destroyed(self.id))

        # Unsubscribe from state updates
        self.state_store.unsubscribe(self.id)

        return True

    def add_exit_button(self, label="Exit", style=discord.ButtonStyle.secondary,
                        row=None, emoji="❌", delete_message=False):
        """Add a button that exits this view when clicked."""
        button = discord.ui.Button(
            label=label,
            style=style,
            row=row,
            emoji=emoji
        )

        async def exit_callback(interaction):
            await interaction.response.defer(ephemeral=True)
            await self.exit(delete_message=delete_message)
            await interaction.followup.send("View closed.", ephemeral=True)

        button.callback = exit_callback
        self.add_item(button)
        return button

    def __del__(self):
        """Ensure cleanup when this view is garbage collected."""
        # Unsubscribe from state updates
        if hasattr(self, "state_store") and hasattr(self, "id"):
            self.state_store.unsubscribe(self.id)
