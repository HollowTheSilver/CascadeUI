"""Tests for component wrappers: with_loading_state, with_cooldown, with_confirmation."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from helpers import make_interaction

from cascadeui.components.wrappers import with_confirmation, with_cooldown, with_loading_state
from cascadeui.views.base import _StatefulMixin

# // ========================================( Helpers )======================================== // #


def _make_button(callback=None, label="Test", emoji=None, stateful_view=False):
    """Create a minimal mock component for wrapper tests.

    Args:
        stateful_view: When True, ``component.view`` is an ``AsyncMock`` spec'd
            to ``_StatefulMixin`` so ``isinstance(view, _StatefulMixin)`` checks
            inside the wrappers hit the stateful branch.
    """
    btn = MagicMock()
    btn.callback = callback or AsyncMock()
    btn.label = label
    btn.emoji = emoji
    btn.disabled = False
    if stateful_view:
        btn.view = AsyncMock(spec=_StatefulMixin)
        btn.view._message = MagicMock(id=555)
        btn.view.is_finished = MagicMock(return_value=False)
    else:
        btn.view = MagicMock()
        btn.view.is_finished.return_value = False
    return btn


# // ========================================( with_loading_state )======================================== // #


class TestWithLoadingState:
    """with_loading_state disables the component during callback and restores after."""

    async def test_sets_loading_appearance(self):
        """Component should be disabled with loading label while callback runs."""
        captured_states = []

        async def capture_callback(interaction):
            captured_states.append({"disabled": component.disabled, "label": component.label})

        component = _make_button(callback=capture_callback, label="Click Me")
        with_loading_state(component, loading_label="Working...")
        interaction = make_interaction()

        await component.callback(interaction)

        assert captured_states[0]["disabled"] is True
        assert captured_states[0]["label"] == "Working..."

    async def test_restores_original_state(self):
        """Component should restore original label and enabled state after callback."""
        component = _make_button(label="Original")
        with_loading_state(component, loading_label="Loading...")
        interaction = make_interaction()

        await component.callback(interaction)

        assert component.disabled is False
        assert component.label == "Original"

    async def test_restores_on_error(self):
        """Component state should be restored even if the callback raises."""

        async def failing_callback(interaction):
            raise ValueError("boom")

        component = _make_button(callback=failing_callback, label="Original")
        with_loading_state(component, loading_label="Loading...")
        interaction = make_interaction()

        with pytest.raises(ValueError, match="boom"):
            await component.callback(interaction)

        assert component.disabled is False
        assert component.label == "Original"

    async def test_uses_interaction_response_when_available(self):
        """Plain-view path: should use interaction.response.edit_message when not yet responded."""
        component = _make_button()
        with_loading_state(component)
        interaction = make_interaction(is_done=False)

        await component.callback(interaction)

        interaction.response.edit_message.assert_called_once()

    async def test_falls_back_when_already_deferred(self):
        """Plain-view path: should fall back to message.edit when interaction is already responded."""
        component = _make_button()
        with_loading_state(component)
        interaction = make_interaction(is_done=True)
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        await component.callback(interaction)

        interaction.response.edit_message.assert_not_called()
        interaction.message.edit.assert_called()

    async def test_stateful_view_routes_through_refresh(self):
        """_StatefulMixin view path: pre-edit and restore both call view.refresh()."""
        component = _make_button(stateful_view=True)
        with_loading_state(component)
        interaction = make_interaction()

        await component.callback(interaction)

        # Pre-edit + restore = 2 refresh() calls through the stateful branch.
        assert component.view.refresh.await_count == 2
        # Plain-path edit_message must NOT fire when routing through refresh.
        interaction.response.edit_message.assert_not_called()

    async def test_skips_restore_when_view_finished(self):
        """Should not edit message in finally block if the view is finished."""
        component = _make_button()
        component.view.is_finished.return_value = True
        with_loading_state(component)
        interaction = make_interaction()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        await component.callback(interaction)

        interaction.message.edit.assert_not_called()

    async def test_loading_emoji(self):
        """Loading emoji should replace the original during callback."""
        captured = []

        async def capture(interaction):
            captured.append(component.emoji)

        component = _make_button(callback=capture, emoji="\u2705")
        with_loading_state(component, loading_emoji="\u23f3")
        interaction = make_interaction()

        await component.callback(interaction)

        assert captured[0] == "\u23f3"
        assert component.emoji == "\u2705"


# // ========================================( with_cooldown )======================================== // #


class TestWithCooldown:
    """with_cooldown blocks rapid re-invocations within the cooldown window."""

    async def test_allows_first_call(self):
        """First call should always pass through to the original callback."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5)
        interaction = make_interaction()

        await component.callback(interaction)

        original.assert_called_once()

    async def test_rejects_during_cooldown(self):
        """Second call within cooldown period should send rejection message."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5)
        interaction = make_interaction()

        await component.callback(interaction)

        interaction2 = make_interaction()
        await component.callback(interaction2)

        assert original.call_count == 1
        assert interaction2.response.send_message.called or interaction2.followup.send.called

    async def test_allows_after_cooldown_expires(self):
        """Calls should succeed after the monotonic clock advances past the deadline."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=1)

        interaction = make_interaction()
        await component.callback(interaction)

        base = time.monotonic()
        with patch("cascadeui.components.wrappers.time.monotonic", return_value=base + 2.0):
            interaction2 = make_interaction()
            await component.callback(interaction2)

        assert original.call_count == 2

    async def test_user_scope_isolates_users(self):
        """Per-user cooldown should not affect other users."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, scope="user")

        await component.callback(make_interaction(user_id=1))
        await component.callback(make_interaction(user_id=2))

        assert original.call_count == 2

    async def test_guild_scope(self):
        """Per-guild cooldown should block all users in the same guild."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, scope="guild")

        await component.callback(make_interaction(user_id=1, guild_id=300))
        await component.callback(make_interaction(user_id=2, guild_id=300))

        assert original.call_count == 1

    async def test_user_guild_scope(self):
        """Per-user-per-guild scope: same user in different guilds gets independent cooldowns."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, scope="user_guild")

        # Same user, different guilds -- both should pass.
        await component.callback(make_interaction(user_id=1, guild_id=300))
        await component.callback(make_interaction(user_id=1, guild_id=400))
        assert original.call_count == 2

        # Same user, same guild again -- blocked.
        await component.callback(make_interaction(user_id=1, guild_id=300))
        assert original.call_count == 2

    async def test_global_scope(self):
        """Global cooldown should block everyone."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, scope="global")

        await component.callback(make_interaction(user_id=1, guild_id=300))
        await component.callback(make_interaction(user_id=2, guild_id=400))

        assert original.call_count == 1

    async def test_invalid_scope_raises_at_decoration_time(self):
        """Typo'd scope value should raise ValueError before any click arrives."""
        component = _make_button()
        with pytest.raises(ValueError, match="not a valid cooldown scope"):
            with_cooldown(component, seconds=5, scope="guld")

    async def test_custom_message(self):
        """Custom cooldown message with {remaining} placeholder."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, message="Wait {remaining}s!")
        interaction = make_interaction()

        await component.callback(interaction)

        interaction2 = make_interaction()
        await component.callback(interaction2)

        if interaction2.response.send_message.called:
            msg = interaction2.response.send_message.call_args[0][0]
        else:
            msg = interaction2.followup.send.call_args[0][0]
        assert msg.startswith("Wait ")
        assert msg.endswith("s!")

    async def test_falls_back_to_followup_when_deferred(self):
        """Plain-view path: should use followup.send when interaction is already responded."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5)

        await component.callback(make_interaction())

        interaction2 = make_interaction(is_done=True)
        await component.callback(interaction2)

        interaction2.response.send_message.assert_not_called()
        interaction2.followup.send.assert_called_once()

    async def test_stateful_view_rejects_via_respond(self):
        """_StatefulMixin view path: rejection routes through view.respond()."""
        original = AsyncMock()
        component = _make_button(callback=original, stateful_view=True)
        with_cooldown(component, seconds=5)

        await component.callback(make_interaction())
        interaction2 = make_interaction()
        await component.callback(interaction2)

        component.view.respond.assert_awaited_once()
        # Raw interaction.response/followup must NOT be touched on stateful path.
        interaction2.response.send_message.assert_not_called()


