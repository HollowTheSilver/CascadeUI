
# // ========================================( Modules )======================================== // #


import uuid
from typing import Any, Optional, Set
from ..utils.tasks import get_task_manager
from ..utils.errors import with_error_boundary, safe_execute

import discord
from discord import Interaction
from discord.ui import View, Item

from ..state.singleton import get_store
from ..state.actions import ActionCreators

# Add logger
from ..utils.logging import AsyncLogger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")


# // ========================================( Classes )======================================== // #


class StatefulView(View):
    """Base class for all stateful UI views."""

    def __init__(self, *args, **kwargs):
        # Extract custom arguments before passing to View
        self.state_store = kwargs.pop("state_store", None) or get_store()
        self.session_id = kwargs.pop("session_id", None)
        self.user_id = kwargs.pop("user_id", None)
        self.context = kwargs.pop("context", None)
        self.interaction = kwargs.pop("interaction", None)
        self.theme = kwargs.pop("theme", None)

        # Initialize standard View class
        super().__init__(*args, **kwargs)

        # Unique identifier for this view instance
        self.id = str(uuid.uuid4())

        # Message reference
        self._message = None

        # Whether state registration has been done
        self._registered = False

        # Get task manager
        self.task_manager = get_task_manager()

        # Derive user_id and session_id from context/interaction
        if self.interaction is None and self.context is not None:
            if hasattr(self.context, "interaction") and self.context.interaction:
                self.interaction = self.context.interaction

            if self.user_id is None and hasattr(self.context, "author"):
                self.user_id = self.context.author.id

        if self.interaction is not None and self.user_id is None:
            self.user_id = self.interaction.user.id

        if self.session_id is None and self.user_id is not None:
            self.session_id = f"user_{self.user_id}"

        # Action types this view cares about (subclasses can override)
        self.subscribed_actions: Optional[Set[str]] = {
            "VIEW_UPDATED", "VIEW_DESTROYED",
            "COMPONENT_INTERACTION", "SESSION_UPDATED",
        }

        # Subscribe to state updates with action filter
        self.state_store.subscribe(self.id, self._on_state_changed, self.subscribed_actions)

    def create_task(self, coro):
        """Create a task owned by this view."""
        return self.task_manager.create_task(self.id, coro)

    async def _register_state(self):
        """Register this view in the state store. Called once on first send."""
        if self._registered:
            return
        self._registered = True

        # Ensure session exists
        if self.session_id:
            payload = ActionCreators.session_created(
                session_id=self.session_id,
                user_id=self.user_id
            )
            await self.state_store.dispatch("SESSION_CREATED", payload)

        # Register the view
        payload = ActionCreators.view_created(
            view_id=self.id,
            view_type=self.__class__.__name__,
            user_id=self.user_id,
            session_id=self.session_id
        )
        await self.state_store.dispatch("VIEW_CREATED", payload)

    async def _update_message_state(self, message):
        """Update state store with message info after sending."""
        if message is None:
            return
        payload = ActionCreators.view_updated(
            view_id=self.id,
            message_id=str(message.id),
            channel_id=str(message.channel.id) if message.channel else None
        )
        await self.dispatch("VIEW_UPDATED", payload)

    async def send(self, content=None, *, embed=None, embeds=None, ephemeral=False):
        """
        Send this view as a message using the stored context or interaction.

        This is the preferred way to display a StatefulView. It handles
        state registration and message tracking automatically.

        Args:
            content: Text content for the message.
            embed: A single embed to include.
            embeds: A list of embeds to include.
            ephemeral: Whether the message should be ephemeral (interaction only).
        """
        await self._register_state()

        send_kwargs = {"view": self}
        if content is not None:
            send_kwargs["content"] = content
        if embed is not None:
            send_kwargs["embed"] = embed
        if embeds is not None:
            send_kwargs["embeds"] = embeds

        if self.context and hasattr(self.context, "send"):
            if ephemeral:
                send_kwargs["ephemeral"] = ephemeral
            message = await self.context.send(**send_kwargs)
            self._message = message
            await self._update_message_state(message)
            return message

        elif self.interaction:
            send_kwargs["ephemeral"] = ephemeral
            if not self.interaction.response.is_done():
                await self.interaction.response.send_message(**send_kwargs)
                message = await self.interaction.original_response()
            else:
                message = await self.interaction.followup.send(**send_kwargs, wait=True)
            self._message = message
            await self._update_message_state(message)
            return message

        else:
            raise RuntimeError(
                "StatefulView.send() requires either 'context' or 'interaction' to be set."
            )

    def get_theme(self):
        """Get the theme for this view, falling back to the global default.

        Returns a Theme instance. If no per-view theme is set and no global
        default exists, returns a bare Theme with standard defaults.
        """
        if self.theme is not None:
            return self.theme
        from ..theming.core import get_default_theme, Theme
        return get_default_theme() or Theme("fallback")

    async def on_timeout(self) -> None:
        """Called when the view times out. Disables all components and cleans up state."""
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

        if self._message:
            try:
                await self._message.edit(view=self)
            except discord.NotFound:
                pass  # Message was already deleted
            except Exception as e:
                logger.debug(f"Could not disable components on timeout: {e}")

        # Cancel tasks and clean up state, mirroring exit()
        self.task_manager.cancel_tasks(self.id)
        await self.dispatch("VIEW_DESTROYED", ActionCreators.view_destroyed(self.id))
        self.state_store.unsubscribe(self.id)

    async def _on_state_changed(self, state, action):
        """React to state changes."""
        logger.debug(f"View '{self.id}' received state update for action '{action['type']}'")

        # Default implementation - update UI if needed
        await self.update_from_state(state)

    async def update_from_state(self, state):
        """
        Update this view based on current state.

        This default implementation does nothing. Subclasses should override
        this method to implement state-driven UI updates when needed.

        Args:
            state: The current application state
        """
        pass

    async def dispatch(self, action_type, payload=None):
        """Dispatch an action to the state store."""
        return await self.state_store.dispatch(action_type, payload, source_id=self.id)

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
            self.create_task(self.dispatch("VIEW_UPDATED", payload))

    async def transition_to(self, view_class, interaction=None, **kwargs):
        """Transition from this view to a new view."""
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
        """Cleanly exit and clean up this view."""
        # Cancel all tasks owned by this view
        self.task_manager.cancel_tasks(self.id)

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
            await interaction.response.defer()
            await self.exit(delete_message=delete_message)

        button.callback = exit_callback
        self.add_item(button)
        return button

    def __del__(self):
        """Clean reference to ensure GC can collect this view."""
        if hasattr(self, "state_store") and hasattr(self, "id"):
            self.state_store.unsubscribe(self.id)
