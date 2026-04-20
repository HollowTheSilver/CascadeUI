"""Tests for session limiting: registry, enforcement, scope isolation, and cleanup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helpers import make_interaction as _make_interaction

from cascadeui.state.singleton import get_store
from cascadeui import InstanceLimitError
from cascadeui.views.view import StatefulView
from cascadeui.views.persistent import PersistentView


class _RaiseOnLimit:
    """Test helper: re-raise InstanceLimitError instead of auto-handling.

    The library's default ``on_instance_limit`` sends an ephemeral and
    returns ``None`` from ``send()``.  These legacy tests assert the
    raise behavior of the underlying enforcement, so they mix in this
    class to restore the pre-v2.2.0 propagation semantics.
    """

    async def on_instance_limit(self, error):
        raise error


# // ========================================( Default Behavior )======================================== // #


class TestNoLimitByDefault:
    """Views without instance_limit send without restriction."""
    async def test_unlimited_views(self):
        """Views with instance_limit=None should send without restriction."""
        store = get_store()

        class _UnlimitedView(StatefulView):
            pass

        for _ in range(5):
            view = _UnlimitedView(interaction=_make_interaction())
            await view.send()

        # All 5 should be registered
        assert len(store._active_views) == 5


# // ========================================( Replace Policy )======================================== // #


class TestReplacePolicy:
    """instance_policy='replace' exits the oldest view when the limit is exceeded."""

    async def test_replace_exits_oldest(self):
        """Second send() with instance_limit=1 should exit the first view."""

        class _LimitedView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        interaction = _make_interaction()
        view1 = _LimitedView(interaction=interaction)
        await view1.send()
        assert view1.id in get_store()._active_views

        view2 = _LimitedView(interaction=_make_interaction())
        with patch.object(view1, "exit", new_callable=AsyncMock) as mock_exit:
            await view2.send()
            mock_exit.assert_called_once()

        assert view2.id in get_store()._active_views

    async def test_limit_greater_than_one(self):
        """instance_limit=3 should allow 3 views, replace oldest on 4th."""

        class _ThreeView(StatefulView):
            instance_limit = 3
            instance_scope = "user_guild"

        store = get_store()
        views = []
        for _ in range(3):
            v = _ThreeView(interaction=_make_interaction())
            await v.send()
            views.append(v)

        assert (
            len(store._get_active_views(_ThreeView._class_session_key(), "user_guild:100:200")) == 3
        )

        # 4th view should replace the oldest
        v4 = _ThreeView(interaction=_make_interaction())
        with patch.object(views[0], "exit", new_callable=AsyncMock) as mock_exit:
            await v4.send()
            mock_exit.assert_called_once()


# // ========================================( Reject Policy )======================================== // #


class TestRejectPolicy:
    """instance_policy='reject' blocks new views via on_instance_limit."""
    async def test_reject_invokes_on_instance_limit(self):
        """Second send() with reject policy should auto-handle via
        on_instance_limit and return None instead of propagating
        InstanceLimitError to the caller. Custom UX is opt-in by
        overriding on_instance_limit on the view class.
        """
        captured: list[InstanceLimitError] = []

        class _RejectView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

            async def on_instance_limit(self, error):
                captured.append(error)

        view1 = _RejectView(interaction=_make_interaction())
        await view1.send()

        view2 = _RejectView(interaction=_make_interaction())
        result = await view2.send()

        assert result is None
        assert len(captured) == 1
        assert captured[0].view_type == "_RejectView"
        assert captured[0].limit == 1


# // ========================================( Scope Isolation )======================================== // #


class TestScopeIsolation:
    """Instance limits apply independently per scope (user, guild, user_guild, global)."""
    async def test_user_scope_isolates_users(self):
        """Two different users should each get their own instance with user scope."""

        class _UserView(StatefulView):
            instance_limit = 1
            instance_scope = "user"
            instance_policy = "reject"

        view_a = _UserView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view_a.send()

        # Different user should succeed
        view_b = _UserView(interaction=_make_interaction(user_id=2, guild_id=100))
        await view_b.send()

        assert view_a.id in get_store()._active_views
        assert view_b.id in get_store()._active_views

    async def test_guild_scope_shared(self):
        """Two users in the same guild share the limit with guild scope."""

        class _GuildView(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "guild"
            instance_policy = "reject"

        view_a = _GuildView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view_a.send()

        # Same guild, different user — should be rejected
        view_b = _GuildView(interaction=_make_interaction(user_id=2, guild_id=100))
        with pytest.raises(InstanceLimitError):
            await view_b.send()

    async def test_global_scope(self):
        """Any view of the same type shares the limit with global scope."""

        class _GlobalView(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "global"
            instance_policy = "reject"

        view_a = _GlobalView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view_a.send()

        # Completely different user and guild — should still be rejected
        view_b = _GlobalView(interaction=_make_interaction(user_id=2, guild_id=200))
        with pytest.raises(InstanceLimitError):
            await view_b.send()


# // ========================================( Persistent View Protection )======================================== // #


class TestPersistentProtection:
    """Non-persistent views cannot replace persistent views; persistent-to-persistent is allowed."""
    async def test_non_persistent_cannot_replace_persistent(self):
        """A non-persistent view should not be able to replace a persistent view."""

        class _PersistentPanel(PersistentView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _RegularView(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        # Manually place a persistent view into the index under _RegularView's
        # type key.  In practice this corresponds to a class hierarchy where the
        # same view name resolves to both a PersistentView and a StatefulView
        # (e.g. across module reloads or subclass overrides).
        store = get_store()
        pv = _PersistentPanel(interaction=_make_interaction(), persistence_key="test:panel")
        scope_key = "user_guild:100:200"
        store._active_views[pv.id] = pv
        store._instance_index[(_RegularView._class_session_key(), scope_key)] = [pv.id]

        # Non-persistent view trying to replace it should raise
        rv = _RegularView(interaction=_make_interaction())
        with pytest.raises(InstanceLimitError):
            await rv.send()

    async def test_persistent_can_replace_persistent(self):
        """A persistent view CAN replace another persistent view."""

        class _ReplacePanel(PersistentView):
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        pv1 = _ReplacePanel(interaction=_make_interaction(), persistence_key="test:p1")
        store._register_view(pv1)

        pv2 = _ReplacePanel(interaction=_make_interaction(), persistence_key="test:p2")
        with patch.object(pv1, "exit", new_callable=AsyncMock):
            await pv2.send()

        assert pv2.id in store._active_views


# // ========================================( Cleanup Paths )======================================== // #


class TestCleanupPaths:
    """exit, timeout, and replace all unregister the view from the instance index."""
    async def test_exit_unregisters(self):
        """exit() should remove the view from _active_views."""

        class _ExitView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        view = _ExitView(interaction=_make_interaction())
        await view.send()
        assert view.id in store._active_views

        await view.exit()
        assert view.id not in store._active_views

    async def test_on_timeout_unregisters(self):
        """on_timeout() should remove the view from _active_views."""

        class _TimeoutView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        view = _TimeoutView(interaction=_make_interaction())
        await view.send()
        assert view.id in store._active_views

        await view.on_timeout()
        assert view.id not in store._active_views

    async def test_navigate_to_unregisters(self):
        """_navigate_to() should remove the old view from _active_views."""

        class _NavView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _TargetView(StatefulView):
            pass

        store = get_store()
        view = _NavView(interaction=_make_interaction())
        await view.send()
        assert view.id in store._active_views

        await view.replace(_TargetView)
        assert view.id not in store._active_views


# // ========================================( Missing Identity )======================================== // #


class TestMissingIdentity:
    """Enforcement is skipped when scope identifiers are unavailable (e.g. DMs)."""
    async def test_dm_with_user_guild_scope_skips_enforcement(self):
        """DM (no guild_id) with instance_scope='user_guild' should skip enforcement."""

        class _DmView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        # Simulate DM: no guild
        interaction = _make_interaction(user_id=1)
        interaction.guild = None
        interaction.guild_id = None

        # Should succeed even though limit=1 and policy=reject, because scope can't resolve
        view1 = _DmView(interaction=interaction)
        await view1.send()

        interaction2 = _make_interaction(user_id=1)
        interaction2.guild = None
        interaction2.guild_id = None

        view2 = _DmView(interaction=interaction2)
        await view2.send()  # No error — enforcement skipped


# // ========================================( Navigation Chain Tracking )======================================== // #


class TestNavigationChainTracking:
    """Session limiting must track the entire push/pop navigation chain under
    the root view's class name so that sub-views count against the root's limit."""

    async def test_push_registers_subview_under_root(self):
        """After push(), the sub-view should be indexed under the root view's class name."""

        class _HubView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _SubView(StatefulView):
            async def on_state_changed(self, state):
                pass

        store = get_store()
        hub = _HubView(interaction=_make_interaction())
        await hub.send()
        assert len(store._get_active_views(_HubView._class_session_key(), "user_guild:100:200")) == 1

        # Push to sub-view — _navigate_to registers it, no send() needed
        sub = await hub.push(_SubView)

        # Sub-view should be tracked under _HubView, not _SubView
        assert len(store._get_active_views(_HubView._class_session_key(), "user_guild:100:200")) == 1
        assert len(store._get_active_views(_SubView._class_session_key(), "user_guild:100:200")) == 0

    async def test_second_command_replaces_subview(self):
        """A second root command should find and replace the active sub-view."""

        class _MenuView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _DetailView(StatefulView):
            async def on_state_changed(self, state):
                pass

        store = get_store()

        # First command: menu -> detail
        menu1 = _MenuView(interaction=_make_interaction())
        await menu1.send()
        detail = await menu1.push(_DetailView)

        # Second command: new menu should trigger replace on the detail view
        menu2 = _MenuView(interaction=_make_interaction())
        with patch.object(detail, "exit", new_callable=AsyncMock) as mock_exit:
            await menu2.send()
            mock_exit.assert_called_once()

    async def test_second_command_rejected_when_on_subview(self):
        """With reject policy, a second command should fail even when on a sub-view."""

        class _StrictHub(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        class _StrictSub(StatefulView):
            async def on_state_changed(self, state):
                pass

        store = get_store()
        hub = _StrictHub(interaction=_make_interaction())
        await hub.send()
        sub = await hub.push(_StrictSub)

        # Second command should be rejected
        hub2 = _StrictHub(interaction=_make_interaction())
        with pytest.raises(InstanceLimitError):
            await hub2.send()

    async def test_origin_chains_through_multiple_pushes(self):
        """Pushing A -> B -> C should track C under A's class name."""

        class _Root(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _Middle(StatefulView):
            async def on_state_changed(self, state):
                pass

        class _Deep(StatefulView):
            async def on_state_changed(self, state):
                pass

        store = get_store()
        root = _Root(interaction=_make_interaction())
        await root.send()

        mid = await root.push(_Middle)

        deep = await mid.push(_Deep)

        # Deep view should be tracked under _Root
        assert len(store._get_active_views(_Root._class_session_key(), "user_guild:100:200")) == 1
        assert deep._instance_root_class == _Root._class_session_key()

    async def test_pop_preserves_origin(self):
        """After pop(), the restored parent view should still carry the root origin."""

        class _PopRoot(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _PopChild(StatefulView):
            async def on_state_changed(self, state):
                pass

        store = get_store()
        root = _PopRoot(interaction=_make_interaction())
        await root.send()

        child = await root.push(_PopChild)

        # Pop back to root
        restored = await child.pop()

        # Restored view should be tracked under _PopRoot
        assert len(store._get_active_views(_PopRoot._class_session_key(), "user_guild:100:200")) == 1
        # The restored view IS a _PopRoot — origin is cleared back to None
        assert restored._instance_root_class is None

    async def test_replace_does_not_set_origin(self):
        """replace() is a one-way transition — should NOT propagate session origin."""

        class _SourceView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _DestView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

            async def on_state_changed(self, state):
                pass

        store = get_store()
        source = _SourceView(interaction=_make_interaction())
        await source.send()

        dest = await source.replace(_DestView)
        dest._message = MagicMock()
        await dest.send()

        # replace() is one-way — dest view should be independent, tracked under
        # its own class name with origin cleared.
        assert dest._instance_root_class is None
        assert (
            len(store._get_active_views(_DestView._class_session_key(), "user_guild:100:200")) == 1
        )
        assert (
            len(store._get_active_views(_SourceView._class_session_key(), "user_guild:100:200")) == 0
        )


# // ========================================( Participant Sessions )======================================== // #


class TestParticipantSessions:
    """Participant registration, index tracking, and cross-session limit enforcement."""

    # -- Registration --

    async def test_register_participant_adds_to_index(self):
        """Registering a participant should add their scope key to the session index."""

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        view = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await view.send()

        await view.register_participant(300)

        # Participant's scope key should be in the index
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:300:200")) == 1
        )
        assert 300 in view._participants

    async def test_register_participant_coerces_snowflake_object(self):
        """v3.0.0: register_participant accepts Member-shaped objects."""

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        view = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await view.send()

        member = MagicMock()
        member.id = 777
        await view.register_participant(member)
        assert 777 in view._participants

    async def test_register_participant_invalid_type_raises(self):
        """v3.0.0: register_participant rejects non-Snowflake input loudly."""

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        view = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await view.send()

        with pytest.raises(TypeError, match="Snowflake"):
            await view.register_participant("999")

    async def test_register_participant_skips_owner(self):
        """Registering the owner's own ID should be a no-op."""

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        view = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await view.send()

        await view.register_participant(100)

        # Owner is not in _participants (tracked via register_view instead)
        assert 100 not in view._participants
        # Only one entry under the owner's scope key (the view itself)
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:100:200")) == 1
        )

    # -- Cleanup --

    async def test_unregister_view_cleans_participant_keys(self):
        """unregister_view should remove all participant scope keys."""

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        view = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await view.send()
        await view.register_participant(300)
        await view.register_participant(400)

        # Both participant scope keys should exist
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:300:200")) == 1
        )
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:400:200")) == 1
        )

        store._unregister_view(view.id)

        # All scope keys gone (owner + both participants)
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:100:200")) == 0
        )
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:300:200")) == 0
        )
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:400:200")) == 0
        )

    async def test_unregister_participant_individual(self):
        """Removing one participant should leave others intact."""

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        view = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await view.send()
        await view.register_participant(300)
        await view.register_participant(400)

        view.unregister_participant(300)

        assert 300 not in view._participants
        assert 400 in view._participants
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:300:200")) == 0
        )
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:400:200")) == 1
        )

    # -- Blocking --

    async def test_participant_blocks_new_owner_session(self):
        """A user who is a participant in game 1 should be blocked from creating game 2."""

        class _GameView(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        # Game 1: owner=100, participant=300
        game1 = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await game1.send()
        await game1.register_participant(300)

        # User 300 tries to create their own game (as owner)
        game2 = _GameView(interaction=_make_interaction(user_id=300, guild_id=200))
        with pytest.raises(InstanceLimitError):
            await game2.send()

    async def test_participant_blocks_joining_another(self):
        """A participant in game 1 should be blocked from joining game 2.

        v3.0.0: ``register_participant`` returns ``False`` instead of
        raising ``InstanceLimitError``. The default ``on_instance_limit``
        hook fires from inside the call.
        """

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        store = get_store()
        # Game 1: owner=100, participant=300
        game1 = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await game1.send()
        assert await game1.register_participant(300) is True

        # Game 2: owner=400, tries to add participant=300
        game2 = _GameView(interaction=_make_interaction(user_id=400, guild_id=200))
        await game2.send()

        with patch.object(game2, "on_instance_limit", new_callable=AsyncMock) as mock_hook:
            result = await game2.register_participant(300)

        assert result is False
        mock_hook.assert_awaited_once()
        # The error passed to the hook should carry the blocked_user_id.
        error = mock_hook.await_args.args[0]
        assert isinstance(error, InstanceLimitError)
        assert error.blocked_user_id == 300


class TestParticipantLimit:
    """v3.0.0: ``participant_limit`` caps total users (owner + participants)
    in a single view instance. Distinct from ``instance_limit`` (per-user
    instance cap).
    """

    async def test_default_none_allows_unlimited(self):
        """Without participant_limit, registration is unbounded."""

        class _Lobby(StatefulView):
            pass  # no participant_limit

        view = _Lobby(interaction=_make_interaction(user_id=100))
        await view.send()
        for uid in range(200, 220):
            assert await view.register_participant(uid) is True
        assert len(view._participants) == 20

    async def test_hard_cap_rejects_overflow(self):
        """participant_limit=3 with an owner accepts 2 more, rejects the 3rd."""

        class _Lobby(StatefulView):
            participant_limit = 3

        view = _Lobby(interaction=_make_interaction(user_id=100))
        await view.send()
        assert await view.register_participant(200) is True
        assert await view.register_participant(300) is True
        # Owner (100) + 200 + 300 = 3, at cap
        assert await view.register_participant(400) is False
        assert 400 not in view._participants

    async def test_owner_counted_against_cap(self):
        """A view with user_id=None can hold the full cap as participants."""

        class _Lobby(StatefulView):
            participant_limit = 2

        view = _Lobby()  # no owner
        view._registered = True  # skip send()
        assert await view.register_participant(200) is True
        assert await view.register_participant(300) is True
        assert await view.register_participant(400) is False

    async def test_owner_register_is_noop(self):
        """register_participant with the owner's own ID returns True without adding."""

        class _Lobby(StatefulView):
            participant_limit = 2

        view = _Lobby(interaction=_make_interaction(user_id=100))
        await view.send()
        assert await view.register_participant(100) is True
        assert 100 not in view._participants

    async def test_overflow_fires_on_participant_limit_hook(self):
        """The on_participant_limit hook should fire on rejection."""

        class _Lobby(StatefulView):
            participant_limit = 1  # owner only, no participants allowed

        view = _Lobby(interaction=_make_interaction(user_id=100))
        await view.send()
        with patch.object(view, "on_participant_limit", new_callable=AsyncMock) as mock_hook:
            result = await view.register_participant(200)
        assert result is False
        mock_hook.assert_awaited_once()
        assert mock_hook.await_args.args[0] == 200

    async def test_hook_receives_interaction_kwarg(self):
        """The interaction kwarg should be forwarded to the hook."""

        class _Lobby(StatefulView):
            participant_limit = 1

        view = _Lobby(interaction=_make_interaction(user_id=100))
        await view.send()
        joiner_interaction = _make_interaction(user_id=200)
        with patch.object(view, "on_participant_limit", new_callable=AsyncMock) as mock_hook:
            await view.register_participant(200, interaction=joiner_interaction)
        mock_hook.assert_awaited_once()
        assert mock_hook.await_args.kwargs["interaction"] is joiner_interaction

    async def test_default_hook_sends_ephemeral(self):
        """Default on_participant_limit sends participant_limit_message ephemerally."""

        class _Lobby(StatefulView):
            participant_limit = 1
            participant_limit_message = "No room left."

        view = _Lobby(interaction=_make_interaction(user_id=100))
        await view.send()
        joiner = _make_interaction(user_id=200)
        await view.register_participant(200, interaction=joiner)
        joiner.response.send_message.assert_awaited_once_with("No room left.", ephemeral=True)

    async def test_default_hook_no_interaction_silent(self):
        """Without an interaction, the default hook returns silently."""

        class _Lobby(StatefulView):
            participant_limit = 1

        view = _Lobby(interaction=_make_interaction(user_id=100))
        await view.send()
        # Should not raise even with no interaction kwarg
        result = await view.register_participant(200)
        assert result is False

    async def test_auto_register_happy_path(self):
        """auto_register_participants=True claims every non-owner in allowed_users."""

        class _Lobby(StatefulView):
            auto_register_participants = True
            participant_limit = 4

        view = _Lobby(interaction=_make_interaction(user_id=100))
        view.allowed_users = {100, 200, 300}
        msg = await view.send()
        assert msg is not None
        assert view._participants == {200, 300}

    async def test_auto_register_owner_excluded(self):
        """The owner ID in allowed_users should be skipped, not registered."""

        class _Lobby(StatefulView):
            auto_register_participants = True

        view = _Lobby(interaction=_make_interaction(user_id=100))
        view.allowed_users = {100}
        await view.send()
        assert view._participants == set()

    async def test_auto_register_rollback_on_failure(self):
        """First rejection rolls back claimed participants and returns None."""

        class _Lobby(StatefulView):
            auto_register_participants = True
            participant_limit = 2  # owner + 1 participant max

        view = _Lobby(interaction=_make_interaction(user_id=100))
        view.allowed_users = {100, 200, 300}  # 200 fits, 300 overflows
        result = await view.send()
        assert result is None
        # Rollback: no participant should remain
        assert view._participants == set()
        # View should be unregistered from the active view registry
        assert view.id not in get_store()._active_views

    async def test_auto_register_rollback_cleans_state_tree(self):
        """Rollback dispatches VIEW_DESTROYED so the state tree has no ghost entries."""

        class _Lobby(StatefulView):
            auto_register_participants = True
            participant_limit = 2

        view = _Lobby(interaction=_make_interaction(user_id=100))
        view.allowed_users = {100, 200, 300}
        result = await view.send()
        assert result is None

        store = get_store()
        # No view entry should remain in the state tree
        assert view.id not in store.state.get("views", {})
        # Subscriber should be removed
        assert view.id not in store.subscribers

    async def test_instance_limit_checked_before_participant_limit(self):
        """Per-user session overflow takes priority over view capacity."""

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            participant_limit = 10  # plenty of room here

        game1 = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await game1.send()
        await game1.register_participant(300)

        game2 = _GameView(interaction=_make_interaction(user_id=400, guild_id=200))
        await game2.send()
        with patch.object(game2, "on_instance_limit", new_callable=AsyncMock) as session_hook:
            with patch.object(game2, "on_participant_limit", new_callable=AsyncMock) as cap_hook:
                result = await game2.register_participant(300)
        assert result is False
        session_hook.assert_awaited_once()
        cap_hook.assert_not_awaited()

    # -- Navigation propagation --

    async def test_navigate_propagates_participants(self):
        """push() should carry participants to the new view."""

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _SubView(StatefulView):
            instance_scope = "user_guild"

        store = get_store()
        root = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await root.send()
        await root.register_participant(300)

        child = await root.push(_SubView)

        assert 300 in child._participants
        # Participant scope key should exist under the child view
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:300:200")) == 1
        )

    async def test_replace_does_not_propagate_participants(self):
        """replace() should not carry participants to the destination view."""

        class _GameView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _ResultView(StatefulView):
            instance_scope = "user_guild"

        store = get_store()
        game = _GameView(interaction=_make_interaction(user_id=100, guild_id=200))
        await game.send()
        await game.register_participant(300)

        dest = await game.replace(_ResultView)

        assert 300 not in dest._participants
        # Participant's scope key should be cleaned up (game was unregistered)
        assert (
            len(store._get_active_views(_GameView._class_session_key(), "user_guild:300:200")) == 0
        )

    # -- Scope edge cases --

    async def test_guild_scope_skips_participant_index(self):
        """Guild scope uses guild_id only, so participant index entries are skipped."""

        class _GuildView(StatefulView):
            instance_limit = 1
            instance_scope = "guild"

        store = get_store()
        view = _GuildView(interaction=_make_interaction(user_id=100, guild_id=200))
        await view.send()
        await view.register_participant(300)

        # Participant is tracked on the view for propagation
        assert 300 in view._participants
        # But only one entry in the session index (the owner's, not duplicated)
        guild_views = store._get_active_views(_GuildView._class_session_key(), "guild:200")
        assert len(guild_views) == 1

    async def test_instance_limit_error_blocked_user_id(self):
        """InstanceLimitError should carry the blocked_user_id when raised by register_participant."""
        error = InstanceLimitError("TestView", 1, blocked_user_id=999)
        assert error.view_type == "TestView"
        assert error.limit == 1
        assert error.blocked_user_id == 999

    async def test_instance_limit_error_no_blocked_user_id(self):
        """InstanceLimitError from send() should have blocked_user_id=None (backwards compat)."""
        error = InstanceLimitError("TestView", 1)
        assert error.blocked_user_id is None


