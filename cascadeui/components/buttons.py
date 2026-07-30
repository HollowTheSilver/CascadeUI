# // ========================================( Modules )======================================== // #


import logging
from typing import Any, Callable, Optional

import discord
from discord import ButtonStyle, Interaction

from ..utils.hooks import await_maybe
from ..utils.responses import trailing_ack
from .base import StatefulButton

logger = logging.getLogger(__name__)

# // ========================================( Classes )======================================== // #


class PrimaryButton(StatefulButton):
    """A primary-styled button with state management."""

    def __init__(self, label: str, callback: Optional[Callable] = None, **kwargs):
        kwargs.setdefault("style", ButtonStyle.primary)
        super().__init__(label=label, callback=callback, **kwargs)


class SecondaryButton(StatefulButton):
    """A secondary-styled button with state management."""

    def __init__(self, label: str, callback: Optional[Callable] = None, **kwargs):
        kwargs.setdefault("style", ButtonStyle.secondary)
        super().__init__(label=label, callback=callback, **kwargs)


class SuccessButton(StatefulButton):
    """A success-styled button with state management."""

    def __init__(self, label: str, callback: Optional[Callable] = None, **kwargs):
        kwargs.setdefault("style", ButtonStyle.success)
        super().__init__(label=label, callback=callback, **kwargs)


class DangerButton(StatefulButton):
    """A danger-styled button with state management."""

    def __init__(self, label: str, callback: Optional[Callable] = None, **kwargs):
        kwargs.setdefault("style", ButtonStyle.danger)
        super().__init__(label=label, callback=callback, **kwargs)


class LinkButton(discord.ui.Button):
    """A link button that doesn't require a callback."""

    def __init__(self, label: str, url: str, **kwargs):
        kwargs.setdefault("style", ButtonStyle.link)
        super().__init__(label=label, url=url, **kwargs)


class ToggleButton(StatefulButton):
    """A button that toggles between two states.

    The flip runs inside the same stateful callback every other
    component uses, so ``owner_only=``, the acting-view fast path, and
    the finished-view dispatch skip all apply here as they do to a
    plain ``StatefulButton``. ``value`` reports the state the button
    ended on, which is what ``COMPONENT_INTERACTION`` records.
    """

    def __init__(
        self,
        label: str,
        toggled_label: str = None,
        toggled: bool = False,
        callback: Optional[Callable] = None,
        **kwargs,
    ):
        self.original_label = label
        self.toggled_label = toggled_label or f"{label} ✓"
        self.is_toggled = toggled
        self.user_callback = callback

        # Set initial style
        if toggled:
            kwargs.setdefault("style", ButtonStyle.success)
            current_label = self.toggled_label
        else:
            kwargs.setdefault("style", ButtonStyle.secondary)
            current_label = self.original_label

        # Passing the toggle through ``callback=`` is what routes it into
        # ``create_stateful_callback``. Assigning ``self.callback`` after
        # construction instead would replace that wrapper and drop every
        # guard it carries.
        super().__init__(label=current_label, callback=self._toggle, **kwargs)

    @property
    def value(self) -> bool:
        return self.is_toggled

    async def _toggle(self, interaction: Interaction) -> None:
        self.is_toggled = not self.is_toggled
        self.label = self.toggled_label if self.is_toggled else self.original_label
        self.style = ButtonStyle.success if self.is_toggled else ButtonStyle.secondary

        if self.user_callback:
            await await_maybe(self.user_callback(interaction))
        else:
            await trailing_ack(interaction, owner="ToggleButton", log=logger)
