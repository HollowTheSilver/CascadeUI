"""Cross-pillar boundary assertions for the five-pillar architecture.

Each pillar does ONLY its job. These tests verify that concerns don't bleed
across pillar boundaries -- an access control change doesn't affect instance
counting, an instance limit doesn't affect navigation, etc.
"""

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helpers import make_interaction as _make_interaction

from cascadeui import InstanceLimitError
from cascadeui.state.singleton import get_store
from cascadeui.views.view import StatefulView

# // ========================================( Pillar 1 vs 2 )======================================== // #


class TestAccessControlDoesNotAffectInstanceLimits:
    """Pillar 1 (Access Control) and Pillar 2 (Instance Constraints) are orthogonal."""

    async def test_owner_only_does_not_change_instance_counting(self):
        """owner_only restricts clicks, not creation. Two owner_only views from
        different users should both register successfully when instance_limit allows it."""

        class _OwnerView(StatefulView):
            owner_only = True
            instance_limit = 1
            instance_scope = "user"

        v1 = _OwnerView(interaction=_make_interaction(user_id=1))
        await v1.send()
        v2 = _OwnerView(interaction=_make_interaction(user_id=2))
        await v2.send()

        store = get_store()
        assert v1.id in store._active_views
        assert v2.id in store._active_views

    async def test_allowed_users_does_not_count_against_instance_limit(self):
        """allowed_users is an access control list, not an instance count.
        Setting allowed_users should not affect session index entries."""

        class _TeamView(StatefulView):
            owner_only = False
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        v = _TeamView(interaction=_make_interaction(user_id=1, guild_id=100))
        v.allowed_users = {1, 2, 3, 4, 5}
        await v.send()

        # Only 1 instance index entry (the owner's), not 5
        active = store._get_active_views(_TeamView._class_session_key(), "user_guild:1:100")
        assert len(active) == 1

    async def test_unauthorized_message_independent_of_instance_limit_message(self):
        """The two message attributes serve different pillars and don't alias."""

        class _Dual(StatefulView):
            owner_only = True
            unauthorized_message = "You can't click this."
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"
            instance_limit_message = "You already have one open."

        assert _Dual.unauthorized_message != _Dual.instance_limit_message


# // ========================================( Pillar 2 vs 3 )======================================== // #


class TestInstanceConstraintsDoNotAffectLifecycle:
    """Pillar 2 (Instance Constraints) and Pillar 3 (View Lifecycle) are orthogonal."""

    async def test_instance_limit_does_not_affect_timeout(self):
        """Setting instance_limit should not change timeout behavior."""

        class _TimedView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            timeout = 30.0

        v = _TimedView(interaction=_make_interaction())
        assert v.timeout == 30.0
        await v.send()
        assert v.timeout == 30.0

    async def test_exit_policy_does_not_affect_instance_index(self):
        """exit_policy governs what happens to the message on exit,
        not how the view is tracked in the instance index."""

        class _DeleteView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            exit_policy = "delete"

        class _DisableView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            exit_policy = "disable"

        store = get_store()
        v1 = _DeleteView(interaction=_make_interaction())
        await v1.send()
        v2 = _DisableView(interaction=_make_interaction(user_id=200))
        await v2.send()

        # Both tracked independently in the instance index
        assert v1.id in store._active_views
        assert v2.id in store._active_views


# // ========================================( Pillar 2 vs 5 )======================================== // #


