"""Tests for interaction ownership: owner_only check on StatefulView."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cascadeui.views.base import StatefulView
from cascadeui.views.persistent import PersistentView
from cascadeui.state.singleton import get_store
from helpers import make_interaction as _make_interaction

# // ========================================( Owner Can Interact )======================================== // #


class TestOwnerCanInteract:
    async def test_owner_passes_check(self):
        """The user who created the view should pass interaction_check."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        owner_interaction = _make_interaction(user_id=100)

        result = await view.interaction_check(owner_interaction)
        assert result is True

    async def test_owner_no_ephemeral_sent(self):
        """Owner interactions should not trigger a rejection message."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        owner_interaction = _make_interaction(user_id=100)

        await view.interaction_check(owner_interaction)
        owner_interaction.response.send_message.assert_not_called()


# // ========================================( Non-Owner Rejected )======================================== // #


class TestNonOwnerRejected:
    async def test_non_owner_fails_check(self):
        """A different user should be rejected by interaction_check."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        other_interaction = _make_interaction(user_id=999)

        result = await view.interaction_check(other_interaction)
        assert result is False

    async def test_non_owner_gets_ephemeral(self):
        """Rejected users should receive an ephemeral message."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        other_interaction = _make_interaction(user_id=999)

        await view.interaction_check(other_interaction)
        other_interaction.response.send_message.assert_called_once_with(
            view.owner_only_message, ephemeral=True
        )

    async def test_custom_rejection_message(self):
        """Subclasses can customize the rejection message."""

        class _CustomView(StatefulView):
            owner_only_message = "Hands off!"

        view = _CustomView(interaction=_make_interaction(user_id=100))
        other_interaction = _make_interaction(user_id=999)

        await view.interaction_check(other_interaction)
        other_interaction.response.send_message.assert_called_once_with(
            "Hands off!", ephemeral=True
        )


# // ========================================( Opt-Out )======================================== // #


class TestOwnerOnlyOptOut:
    async def test_owner_only_false_allows_anyone(self):
        """Setting owner_only=False should let any user interact."""

        class _PublicView(StatefulView):
            owner_only = False

        view = _PublicView(interaction=_make_interaction(user_id=100))
        other_interaction = _make_interaction(user_id=999)

        result = await view.interaction_check(other_interaction)
        assert result is True

    async def test_opt_out_no_message_sent(self):
        """With owner_only=False, no rejection message should be sent."""

        class _PublicView(StatefulView):
            owner_only = False

        view = _PublicView(interaction=_make_interaction(user_id=100))
        other_interaction = _make_interaction(user_id=999)

        await view.interaction_check(other_interaction)
        other_interaction.response.send_message.assert_not_called()


# // ========================================( PersistentView Default )======================================== // #


class TestPersistentViewDefault:
    async def test_persistent_view_allows_anyone(self):
        """PersistentView defaults to owner_only=False."""

        class _Panel(PersistentView):
            pass

        assert _Panel.owner_only is False

    async def test_persistent_view_any_user_passes(self):
        """Any user should be able to interact with a PersistentView."""

        class _Panel(PersistentView):
            pass

        view = _Panel(
            interaction=_make_interaction(user_id=100),
            state_key="test_panel",
        )
        other_interaction = _make_interaction(user_id=999)

        result = await view.interaction_check(other_interaction)
        assert result is True


# // ========================================( None user_id )======================================== // #


class TestNoneUserId:
    async def test_none_user_id_skips_check(self):
        """Views with no user_id (e.g. restored) should skip ownership check."""
        view = StatefulView()  # No context or interaction — user_id is None
        assert view.user_id is None

        other_interaction = _make_interaction(user_id=999)
        result = await view.interaction_check(other_interaction)
        assert result is True

    async def test_none_user_id_no_message(self):
        """No rejection message when user_id is None."""
        view = StatefulView()
        other_interaction = _make_interaction(user_id=999)

        await view.interaction_check(other_interaction)
        other_interaction.response.send_message.assert_not_called()


# // ========================================( Allowed Users )======================================== // #


class TestAllowedUsers:
    async def test_allowed_user_passes(self):
        """A user in allowed_users should pass interaction_check."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        view.allowed_users = {100, 200}

        result = await view.interaction_check(_make_interaction(user_id=200))
        assert result is True

    async def test_non_allowed_user_rejected(self):
        """A user not in allowed_users should be rejected."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        view.allowed_users = {100, 200}

        result = await view.interaction_check(_make_interaction(user_id=999))
        assert result is False

    async def test_non_allowed_gets_ephemeral(self):
        """Rejected users should receive the owner_only_message."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        view.allowed_users = {100, 200}

        other = _make_interaction(user_id=999)
        await view.interaction_check(other)
        other.response.send_message.assert_called_once_with(view.owner_only_message, ephemeral=True)

    async def test_allowed_overrides_owner_only_true(self):
        """allowed_users takes priority over owner_only=True."""

        class _RestrictedView(StatefulView):
            owner_only = True  # Would normally reject user 200

        view = _RestrictedView(interaction=_make_interaction(user_id=100))
        view.allowed_users = {100, 200}

        # User 200 is not the owner, but IS in allowed_users — should pass
        result = await view.interaction_check(_make_interaction(user_id=200))
        assert result is True

    async def test_allowed_empty_set_rejects_all(self):
        """An empty set should reject everyone, including the owner."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        view.allowed_users = set()

        result = await view.interaction_check(_make_interaction(user_id=100))
        assert result is False

    async def test_allowed_none_falls_through_to_owner_only(self):
        """allowed_users=None should defer to the owner_only check."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        assert view.allowed_users is None

        # owner_only=True (default) should reject non-owner
        result = await view.interaction_check(_make_interaction(user_id=999))
        assert result is False

        # But allow the owner
        result = await view.interaction_check(_make_interaction(user_id=100))
        assert result is True

    async def test_allowed_users_runtime_mutation(self):
        """Adding a user to allowed_users at runtime should take effect."""
        view = StatefulView(interaction=_make_interaction(user_id=100))
        view.allowed_users = {100}

        # User 200 not allowed yet
        result = await view.interaction_check(_make_interaction(user_id=200))
        assert result is False

        # Add user 200
        view.allowed_users.add(200)
        result = await view.interaction_check(_make_interaction(user_id=200))
        assert result is True

    async def test_allowed_users_with_persistent_view(self):
        """allowed_users should work on PersistentView (owner_only=False by default)."""

        class _Panel(PersistentView):
            pass

        view = _Panel(
            interaction=_make_interaction(user_id=100),
            state_key="test_panel",
        )
        view.allowed_users = {100, 200}

        # User in set passes
        result = await view.interaction_check(_make_interaction(user_id=200))
        assert result is True

        # User not in set rejected
        result = await view.interaction_check(_make_interaction(user_id=999))
        assert result is False
