"""Tests for auto_refresh_ephemeral: button arming, emoji validation, and
session-limit replace behavior on ephemeral views.

Covers the v2.2.0 fixes:
- Default refresh_button_emoji is a valid Discord button emoji
- _arm_refresh_button retries without the emoji if Discord rejects it (50035)
- Replace-policy session limiting deletes old ephemeral messages instead of
  freezing them
"""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from helpers import make_interaction as _make_interaction

from cascadeui import InstanceLimitError
from cascadeui.views.view import StatefulView
from cascadeui.views.layout import StatefulLayoutView

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
        view._message.delete = AsyncMock(
            side_effect=discord.NotFound(response, "Unknown Message")
        )

        with caplog.at_level(logging.ERROR, logger="cascadeui.views.base"):
            await view.exit()

        assert not any(
            "Error cleaning up message" in record.message for record in caplog.records
        )

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
            "Error cleaning up message" in r.message for r in caplog.records
            if r.levelname == "ERROR"
        )


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
