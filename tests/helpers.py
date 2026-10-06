"""Shared test helpers for CascadeUI tests."""

import asyncio
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
    # InteractionResponse.is_done() is sync in discord.py: use MagicMock
    # so the return value is a plain bool, not a coroutine.
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = is_done
    # What answered the slot, as discord.py records it; the library tells a
    # defer from a reply by this. A slot that starts answered was deferred,
    # the common case (an auto-defer landed first): discord.py never reports
    # an answered slot with no type.
    interaction.response.type = (
        discord.InteractionResponseType.deferred_message_update if is_done else None
    )

    # Flip is_done after any response method is called (matches real behavior).
    def _answered_by(kind):
        def _flip_done(*args, **kwargs):
            interaction.response.is_done.return_value = True
            interaction.response.type = kind

        return _flip_done

    kinds = discord.InteractionResponseType
    interaction.response.defer = AsyncMock(side_effect=_answered_by(kinds.deferred_message_update))
    interaction.response.send_message = AsyncMock(side_effect=_answered_by(kinds.channel_message))
    interaction.response.send_modal = AsyncMock(side_effect=_answered_by(kinds.modal))
    interaction.response.edit_message = AsyncMock(side_effect=_answered_by(kinds.message_update))
    interaction.data = {}
    # The message original_response() returns carries awaitable edit/delete so a
    # probe that drives send() -> background-subscriber refresh() (which awaits
    # self._message.edit(...)) does not trip on a non-awaitable MagicMock.
    _response_message = MagicMock(id=999, channel=MagicMock(id=888))
    _response_message.edit = AsyncMock(return_value=_response_message)
    _response_message.delete = AsyncMock()
    interaction.original_response = AsyncMock(return_value=_response_message)
    return interaction


def schedule_bounded_waits_as_before_312(monkeypatch):
    """Make the library's bounded waits run as they do before Python 3.12.

    There the bound runs the work as a task, so the work starts a loop
    iteration after the call, and the caller wakes one iteration after it
    finishes. Both hops open windows the direct await from 3.12 closes.
    """
    import cascadeui.utils.tasks as tasks

    monkeypatch.setattr(tasks, "_TIMEOUT_CONTEXT", False)


async def until(condition, timeout=5, interval=0):
    """Wait until ``condition()`` is true, failing after ``timeout`` seconds.

    For a result that arrives on a real timer: the bound is reached only when
    the result never comes, so a slow machine waits longer instead of failing.
    """

    async def poll():
        while not condition():
            await asyncio.sleep(interval)

    await asyncio.wait_for(poll(), timeout)


def refresh_timers(view):
    """The view's refresh-handoff timers still waiting for their deadline."""
    return list(view.task_manager._timers.get(view.id, ()))


async def wait_for_timer(view, timer, timeout=30):
    """Wait until ``timer`` fires, then for the arming it starts, if any."""
    await until(lambda: timer not in refresh_timers(view), timeout=timeout)
    await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout)
