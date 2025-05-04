
# // ========================================( Modules )======================================== // #


from typing import List, Optional, Callable, Union, Dict, Any

import discord
from discord import Interaction, SelectOption

from .base import StatefulComponent, StatefulSelect


# // ========================================( Classes )======================================== // #


class Dropdown(StatefulSelect):
    """A dropdown select menu with state management."""

    def __init__(self, options: List[Union[SelectOption, Dict[str, Any]]],
                 placeholder: Optional[str] = None,
                 callback: Optional[Callable] = None, **kwargs):
        # Process options if they're dictionaries
        processed_options = []
        for opt in options:
            if isinstance(opt, dict):
                processed_options.append(SelectOption(
                    label=opt.get("label", "Option"),
                    value=opt.get("value", opt.get("label", "Option")),
                    description=opt.get("description"),
                    emoji=opt.get("emoji"),
                    default=opt.get("default", False)
                ))
            else:
                processed_options.append(opt)

        super().__init__(
            options=processed_options,
            placeholder=placeholder,
            callback=callback,
            **kwargs
        )


class RoleSelect(discord.ui.RoleSelect, StatefulComponent):
    """A role select menu with state management."""

    def __init__(self, placeholder: Optional[str] = None,
                 callback: Optional[Callable] = None, **kwargs):
        super().__init__(placeholder=placeholder, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)


class ChannelSelect(discord.ui.ChannelSelect, StatefulComponent):
    """A channel select menu with state management."""

    def __init__(self, placeholder: Optional[str] = None,
                 callback: Optional[Callable] = None, **kwargs):
        super().__init__(placeholder=placeholder, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)


class UserSelect(discord.ui.UserSelect, StatefulComponent):
    """A user select menu with state management."""

    def __init__(self, placeholder: Optional[str] = None,
                 callback: Optional[Callable] = None, **kwargs):
        super().__init__(placeholder=placeholder, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)


class MentionableSelect(discord.ui.MentionableSelect, StatefulComponent):
    """A mentionable select menu with state management."""

    def __init__(self, placeholder: Optional[str] = None,
                 callback: Optional[Callable] = None, **kwargs):
        super().__init__(placeholder=placeholder, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)
