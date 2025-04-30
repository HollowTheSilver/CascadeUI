"""Base view class for CascadeUI."""

# // ========================================( Modules )======================================== // #


import asyncio
import discord
from discord import Interaction
from discord.ui import View, Item
from typing import List, Optional, Union, Callable, Any, Type, TYPE_CHECKING
from datetime import datetime

# Import the logger
from .utils.logger import AsyncLogger

# Import type references
from .types import UISessionObj, UIManagerObj, CascadeViewObj

# Create logger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")

# Import only for type checking
if TYPE_CHECKING:
    from .session import UISession
    from .manager import UIManager


# // ========================================( Class )======================================== // #


class CascadeView(View):
    """Base class for all CascadeUI views."""

    __attrs__ = ("ephemeral", "embeds", "interaction")

    # This will be set in __init__.py to avoid circular imports
    manager = None  # Type: Optional[UIManager]
    _processed_interactions = {}  # Class-level dictionary

    def __init__(self, *args, **kwargs) -> None:
        # Extract custom attributes
        custom_kwargs = {}
        for key in list(kwargs):
            if key in self.__class__.__attrs__:
                custom_kwargs[key] = kwargs.pop(key)

        # Initialize the discord.ui.View
        super().__init__(*args, **kwargs)

        # Basic setup
        self.name: str = self.__class__.__name__
        self.__session = None
        self.__interaction = None
        self.__message = None
        self.__ephemeral = False
        self.__embeds = None
        self._transition_in_progress = False
        self._is_duplicate = False

        # Process standard attributes
        self.ephemeral = custom_kwargs.get("ephemeral", False)
        self.embeds = custom_kwargs.get("embeds", list())

        # Handle interaction if provided
        interaction = custom_kwargs.get("interaction")
        if interaction is not None:
            # Set interaction without triggering handler
            self.__interaction = interaction

            # Check for existing view of this class
            existing_view = self.manager.check_for_existing_view(self)

            # Stop here if we're a duplicate
            if self._is_duplicate:
                return

            # Now trigger the handler
            self.interaction = interaction

    @property
    def session(self) -> Optional[UISessionObj]:
        """Get the session associated with this view."""
        return self.__session

    @session.setter
    def session(self, value: UISessionObj) -> None:
        """Set the session for this view."""
        from .session import UISession
        if not isinstance(value, UISession) and value is not None:
            exception: str = \
                f"Invalid attribute type '{type(value)}' provided. Attribute 'session' must be a valid UI session object."
            raise AttributeError(exception)
        self.__session = value

    @property
    def interaction(self) -> Optional[Interaction]:
        """Get the interaction associated with this view."""
        return self.__interaction

    @interaction.setter
    def interaction(self, value: Interaction) -> None:
        """Set the interaction and trigger message handling."""
        if value is None:
            return

        if not isinstance(value, Interaction):
            exception: str = \
                f"Invalid attribute type '{type(value)}' provided. Attribute 'interaction' must be a discord interaction object."
            raise AttributeError(exception)

        user_id = value.user.id
        interaction_id = value.id

        # Process through middlewares if set
        if hasattr(self.manager, 'middlewares') and self.manager.middlewares:
            value.client.loop.create_task(self.manager.process_interaction(value))

        # Check for duplicate views (same class for same user)
        if cached_session := self.manager.get(session=user_id):
            for existing_view in cached_session.views:
                if (existing_view is not self and
                        existing_view.__class__.__name__ == self.__class__.__name__):
                    # Found a duplicate - update it instead of this one
                    logger.debug(f"Found duplicate view {existing_view.name}, updating instead")

                    # Update the existing view's interaction
                    existing_view.__interaction = value

                    # Mark this view as a duplicate and fully stop it
                    self._is_duplicate = True
                    self.stop()

                    # Trigger the existing view's handler
                    value.client.loop.create_task(existing_view.__handle_interaction())

                    # Exit early
                    return

        # Track this interaction
        self.__class__._processed_interactions[interaction_id] = id(self)

        # Set the interaction
        self.__interaction = value
        logger.debug(f"Interaction '{value.id}' set for view '{self.name}'")

        # Trigger message handling
        if not self._is_duplicate:
            value.client.loop.create_task(self.__handle_interaction())

    @property
    def message(self) -> Optional[discord.Message]:
        """Get the message associated with this view."""
        return self.__message

    @message.setter
    def message(self, value: discord.Message) -> None:
        """Set the message for this view."""
        if value and not isinstance(value, discord.Message):
            exception: str = \
                f"Invalid attribute type '{type(value)}' provided. Attribute 'message' must be a discord message object."
            raise AttributeError(exception)

        # If we already have a message reference and it's different, clear it first
        if self.__message and value and self.__message.id != value.id:
            self.interaction.client.loop.create_task(self.__clear_old_messages())

        self.__message = value
        if value:
            logger.debug(f"Message '{value.id}' set for view '{self.name}'")

    @property
    def ephemeral(self) -> bool:
        """Get whether this view is ephemeral."""
        return self.__ephemeral

    @ephemeral.setter
    def ephemeral(self, value: bool) -> None:
        """Set whether this view is ephemeral."""
        if not isinstance(value, bool):
            exception: str = \
                f"Invalid attribute type '{type(value)}' provided. Attribute 'ephemeral' must be a boolean."
            raise AttributeError(exception)
        self.__ephemeral = value

    @property
    def embeds(self) -> List[Optional[discord.Embed]]:
        """Get the embeds associated with this view."""
        return self.__embeds

    @embeds.setter
    def embeds(self, value: List[Optional[discord.Embed]]) -> None:
        """Set the embeds for this view."""
        if not isinstance(value, list):
            exception: str = \
                f"Invalid attribute type '{type(value)}' provided. Embeds must be a list of discord embed objects."
            raise AttributeError(exception)
        if invalid := [embed for embed in value if not isinstance(embed, discord.Embed)]:
            exception: str = \
                f"Invalid list {f'elements' if len(invalid) > 1 else f'element'} contained. The '{len(invalid)}' invalid elements must be a discord embed object."
            raise AttributeError(exception)
        self.__embeds = value

    async def __handle_interaction(self) -> None:
        """Central method for handling interactions and message management."""
        user_id = self.interaction.user.id
        interaction_id = self.interaction.id

        try:
            # Skip if this is a duplicate or transition
            if self._is_duplicate or (hasattr(self, '_transition_in_progress') and self._transition_in_progress):
                logger.debug(f"View '{self.name}' is skipping interaction handling")
                return

            logger.debug(f"Handling interaction for view '{self.name}' (id: {id(self)}) for user {user_id}")

            # Ensure session is set up
            await self.__ensure_session(user_id)

            # Run session cleanup in the background
            self.interaction.client.loop.create_task(self.manager.cleanup_old_sessions())  # NOQA

            # Defer early to prevent timeouts
            if not (hasattr(self.interaction, 'response') and self.interaction.response.is_done()):
                try:
                    await self.interaction.response.defer(ephemeral=self.ephemeral)
                except discord.errors.HTTPException:
                    # Interaction might already be acknowledged
                    pass

            # Create a new message
            new_message = await self.interaction.original_response()
            await new_message.edit(view=self, embeds=self.embeds)

            # Set the message (this will trigger cleanup of old messages via the setter)
            self.message = new_message

            logger.debug(f"Created new message '{new_message.id}' for view '{self.name}'")

        except Exception as e:
            logger.error(f"Error in interaction handling: {e}", exc_info=True)
            # Send fallback message
            try:
                await self.interaction.followup.send(
                    view=self,
                    embeds=[discord.Embed(
                        title=f"{self.name}",
                        description="An error occurred",
                        color=0xE02B2B
                    )],
                    ephemeral=self.ephemeral
                )
            except Exception as follow_up_error:
                logger.error(f"Failed to send followup message: {follow_up_error}")
        finally:
            # Clean up tracking
            if interaction_id in self.__class__._processed_interactions:
                if self.__class__._processed_interactions[interaction_id] == id(self):
                    del self.__class__._processed_interactions[interaction_id]
                    logger.debug(f"Removed interaction '{interaction_id}' from processed tracking")

    async def __ensure_session(self, user_id: int) -> None:
        """
        Ensure this view has a valid session, creating one if needed.

        Parameters:
        -----------
        user_id: int
            The user ID to get or create a session for
        """
        from .session import UISession
        if not self.session:
            # Look up by user ID
            if cached_session := self.manager.get(session=user_id):
                self.session = cached_session
                logger.debug(f"Found existing session '{self.session.uid}' for view '{self.name}'")
            # Create new session as last resort
            else:
                session = UISession(uid=user_id)
                self.manager.set(session=session)
                self.session = session
                logger.debug(f"Created new session '{self.session.uid}' for view '{self.name}'")

        # Always add this view to the session
        self.session.set(view=self)

        # Add to navigation history
        if hasattr(self.session, 'add_to_history'):
            self.session.add_to_history(self)

    async def __clear_old_messages(self) -> None:
        """Find and clear old messages from this same instance."""
        if not self.message:
            return

        try:
            # Store the current message for later
            old_message = self.message

            # Immediately clear our reference to prevent recursion issues
            self.__message = None

            # Update old message to show it's been moved
            await old_message.edit(
                view=None,
                embeds=[discord.Embed(
                    title=f"{self.name}",
                    description="This view has been moved to another channel.",
                    color=0x808080  # Gray color
                )]
            )
            logger.debug(f"Cleared old message '{old_message.id}' for view '{self.name}'")
        except Exception as e:
            logger.debug(f"Could not clear old message: {e}")

    async def transition_to(self, view_class, interaction=None, **kwargs) -> CascadeViewObj:
        """
        Transition from this view to a new view class.
        This is the recommended way to create a new view from a button handler.

        Parameters:
        -----------
        view_class: Type[CascadeViewObj]
            The view class to create
        interaction: Optional[discord.Interaction]
            The interaction that triggered this transition
        **kwargs:
            Additional kwargs to pass to the view constructor

        Returns:
        --------
        CascadeViewObj
            The new view instance for method chaining
        """
        # Store the current interaction before we create the new view
        current_interaction = interaction or self.interaction

        # Mark this interaction as handled to prevent further processing
        self._transition_in_progress = True

        # Stop this view to prevent it from processing further
        self.stop()

        # Defer the interaction to prevent timeouts
        if not (hasattr(current_interaction, 'response') and current_interaction.response.is_done()):
            try:
                await current_interaction.response.defer(ephemeral=kwargs.get('ephemeral', False))
            except discord.errors.HTTPException:
                # Interaction might already be acknowledged, that's okay
                pass

        # Clean up this view
        self.clear_items()
        if self.session:
            if self in self.session.views:
                self.session.views.remove(self)

        # Create the new view WITHOUT setting the interaction
        new_view = view_class(**kwargs)

        # Update the original message with the new view
        if self.message:
            try:
                if not new_view.embeds:
                    new_view.embeds = self.embeds
                await self.message.edit(view=new_view, embeds=new_view.embeds)

                # Set the message on the new view
                new_view.message = self.message

                # Add the new view to the session
                if self.session:
                    new_view.session = self.session
                    self.session.set(view=new_view)

                logger.debug(f"Transitioned from '{self.name}' to '{new_view.name}' on message '{self.message.id}'")
            except discord.errors.NotFound:
                # If the message is gone, create a new one
                followup_message = await current_interaction.followup.send(
                    view=new_view,
                    embeds=new_view.embeds,
                    ephemeral=kwargs.get('ephemeral', False),
                    wait=True
                )
                new_view.message = followup_message

                # Add the new view to the session
                if self.session:
                    new_view.session = self.session
                    self.session.set(view=new_view)

                logger.debug(f"Created new message for '{new_view.name}' after transition failed")
        else:
            # If there's no original message, create a new one
            followup_message = await current_interaction.followup.send(
                view=new_view,
                embeds=new_view.embeds,
                ephemeral=kwargs.get('ephemeral', False),
                wait=True
            )
            new_view.message = followup_message

            # Add the new view to the session
            if self.session:
                new_view.session = self.session
                self.session.set(view=new_view)

            logger.debug(f"Created new message for '{new_view.name}' during transition")

        # Clear reference to this message to prevent further processing
        self.__message = None

        # Remove this interaction from processing to prevent duplicate responses
        interaction_id = current_interaction.id
        if interaction_id in self.__class__._processed_interactions:
            del self.__class__._processed_interactions[interaction_id]

        return new_view

    def clear_message_reference(self):
        """Clear the message reference."""
        self.__message = None
        logger.debug(f"Cleared message reference for view '{self.name}'")

    async def on_timeout(self) -> None:
        """Handle view timeout."""
        # Don't do anything if this view has no message reference
        if not self.message:
            logger.debug(f"View '{self.name}' (id: {id(self)}) has no message, skipping timeout")
            return

        try:
            # Update the message to show timeout
            self.stop()
            self.clear_items()
            await self.message.edit(
                view=None,
                embed=discord.Embed(description="Interaction timed out. ❌", color=0xBEBEFE)
            )
            logger.debug(f"View '{self.name}' (id: {id(self)}) timed out for message '{self.message.id}'")

            # Remove this view from the session
            if self.session:
                if self in self.session.views:
                    self.session.views.remove(self)
                logger.debug(f"Removed view '{self.name}' (id: {id(self)}) from session '{self.session.uid}'")

                # Log if session is empty
                if not self.session.views:
                    logger.debug(f"Session '{self.session.uid}' is empty, marking for cleanup")

            # Clear message reference
            self.__message = None
        except Exception as e:
            logger.error(f"Error in timeout handler: {e}", exc_info=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Item) -> None:
        """Handle view errors with enhanced error reporting."""
        # User-friendly error message
        user_embed = discord.Embed(
            title="Something went wrong",
            description="The bot encountered an issue while processing your interaction.",
            color=0xE02B2B
        )

        # Add a retry button if appropriate
        retry_view = None
        if isinstance(error, (discord.HTTPException, discord.RateLimited)):
            retry_view = discord.ui.View(timeout=60)
            retry_button = discord.ui.Button(label="Retry", style=discord.ButtonStyle.primary)

            async def retry_callback(retry_interaction):
                await retry_interaction.response.defer(ephemeral=True)
                # Store current view class to recreate it
                view_class = self.__class__
                await self.transition_to(view_class, interaction=retry_interaction)

            retry_button.callback = retry_callback
            retry_view.add_item(retry_button)

        # Log detailed error information
        logger.error(f"Error in view '{self.name}': {error}", exc_info=True)

        try:
            if self.message:
                await self.message.edit(view=retry_view, embed=user_embed)
            else:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=user_embed, view=retry_view, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=user_embed, view=retry_view, ephemeral=True)
        except Exception as follow_up_error:
            logger.error(f"Failed to send error message: {follow_up_error}")

        # Clean up this view
        if self.session and self in self.session.views:
            self.session.views.remove(self)

        raise error

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if the interaction is valid for this view."""
        if self.interaction and self.interaction.id != interaction.id:
            self.interaction = interaction
        return True

    def is_interacting(self) -> bool:
        """Return if view is receiving interactions."""
        return True if self.interaction and not self.interaction.is_expired() else False

    # Component helpers
    def add_button(self, label: str, style=discord.ButtonStyle.primary,
                   callback: Callable = None, custom_id: str = None,
                   row: int = None, emoji: str = None, disabled: bool = False) -> discord.ui.Button:
        """
        Quick button creation helper.

        Parameters:
        -----------
        label: str
            Text to display on the button
        style: discord.ButtonStyle
            Button style (primary, secondary, success, danger)
        callback: Callable
            Function to call when button is clicked
        custom_id: str
            Optional custom ID for the button
        row: int
            Row to place button in (0-4)
        emoji: str
            Optional emoji to display on button
        disabled: bool
            Whether the button is disabled

        Returns:
        --------
        discord.ui.Button
            The created button (already added to view)
        """
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=custom_id or f"button_{label.lower().replace(' ', '_')}",
            row=row,
            emoji=emoji,
            disabled=disabled
        )

        if callback:
            button.callback = callback

        self.add_item(button)
        return button

    def add_select(self, options: List[discord.SelectOption], placeholder: str = None,
                   callback: Callable = None, custom_id: str = None,
                   min_values: int = 1, max_values: int = 1, row: int = None) -> discord.ui.Select:
        """
        Quick select menu creation helper.

        Parameters:
        -----------
        options: List[discord.SelectOption]
            List of options for the menu
        placeholder: str
            Text to display when nothing is selected
        callback: Callable
            Function to call when selection changes
        custom_id: str
            Optional custom ID for the select menu
        min_values: int
            Minimum number of values to select
        max_values: int
            Maximum number of values to select
        row: int
            Row to place menu in (0-4)

        Returns:
        --------
        discord.ui.Select
            The created select menu (already added to view)
        """
        select = discord.ui.Select(
            options=options,
            placeholder=placeholder,
            custom_id=custom_id or f"select_{placeholder.lower().replace(' ', '_') if placeholder else 'menu'}",
            min_values=min_values,
            max_values=max_values,
            row=row
        )

        if callback:
            select.callback = callback

        self.add_item(select)
        return select

    async def back(self, interaction: Interaction = None) -> Optional[CascadeViewObj]:
        """
        Return to previous view in history.

        Parameters:
        -----------
        interaction: Optional[Interaction]
            The interaction triggering this navigation

        Returns:
        --------
        Optional[CascadeViewObj]
            The previous view or None if history is empty
        """
        if not self.session or not hasattr(self.session, 'view_history') or len(self.session.view_history) < 2:
            logger.debug(
                f"No history available for back navigation in session {self.session.uid if self.session else 'None'}")
            return None

        # Get previous view (current view is last in history)
        previous = self.session.view_history[-2]
        view_class = previous[1]

        # Remove current view from history
        self.session.view_history.pop()

        # Transition to previous view
        return await self.transition_to(view_class, interaction=interaction or self.interaction)

    async def get_embed(self, index: Union[int, List[int]] = None) -> List[Optional[discord.Embed]]:
        """
        Retrieve discord embed object(s) from the embed sequence.

        Parameters:
        -----------
        index: Union[int, List[int]]
            The index or indices to retrieve

        Returns:
        --------
        List[Optional[discord.Embed]]
            The retrieved embeds
        """
        index: int = len(self.embeds) if index is None else index
        indices: List[Any] = index if isinstance(index, list) else [index]
        retrieved: List[Optional[discord.Embed]] = list()
        for index in indices:
            if not isinstance(index, int):
                raise TypeError(
                    f"Invalid index parameter type '{type(index)}' provided. Index only accepts an integer.")
            retrieved.append(self.embeds[index])
        return retrieved

    async def set_embed(self, embed: Union[discord.Embed, List[discord.Embed]], index: int = None) -> List[
        discord.Embed]:
        """
        Set discord embed object(s) to the embed sequence.

        Parameters:
        -----------
        embed: Union[discord.Embed, List[discord.Embed]]
            The embed(s) to set
        index: int
            The index to insert at

        Returns:
        --------
        List[discord.Embed]
            The set embeds
        """
        if not embed and isinstance(embed, list):
            raise TypeError(
                f"Empty list provided for embed parameter. Embed only accepts a list of discord embed objects.")
        elif not embed:
            raise TypeError(
                f"Embed parameter required. Must provide an individual or list of discord embed objects.")
        index: int = len(self.embeds) if index is None else index
        if not isinstance(index, int):
            raise TypeError(
                f"Invalid index parameter type '{type(index)}' provided. Index only accepts an integer.")
        embeds: List[Any] = embed if isinstance(embed, list) else [embed]
        for embed in embeds:
            if not isinstance(embed, discord.Embed):
                raise TypeError(
                    f"Invalid embed parameter type '{type(embed)}' provided. Must be a list of exclusively discord embed objects.")
        self.embeds[index:index] = embeds
        return embeds

    async def delete_embed(self, embed: Union[discord.Embed, List[discord.Embed]]) -> List[discord.Embed]:
        """
        Delete discord embed object(s) from the embed sequence.

        Parameters:
        -----------
        embed: Union[discord.Embed, List[discord.Embed]]
            The embed(s) to delete

        Returns:
        --------
        List[discord.Embed]
            The deleted embeds
        """
        if not embed and isinstance(embed, list):
            raise TypeError(
                f"Empty list provided for embed parameter. Embed only accepts a list of discord embed objects.")
        elif not embed:
            raise TypeError(
                f"Embed parameter required. Must provide an individual or list of discord embed objects.")
        embeds: List[Any] = embed if isinstance(embed, list) else [embed]
        for embed in embeds:
            if not isinstance(embed, discord.Embed):
                raise TypeError(
                    f"Invalid embed parameter type '{type(embed)}' provided. Must be a list of exclusively discord embed objects.")
            self.embeds.remove(embed)
        return embeds
