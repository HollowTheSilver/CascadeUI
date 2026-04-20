"""Tests for component wrappers: with_loading_state, with_cooldown, with_confirmation."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helpers import make_interaction

from cascadeui.components.wrappers import with_confirmation, with_cooldown, with_loading_state


# // ========================================( Helpers )======================================== // #


def _make_button(callback=None, label="Test", emoji=None):
    """Create a minimal mock component for wrapper tests."""
    btn = MagicMock()
    btn.callback = callback or AsyncMock()
    btn.label = label
    btn.emoji = emoji
    btn.disabled = False
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
            captured_states.append(
                {"disabled": component.disabled, "label": component.label}
            )

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
        """Should use interaction.response.edit_message when not yet responded."""
        component = _make_button()
        with_loading_state(component)
        interaction = make_interaction(is_done=False)

        await component.callback(interaction)

        interaction.response.edit_message.assert_called_once()

    async def test_falls_back_when_already_deferred(self):
        """Should fall back to message.edit when interaction is already responded."""
        component = _make_button()
        with_loading_state(component)
        interaction = make_interaction(is_done=True)
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        await component.callback(interaction)

        interaction.response.edit_message.assert_not_called()
        interaction.message.edit.assert_called()

    async def test_skips_restore_when_view_finished(self):
        """Should not edit message in finally block if the view is finished."""
        component = _make_button()
        # View becomes finished during callback (simulates exit/push)
        component.view.is_finished.return_value = True
        with_loading_state(component)
        interaction = make_interaction()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        await component.callback(interaction)

        # The finally block should skip the restore edit
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
        # Rejection should be sent via response or followup
        assert (
            interaction2.response.send_message.called
            or interaction2.followup.send.called
        )

    async def test_allows_after_cooldown_expires(self):
        """Calls should succeed after the cooldown period expires."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=1)

        interaction = make_interaction()
        await component.callback(interaction)

        # Manually expire the cooldown
        key = interaction.user.id
        # Access the closure's cooldowns dict by calling with expired time
        with patch("cascadeui.components.wrappers.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.now() + timedelta(seconds=2)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
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

    async def test_global_scope(self):
        """Global cooldown should block everyone."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, scope="global")

        await component.callback(make_interaction(user_id=1, guild_id=300))
        await component.callback(make_interaction(user_id=2, guild_id=400))

        assert original.call_count == 1

    async def test_custom_message(self):
        """Custom cooldown message with {remaining} placeholder."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, message="Wait {remaining}s!")
        interaction = make_interaction()

        await component.callback(interaction)

        interaction2 = make_interaction()
        await component.callback(interaction2)

        # Check the rejection message was sent with the custom format
        if interaction2.response.send_message.called:
            msg = interaction2.response.send_message.call_args[0][0]
        else:
            msg = interaction2.followup.send.call_args[0][0]
        assert msg.startswith("Wait ")
        assert msg.endswith("s!")

    async def test_falls_back_to_followup_when_deferred(self):
        """Should use followup.send when interaction is already responded."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5)

        await component.callback(make_interaction())

        # Second call with already-responded interaction
        interaction2 = make_interaction(is_done=True)
        await component.callback(interaction2)

        interaction2.response.send_message.assert_not_called()
        interaction2.followup.send.assert_called_once()


# // ========================================( with_confirmation )======================================== // #


class TestWithConfirmation:
    """with_confirmation sends a confirm/cancel prompt before running the callback."""
    async def test_sends_ephemeral_prompt(self):
        """Should send an ephemeral embed with confirm/cancel buttons."""
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