# // ========================================( Rejection Cleanup )======================================== // #


class TestRejectionCleanup:
    """Verify that rejected send() calls clean up all __init__-created resources."""

    async def test_session_reject_cleans_subscriber(self):
        """Session-limit rejection must unsubscribe the view created in __init__."""

        class _OneAtATime(StatefulView):
            instance_limit = 1
            instance_policy = "reject"
            instance_scope = "user_guild"

        store = get_store()
        v1 = _OneAtATime(interaction=_make_interaction(user_id=100, guild_id=200))
        await v1.send()
        assert v1.id in store.subscribers

        v2 = _OneAtATime(interaction=_make_interaction(user_id=100, guild_id=200))
        result = await v2.send()
        assert result is None
        # The rejected view's subscriber must be gone
        assert v2.id not in store.subscribers
        # The rejected view must NOT be in the state tree
        assert v2.id not in store.state.get("views", {})

    async def test_v2_auto_register_rollback_cleans_state_tree(self):
        """V2 layout.py rollback must dispatch VIEW_DESTROYED (mirrors base.py fix)."""
        from cascadeui.views.layout import StatefulLayoutView

        class _V2Game(StatefulLayoutView):
            auto_register_participants = True
            participant_limit = 2
            instance_scope = "user_guild"

        store = get_store()
        view = _V2Game(interaction=_make_interaction(user_id=100, guild_id=200))
        view.allowed_users = {100, 200, 300}
        result = await view.send()
        assert result is None

        # State tree must be clean
        assert view.id not in store.state.get("views", {})
        # Subscriber must be removed
        assert view.id not in store.subscribers


