"""Shared test helpers for CascadeUI tests."""

from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ui import TextDisplay

from cascadeui import StatefulLayoutView


class RenderableLayoutView(StatefulLayoutView):
    """Minimal non-empty V2 view for tests that exercise state, slot, or
    lifecycle logic without a real component tree.

    Adds one ``TextDisplay`` at construction so the tree is a valid
    components-v2 message. ``validate_placement`` requires at least one
    top-level component; a bare ``StatefulLayoutView`` subclass with no
    ``build_ui`` sends an empty tree that Discord rejects with HTTP 400, so
    state-focused fixtures inherit from this base instead of subclassing
    ``StatefulLayoutView`` directly.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(TextDisplay("test content"))


def make_interaction(user_id=100, guild_id=200, is_done=False, message=None):
    """Create a mock discord.Interaction for testing.

    Covers all interaction attributes used across the test suite:
    response (is_done, defer, send_message, send_modal), user, guild,
    guild_id, data, message, and original_response.

    Mirrors real discord.py behavior: calling ``defer()``,
    ``send_message()``, or ``send_modal()`` flips ``is_done()`` to
    ``True`` so downstream checks see the interaction as acknowledged.

    ``message`` defaults to ``None`` (the neutral case -- no specific
    message context). Navigation routes the edit to the view's own message
    unless the acting interaction carries a *different* message, so tests
    that exercise the foreign-message path (a with_confirmation prompt) set
    ``message`` to a mock whose id differs from the view's.
    """
    interaction = AsyncMock()
    interaction.user = MagicMock(id=user_id)
    interaction.guild = MagicMock(id=guild_id)
    interaction.guild_id = guild_id
    interaction.message = message
    # Default to a component interaction (the common navigation/click case) so
    # the acting-view and navigation fast paths, which gate on interaction type,
    # engage by default.
    interaction.type = discord.InteractionType.component
    # InteractionResponse.is_done() is sync in discord.py — use MagicMock
    # so the return value is a plain bool, not a coroutine.
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = is_done

    # Flip is_done after any response method is called (matches real behavior).
    def _flip_done(*args, **kwargs):
        interaction.response.is_done.return_value = True

    interaction.response.defer = AsyncMock(side_effect=_flip_done)
    interaction.response.send_message = AsyncMock(side_effect=_flip_done)
    interaction.response.send_modal = AsyncMock(side_effect=_flip_done)
    interaction.response.edit_message = AsyncMock(side_effect=_flip_done)
    interaction.data = {}
    interaction.original_response = AsyncMock(
        return_value=MagicMock(id=999, channel=MagicMock(id=888))
    )
    return interaction
