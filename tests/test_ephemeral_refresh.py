"""Tests for auto_refresh_ephemeral: button arming, emoji validation, and
session-limit replace behavior on ephemeral views.

Covers the v2.2.0 fixes:
- Default refresh_button_emoji is a valid Discord button emoji
- _arm_refresh_button retries without the emoji if Discord rejects it (50035)
- Replace-policy session limiting deletes old ephemeral messages instead of
  freezing them
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ui import ActionRow, TextDisplay
from helpers import RenderableLayoutView
from helpers import make_interaction as _make_interaction

from cascadeui import InstanceLimitError
from cascadeui.components.base import StatefulButton
from cascadeui.state.actions import ActionCreators
from cascadeui.state.singleton import get_store
from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.view import StatefulView

# // ========================================( Default Emoji Validity )======================================== // #


class TestDefaultRefreshEmoji:
    """Default refresh_button_emoji is a valid Unicode emoji accepted by Discord."""

    def test_default_emoji_is_in_emoji_range(self):
        """The default refresh_button_emoji must be a Unicode emoji code
        point (U+1F000+), not an arrow symbol like U+21BB which Discord
        rejects as an invalid button emoji with error 50035.
        """
        default = StatefulView.refresh_button_emoji
        assert len(default) == 1, "Default emoji should be a single code point"
        code_point = ord(default)
        # Emoji live in the Supplementary Multilingual Plane (U+1F000+).
        # The U+2100-U+27FF symbol blocks contain glyphs that look like
        # emoji but are rejected by Discord without VS16.
        assert code_point >= 0x1F000, (
            f"Default emoji U+{code_point:04X} is outside the U+1F000+ "
            f"Unicode emoji range. Discord rejects symbols from the U+2100-"
            f"U+27FF blocks as invalid button emoji."
        )

    def test_default_emoji_matches_across_v1_and_v2(self):
        """V1 and V2 share the same default via _StatefulMixin."""
        assert StatefulView.refresh_button_emoji == StatefulLayoutView.refresh_button_emoji


# // ========================================( Arm Refresh Button )======================================== // #


def _make_emoji_error() -> discord.HTTPException:
    """Construct a 50035 HTTPException matching Discord's emoji rejection."""
    response = MagicMock(status=400, reason="Bad Request")
    err = discord.HTTPException(
        response,
        {
            "code": 50035,
            "message": "Invalid Form Body",
            "errors": {
                "components": {"0": {"components": {"0": {"emoji": {"name": {"_errors": []}}}}}}
            },
        },
    )
    err.code = 50035
    return err