# // ========================================( with_confirmation )======================================== // #


class TestWithConfirmation:
    """with_confirmation sends a confirm/cancel prompt before running the callback."""

    async def test_sends_ephemeral_prompt(self):
        """Plain-view path: should send an ephemeral embed with confirm/cancel buttons."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_confirmation(component, title="Delete?", message="This is permanent.")
        interaction = make_interaction()

        await component.callback(interaction)

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        assert call_kwargs["ephemeral"] is True
        assert call_kwargs["embed"].title == "Delete?"

    async def test_does_not_call_original_before_confirm(self):
        """Original callback should not fire until confirm button is clicked."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)

        original.assert_not_called()

    async def test_custom_labels(self):
        """Custom confirm and cancel labels should appear on the buttons."""
        component = _make_button()
        with_confirmation(
            component,
            confirm_label="Destroy",
            cancel_label="Keep",
            confirm_style=MagicMock(),
            cancel_style=MagicMock(),
        )
        interaction = make_interaction()

        await component.callback(interaction)

        call_kwargs = interaction.response.send_message.call_args[1]
        view = call_kwargs["view"]
        labels = [child.label for child in view.children]
        assert "Destroy" in labels
        assert "Keep" in labels

    async def test_uses_interaction_response(self):
        """Confirmation wrapper should consume the interaction response slot."""
        component = _make_button()
        with_confirmation(component)
        interaction = make_interaction(is_done=False)

        await component.callback(interaction)

        interaction.response.send_message.assert_called_once()

    async def test_stateful_view_prompt_uses_respond(self):
        """_StatefulMixin view path: prompt routes through view.respond() not raw send."""
        component = _make_button(stateful_view=True)
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)

        component.view.respond.assert_awaited_once()
        interaction.response.send_message.assert_not_called()

    async def test_confirm_stops_inner_view(self):
        """Confirm branch must call stop() so the timeout task doesn't leak."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        with patch.object(inner_view, "stop") as mock_stop:
            confirm_btn = next(c for c in inner_view.children if c.label == "Yes")
            confirm_interaction = make_interaction()
            await confirm_btn.callback(confirm_interaction)
            mock_stop.assert_called_once()

    async def test_cancel_stops_inner_view(self):
        """Cancel branch must call stop() so the timeout task doesn't leak."""
        component = _make_button()
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        with patch.object(inner_view, "stop") as mock_stop:
            cancel_btn = next(c for c in inner_view.children if c.label == "No")
            cancel_interaction = make_interaction()
            await cancel_btn.callback(cancel_interaction)
            mock_stop.assert_called_once()

    async def test_cancel_stops_even_on_callback_error(self):
        """stop() fires in finally so a failing on_cancel still cleans up the View."""

        async def boom(_):
            raise RuntimeError("cancel handler failed")

        component = _make_button()
        with_confirmation(component, on_cancel=boom)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        with patch.object(inner_view, "stop") as mock_stop:
            cancel_btn = next(c for c in inner_view.children if c.label == "No")
            with pytest.raises(RuntimeError, match="cancel handler failed"):
                await cancel_btn.callback(make_interaction())
            mock_stop.assert_called_once()

    async def test_confirm_runs_action_even_when_prompt_edit_fails(self):
        """A failed prompt edit (deleted message, dead ack, transient 5xx) must
        not skip the confirmed action -- the contract is 'on confirm, run the
        callback'. The cosmetic edit failure is contained; the action still runs."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        confirm_btn = next(c for c in inner_view.children if c.label == "Yes")

        confirm_interaction = make_interaction()
        confirm_interaction.response.edit_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(), "")
        )

        await confirm_btn.callback(confirm_interaction)  # must not raise
        original.assert_awaited_once()

    async def test_cancel_runs_hook_even_when_prompt_edit_fails(self):
        """Symmetric to the confirm path: a failed prompt edit must not skip the
        on_cancel hook."""
        on_cancel = AsyncMock()
        component = _make_button()
        with_confirmation(component, on_cancel=on_cancel)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        cancel_btn = next(c for c in inner_view.children if c.label == "No")

        cancel_interaction = make_interaction()
        cancel_interaction.response.edit_message = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "boom")
        )

        await cancel_btn.callback(cancel_interaction)  # must not raise
        on_cancel.assert_awaited_once()
