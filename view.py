"""Base view class for CascadeUI."""

# // ========================================( Modules )======================================== // #


import asyncio
import discord
from discord import Interaction
from discord.ui import View, Item
from typing import List, Optional, Union, Callable, Any, Type, TYPE_CHECKING
from datetime import datetime

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

    # Expanded __attrs__ to include all supported message properties
    __attrs__ = (
        "content", "embeds", "embed", "file", "files", "tts", "ephemeral", "interaction",
        "allowed_mentions", "suppress_embeds", "silent", "delete_after", "poll"
    )

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

        # Initialize message properties with None
        self.__content = None
        self.__embeds = None
        self.__embed = None
        self.__file = None
        self.__files = None
        self.__tts = None
        self.__ephemeral = False
        self.__allowed_mentions = None
        self.__suppress_embeds = None
        self.__silent = None
        self.__delete_after = None
        self.__poll = None

        self._transition_in_progress = False
        self._is_duplicate = False

        # Temporarily store interaction for later
        interaction = custom_kwargs.pop("interaction", None)

        # Process standard attributes from kwargs
        for key, value in custom_kwargs.items():
            setattr(self, key, value)

        # Handle interaction if provided
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

    # Basic Properties
    @property
    def session(self) -> Optional[UISessionObj]:
        """Get the session associated with this view."""
        return self.__session

    @session.setter
    def session(self, value: UISessionObj) -> None:
        """Set the session for this view."""
        # Local import to avoid circular imports
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
        self.__message = value
        if value:
            logger.debug(f"Message '{value.id}' set for view '{self.name}'")

    # Message content properties
    @property
    def content(self) -> Optional[str]:
        """Get the content for this view's messages."""
        return self.__content

    @content.setter
    def content(self, value: Optional[str]) -> None:
        """Set the content for this view's messages."""
        self.__content = value

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

    @property
    def embed(self) -> Optional[discord.Embed]:
        """Get the embed associated with this view."""
        return self.__embed

    @embed.setter
    def embed(self, value: Optional[discord.Embed]) -> None:
        """Set the embed for this view."""
        if value is not None and not isinstance(value, discord.Embed):
            exception: str = f"Invalid attribute type '{type(value)}' provided. Embed must be a discord embed object."
            raise AttributeError(exception)
        self.__embed = value

    @property
    def file(self) -> Optional[discord.File]:
        """Get the file for this view's messages."""
        return self.__file

    @file.setter
    def file(self, value: Optional[discord.File]) -> None:
        """Set the file for this view's messages."""
        if value is not None and not isinstance(value, discord.File):
            exception: str = f"Invalid attribute type '{type(value)}' provided. File must be a discord.File object."
            raise AttributeError(exception)
        self.__file = value

    @property
    def files(self) -> Optional[List[discord.File]]:
        """Get the files for this view's messages."""
        return self.__files

    @files.setter
    def files(self, value: Optional[List[discord.File]]) -> None:
        """Set the files for this view's messages."""
        if value is not None:
            if not isinstance(value, list):
                exception: str = f"Invalid attribute type '{type(value)}' provided. Files must be a list of discord.File objects."
                raise AttributeError(exception)
            if invalid := [file for file in value if not isinstance(file, discord.File)]:
                exception: str = f"Invalid list elements contained. All files must be discord.File objects."
                raise AttributeError(exception)
        self.__files = value

    @property
    def tts(self) -> Optional[bool]:
        """Get whether text-to-speech is enabled for this view's messages."""
        return self.__tts

    @tts.setter
    def tts(self, value: Optional[bool]) -> None:
        """Set whether text-to-speech is enabled for this view's messages."""
        if value is not None and not isinstance(value, bool):
            exception: str = f"Invalid attribute type '{type(value)}' provided. TTS must be a boolean."
            raise AttributeError(exception)
        self.__tts = value

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
    def allowed_mentions(self) -> Optional[discord.AllowedMentions]:
        """Get the allowed mentions for this view's messages."""
        return self.__allowed_mentions

    @allowed_mentions.setter
    def allowed_mentions(self, value: Optional[discord.AllowedMentions]) -> None:
        """Set the allowed mentions for this view's messages."""
        if value is not None and not isinstance(value, discord.AllowedMentions):
            exception: str = f"Invalid attribute type '{type(value)}' provided. Allowed mentions must be a discord.AllowedMentions object."
            raise AttributeError(exception)
        self.__allowed_mentions = value

    @property
    def suppress_embeds(self) -> Optional[bool]:
        """Get whether embeds are suppressed for this view's messages."""
        return self.__suppress_embeds

    @suppress_embeds.setter
    def suppress_embeds(self, value: Optional[bool]) -> None:
        """Set whether embeds are suppressed for this view's messages."""
        if value is not None and not isinstance(value, bool):
            exception: str = f"Invalid attribute type '{type(value)}' provided. Suppress embeds must be a boolean."
            raise AttributeError(exception)
        self.__suppress_embeds = value

    @property
    def silent(self) -> Optional[bool]:
        """Get whether notifications are suppressed for this view's messages."""
        return self.__silent

    @silent.setter
    def silent(self, value: Optional[bool]) -> None:
        """Set whether notifications are suppressed for this view's messages."""
        if value is not None and not isinstance(value, bool):
            exception: str = f"Invalid attribute type '{type(value)}' provided. Silent must be a boolean."
            raise AttributeError(exception)
        self.__silent = value

    @property
    def delete_after(self) -> Optional[float]:
        """Get the time after which this view's messages will be deleted."""
        return self.__delete_after

    @delete_after.setter
    def delete_after(self, value: Optional[float]) -> None:
        """Set the time after which this view's messages will be deleted."""
        if value is not None and not isinstance(value, (int, float)):
            exception: str = f"Invalid attribute type '{type(value)}' provided. Delete after must be a number."
            raise AttributeError(exception)
        self.__delete_after = value

    @property
    def poll(self) -> Optional[object]:  # Changed from discord.Poll to object
        """Get the poll for this view's messages."""
        return self.__poll

    @poll.setter
    def poll(self, value: Optional[object]) -> None:  # Changed from discord.Poll to object
        """Set the poll for this view's messages."""
        if value is not None:
            # Just check if it has poll-like attributes or if discord has Poll
            if not hasattr(discord, 'Poll'):
                logger.warning("Poll feature not available in this discord.py version")
            # Don't validate the type if discord.Poll doesn't exist
        self.__poll = value

    def get_message_kwargs(self) -> dict:
        """
        Get a dictionary of message properties for this view.

        Returns:
        --------
        dict
            A dictionary of message properties to use when creating/editing messages
        """
        kwargs = {'view': self}

        # Add all non-None properties
        if self.content is not None:
            kwargs['content'] = self.content
        if self.embeds is not None:
            kwargs['embeds'] = self.embeds
        if self.embed is not None:
            kwargs['embed'] = self.embed
        if self.tts is not None:
            kwargs['tts'] = self.tts
        if self.allowed_mentions is not None:
            kwargs['allowed_mentions'] = self.allowed_mentions
        if self.suppress_embeds is not None:
            kwargs['suppress_embeds'] = self.suppress_embeds
        if self.silent is not None:
            kwargs['silent'] = self.silent

        # These are only used in send, not in edit - check if they're supported
        if self.file is not None and hasattr(discord.Message, 'edit') and hasattr(discord.Message.edit,
                                                                                  '__annotations__') and 'file' in discord.Message.edit.__annotations__:
            kwargs['file'] = self.file
        if self.files is not None and hasattr(discord.Message, 'edit') and hasattr(discord.Message.edit,
                                                                                   '__annotations__') and 'files' in discord.Message.edit.__annotations__:
            kwargs['files'] = self.files
        if self.delete_after is not None and hasattr(discord.Message, 'edit') and hasattr(discord.Message.edit,
                                                                                          '__annotations__') and 'delete_after' in discord.Message.edit.__annotations__:
            kwargs['delete_after'] = self.delete_after

        # Only include poll if it exists in discord
        if self.poll is not None and hasattr(discord, 'Poll'):
            kwargs['poll'] = self.poll

        return kwargs

    def get_followup_kwargs(self) -> dict:
        """
        Get a dictionary of message properties for followup messages.

        Returns:
        --------
        dict
            A dictionary of message properties to use when creating followup messages
        """
        kwargs = {'view': self}

        # Add all non-None properties
        if self.content is not None:
            kwargs['content'] = self.content
        if self.embeds is not None:
            kwargs['embeds'] = self.embeds
        if self.embed is not None:
            kwargs['embed'] = self.embed
        if self.file is not None:
            kwargs['file'] = self.file
        if self.files is not None:
            kwargs['files'] = self.files
        if self.tts is not None:
            kwargs['tts'] = self.tts
        if self.ephemeral:
            kwargs['ephemeral'] = self.ephemeral
        if self.allowed_mentions is not None:
            kwargs['allowed_mentions'] = self.allowed_mentions
        if self.suppress_embeds is not None:
            kwargs['suppress_embeds'] = self.suppress_embeds
        if self.silent is not None:
            kwargs['silent'] = self.silent
        if self.delete_after is not None:
            kwargs['delete_after'] = self.delete_after

        # Only include poll if it exists in discord
        if self.poll is not None and hasattr(discord, 'Poll'):
            kwargs['poll'] = self.poll

        return kwargs

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
            self.interaction.client.loop.create_task(self.manager.cleanup_old_sessions())

            # Defer early to prevent timeouts
            if not (hasattr(self.interaction, 'response') and self.interaction.response.is_done()):
                try:
                    await self.interaction.response.defer(ephemeral=self.ephemeral)
                except discord.errors.HTTPException:
                    # Interaction might already be acknowledged
                    pass

            # Store current message reference (for this instance)
            current_message = self.message

            # Clear our own message reference - CRITICAL for proper cleanup
            self.__message = None

            # Find ALL other active messages in the session (from other views)
            other_messages = []
            if self.session:
                for view in list(self.session.views):
                    if view is not self and view.message is not None:
                        other_messages.append((view, view.message))
                        logger.debug(f"Found message '{view.message.id}' from view '{view.name}' to clean up")

            # Create a new message
            new_message = await self.interaction.original_response()

            # Get message properties for edit
            edit_kwargs = self.get_message_kwargs()

            # Edit the message with all properties
            await new_message.edit(**edit_kwargs)

            # Set the message property
            self.message = new_message
            logger.debug(f"Created new message '{new_message.id}' for view '{self.name}'")

            # First, clean up our own previous message if it exists
            if current_message:
                try:
                    # Try to delete first
                    try:
                        await current_message.delete()
                        logger.debug(f"Deleted our own previous message '{current_message.id}'")
                    except (discord.Forbidden, discord.NotFound):
                        # Fall back to updating
                        await current_message.edit(
                            view=None,
                            embeds=[discord.Embed(
                                title=f"{self.name}",
                                description="This view has been moved to another channel.",
                                color=0x808080  # Gray color
                            )]
                        )
                        logger.debug(f"Updated our own previous message '{current_message.id}'")
                except Exception as e:
                    logger.debug(f"Could not clean up previous message: {e}")

            # Then clean up ALL other messages in the session
            for view, old_message in other_messages:
                try:
                    # Try to delete first
                    try:
                        await old_message.delete()
                        logger.debug(f"Deleted other message '{old_message.id}' from view '{view.name}'")
                    except (discord.Forbidden, discord.NotFound):
                        # Fall back to updating
                        await old_message.edit(
                            view=None,
                            embeds=[discord.Embed(
                                title=f"{view.name}",
                                description="This view has been moved to another channel.",
                                color=0x808080  # Gray color
                            )]
                        )
                        logger.debug(f"Updated other message '{old_message.id}' from view '{view.name}'")

                    # Clear the view's message reference and items
                    view.clear_message_reference()
                    view.clear_items()
                    view.stop()

                    # Remove the view from the session
                    if view in self.session.views:
                        self.session.views.remove(view)
                        logger.debug(f"Removed view '{view.name}' from session")

                except Exception as e:
                    logger.error(f"Error cleaning up message '{old_message.id}': {e}")

        except Exception as e:
            logger.error(f"Error in interaction handling: {e}", exc_info=True)
            # Send fallback message
            try:
                fallback_kwargs = self.get_followup_kwargs()
                if 'embeds' not in fallback_kwargs:
                    fallback_kwargs['embeds'] = [discord.Embed(
                        title=f"{self.name}",
                        description="An error occurred",
                        color=0xE02B2B
                    )]
                await self.interaction.followup.send(**fallback_kwargs)
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
        # Local import to avoid circular imports
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

        # Save current message reference
        current_message = self.message

        # Important: Clear our message reference to prevent the new view
        # from being associated with us during cleanup scans
        self.clear_message_reference()

        # Transfer message properties that weren't explicitly overridden
        for attr in self.__class__.__attrs__:
            if attr not in ('interaction', 'message') and attr not in kwargs:
                prop_value = getattr(self, attr)
                if prop_value is not None:
                    kwargs[attr] = prop_value

        # Create the new view WITHOUT setting the interaction
        new_view = view_class(**kwargs)

        # Update the original message with the new view
        if current_message:
            # Special handling for file attachments
            has_files = 'file' in kwargs or 'files' in kwargs

            try:
                if has_files:
                    # Can't edit with new files, must create new message
                    followup_kwargs = new_view.get_followup_kwargs()
                    followup_message = await current_interaction.followup.send(wait=True, **followup_kwargs)

                    # Try to delete the old message
                    try:
                        await current_message.delete()
                    except (discord.Forbidden, discord.NotFound):
                        # If we can't delete, at least update it to show it's been moved
                        await current_message.edit(
                            view=None,
                            embeds=[discord.Embed(
                                title=f"{self.name}",
                                description="This view has been moved to another message.",
                                color=0x808080  # Gray color
                            )]
                        )

                    # Set the new message
                    new_view.message = followup_message

                    logger.debug(f"Created new message for '{new_view.name}' with files during transition")
                else:
                    # No files, just edit the existing message
                    edit_kwargs = new_view.get_message_kwargs()
                    await current_message.edit(**edit_kwargs)

                    # Set the message on the new view
                    new_view.message = current_message

                    logger.debug(
                        f"Transitioned from '{self.name}' to '{new_view.name}' on message '{current_message.id}'")

                # Add the new view to the session
                if self.session:
                    new_view.session = self.session
                    self.session.set(view=new_view)

            except discord.errors.NotFound:
                # If the message is gone, create a new one
                followup_kwargs = new_view.get_followup_kwargs()
                followup_message = await current_interaction.followup.send(wait=True, **followup_kwargs)

                new_view.message = followup_message

                # Add the new view to the session
                if self.session:
                    new_view.session = self.session
                    self.session.set(view=new_view)

                logger.debug(f"Created new message for '{new_view.name}' after transition failed")
        else:
            # If there's no original message, create a new one
            followup_kwargs = new_view.get_followup_kwargs()
            followup_message = await current_interaction.followup.send(wait=True, **followup_kwargs)

            new_view.message = followup_message

            # Add the new view to the session
            if self.session:
                new_view.session = self.session
                self.session.set(view=new_view)

            logger.debug(f"Created new message for '{new_view.name}' during transition")

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