class TestArmRefreshButton:
    """Arming the ephemeral refresh button installs a working reopen mechanism."""

    async def test_arm_with_default_emoji_succeeds(self):
        """Arming with the default (valid) emoji should not raise and
        should leave the view with a single refresh button installed.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._arm_refresh_button()

        assert view._refresh_armed is True
        view._message.edit.assert_awaited_once()

    async def test_arm_ships_inside_an_active_cooldown_window(self):
        """The arming edit answers to the 900s token expiry, not to the
        library's own pacing.

        A view carrying ``refresh_cooldown_ms`` would otherwise queue its
        handoff behind the cooldown window, and a window longer than the
        90-second arming margin would strand it entirely.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            refresh_cooldown_ms = 120000  # longer than the 90s arming margin

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._cooldown_not_before = time.monotonic() + 120

        await view._arm_refresh_button()

        view._message.edit.assert_awaited_once()
        assert view._deferred_refresh_task is None

    async def test_deferred_refresh_does_not_rebuild_over_the_armed_tree(self):
        """An armed view is frozen on its refresh button.

        ``_handle_state_notification`` enforces that freeze, but
        ``_deferred_refresh`` calls ``on_state_changed`` directly and used to
        walk past it: the rebuild cleared the button, and the armed flag then
        dropped every notification that could put it back, leaving a dead
        panel at the token expiry.
        """
        rebuilds = []

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

            def build_ui(self):
                rebuilds.append(1)
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Normal", custom_id="n")))

        view = _View(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._refresh_armed = True
        view._install_refresh_button(view._build_refresh_button())
        armed_tree = list(view.children)
        rebuilds.clear()

        await view._deferred_refresh(0.01)

        assert rebuilds == []  # build_ui must not run
        assert list(view.children) == armed_tree
        view._message.edit.assert_awaited_once()

    async def test_arm_retries_without_emoji_on_50035(self):
        """If Discord rejects the button emoji with error 50035, the
        library should retry the arm once without the emoji and succeed.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            refresh_button_emoji = "\u21bb"  # deliberately bad

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        # First edit raises the emoji rejection, second edit succeeds
        view._message.edit = AsyncMock(side_effect=[_make_emoji_error(), None])

        await view._arm_refresh_button()

        # Two edits means the retry path ran
        assert view._message.edit.await_count == 2

    async def test_arm_does_not_retry_on_non_emoji_error(self):
        """A 50035 without 'emoji' in the message should NOT trigger the
        retry path; it logs and swallows instead.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()

        response = MagicMock(status=400, reason="Bad Request")
        other_err = discord.HTTPException(response, {"code": 50035, "message": "Invalid Form Body"})
        other_err.code = 50035
        view._message.edit = AsyncMock(side_effect=other_err)

        await view._arm_refresh_button()

        # Only the initial attempt, no retry
        assert view._message.edit.await_count == 1

    async def test_arm_is_idempotent(self):
        """_refresh_armed gate should prevent double-arming."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._arm_refresh_button()
        await view._arm_refresh_button()

        # Second call should be a no-op
        view._message.edit.assert_awaited_once()


# // ========================================( Armed View Freeze )======================================== // #


class TestArmedViewFreeze:
    """An armed ephemeral view ignores subsequent state notifications.

    Once ``_arm_refresh_button`` swaps the view's children for the
    refresh button, any state-driven rebuild would clobber it -- leaving
    the user with no recovery path once the interaction token expires.
    The notification handler short-circuits to keep the button visible
    inside the 90-second pre-warning window.
    """

    async def test_armed_view_drops_state_notifications(self):
        """After arming, _handle_state_notification must skip on_state_changed."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view.on_state_changed = AsyncMock()

        view._refresh_armed = True

        await view._handle_state_notification({}, {"type": "DEMO_ACTION"})

        view.on_state_changed.assert_not_awaited()

    async def test_unarmed_view_still_runs_on_state_changed(self):
        """The guard must only fire when armed; normal flow is preserved."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view.on_state_changed = AsyncMock()

        await view._handle_state_notification({}, {"type": "DEMO_ACTION"})

        view.on_state_changed.assert_awaited_once()


# // ========================================( Replace-Path replace_policy )======================================== // #


class TestReplacePolicyExitBehavior:
    """replace_policy controls whether instance replacement deletes or disables the old message."""

    def test_default_is_delete(self):
        """replace_policy should default to "delete" on StatefulView so
        the replace path cleanly supplants the old message.
        """
        assert StatefulView.replace_policy == "delete"
        assert StatefulLayoutView.replace_policy == "delete"

    async def test_replace_deletes_by_default(self):
        """Under replace policy with default replace_policy="delete",
        exiting the old view should pass delete_message=True.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        view1 = _View(interaction=_make_interaction())
        from cascadeui.state.singleton import get_store

        get_store()._register_view(view1)

        view2 = _View(interaction=_make_interaction())
        with patch.object(view1, "exit", new_callable=AsyncMock) as mock_exit:
            await view2._enforce_instance_limit()
            mock_exit.assert_awaited_once_with(delete_message=True)

    async def test_replace_disables_when_opted_in(self):
        """Views that opt into the frozen pattern via replace_policy="disable"
        should see the old view exited with delete_message=False, leaving
        the frozen message visible in channel history.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            replace_policy = "disable"

        view1 = _View(interaction=_make_interaction())
        from cascadeui.state.singleton import get_store

        get_store()._register_view(view1)

        view2 = _View(interaction=_make_interaction())
        with patch.object(view1, "exit", new_callable=AsyncMock) as mock_exit:
            await view2._enforce_instance_limit()
            mock_exit.assert_awaited_once_with(delete_message=False)


# // ========================================( Bare exit() exit_policy )======================================== // #


class TestExitPolicy:
    """exit_policy controls whether bare exit() freezes or deletes the message."""

    def test_default_is_disable(self):
        """exit_policy should default to "disable" on StatefulView so
        bare exit() calls preserve the historical safe-by-default
        freeze behavior.
        """
        assert StatefulView.exit_policy == "disable"
        assert StatefulLayoutView.exit_policy == "disable"

    async def test_bare_exit_freezes_by_default(self):
        """Bare exit() with default exit_policy="disable" should
        resolve delete_message to False, preserving the message.
        """

        class _View(StatefulView):
            pass

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.delete = AsyncMock()

        await view.exit()

        view._message.delete.assert_not_awaited()
        view._message.edit.assert_awaited()

    async def test_bare_exit_deletes_when_opted_in(self):
        """Views that opt into exit_policy="delete" should have
        bare exit() calls remove the message.
        """

        class _View(StatefulView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.delete = AsyncMock()

        await view.exit()

        view._message.delete.assert_awaited_once()

    def test_instance_limit_error_default_message_singular(self):
        """default_message should use singular phrasing when limit == 1."""
        err = InstanceLimitError("MyView", 1)
        assert "already have one" in err.default_message
        assert "MyView" not in err.default_message  # No internal class names

    def test_instance_limit_error_default_message_plural(self):
        """default_message should use plural phrasing when limit > 1."""
        err = InstanceLimitError("MyView", 3)
        assert "3" in err.default_message
        assert "MyView" not in err.default_message

    async def test_explicit_argument_overrides_policy(self):
        """An explicit delete_message argument to exit() must override
        whatever exit_policy says.
        """

        class _View(StatefulView):
            exit_policy = "delete"  # would delete by default

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.delete = AsyncMock()

        # Explicit False must override the "delete" policy
        await view.exit(delete_message=False)

        view._message.delete.assert_not_awaited()
        view._message.edit.assert_awaited()

    async def test_exit_swallows_not_found(self, caplog):
        """404 on the cleanup call is expected lifecycle (dismissed
        ephemeral, admin delete, channel delete). exit() must swallow
        silently -- no ERROR log, no raised exception.
        """
        import logging

        class _View(StatefulView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        response = MagicMock(status=404, reason="Not Found")
        view._message.delete = AsyncMock(side_effect=discord.NotFound(response, "Unknown Message"))

        with caplog.at_level(logging.ERROR, logger="cascadeui.views.base"):
            await view.exit()

        assert not any("Error cleaning up message" in record.message for record in caplog.records)

    async def test_exit_debug_logs_ephemeral_token_expired(self, caplog):
        """401 on ephemeral cleanup is the webhook-token cliff -- expected
        lifecycle past the 15-minute wall. exit() demotes to DEBUG and
        never fires an ERROR log for this case.
        """
        import logging

        class _View(StatefulView):
            pass

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._ephemeral = True
        response = MagicMock(status=401, reason="Unauthorized")
        view._message.edit = AsyncMock(
            side_effect=discord.HTTPException(response, "Invalid Webhook Token")
        )

        with caplog.at_level(logging.DEBUG, logger="cascadeui.views.base"):
            await view.exit()

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert not error_records
        assert any("webhook token expired" in r.message for r in debug_records)

    async def test_exit_error_logs_unexpected_http_error(self, caplog):
        """A genuine HTTP error (not 404, not ephemeral 401) must still
        land in the ERROR log so operators notice real problems.
        """
        import logging

        class _View(StatefulView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        response = MagicMock(status=500, reason="Internal Server Error")
        view._message.delete = AsyncMock(
            side_effect=discord.HTTPException(response, "Server error")
        )

        with caplog.at_level(logging.ERROR, logger="cascadeui.views.base"):
            await view.exit()

        assert any(
            "Error cleaning up message" in r.message
            for r in caplog.records
            if r.levelname == "ERROR"
        )


# // ========================================( exit_policy Through Helpers )======================================== // #


class TestExitPolicyThroughHelpers:
    """The exit controls the library ships honor exit_policy.

    make_exit_button / add_exit_button / make_nav_row forward
    ``delete_message=None`` so the class attribute stays reachable. A
    concrete default here would occupy the explicit-argument tier and
    silently strand the policy.
    """

    @staticmethod
    def _armed(view):
        """Attach a mock message whose edit/delete calls are observable."""
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view._message.edit = AsyncMock()
        return view._message

    async def test_v2_add_exit_button_deletes_under_delete_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button()
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 1
        assert message.edit.await_count == 0

    async def test_v2_add_exit_button_freezes_under_disable_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "disable"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button()
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 0
        assert message.edit.await_count == 1

    async def test_v1_add_exit_button_deletes_under_delete_policy(self):
        class _View(StatefulView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button()
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 1

    async def test_make_exit_button_deletes_under_delete_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        button = view.make_exit_button()
        view.add_item(ActionRow(button))
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 1

    async def test_make_nav_row_exit_deletes_under_delete_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        row = view.make_nav_row(back=False)
        view.add_item(row)
        message = self._armed(view)

        await row.children[0].callback(_make_interaction())

        assert message.delete.await_count == 1

    async def test_explicit_true_overrides_disable_policy(self):
        """The explicit argument is the winning tier in both directions."""

        class _View(StatefulLayoutView):
            exit_policy = "disable"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button(delete_message=True)
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 1

    async def test_explicit_false_overrides_delete_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button(delete_message=False)
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 0
        assert message.edit.await_count == 1

    async def test_on_timeout_freezes_regardless_of_delete_policy(self):
        """An expiry is not a close gesture, so exit_policy does not apply."""

        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        view.add_exit_button()
        message = self._armed(view)

        await view.on_timeout()

        assert message.delete.await_count == 0
        assert message.edit.await_count == 1


# // ========================================( on_instance_limit Hook )======================================== // #


class TestOnInstanceLimit:
    """on_instance_limit hook sends ephemeral rejection with default or custom messages."""

    async def test_default_sends_ephemeral_with_default_message(self):
        """The default on_instance_limit should send error.default_message
        as an ephemeral on the originating interaction when no
        instance_limit_message is set.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        interaction2 = _make_interaction()
        view2 = _View(interaction=interaction2)
        result = await view2.send()

        assert result is None
        # Default message used (no class-level override)
        interaction2.response.send_message.assert_called_once()
        sent_msg = interaction2.response.send_message.call_args[0][0]
        assert "already have one" in sent_msg

    async def test_custom_message_takes_precedence(self):
        """A non-empty instance_limit_message should be sent instead
        of the default.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"
            instance_limit_message = "Whoa there, partner."

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        interaction2 = _make_interaction()
        view2 = _View(interaction=interaction2)
        await view2.send()

        interaction2.response.send_message.assert_called_once_with(
            "Whoa there, partner.", ephemeral=True
        )

    async def test_falsy_message_falls_back_to_default(self):
        """An empty string for instance_limit_message should fall back
        to error.default_message via the ``or`` chain.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"
            instance_limit_message = ""

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        interaction2 = _make_interaction()
        view2 = _View(interaction=interaction2)
        await view2.send()

        sent_msg = interaction2.response.send_message.call_args[0][0]
        assert "already have one" in sent_msg

    async def test_override_method_is_called(self):
        """A subclass that overrides on_instance_limit should have its
        override invoked instead of the default behavior.
        """
        captured: list = []

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

            async def on_instance_limit(self, error):
                captured.append((error.view_type, error.limit))

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        view2 = _View(interaction=_make_interaction())
        result = await view2.send()

        assert result is None
        assert len(captured) == 1
        assert captured[0][1] == 1

    async def test_followup_used_when_response_done(self):
        """If interaction.response.is_done() returns True, the handler
        should fall back to followup.send.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        interaction2 = _make_interaction()
        interaction2.response.is_done = MagicMock(return_value=True)
        view2 = _View(interaction=interaction2)
        await view2.send()

        interaction2.followup.send.assert_called_once()
        interaction2.response.send_message.assert_not_called()


# // ========================================( Track Child + Refresh Handoff )======================================== // #


class TestAttachChildRefreshHandoff:
    """Verify that auto_refresh_ephemeral migrates the tracked-child slot
    from the old instance to the refreshed one.

    Without this transfer, a parent that called attach_child(old) would
    still hold a reference to the (about-to-exit) old view after refresh,
    and its _cleanup_attached_children pass would silently skip the new view --
    leaving an orphan ephemeral after the parent ends. This was discovered
    during a live Battleship rematch test where MyShipsView panels lingered
    after the game finished, but only when both panels had been refreshed
    past the original 15-minute token window.
    """

    def test_attach_child_sets_back_pointer(self):
        """attach_child should record a back-pointer on the child so it
        can find its parent later.
        """
        parent = StatefulLayoutView(interaction=_make_interaction())
        child = StatefulLayoutView(interaction=_make_interaction())

        parent.attach_child(child)

        assert child in parent._attached_children
        assert child._attached_to is parent

    def test_attach_child_is_idempotent(self):
        """Tracking the same child twice should not duplicate or rebind."""
        parent = StatefulLayoutView(interaction=_make_interaction())
        child = StatefulLayoutView(interaction=_make_interaction())

        parent.attach_child(child)
        parent.attach_child(child)

        assert parent._attached_children.count(child) == 1
        assert child._attached_to is parent

    async def test_cleanup_attached_children_clears_back_pointer(self):
        """After cleanup, surviving children should have no dangling
        back-pointer to the (now-finished) parent.
        """
        parent = StatefulLayoutView(interaction=_make_interaction())
        child = StatefulLayoutView(interaction=_make_interaction())
        child._message = MagicMock()
        child._message.delete = AsyncMock()

        parent.attach_child(child)
        await parent._cleanup_attached_children()

        assert parent._attached_children == []
        assert child._attached_to is None

    async def test_cleanup_attached_children_skips_already_finished(self):
        """Finished children should be silently dropped, not re-exited."""
        parent = StatefulLayoutView(interaction=_make_interaction())
        child = StatefulLayoutView(interaction=_make_interaction())

        parent.attach_child(child)
        child.stop()  # mark finished

        with patch.object(child, "exit", new=AsyncMock()) as mock_exit:
            await parent._cleanup_attached_children()
            mock_exit.assert_not_awaited()

        assert parent._attached_children == []

    async def test_reopen_migrates_tracked_child_slot(self):
        """The headline regression: after _reopen_ephemeral runs, the
        parent's tracked-child slot should hold the new view, not the
        old one. Without the migration, _cleanup_attached_children silently
        skips the new view because it was never tracked.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        parent = StatefulLayoutView(interaction=_make_interaction())
        old_child = _Refreshable(interaction=_make_interaction())
        old_child._message = MagicMock()
        old_child._message.delete = AsyncMock()
        old_child._message.edit = AsyncMock()

        parent.attach_child(old_child)

        # _reopen_factory lets us hand a pre-built replacement to the
        # refresh path without exercising __init__ kwarg snapshotting.
        new_child = _Refreshable(interaction=_make_interaction())
        old_child._reopen_factory = lambda: new_child

        # Mock send -- no working channel/webhook in test context.
        new_child.send = AsyncMock()
        # Mock self.exit so the old view's exit doesn't try to dispatch.
        old_child.exit = AsyncMock()

        refresh_interaction = _make_interaction()
        await old_child._reopen_ephemeral(refresh_interaction)

        # The slot must now hold the new view, not the old one.
        assert new_child in parent._attached_children
        assert old_child not in parent._attached_children
        assert new_child._attached_to is parent
        assert old_child._attached_to is None

    async def test_reopen_skips_migration_when_parent_finished(self):
        """If the parent already exited while the child was waiting on
        a refresh click, the migration should silently no-op rather than
        re-adding the child to a dead parent's list.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        parent = StatefulLayoutView(interaction=_make_interaction())
        old_child = _Refreshable(interaction=_make_interaction())
        old_child._message = MagicMock()
        old_child._message.delete = AsyncMock()

        parent.attach_child(old_child)
        parent.stop()  # parent ends before child refresh fires

        new_child = _Refreshable(interaction=_make_interaction())
        old_child._reopen_factory = lambda: new_child
        new_child.send = AsyncMock()
        old_child.exit = AsyncMock()

        refresh_interaction = _make_interaction()
        await old_child._reopen_ephemeral(refresh_interaction)

        # Parent's list is untouched (cleanup will run on its own exit
        # path), but the back-pointer is cleared so the new child
        # doesn't think it's still parented.
        assert new_child._attached_to is None
        assert new_child not in parent._attached_children

    async def test_reopen_reparents_own_children_instead_of_deleting(self):
        """The reopening view's OWN attached children survive the handoff.

        exit() at the end of _reopen_ephemeral cascades into
        _cleanup_attached_children, which exits every still-tracked child
        with delete_message=True. Children must be re-parented onto the
        replacement first so the exit cascade finds nothing to delete.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        old_parent = _Refreshable(interaction=_make_interaction())
        old_parent._message = MagicMock()
        old_parent._message.delete = AsyncMock()
        old_parent._message.edit = AsyncMock()

        child = StatefulLayoutView(interaction=_make_interaction())
        old_parent.attach_child(child)

        new_parent = _Refreshable(interaction=_make_interaction())
        old_parent._reopen_factory = lambda: new_parent
        new_parent.send = AsyncMock()

        # exit() runs for real so the cascade is exercised; only the
        # child's exit is spied to prove it never fires.
        with patch.object(child, "exit", new=AsyncMock()) as child_exit:
            await old_parent._reopen_ephemeral(_make_interaction())
            child_exit.assert_not_awaited()

        assert child in new_parent._attached_children
        assert child._attached_to is new_parent
        assert old_parent._attached_children == []


# // ========================================( on_reopen_failure Hook )======================================== // #


class TestOnReopenFailure:
    """Verify that the on_reopen_failure hook fires when _reopen_ephemeral
    cannot construct a replacement view, and that the default implementation
    sends the class-level reopen_failure_message as an ephemeral.
    """

    async def test_factory_error_sends_reopen_failure_message(self):
        """When the reopen factory raises, the default on_reopen_failure
        should send reopen_failure_message as an ephemeral.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view._reopen_factory = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        refresh_interaction.response.send_message.assert_called_once_with(
            view.reopen_failure_message, ephemeral=True
        )

    async def test_factory_none_sends_session_ended(self):
        """When the reopen factory returns None, the default
        on_reopen_failure should send "This session has ended." and
        call exit().
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view._reopen_factory = lambda: None
        view.exit = AsyncMock()

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        refresh_interaction.response.send_message.assert_called_once_with(
            "This session has ended.", ephemeral=True
        )
        view.exit.assert_awaited_once()

    async def test_custom_message_attribute(self):
        """A subclass that overrides reopen_failure_message should see
        the custom text in the ephemeral response.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            reopen_failure_message = "Oops, try /settings again."

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view._reopen_factory = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        refresh_interaction.response.send_message.assert_called_once_with(
            "Oops, try /settings again.", ephemeral=True
        )

    async def test_hook_override_replaces_default(self):
        """A subclass that overrides on_reopen_failure should have its
        custom logic invoked instead of the default ephemeral send.
        """
        captured: list = []

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

            async def on_reopen_failure(self, interaction, error=None):
                captured.append(("factory_error" if error else "session_ended", str(error)))

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view._reopen_factory = lambda: (_ for _ in ()).throw(ValueError("test"))

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        assert len(captured) == 1
        assert captured[0][0] == "factory_error"
        # The default send_message should NOT have been called
        refresh_interaction.response.send_message.assert_not_called()


# // ========================================( Reopen Carries Identity )======================================== // #


class TestReopenCarriesIdentity:
    """A reopened ephemeral carries the navigation identity forward.

    Since the arm deadline rides the navigation chain, the view that reopens
    can be a mid-chain sub-view, not just a fresh root. _reopen_ephemeral must
    hand off the post-construction selection, nav stack, root-class accounting,
    reopen factory, undo/redo timeline, and the Back button, or the replacement
    comes back as a stateless root.
    """

    async def test_reopen_applies_nav_state_stack_and_root(self):
        """Post-construction selection, _nav_stack, and _instance_root_class
        transfer to the replacement before send() runs on_load.
        """

        class _NavCarry(StatefulLayoutView):
            auto_refresh_ephemeral = True

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._sel = "default"

            def get_nav_state(self):
                return {"sel": self._sel}

            def restore_nav_state(self, state):
                self._sel = state.get("sel", self._sel)

        entry = {"class_name": "Parent", "module": "m", "kwargs": {}, "view_state": {}}
        old = _NavCarry(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old._sel = "chosen"
        old._nav_stack = [entry]
        old._instance_root_class = "RootView"

        new = _NavCarry(interaction=_make_interaction())
        old._reopen_factory = lambda: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert new._sel == "chosen"
        assert new._nav_stack == [entry]
        assert new._instance_root_class == "RootView"

    async def test_reopen_carries_session_for_continuity(self):
        """The replacement rejoins the old view's session so shared_data
        survives the token-cliff swap instead of resetting to a fresh one.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old.session_id = "session-continuity-1"

        new = _Refreshable(interaction=_make_interaction())
        old._reopen_factory = lambda: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert new.session_id == "session-continuity-1"

    async def test_reopen_carries_participants(self):
        """A multi-user ephemeral keeps its participants across the reopen."""

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old._participants.add(42)
        old._participants.add(99)

        new = _Refreshable(interaction=_make_interaction())
        old._reopen_factory = lambda: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert 42 in new._participants
        assert 99 in new._participants

    async def test_reopen_carries_factory_for_next_cycle(self):
        """The replacement inherits the reopen factory so a second reopen
        does not fall back to the _init_kwargs path the factory replaced.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()

        new = _Refreshable(interaction=_make_interaction())
        factory = lambda: new
        old._reopen_factory = factory
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert new._reopen_factory is factory

    async def test_reopen_transfers_undo_redo_timeline(self):
        """The old view's undo/redo stacks dispatch onto the replacement's
        state row before the old row is destroyed at exit().
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()

        store = get_store()
        undo = [{"application_slots": {}, "shared_data": None}]
        redo = [{"application_slots": {}, "shared_data": None}]
        store.state.setdefault("views", {})[old.id] = {
            "undo_stack": undo,
            "redo_stack": redo,
        }
        try:
            new = _Refreshable(interaction=_make_interaction())
            old._reopen_factory = lambda: new
            new.send = AsyncMock()
            new.dispatch = AsyncMock()
            old.exit = AsyncMock()

            await old._reopen_ephemeral(_make_interaction())

            new.dispatch.assert_awaited_once()
            action_type, payload = new.dispatch.await_args.args
            assert action_type == "VIEW_UPDATED"
            assert payload == ActionCreators.view_updated(new.id, undo_stack=undo, redo_stack=redo)
        finally:
            store.state.get("views", {}).pop(old.id, None)

    async def test_reopen_readds_back_button_for_mid_chain_view(self):
        """A reopened view with a non-empty stack gets its Back button back --
        send() builds from build_ui/on_load, which never adds it.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True
            auto_back_button = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old._nav_stack = [{"class_name": "Parent", "module": "m", "kwargs": {}, "view_state": {}}]

        new = _Refreshable(interaction=_make_interaction())
        old._reopen_factory = lambda: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert getattr(new, "_auto_back_item", None) is not None

    async def test_reopen_root_view_adds_no_back_button(self):
        """A root-stack reopen adds no Back button -- there is nowhere to go."""

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True
            auto_back_button = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()

        new = _Refreshable(interaction=_make_interaction())
        old._reopen_factory = lambda: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert getattr(new, "_auto_back_item", None) is None


# // ========================================( Ephemeral Flag Reset On Send )======================================== // #


class TestEphemeralFlagResetOnSend:
    """send() sets _ephemeral to match the current send, so a reused instance
    does not carry a stale flag from an earlier ephemeral context.
    """

    async def test_non_ephemeral_send_clears_stale_flag(self):
        """A view carrying a stale _ephemeral=True, sent non-ephemerally,
        ends with the flag cleared -- otherwise its refreshes take the
        ephemeral no-op branch and exit misclassifies real 401s.
        """
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        view._ephemeral = True

        await view.send(ephemeral=False)

        assert view._ephemeral is False

    async def test_ephemeral_send_sets_flag(self):
        """The ephemeral path still sets the flag."""
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        assert view._ephemeral is False

        await view.send(ephemeral=True)

        assert view._ephemeral is True


class TestEphemeralRearmOnNavRollback:
    """A failed-navigation rollback re-arms the source's ephemeral refresh
    handoff -- cancelled in _navigate_to ahead of the deferred teardown -- so a
    recovered long-lived ephemeral source still swaps in its refresh button
    before the 900s token cliff. The re-schedule uses the original deadline, so
    it sleeps the remaining time rather than a fresh window.
    """

    @staticmethod
    def _capture_tasks(view):
        scheduled: list = []

        def _spy(coro):
            scheduled.append(coro)
            coro.close()  # discard without "never awaited" warning

        return scheduled, patch.object(view, "create_task", side_effect=_spy)

    async def test_rollback_reschedules_ephemeral_handoff(self):
        import time as _time

        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        # Engaged handoff: deadline stamped at send, button not yet armed.
        source._ephemeral = True
        source._ephemeral_arm_deadline = _time.monotonic() + 500
        source._refresh_armed = False

        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        scheduled, spy = self._capture_tasks(source)
        with spy:
            await source._rollback_navigation(new_view)

        assert len(scheduled) == 1  # the ephemeral handoff was re-scheduled

    async def test_rollback_skips_reschedule_without_handoff(self):
        # A source that never engaged the handoff (no deadline stamped) gets no
        # re-schedule on rollback.
        class _Source(RenderableLayoutView):
            pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))

        scheduled, spy = self._capture_tasks(source)
        with spy:
            await source._rollback_navigation(new_view)

        assert scheduled == []

    async def test_rollback_skips_reschedule_when_already_armed(self):
        import time as _time

        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._ephemeral = True
        source._ephemeral_arm_deadline = _time.monotonic() + 500
        source._refresh_armed = True  # already armed -> nothing to re-arm

        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        scheduled, spy = self._capture_tasks(source)
        with spy:
            await source._rollback_navigation(new_view)

        assert scheduled == []


class TestEphemeralHandoffOnNavSuccess:
    """A successful push carries the ephemeral arming deadline onto the
    destination and schedules its handoff timer at the post-commit seam in
    _settle_navigation -- the same guard shape as the rollback re-arm, but on
    the new view. The carried deadline (stamped once at the original send)
    means a mid-chain hop sleeps only the remaining time to the 900s token
    cliff, and the next hop's cancel_tasks reaps the prior destination's
    timer so the chain holds one live timer.
    """

    @staticmethod
    def _http_error(status=503):
        return discord.HTTPException(MagicMock(status=status), "boom")

    async def test_push_carries_deadline_and_schedules_handoff(self):
        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        class _Dest(RenderableLayoutView):
            async def on_state_changed(self, state):
                pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._ephemeral = True
        deadline = time.monotonic() + 500
        source._ephemeral_arm_deadline = deadline

        dest = await source.push(_Dest)
        try:
            # Carried, not recomputed: the token belongs to the original send.
            assert dest._ephemeral_arm_deadline == deadline
            assert dest.task_manager.get_task_count(dest.id) == 1
        finally:
            dest.task_manager.cancel_tasks(dest.id)

    async def test_second_hop_cancels_prior_destination_timer(self):
        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        class _Mid(RenderableLayoutView):
            async def on_state_changed(self, state):
                pass

        class _Deep(RenderableLayoutView):
            async def on_state_changed(self, state):
                pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._ephemeral = True
        source._ephemeral_arm_deadline = time.monotonic() + 500

        mid = await source.push(_Mid)
        mid_timer = next(iter(mid.task_manager._tasks[mid.id]))

        deep = await mid.push(_Deep)
        try:
            # The second hop's cancel_tasks(mid.id) reaps the first
            # destination's timer; only the new destination holds one.
            for _ in range(3):
                await asyncio.sleep(0)
            assert mid_timer.done()
            assert mid.task_manager.get_task_count(mid.id) == 0
            assert deep.task_manager.get_task_count(deep.id) == 1
        finally:
            deep.task_manager.cancel_tasks(deep.id)

    async def test_rollback_leaves_no_destination_timer(self):
        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        class _Dest(RenderableLayoutView):
            async def on_state_changed(self, state):
                pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._ephemeral = True
        source._ephemeral_arm_deadline = time.monotonic() + 500

        # Every edit endpoint fails -> _rollback_navigation.
        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=self._http_error())
        nav.response.defer = AsyncMock(side_effect=self._http_error())
        nav.edit_original_response = AsyncMock(side_effect=self._http_error())
        source._message.edit = AsyncMock(side_effect=self._http_error())

        dest = await source.push(_Dest, interaction=nav)
        try:
            # The deadline carried in the navigation batch, but the timer
            # never armed: scheduling is post-commit and the edit never
            # confirmed. The rollback re-armed the recovered source instead.
            assert dest._ephemeral_arm_deadline is not None
            assert dest.task_manager.get_task_count(dest.id) == 0
            assert source.task_manager.get_task_count(source.id) == 1
        finally:
            source.task_manager.cancel_tasks(source.id)