# // ========================================( Participant Replacement Protection )======================================== // #


class TestProtectAttached:
    """Tests for protect_attached and the on_replaced/replaced_message pair."""

    async def test_protect_blocks_replacement_when_participants_exist(self):
        """Views with protect_attached=True and active participants
        are excluded from replacement candidates, falling back to reject."""

        class _ProtectedGame(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = True

        store = get_store()
        game1 = _ProtectedGame(interaction=_make_interaction(user_id=1, guild_id=100))
        await game1.send()
        game1._participants.add(2)
        store._register_participant(game1, 2)

        game2 = _ProtectedGame(interaction=_make_interaction(user_id=1, guild_id=100))
        with pytest.raises(InstanceLimitError):
            await game2.send()

        # game1 must still be alive
        assert game1.id in store._active_views

    async def test_protect_allows_replacement_without_participants(self):
        """Views with protect_attached=True but no participants
        are still replaceable (the flag is a no-op without participants)."""

        class _ProtectedView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = True

        store = get_store()
        view1 = _ProtectedView(interaction=_make_interaction())
        await view1.send()

        view2 = _ProtectedView(interaction=_make_interaction())
        with patch.object(view1, "exit", new_callable=AsyncMock):
            await view2.send()

        assert view2.id in store._active_views

    async def test_protect_false_allows_replacement_with_participants(self):
        """Views with protect_attached=False allow replacement even
        when participants exist (opt-out of protection)."""

        class _UnprotectedGame(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = False

        store = get_store()
        game1 = _UnprotectedGame(interaction=_make_interaction(user_id=1, guild_id=100))
        await game1.send()
        game1._participants.add(2)
        store._register_participant(game1, 2)

        game2 = _UnprotectedGame(interaction=_make_interaction(user_id=1, guild_id=100))
        with patch.object(game1, "exit", new_callable=AsyncMock):
            await game2.send()

        assert game2.id in store._active_views

    async def test_on_replaced_fires_before_exit(self):
        """on_replaced is called on the old view before exit during replacement."""
        call_order = []

        class _TrackedView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = False

            async def on_replaced(self):
                call_order.append("on_replaced")

            async def exit(self, **kwargs):
                call_order.append("exit")
                await super().exit(**kwargs)

        view1 = _TrackedView(interaction=_make_interaction())
        await view1.send()

        view2 = _TrackedView(interaction=_make_interaction())
        await view2.send()

        assert call_order == ["on_replaced", "exit"]

    async def test_on_replaced_error_does_not_block_send(self):
        """Errors in on_replaced are logged but the new view still sends."""

        class _BrokenNotify(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = False

            async def on_replaced(self):
                raise RuntimeError("notification failed")

        view1 = _BrokenNotify(interaction=_make_interaction())
        await view1.send()

        view2 = _BrokenNotify(interaction=_make_interaction())
        result = await view2.send()

        assert result is not None
        assert view2.id in get_store()._active_views

    async def test_replaced_message_sends_to_channel(self):
        """Default on_replaced sends replaced_message to the channel
        when set and the view has participants."""

        class _NotifyGame(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = False
            replaced_message = "This game has been cancelled."

        store = get_store()
        game1 = _NotifyGame(interaction=_make_interaction(user_id=1, guild_id=100))
        await game1.send()
        game1._participants.add(2)
        store._register_participant(game1, 2)

        # Mock the channel.send on the old view's message
        channel_send = AsyncMock()
        game1._message = MagicMock()
        game1._message.channel.send = channel_send
        game1._message.delete = AsyncMock()

        game2 = _NotifyGame(interaction=_make_interaction(user_id=1, guild_id=100))
        await game2.send()

        channel_send.assert_called_once_with("This game has been cancelled.")

    async def test_replaced_message_skips_without_participants(self):
        """Default on_replaced does not send replaced_message when
        the view has no participants (single-user replacement)."""

        class _NotifyView(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = False
            replaced_message = "This should not be sent."

        view1 = _NotifyView(interaction=_make_interaction())
        await view1.send()

        channel_send = AsyncMock()
        view1._message = MagicMock()
        view1._message.channel.send = channel_send
        view1._message.delete = AsyncMock()

        view2 = _NotifyView(interaction=_make_interaction())
        await view2.send()

        channel_send.assert_not_called()

    async def test_protect_attached_validated_as_bool(self):
        """protect_attached must be a bool at class definition time."""
        with pytest.raises(ValueError, match="protect_attached"):

            class _BadProtect(StatefulView):
                protect_attached = "yes"

    async def test_default_is_true(self):
        """protect_attached defaults to True."""

        class _DefaultView(StatefulView):
            pass

        assert _DefaultView.protect_attached is True

    async def test_protect_blocks_replacement_when_attached_children_from_other_user(self):
        """Views with attached children belonging to a different user
        are excluded from replacement, same as participants."""

        class _Game(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = True

        store = get_store()
        game1 = _Game(interaction=_make_interaction(user_id=1, guild_id=100))
        await game1.send()

        # Attach a child owned by a different user
        child = StatefulView(interaction=_make_interaction(user_id=2))
        game1.attach_child(child)

        game2 = _Game(interaction=_make_interaction(user_id=1, guild_id=100))
        with pytest.raises(InstanceLimitError):
            await game2.send()

        assert game1.id in store._active_views

    async def test_protect_allows_replacement_when_attached_children_same_user(self):
        """Attached children belonging to the same user as the requester
        do not trigger protection -- the owner can replace their own stuff."""

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = True

        store = get_store()
        view1 = _View(interaction=_make_interaction(user_id=1, guild_id=100))
        await view1.send()

        # Attach a child owned by the SAME user
        child = StatefulView(interaction=_make_interaction(user_id=1))
        view1.attach_child(child)

        view2 = _View(interaction=_make_interaction(user_id=1, guild_id=100))
        with patch.object(view1, "exit", new_callable=AsyncMock):
            await view2.send()

        assert view2.id in store._active_views

    async def test_protect_blocks_with_mixed_participants_and_children(self):
        """Both participants and attached children from other users
        independently trigger protection."""

        class _Game(_RaiseOnLimit, StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = True

        store = get_store()
        game1 = _Game(interaction=_make_interaction(user_id=1, guild_id=100))
        await game1.send()

        # Attach a child from user 2 (no participants registered)
        child = StatefulView(interaction=_make_interaction(user_id=2))
        game1.attach_child(child)

        # Verify the child alone blocks replacement
        game2 = _Game(interaction=_make_interaction(user_id=1, guild_id=100))
        with pytest.raises(InstanceLimitError):
            await game2.send()

    async def test_protect_ignores_finished_attached_children(self):
        """Finished attached children do not count toward protection."""

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "replace"
            protect_attached = True

        store = get_store()
        view1 = _View(interaction=_make_interaction(user_id=1, guild_id=100))
        await view1.send()

        child = StatefulView(interaction=_make_interaction(user_id=2))
        view1.attach_child(child)
        child.stop()  # mark as finished

        view2 = _View(interaction=_make_interaction(user_id=1, guild_id=100))
        with patch.object(view1, "exit", new_callable=AsyncMock):
            await view2.send()

        assert view2.id in store._active_views


# // ========================================( Parent Kwarg Auto-Attach )======================================== // #


class TestParentKwarg:
    """Tests for the parent= constructor kwarg that auto-attaches on send."""

    async def test_parent_kwarg_attaches_on_send(self):
        """Passing parent= in the constructor auto-attaches after send."""
        parent = StatefulView(interaction=_make_interaction())
        await parent.send()

        child = StatefulView(interaction=_make_interaction(), parent=parent)
        await child.send()

        assert child in parent._attached_children
        assert child._attached_to is parent

    async def test_parent_kwarg_clears_pending_after_attach(self):
        """_pending_parent is cleared after successful send."""
        parent = StatefulView(interaction=_make_interaction())
        await parent.send()

        child = StatefulView(interaction=_make_interaction(), parent=parent)
        assert child._pending_parent is parent

        await child.send()
        assert child._pending_parent is None

    async def test_parent_kwarg_no_attach_on_instance_limit_reject(self):
        """When send() returns None due to instance_limit, no attach happens."""

        class _Limited(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        parent = StatefulView(interaction=_make_interaction())
        await parent.send()

        existing = _Limited(interaction=_make_interaction(user_id=1, guild_id=100))
        await existing.send()

        child = _Limited(
            interaction=_make_interaction(user_id=1, guild_id=100),
            parent=parent,
        )
        result = await child.send()

        assert result is None
        assert child not in parent._attached_children

    async def test_parent_kwarg_not_in_init_kwargs(self):
        """parent= is excluded from _init_kwargs (non-reconstructible)."""
        parent = StatefulView(interaction=_make_interaction())
        child = StatefulView(interaction=_make_interaction(), parent=parent)

        assert "parent" not in child._init_kwargs

    async def test_manual_attach_still_works(self):
        """attach_child() works as a standalone call without parent= kwarg."""
        parent = StatefulView(interaction=_make_interaction())
        await parent.send()

        child = StatefulView(interaction=_make_interaction())
        await child.send()
        parent.attach_child(child)

        assert child in parent._attached_children
        assert child._attached_to is parent

    async def test_reparent_on_attach_to_different_parent(self):
        """Attaching a child to a new parent detaches from the old one."""
        parent_a = StatefulView(interaction=_make_interaction())
        await parent_a.send()

        parent_b = StatefulView(interaction=_make_interaction())
        await parent_b.send()

        child = StatefulView(interaction=_make_interaction(), parent=parent_a)
        await child.send()

        assert child in parent_a._attached_children
        assert child._attached_to is parent_a

        parent_b.attach_child(child)

        assert child not in parent_a._attached_children
        assert child in parent_b._attached_children
        assert child._attached_to is parent_b

    async def test_reparent_does_not_duplicate(self):
        """Re-parenting leaves exactly one entry in the new parent's list."""
        parent_a = StatefulView(interaction=_make_interaction())
        await parent_a.send()

        parent_b = StatefulView(interaction=_make_interaction())
        await parent_b.send()

        child = StatefulView(interaction=_make_interaction(), parent=parent_a)
        await child.send()

        parent_b.attach_child(child)
        parent_b.attach_child(child)  # idempotent second call

        assert parent_b._attached_children.count(child) == 1
        assert len(parent_a._attached_children) == 0

    async def test_self_attachment_raises(self):
        """A view cannot attach itself as its own child."""
        view = StatefulView(interaction=_make_interaction())
        await view.send()

        with pytest.raises(ValueError, match="cannot attach itself"):
            view.attach_child(view)

    async def test_circular_attachment_raises(self):
        """Circular parent chains are rejected."""
        view_a = StatefulView(interaction=_make_interaction())
        await view_a.send()

        view_b = StatefulView(interaction=_make_interaction())
        await view_b.send()

        view_a.attach_child(view_b)

        with pytest.raises(ValueError, match="Circular attachment"):
            view_b.attach_child(view_a)

    async def test_circular_chain_three_deep(self):
        """Circular detection works across multi-hop chains."""
        a = StatefulView(interaction=_make_interaction())
        await a.send()
        b = StatefulView(interaction=_make_interaction())
        await b.send()
        c = StatefulView(interaction=_make_interaction())
        await c.send()

        a.attach_child(b)
        b.attach_child(c)

        with pytest.raises(ValueError, match="Circular attachment"):
            c.attach_child(a)

    async def test_parent_kwarg_invalid_type_raises(self):
        """parent= rejects non-view values at construction time."""
        with pytest.raises(TypeError, match="parent= must be"):
            StatefulView(interaction=_make_interaction(), parent="not_a_view")

    async def test_parent_kwarg_rejects_int(self):
        """parent= rejects int values (common mistake: passing a user ID)."""
        with pytest.raises(TypeError, match="parent= must be"):
            StatefulView(interaction=_make_interaction(), parent=12345)


# // ========================================( Participants Property )======================================== // #


class TestParticipantsProperty:
    """Public read-only ``participants`` property on ``_StatefulMixin``."""

    def test_empty_by_default(self):
        """New view starts with no participants."""
        view = StatefulView(interaction=_make_interaction())
        assert view.participants == frozenset()

    async def test_reflects_registered_participants(self):
        """Property reflects participants added via register_participant."""
        view = StatefulView(interaction=_make_interaction())
        await view.register_participant(999)
        assert 999 in view.participants
        assert isinstance(view.participants, frozenset)

    async def test_reflects_unregistered_participants(self):
        """Property updates after unregister_participant."""
        view = StatefulView(interaction=_make_interaction())
        await view.register_participant(999)
        view.unregister_participant(999)
        assert 999 not in view.participants

    def test_returns_frozenset(self):
        """Property returns frozenset, preventing accidental mutation."""
        view = StatefulView(interaction=_make_interaction())
        result = view.participants
        assert isinstance(result, frozenset)

    async def test_mutation_does_not_affect_internal_set(self):
        """Mutating the returned frozenset copy has no effect on the view."""
        view = StatefulView(interaction=_make_interaction())
        await view.register_participant(111)
        copy = view.participants
        # frozenset is immutable, but verify the internal set is untouched
        assert view.participants == frozenset({111})