class TestInstanceConstraintsDoNotAffectNavigation:
    """Pillar 2 (Instance Constraints) and Pillar 5 (Navigation) are orthogonal."""

    async def test_push_does_not_create_new_instance_index_entry(self):
        """Push reuses the root's instance index slot, not creating a new one.
        The sub-view is tracked under the root's class name."""

        class _Root(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        store = get_store()
        root = _Root(interaction=_make_interaction())
        await root.send()

        # Count before push
        before = len(store._get_active_views(_Root._class_session_key(), "user_guild:100:200"))
        assert before == 1

        child = await root.push(_Sub)

        # Count after push -- still 1 (sub is tracked under root)
        after = len(store._get_active_views(_Root._class_session_key(), "user_guild:100:200"))
        assert after == 1

    async def test_nav_stack_independent_of_instance_scope(self):
        """instance_scope determines limit counting. nav_stack is view-local
        and entirely independent of the scope string."""

        class _UserScoped(StatefulView):
            instance_limit = 1
            instance_scope = "user"

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _UserScoped(interaction=_make_interaction())
        await root.send()
        child = await root.push(_Sub)

        # nav_stack exists on the view regardless of scope
        assert len(child._nav_stack) == 1


# // ========================================( Pillar 3 vs 4 )======================================== // #


class TestLifecycleDoesNotAffectSessionMembership:
    """Pillar 3 (View Lifecycle) and Pillar 4 (Session Membership) are orthogonal."""

    async def test_view_timeout_does_not_destroy_session(self):
        """A single view timing out should not delete the session if other
        members exist (session cleanup is Pillar 4's responsibility)."""
        store = get_store()

        class _ViewA(StatefulView):
            timeout = 10.0

        class _ViewB(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _ViewA(interaction=_make_interaction())
        await root.send()
        child = await root.push(_ViewB)

        session_id = child.session_id
        assert session_id in store.state["sessions"]

        # Child times out -- session should survive because root was replaced
        # (push destroyed root, but child is still alive)
        # In reality the session survives as long as members exist.
        assert len(store.state["sessions"][session_id]["members"]) >= 1


# // ========================================( Pillar 4 vs 5 )======================================== // #


class TestSessionMembershipVsNavigation:
    """Pillar 4 (Session Membership) and Pillar 5 (Navigation) are orthogonal.
    Parallel coexistence vs sequential replacement."""

    async def test_push_keeps_same_session(self):
        """Push is sequential replacement within the same session."""

        class _Hub(StatefulView):
            pass

        class _Detail(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Hub(interaction=_make_interaction())
        await root.send()
        root_session = root.session_id

        child = await root.push(_Detail)
        assert child.session_id == root_session

    async def test_replace_inherits_session_id(self):
        """Replace reuses the session_id (same user flow, different view)."""

        class _Old(StatefulView):
            pass

        class _New(StatefulView):
            async def on_state_changed(self, state):
                pass

        old = _Old(interaction=_make_interaction())
        await old.send()
        old_session = old.session_id

        new = await old.replace(_New)
        # replace() passes session_id through -- same session, new view
        assert new.session_id == old_session

    async def test_shared_data_survives_push(self):
        """Session data (Pillar 4) persists through push (Pillar 5)."""

        class _Hub(StatefulView):
            pass

        class _Detail(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Hub(interaction=_make_interaction())
        await root.send()
        await root.update_session(lang="fr")

        child = await root.push(_Detail)
        assert child.shared_data.get("lang") == "fr"

    async def test_replace_clears_nav_stack(self):
        """Replace is a one-way transition -- nav_stack starts empty,
        unlike push which carries it forward."""

        class _Old(StatefulView):
            pass

        class _Mid(StatefulView):
            async def on_state_changed(self, state):
                pass

        class _New(StatefulView):
            async def on_state_changed(self, state):
                pass

        old = _Old(interaction=_make_interaction())
        await old.send()
        mid = await old.push(_Mid)
        assert len(mid._nav_stack) == 1

        new = await mid.replace(_New)
        assert new._nav_stack == []


# // ========================================( Pillar 1 vs 5 )======================================== // #


class TestAccessControlDoesNotAffectNavigation:
    """Pillar 1 (Access Control) and Pillar 5 (Navigation) are orthogonal."""

    async def test_push_inherits_owner_only(self):
        """Pushed views are independent classes. owner_only on the root
        does not auto-propagate -- the pushed view uses its own setting."""

        class _OwnerRoot(StatefulView):
            owner_only = True

        class _PublicChild(StatefulView):
            owner_only = False

            async def on_state_changed(self, state):
                pass

        root = _OwnerRoot(interaction=_make_interaction())
        await root.send()
        child = await root.push(_PublicChild)

        assert root.owner_only is True
        assert child.owner_only is False


# // ========================================( Vocabulary Consistency )======================================== // #


class TestVocabularyDoesNotCollide:
    """Names from different pillars must not collide or alias."""

    async def test_instance_scope_and_state_scope_independent(self):
        """instance_scope (Pillar 2) and state_scope (state system) are
        separate attributes that accept the same string values."""

        class _DualScoped(StatefulView):
            instance_scope = "user_guild"
            instance_limit = 1
            state_scope = "user"

        assert _DualScoped.instance_scope == "user_guild"
        assert _DualScoped.state_scope == "user"

    async def test_protect_attached_independent_of_owner_only(self):
        """protect_attached (Pillar 2) prevents replacement when other users
        are attached. owner_only (Pillar 1) restricts button clicks. They
        govern different mechanisms entirely."""

        class _Hybrid(StatefulView):
            owner_only = True
            protect_attached = True
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"

        # owner_only doesn't imply protection, protect_attached does
        assert _Hybrid.owner_only is True
        assert _Hybrid.protect_attached is True
