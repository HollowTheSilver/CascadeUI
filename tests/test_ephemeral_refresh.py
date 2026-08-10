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

import aiohttp
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

    async def test_public_re_send_stops_a_sleeping_timer_from_arming(self):
        """A timer asleep when the same instance is re-sent publicly must not
        arm when it wakes. The public re-send cancels nothing and clears
        nothing: the stale deadline stays (the non-ephemeral branch stamps
        nothing) and the stale True resolution keeps the effective handoff
        True, so the policy backstop would arm the reopen button over the
        live public panel. Only the flag (reassigned at every send) records
        that the managed message changed.

        The drain between the sends is load-bearing: it lets the timer take
        its first step and commit to its sleep while the instance still
        manages the ephemeral send. Without it, none of the second send's
        mocked awaits yields to the event loop, the timer first runs after
        the flag flipped, and the interleaving under test is never reached.
        """

        class _View(RenderableLayoutView):
            timeout = None  # first send resolves the handoff True
            refresh_warning_seconds = 899  # arm deadline = send + max(1, 900 - 899) = ~1s

        view = _View(interaction=_make_interaction())
        captured = []
        real_create = view.create_task

        def _capture(coro):
            task = real_create(coro)
            captured.append(task)
            return task

        with patch.object(view, "create_task", side_effect=_capture):
            await view.send(ephemeral=True)

        try:
            assert view._ephemeral is True
            assert view._refresh_handoff is True
            assert len(captured) == 1
            timer = captured[0]

            # First step: the timer passes the entry backstop (the policy is
            # engaged, the flag is True) and suspends inside its sleep.
            for _ in range(3):
                await asyncio.sleep(0)
            assert not timer.done(), "the entry backstop declined a timer the send engaged"

            view.interaction = _make_interaction()
            await view.send(ephemeral=False)

            # The re-send flipped only the flag. The stale resolution keeps
            # the effective policy True -- the exact value the policy
            # backstop reads -- so it cannot be the gate that declines.
            assert view._ephemeral is False
            assert view._refresh_handoff_resolved is True
            assert view._refresh_handoff is True
            assert view._ephemeral_arm_deadline is not None

            # The second send neither cancelled nor replaced the first
            # timer; it is still asleep with ~1s left on the original
            # deadline. A done timer here means the interleaving under test
            # was never reached.
            assert not timer.cancelled()
            assert not timer.done(), "harness: the timer woke before the second send finished"

            # Every guard other than the flag check is clear going into the
            # wake, so the flag check is the deciding gate.
            assert not view.is_finished()
            assert view._refresh_armed is False
            assert view._message is not None

            public_message = view._message
            try:
                await asyncio.wait_for(timer, timeout=30)
            except asyncio.TimeoutError:
                pytest.fail("the ephemeral refresh timer never woke from its sleep")

            # Still clear after the wake: nothing but the flag check could
            # have declined the arm, and no edit reached the public message.
            assert not view.is_finished()
            assert view._message is not None
            assert view._refresh_armed is False
            public_message.edit.assert_not_awaited()
        finally:
            view.task_manager.cancel_tasks(view.id)


class TestArmDeadlineStampedOnEveryEphemeralSend:
    """The arming deadline records the send token's 900s window (a fact
    about the message), so every ephemeral send stamps it. Whether the
    handoff timer runs stays ``auto_refresh_ephemeral``'s call: stamping by
    itself schedules nothing.
    """

    async def test_declined_send_stamps_deadline_without_scheduling(self):
        runs = []

        class _Declined(RenderableLayoutView):
            auto_refresh_ephemeral = False

            async def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _Declined(interaction=_make_interaction())
        before = time.monotonic()
        await view.send(ephemeral=True)
        after = time.monotonic()
        for _ in range(3):
            await asyncio.sleep(0)

        # 810 = 900 - refresh_warning_seconds (default 90), bracketed
        # between clock readings taken either side of the send.
        assert view._ephemeral_arm_deadline is not None
        assert before + 810 <= view._ephemeral_arm_deadline <= after + 810
        assert runs == []

    async def test_engaged_send_stamps_deadline_and_schedules(self):
        runs = []

        class _Engaged(RenderableLayoutView):
            auto_refresh_ephemeral = True

            async def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _Engaged(interaction=_make_interaction())
        await view.send(ephemeral=True)
        for _ in range(3):
            await asyncio.sleep(0)

        assert view._ephemeral_arm_deadline is not None
        assert runs == [True]

    async def test_non_ephemeral_send_stamps_nothing(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send(ephemeral=False)

        assert view._ephemeral_arm_deadline is None

    async def test_ephemeral_re_send_retires_the_first_sends_sleeping_timer(self):
        """A timer asleep across a same-instance ephemeral re-send must not
        arm against the first send's deadline: the re-send stamped a new
        token's clock and scheduled its own timer, so the first timer waking
        early would clear the new panel's children minutes before the new
        token needs the handoff and freeze state notifications from that
        moment. The liveness, flag, and policy gates are all clear here (the
        second send is ephemeral and engaged), so only the deadline
        comparison can retire the stale timer, and the second send's own
        timer must still arm on the new deadline.

        The drain between the sends is load-bearing: it lets the first timer
        capture the first deadline and commit to its sleep before the
        re-send stamps the new one. The two windows differ by construction
        (899 vs 898 warning seconds), so the mismatch does not depend on
        clock resolution.
        """

        class _View(RenderableLayoutView):
            timeout = None  # both sends resolve the handoff True
            refresh_warning_seconds = 899  # first arm deadline = send + ~1s

        view = _View(interaction=_make_interaction())
        captured = []
        real_create = view.create_task

        def _capture(coro):
            task = real_create(coro)
            captured.append(task)
            return task

        with patch.object(view, "create_task", side_effect=_capture):
            await view.send(ephemeral=True)
            first_deadline = view._ephemeral_arm_deadline

            # First step: the timer captures the first deadline and commits
            # to its sleep while it is still the current send's timer.
            for _ in range(3):
                await asyncio.sleep(0)
            assert len(captured) == 1
            assert not captured[0].done(), "the entry backstop declined a timer the send engaged"

            # Second ephemeral send on the same instance. The wider warning
            # window makes the re-stamped deadline structurally different
            # (send + ~2s), so timer identity is decided by construction.
            view.set_class_attribute("refresh_warning_seconds", 898)
            view.interaction = _make_interaction()
            await view.send(ephemeral=True)

        try:
            assert len(captured) == 2
            timer1, timer2 = captured

            second_deadline = view._ephemeral_arm_deadline
            assert first_deadline is not None
            assert second_deadline is not None
            assert second_deadline != first_deadline

            # The re-send neither cancelled nor woke the first timer.
            assert not timer1.cancelled()
            assert (
                not timer1.done()
            ), "harness: the first timer woke before the second send finished"

            # Liveness, flag, and policy are all clear going into the first
            # wake -- the second send is ephemeral with the handoff engaged --
            # so the deadline comparison is the deciding gate.
            assert not view.is_finished()
            assert view._refresh_armed is False
            assert view._message is not None
            assert view._ephemeral is True
            assert view._refresh_handoff is True

            second_message = view._message
            try:
                await asyncio.wait_for(timer1, timeout=30)
            except asyncio.TimeoutError:
                pytest.fail("the first send's timer never woke from its sleep")

            # Still clear after the wake: nothing but the deadline
            # comparison could have declined, and no early arm reached the
            # second send's message.
            assert not view.is_finished()
            assert view._ephemeral is True
            assert view._refresh_handoff is True
            assert view._refresh_armed is False
            second_message.edit.assert_not_awaited()

            # The second send's own timer still arms on the new deadline --
            # retiring the stale timer must not orphan the engaged handoff.
            try:
                await asyncio.wait_for(timer2, timeout=30)
            except asyncio.TimeoutError:
                pytest.fail("the second send's timer never woke from its sleep")
            assert view._refresh_armed is True
            assert second_message.edit.await_count == 1
        finally:
            view.task_manager.cancel_tasks(view.id)


# // ========================================( Declaration vs Resolution )======================================== // #


class TestDeclarationSeparateFromResolution:
    """``auto_refresh_ephemeral`` is the author's declaration and the library
    never assigns to it. The ``None`` sentinel resolves into the private
    ``_refresh_handoff_resolved`` at each ephemeral send, so introspecting
    the declaration always returns what the class or the caller set, and a
    later send re-derives instead of carrying a stale answer.
    """

    async def test_derived_engagement_leaves_declaration_unset(self):
        runs = []

        class _View(RenderableLayoutView):
            timeout = None

            async def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        for _ in range(3):
            await asyncio.sleep(0)

        assert view.auto_refresh_ephemeral is None
        assert view._refresh_handoff_resolved is True
        assert runs == [True]

    async def test_derived_decline_leaves_declaration_unset(self):
        runs = []

        class _View(RenderableLayoutView):
            timeout = 300

            async def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        for _ in range(3):
            await asyncio.sleep(0)

        assert view.auto_refresh_ephemeral is None
        assert view._refresh_handoff_resolved is False
        assert runs == []

    async def test_explicit_engagement_records_no_resolution(self):
        """An explicit pin is its own answer. The resolution field records
        only what the library decided, which for a pin is nothing.
        """

        class _On(RenderableLayoutView):
            auto_refresh_ephemeral = True
            timeout = 300

        view = _On(interaction=_make_interaction())
        await view.send(ephemeral=True)
        try:
            assert view.auto_refresh_ephemeral is True
            assert view._refresh_handoff_resolved is None
            assert view._refresh_handoff is True
        finally:
            view.task_manager.cancel_tasks(view.id)

    async def test_explicit_decline_records_no_resolution(self):
        class _Off(RenderableLayoutView):
            auto_refresh_ephemeral = False
            timeout = None

        view = _Off(interaction=_make_interaction())
        await view.send(ephemeral=True)

        assert view.auto_refresh_ephemeral is False
        assert view._refresh_handoff_resolved is None
        assert view._refresh_handoff is False

    async def test_non_ephemeral_send_resolves_nothing(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send(ephemeral=False)

        assert view.auto_refresh_ephemeral is None
        assert view._refresh_handoff_resolved is None
        assert view._refresh_handoff is None

    async def test_re_send_re_derives_from_the_current_timeout(self):
        """The resolution is per-send, not per-instance. A second ephemeral
        send derives against the timeout as it stands then, where writing
        the answer onto the declaration froze the first derivation forever.
        """
        runs = []

        class _View(RenderableLayoutView):
            timeout = None

            async def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        assert view._refresh_handoff_resolved is True

        view.timeout = 300
        view.interaction = _make_interaction()
        await view.send(ephemeral=True)
        for _ in range(3):
            await asyncio.sleep(0)

        assert view._refresh_handoff_resolved is False
        assert view.auto_refresh_ephemeral is None
        assert runs == [True]  # only the first send engaged

    async def test_second_send_decline_stops_a_sleeping_timer_from_arming(self):
        """A timer already asleep when a second send re-derives the handoff
        to False must not arm when it wakes. The declaration is still None
        at that point (only the resolution flipped), so a post-sleep check
        that reads ``auto_refresh_ephemeral`` finds None and arms a view
        whose effective policy is False; the check must read
        ``_refresh_handoff``.

        The drain between the sends is load-bearing: it lets the timer take
        its first step and commit to its sleep while the policy is still
        engaged. Without it, none of the second send's mocked awaits yields
        to the event loop, the timer first runs after the re-derivation, and
        the entry backstop (not the post-sleep one) decides, which turns
        this test into a false green for the branch it names.
        """

        class _View(RenderableLayoutView):
            timeout = None  # first send resolves the handoff True
            refresh_warning_seconds = 899  # arm deadline = send + max(1, 900 - 899) = ~1s

        view = _View(interaction=_make_interaction())
        captured = []
        real_create = view.create_task

        def _capture(coro):
            task = real_create(coro)
            captured.append(task)
            return task

        with patch.object(view, "create_task", side_effect=_capture):
            await view.send(ephemeral=True)

        try:
            assert view._refresh_handoff_resolved is True
            assert view._refresh_handoff is True
            assert len(captured) == 1
            timer = captured[0]

            # First step: the timer passes the entry backstop (the policy is
            # engaged) and suspends inside its sleep. A completed task here
            # would mean the entry backstop declined -- the wrong gate.
            for _ in range(3):
                await asyncio.sleep(0)
            assert not timer.done(), "the entry backstop declined a timer the send engaged"

            view.timeout = 300
            view.interaction = _make_interaction()
            await view.send(ephemeral=True)

            # The re-derivation flipped only the resolution. The unset
            # declaration is the exact value the pre-fix post-sleep check
            # read: None is not False, so it armed.
            assert view.auto_refresh_ephemeral is None
            assert view._refresh_handoff_resolved is False
            assert view._refresh_handoff is False

            # The second send neither cancelled nor replaced the first
            # timer; it is still asleep with ~1s left on the original
            # deadline. A done timer here means the interleaving under test
            # was never reached.
            assert not timer.cancelled()
            assert not timer.done(), "harness: the timer woke before the second send finished"

            # Every guard ahead of the policy check is clear going into the
            # wake, so the policy check is the deciding gate.
            assert not view.is_finished()
            assert view._refresh_armed is False
            assert view._message is not None

            try:
                await asyncio.wait_for(timer, timeout=30)
            except asyncio.TimeoutError:
                pytest.fail("the ephemeral refresh timer never woke from its sleep")

            # Still clear after the wake: every gate ahead of the policy
            # check passed, so the policy check declined first. The re-send
            # also restamped the deadline, so the identity comparison after
            # it would have declined too -- these assertions establish
            # first, not sole.
            assert not view.is_finished()
            assert view._message is not None
            assert view._refresh_armed is False
        finally:
            view.task_manager.cancel_tasks(view.id)

    async def test_a_pin_after_send_overrides_a_derived_engagement(self):
        """``set_class_attribute`` writes the declaration, and both of the
        timer's ``is False`` backstops read the declaration -- so a pin
        applied after send wins over the derived engagement without
        disturbing the resolution record.
        """

        class _View(RenderableLayoutView):
            timeout = None

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        view.task_manager.cancel_tasks(view.id)
        assert view.auto_refresh_ephemeral is None
        assert view._refresh_handoff is True

        view.set_class_attribute("auto_refresh_ephemeral", False)

        assert view._refresh_handoff is False
        assert view._refresh_handoff_resolved is True  # record intact
        view._ephemeral_arm_deadline = time.monotonic() + 500
        await view._schedule_ephemeral_refresh()  # entry backstop returns

        assert view._refresh_armed is False

    async def test_reopen_re_derives_on_the_replacement(self):
        """A reopen runs a fresh ``send()`` on a fresh token, so the
        replacement resolves from its own class declaration and timeout.
        The dying instance's resolution (here a chain-inherited decline)
        does not carry.
        """
        created = []

        class _View(RenderableLayoutView):
            timeout = None

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                created.append(self)

        old = _View(interaction=_make_interaction())
        old._ephemeral = True
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old._refresh_handoff_resolved = False  # as if inherited from a declined chain
        old.exit = AsyncMock()

        created.clear()
        await old._reopen_ephemeral(_make_interaction())

        assert len(created) == 1
        replacement = created[0]
        try:
            assert replacement.auto_refresh_ephemeral is None
            assert replacement._refresh_handoff_resolved is True
        finally:
            replacement.task_manager.cancel_tasks(replacement.id)


class TestEphemeralRearmOnNavRollback:
    """A failed-navigation rollback re-arms the source's ephemeral refresh
    handoff (cancelled in _navigate_to ahead of the deferred teardown), so a
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

    async def test_rollback_does_not_rearm_a_pinned_off_source(self):
        """The deadline is stamped for every ephemeral send, so its presence
        alone no longer implies the handoff engaged. A source that pinned the
        handoff off comes back from a rollback still pinned off.
        """

        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = False

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(ephemeral=True)
        assert source._ephemeral_arm_deadline is not None
        assert source._refresh_armed is False

        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        scheduled, spy = self._capture_tasks(source)
        with spy:
            await source._rollback_navigation(new_view)

        assert scheduled == []

    async def test_rollback_rearms_a_derived_engaged_source(self):
        """The rollback gate reads the effective policy, so a send whose
        ``None`` declaration derived "engaged" re-arms -- with the
        declaration itself still reading ``None``.
        """

        class _Source(RenderableLayoutView):
            timeout = None

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(ephemeral=True)
        # Mirror _navigate_to's pre-teardown cancel of the send-scheduled timer.
        source.task_manager.cancel_tasks(source.id)
        assert source.auto_refresh_ephemeral is None

        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        scheduled, spy = self._capture_tasks(source)
        with spy:
            await source._rollback_navigation(new_view)

        assert len(scheduled) == 1

    async def test_rollback_does_not_rearm_a_derived_declined_source(self):
        """The other polarity: a send whose ``None`` declaration derived
        "declined" comes back declined. The deadline is present (stamped at
        every ephemeral send), so only the resolved policy can say no here.
        """

        class _Source(RenderableLayoutView):
            timeout = 300

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(ephemeral=True)
        assert source.auto_refresh_ephemeral is None
        assert source._ephemeral_arm_deadline is not None
        assert source._refresh_armed is False

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


# // ========================================( Expired Token )======================================== // #


def _http_error(status: int) -> discord.HTTPException:
    """An HTTPException carrying a specific status, as Discord would raise."""
    return discord.HTTPException(MagicMock(status=status, reason="Error"), f"{status} Error")


class TestRefreshAbsorbsExpiredEphemeralToken:
    """An ephemeral past the 15-minute webhook cliff cannot be edited.

    ``exit()`` and ``on_timeout()`` both treat that 401 as expected
    lifecycle and log it at debug. ``refresh()`` re-raised it instead, so
    every state dispatch reaching such a view surfaced an ERROR and a
    traceback from the store's subscriber wrapper.
    """

    async def test_ephemeral_401_is_absorbed(self):
        view = RenderableLayoutView()
        view._ephemeral = True
        view._message = AsyncMock()
        view._message.edit.side_effect = _http_error(401)
        view._last_tree_digest = None

        await view.refresh()

        view._message.edit.assert_awaited_once()

    async def test_non_ephemeral_401_still_raises(self):
        """The guard is scoped to ephemerals. A 401 elsewhere is a real error."""
        view = RenderableLayoutView()
        view._ephemeral = False
        view._message = AsyncMock()
        view._message.edit.side_effect = _http_error(401)
        view._last_tree_digest = None

        with pytest.raises(discord.HTTPException):
            await view.refresh()

    async def test_ephemeral_non_401_still_raises(self):
        """Only token expiry is absorbed, not every failure on an ephemeral."""
        view = RenderableLayoutView()
        view._ephemeral = True
        view._message = AsyncMock()
        view._message.edit.side_effect = _http_error(500)
        view._last_tree_digest = None

        with pytest.raises(discord.HTTPException):
            await view.refresh()


class TestArmingRetriesWhileTheTokenLives:
    """A dropped arming edit keeps retrying, the way a rate-limited one does.

    ``_handle_rate_limit`` queues a successor, so a 429 during arming heals
    itself. A transport failure carries no retry-after and took a short fixed
    window instead, and the deferred render that fired on that window did not
    look at whether its own edit landed. Two consecutive drops inside the
    90-second arming budget therefore ended the chain and left the panel
    frozen with no refresh button on it.
    """

    async def test_a_second_dropped_arming_edit_queues_another_attempt(self):
        class _View(RenderableLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock(side_effect=aiohttp.ClientOSError(104, "reset"))
        view._refresh_armed = True

        queued = []
        view._queue_deferred_refresh = lambda wait: queued.append(wait)

        await view._deferred_refresh(0)

        assert queued == [5.0], "the armed branch must re-queue while the token can still edit"

    async def test_a_landed_arming_edit_ends_the_chain(self):
        class _View(RenderableLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._refresh_armed = True

        queued = []
        view._queue_deferred_refresh = lambda wait: queued.append(wait)

        await view._deferred_refresh(0)

        assert queued == []

    async def test_an_expired_token_ends_the_chain(self):
        """Past the cliff the edit answers 401, which does not set the flag."""

        class _View(RenderableLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._ephemeral = True
        view._message = MagicMock()
        view._message.edit = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=401), "expired")
        )
        view._refresh_armed = True

        queued = []
        view._queue_deferred_refresh = lambda wait: queued.append(wait)

        await view._deferred_refresh(0)

        assert queued == [], "a dead token must not spin a retry loop"


class _ArmedSource(RenderableLayoutView):
    """Ephemeral sender with the handoff engaged and the arming point ~1s out
    (``900 - 899``), so a destination timer fires within a short test sleep.
    """

    auto_refresh_ephemeral = True
    refresh_warning_seconds = 899


class _DeclinedSource(RenderableLayoutView):
    """Ephemeral sender with the handoff pinned off. The send still stamps
    the arming deadline -- the token clock is a fact about the message.
    """

    auto_refresh_ephemeral = False
    refresh_warning_seconds = 899


class TestHandoffPolicyAcrossNavigation:
    """``auto_refresh_ephemeral`` holds on navigation destinations in both
    polarities. The arming deadline is stamped at every ephemeral send (it
    records the message's token clock, not the policy), so the post-commit
    gate can honor the destination's own flag against it: an explicit
    ``False`` stays off even when the source armed, an explicit ``True``
    engages even when the source declined, and a ``None`` push destination
    inherits the immediate source's effective policy, carried on the
    private ``_refresh_handoff_resolved`` -- the declaration itself never
    changes. The inheritance is push-only; ``TestHandoffPolicyOnPop``
    covers the pop direction, where the restored view resumes its own
    resolution instead.
    """

    @staticmethod
    async def _push_to(source_cls, dest_cls):
        source = source_cls(interaction=_make_interaction(user_id=1), user_id=1, guild_id=2)
        await source.send(ephemeral=True)
        destination = await source.push(
            dest_cls, interaction=_make_interaction(user_id=1, guild_id=2)
        )
        destination._message = MagicMock()
        destination._message.edit = AsyncMock()
        return destination

    async def test_a_pinned_off_destination_is_not_armed_by_an_armed_source(self):
        armed = []

        class _PinnedOff(RenderableLayoutView):
            auto_refresh_ephemeral = False

            async def _arm_refresh_button(self):
                armed.append(True)

        dest = await self._push_to(_ArmedSource, _PinnedOff)
        assert dest._ephemeral_arm_deadline is not None
        await asyncio.sleep(1.3)

        assert armed == []

    async def test_a_pinned_on_destination_is_armed_by_an_armed_source(self):
        armed = []

        class _PinnedOn(RenderableLayoutView):
            auto_refresh_ephemeral = True

            async def _arm_refresh_button(self):
                armed.append(True)

        await self._push_to(_ArmedSource, _PinnedOn)
        await asyncio.sleep(1.3)

        assert armed == [True]

    async def test_an_unset_destination_inherits_an_armed_source(self):
        armed = []

        class _Derived(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        assert _Derived.auto_refresh_ephemeral is None
        dest = await self._push_to(_ArmedSource, _Derived)
        # The inherited decision rides the private resolution field so the
        # next hop and a rollback re-arm read policy, never deadline
        # presence. The declaration is the author's and stays untouched.
        assert dest.auto_refresh_ephemeral is None
        assert dest._refresh_handoff_resolved is True
        await asyncio.sleep(1.3)

        assert armed == [True]

    async def test_a_pinned_on_destination_is_armed_by_a_declined_source(self):
        """The other polarity of the pinned-off guarantee. Fails on any tree
        where a declined send skips the deadline stamp: there is then no
        clock for the destination's ``True`` to arm against.
        """
        armed = []

        class _PinnedOn(RenderableLayoutView):
            auto_refresh_ephemeral = True

            async def _arm_refresh_button(self):
                armed.append(True)

        dest = await self._push_to(_DeclinedSource, _PinnedOn)
        assert dest._ephemeral_arm_deadline is not None
        await asyncio.sleep(1.3)

        assert armed == [True]

    async def test_a_pinned_off_destination_stays_off_after_a_declined_source(self):
        armed = []

        class _PinnedOff(RenderableLayoutView):
            auto_refresh_ephemeral = False

            async def _arm_refresh_button(self):
                armed.append(True)

        dest = await self._push_to(_DeclinedSource, _PinnedOff)
        # The clock is carried even here: presence records the token window,
        # not the policy.
        assert dest._ephemeral_arm_deadline is not None
        await asyncio.sleep(1.3)

        assert armed == []

    async def test_an_unset_destination_inherits_a_declined_source(self):
        armed = []

        class _Derived(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        assert _Derived.auto_refresh_ephemeral is None
        dest = await self._push_to(_DeclinedSource, _Derived)
        assert dest._ephemeral_arm_deadline is not None
        assert dest.auto_refresh_ephemeral is None
        assert dest._refresh_handoff_resolved is False
        await asyncio.sleep(1.3)

        assert armed == []

    async def test_a_declined_decision_survives_two_unset_hops(self):
        """The carried resolution is what takes the decision past the first
        hop: a second ``None`` destination inherits from the first's
        ``_refresh_handoff_resolved``, while both declarations stay ``None``.
        Without the carry, the second hop would find no policy anywhere and
        fall back to the carried clock, arming a chain whose send declined.
        """
        armed = []

        class _Mid(RenderableLayoutView):
            pass

        class _Deep(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        mid = await self._push_to(_DeclinedSource, _Mid)
        deep = await mid.push(_Deep, interaction=_make_interaction(user_id=1, guild_id=2))
        deep._message = MagicMock()
        deep._message.edit = AsyncMock()
        assert mid.auto_refresh_ephemeral is None
        assert deep.auto_refresh_ephemeral is None
        assert deep._refresh_handoff_resolved is False
        await asyncio.sleep(1.3)

        assert armed == []

    async def test_an_engaged_decision_survives_two_unset_hops(self):
        """The engaged polarity of the two-hop carry. The mid view's own
        in-window ``timeout`` shows navigation inherits the source's
        resolution rather than re-deriving at the destination.
        """

        class _Src(RenderableLayoutView):
            timeout = None

        class _Mid(RenderableLayoutView):
            timeout = 300

        class _Deep(RenderableLayoutView):
            pass

        src = _Src(interaction=_make_interaction(user_id=1), user_id=1, guild_id=2)
        await src.send(ephemeral=True)
        mid = await src.push(_Mid, interaction=_make_interaction(user_id=1, guild_id=2))
        deep = await mid.push(_Deep, interaction=_make_interaction(user_id=1, guild_id=2))
        try:
            assert mid.auto_refresh_ephemeral is None
            assert deep.auto_refresh_ephemeral is None
            assert mid._refresh_handoff_resolved is True
            assert deep._refresh_handoff_resolved is True
            # The carried decision holds a live timer on the deepest hop.
            assert deep.task_manager.get_task_count(deep.id) == 1
        finally:
            deep.task_manager.cancel_tasks(deep.id)

    async def test_an_unresolved_chain_keeps_the_carried_deadline_behavior(self):
        """A deadline stamped outside the send pipeline (no resolved flag
        anywhere on the chain) keeps the presence behavior: with no policy
        to consult, the carried clock arms.
        """
        armed = []

        class _Derived(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        source = RenderableLayoutView(
            interaction=_make_interaction(user_id=1), user_id=1, guild_id=2
        )
        await source.send()
        source._message = MagicMock()
        source._message.edit = AsyncMock()
        source._ephemeral = True
        source._ephemeral_arm_deadline = time.monotonic() - 5
        dest = await source.push(_Derived, interaction=_make_interaction(user_id=1, guild_id=2))
        dest._message = MagicMock()
        dest._message.edit = AsyncMock()
        await asyncio.sleep(1.3)

        assert armed == [True]


# // ========================================( Handoff Policy on Pop )======================================== // #


class TestHandoffPolicyOnPop:
    """Policy inheritance flows down a navigation chain, never back up it.

    A push destination with no answer of its own adopts the source's
    effective policy; a popped-to view resumes the resolution it held when
    it was pushed away from, handed back through the nav-stack entry. The
    departing child's declaration therefore never reaches the restored
    view: a derived engagement survives a round trip through a pinned-off
    child, a pinned parent's declaration stands untouched in both
    polarities, and a view that held no policy comes back holding none.
    """

    @staticmethod
    async def _round_trip(root_cls, child_cls):
        root = root_cls(interaction=_make_interaction(user_id=1), user_id=1, guild_id=2)
        await root.send(ephemeral=True)
        child = await root.push(child_cls, interaction=_make_interaction(user_id=1, guild_id=2))
        child._message = MagicMock()
        child._message.edit = AsyncMock()
        popped = await child.pop(interaction=_make_interaction(user_id=1, guild_id=2))
        assert popped is not None
        popped._message = MagicMock()
        popped._message.edit = AsyncMock()
        return child, popped

    async def test_pop_from_a_pinned_off_child_resumes_a_derived_engagement(self):
        """The freezing shape: a root whose ``None`` declaration derived
        "engaged" at its ephemeral send pushes into a child pinning the
        handoff off, then pops back. The restored root resumes its own
        resolution (the child's ``False`` does not flow up), so the
        post-commit gate schedules its timer against the carried deadline
        rather than leaving the panel frozen at the token cliff.
        """
        armed = []

        class _Root(RenderableLayoutView):
            timeout = None  # the send derives the handoff engaged
            refresh_warning_seconds = 899  # arm deadline = send + ~1s

            async def _arm_refresh_button(self):
                armed.append(True)

        class _PinnedOffChild(RenderableLayoutView):
            auto_refresh_ephemeral = False

        _, popped = await self._round_trip(_Root, _PinnedOffChild)
        try:
            # The post-commit gate is the seam under test: it reads the
            # restored resolution, so its decision is visible before any
            # sleep -- declaration untouched, resumed resolution engaged,
            # timer scheduled.
            assert popped.auto_refresh_ephemeral is None
            assert popped._refresh_handoff_resolved is True
            assert popped._refresh_handoff is True
            assert popped.task_manager.get_task_count(popped.id) == 1

            await asyncio.sleep(1.3)
            assert armed == [True]
        finally:
            popped.task_manager.cancel_tasks(popped.id)

    async def test_pop_from_an_unset_child_keeps_the_engagement(self):
        """The control cell: an unset child inherits the engagement on the
        way down, and the pop hands the root back its own resolution. Both
        directions agree, so the round trip changes nothing.
        """
        armed = []

        class _Root(RenderableLayoutView):
            timeout = None
            refresh_warning_seconds = 899

            async def _arm_refresh_button(self):
                armed.append(True)

        class _UnsetChild(RenderableLayoutView):
            pass

        child, popped = await self._round_trip(_Root, _UnsetChild)
        try:
            assert child._refresh_handoff_resolved is True  # inherited on push
            assert popped._refresh_handoff_resolved is True  # resumed on pop
            assert popped.task_manager.get_task_count(popped.id) == 1

            await asyncio.sleep(1.3)
            assert armed == [True]
        finally:
            popped.task_manager.cancel_tasks(popped.id)

    async def test_a_pinned_on_parent_acquires_no_resolution_from_the_child(self):
        """A declared parent needs no hand-back (the declaration rides the
        class through reconstruction) and must not pick up a resolution
        record it never made. The restored view arms on its own ``True``
        with the resolution still unset.
        """
        armed = []

        class _Root(RenderableLayoutView):
            auto_refresh_ephemeral = True
            refresh_warning_seconds = 899

            async def _arm_refresh_button(self):
                armed.append(True)

        class _PinnedOffChild(RenderableLayoutView):
            auto_refresh_ephemeral = False

        _, popped = await self._round_trip(_Root, _PinnedOffChild)
        try:
            assert popped._refresh_handoff is True  # the declaration decides
            assert popped._refresh_handoff_resolved is None
            assert popped.task_manager.get_task_count(popped.id) == 1

            await asyncio.sleep(1.3)
            assert armed == [True]
        finally:
            popped.task_manager.cancel_tasks(popped.id)

    async def test_a_pinned_off_parent_stays_off_after_pop(self):
        """The other polarity: the parent's own ``False`` declaration
        decides at the post-commit gate, and the engaged child's ``True``
        does not flow up any more than a ``False`` does. No timer.
        """

        class _Root(RenderableLayoutView):
            auto_refresh_ephemeral = False
            refresh_warning_seconds = 899

        class _PinnedOnChild(RenderableLayoutView):
            auto_refresh_ephemeral = True

        _, popped = await self._round_trip(_Root, _PinnedOnChild)
        try:
            assert popped._refresh_handoff is False
            assert popped._refresh_handoff_resolved is None
            assert popped.task_manager.get_task_count(popped.id) == 0
        finally:
            popped.task_manager.cancel_tasks(popped.id)

    async def test_a_parent_with_no_policy_does_not_acquire_the_childs(self):
        """A chain whose deadline was stamped outside the send pipeline has
        no resolution anywhere, so the presence fallback governs it.
        Popping back from a pinned-off child leaves the restored view
        unresolved rather than adopting the child's ``False``, and the
        carried clock still arms.
        """
        armed = []

        class _Root(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        class _PinnedOffChild(RenderableLayoutView):
            auto_refresh_ephemeral = False

        root = _Root(interaction=_make_interaction(user_id=1), user_id=1, guild_id=2)
        await root.send()
        root._message = MagicMock()
        root._message.edit = AsyncMock()
        root._ephemeral = True
        root._ephemeral_arm_deadline = time.monotonic() + 0.5
        child = await root.push(
            _PinnedOffChild, interaction=_make_interaction(user_id=1, guild_id=2)
        )
        child._message = MagicMock()
        child._message.edit = AsyncMock()
        popped = await child.pop(interaction=_make_interaction(user_id=1, guild_id=2))
        assert popped is not None
        popped._message = MagicMock()
        popped._message.edit = AsyncMock()
        try:
            assert popped.auto_refresh_ephemeral is None
            assert popped._refresh_handoff_resolved is None
            assert popped.task_manager.get_task_count(popped.id) == 1

            await asyncio.sleep(1.3)
            assert armed == [True]
        finally:
            popped.task_manager.cancel_tasks(popped.id)
