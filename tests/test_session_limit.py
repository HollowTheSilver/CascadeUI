"""Tests for session limiting: registry, enforcement, scope isolation, and cleanup."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cascadeui.views.base import StatefulView, SessionLimitError
from cascadeui.views.persistent import PersistentView
from cascadeui.state.singleton import get_store


# Helper to create a view with a mock interaction so send() works
def _make_interaction(user_id=100, guild_id=200):
    interaction = AsyncMock()
    interaction.user = MagicMock(id=user_id)
    interaction.guild = MagicMock(id=guild_id)
    interaction.guild_id = guild_id
    # InteractionResponse.is_done() is sync in discord.py — use MagicMock
    # so the return value is a plain bool, not a coroutine.
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.original_response = AsyncMock(return_value=MagicMock(id=999, channel=MagicMock(id=888)))
    return interaction


# // ========================================( Default Behavior )======================================== // #


class TestNoLimitByDefault:
    async def test_unlimited_views(self):
        """Views with session_limit=None should send without restriction."""
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
    async def test_replace_exits_oldest(self):
        """Second send() with session_limit=1 should exit the first view."""

        class _LimitedView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

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
        """session_limit=3 should allow 3 views, replace oldest on 4th."""

        class _ThreeView(StatefulView):
            session_limit = 3
            session_scope = "user_guild"

        store = get_store()
        views = []
        for _ in range(3):
            v = _ThreeView(interaction=_make_interaction())
            await v.send()
            views.append(v)

        assert len(store.get_active_views("_ThreeView", "user_guild:100:200")) == 3

        # 4th view should replace the oldest
        v4 = _ThreeView(interaction=_make_interaction())
        with patch.object(views[0], "exit", new_callable=AsyncMock) as mock_exit:
            await v4.send()
            mock_exit.assert_called_once()


# // ========================================( Reject Policy )======================================== // #


class TestRejectPolicy:
    async def test_reject_raises_error(self):
        """Second send() with reject policy should raise SessionLimitError."""

        class _RejectView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"
            session_policy = "reject"

        view1 = _RejectView(interaction=_make_interaction())
        await view1.send()

        view2 = _RejectView(interaction=_make_interaction())
        with pytest.raises(SessionLimitError) as exc_info:
            await view2.send()

        assert exc_info.value.view_type == "_RejectView"
        assert exc_info.value.limit == 1


# // ========================================( Scope Isolation )======================================== // #


class TestScopeIsolation:
    async def test_user_scope_isolates_users(self):
        """Two different users should each get their own instance with user scope."""

        class _UserView(StatefulView):
            session_limit = 1
            session_scope = "user"
            session_policy = "reject"

        view_a = _UserView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view_a.send()

        # Different user should succeed
        view_b = _UserView(interaction=_make_interaction(user_id=2, guild_id=100))
        await view_b.send()

        assert view_a.id in get_store()._active_views
        assert view_b.id in get_store()._active_views

    async def test_guild_scope_shared(self):
        """Two users in the same guild share the limit with guild scope."""

        class _GuildView(StatefulView):
            session_limit = 1
            session_scope = "guild"
            session_policy = "reject"

        view_a = _GuildView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view_a.send()

        # Same guild, different user — should be rejected
        view_b = _GuildView(interaction=_make_interaction(user_id=2, guild_id=100))
        with pytest.raises(SessionLimitError):
            await view_b.send()

    async def test_global_scope(self):
        """Any view of the same type shares the limit with global scope."""

        class _GlobalView(StatefulView):
            session_limit = 1
            session_scope = "global"
            session_policy = "reject"

        view_a = _GlobalView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view_a.send()

        # Completely different user and guild — should still be rejected
        view_b = _GlobalView(interaction=_make_interaction(user_id=2, guild_id=200))
        with pytest.raises(SessionLimitError):
            await view_b.send()


# // ========================================( Persistent View Protection )======================================== // #


class TestPersistentProtection:
    async def test_non_persistent_cannot_replace_persistent(self):
        """A non-persistent view should not be able to replace a persistent view."""

        class _PersistentPanel(PersistentView):
            session_limit = 1
            session_scope = "user_guild"

        class _RegularView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

        # Manually place a persistent view into the index under _RegularView's
        # type key.  In practice this corresponds to a class hierarchy where the
        # same view name resolves to both a PersistentView and a StatefulView
        # (e.g. across module reloads or subclass overrides).
        store = get_store()
        pv = _PersistentPanel(interaction=_make_interaction(), state_key="test:panel")
        scope_key = "user_guild:100:200"
        store._active_views[pv.id] = pv
        store._session_index[("_RegularView", scope_key)] = [pv.id]

        # Non-persistent view trying to replace it should raise
        rv = _RegularView(interaction=_make_interaction())
        with pytest.raises(SessionLimitError):
            await rv.send()

    async def test_persistent_can_replace_persistent(self):
        """A persistent view CAN replace another persistent view."""

        class _ReplacePanel(PersistentView):
            session_limit = 1
            session_scope = "user_guild"

        store = get_store()
        pv1 = _ReplacePanel(interaction=_make_interaction(), state_key="test:p1")
        store.register_view(pv1)

        pv2 = _ReplacePanel(interaction=_make_interaction(), state_key="test:p2")
        with patch.object(pv1, "exit", new_callable=AsyncMock):
            await pv2.send()

        assert pv2.id in store._active_views


# // ========================================( Cleanup Paths )======================================== // #


class TestCleanupPaths:
    async def test_exit_unregisters(self):
        """exit() should remove the view from _active_views."""

        class _ExitView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

        store = get_store()
        view = _ExitView(interaction=_make_interaction())
        await view.send()
        assert view.id in store._active_views

        await view.exit()
        assert view.id not in store._active_views

    async def test_on_timeout_unregisters(self):
        """on_timeout() should remove the view from _active_views."""

        class _TimeoutView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

        store = get_store()
        view = _TimeoutView(interaction=_make_interaction())
        await view.send()
        assert view.id in store._active_views

        await view.on_timeout()
        assert view.id not in store._active_views

    async def test_navigate_to_unregisters(self):
        """_navigate_to() should remove the old view from _active_views."""

        class _NavView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

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
    async def test_dm_with_user_guild_scope_skips_enforcement(self):
        """DM (no guild_id) with session_scope='user_guild' should skip enforcement."""

        class _DmView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"
            session_policy = "reject"

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
            session_limit = 1
            session_scope = "user_guild"

        class _SubView(StatefulView):
            async def update_from_state(self, state):
                pass

        store = get_store()
        hub = _HubView(interaction=_make_interaction())
        await hub.send()
        assert len(store.get_active_views("_HubView", "user_guild:100:200")) == 1

        # Push to sub-view
        sub = await hub.push(_SubView)
        sub._message = MagicMock()
        await sub.send()

        # Sub-view should be tracked under _HubView, not _SubView
        assert len(store.get_active_views("_HubView", "user_guild:100:200")) == 1
        assert len(store.get_active_views("_SubView", "user_guild:100:200")) == 0

    async def test_second_command_replaces_subview(self):
        """A second root command should find and replace the active sub-view."""

        class _MenuView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

        class _DetailView(StatefulView):
            async def update_from_state(self, state):
                pass

        store = get_store()

        # First command: menu -> detail
        menu1 = _MenuView(interaction=_make_interaction())
        await menu1.send()
        detail = await menu1.push(_DetailView)
        detail._message = MagicMock()
        await detail.send()

        # Second command: new menu should trigger replace on the detail view
        menu2 = _MenuView(interaction=_make_interaction())
        with patch.object(detail, "exit", new_callable=AsyncMock) as mock_exit:
            await menu2.send()
            mock_exit.assert_called_once()

    async def test_second_command_rejected_when_on_subview(self):
        """With reject policy, a second command should fail even when on a sub-view."""

        class _StrictHub(StatefulView):
            session_limit = 1
            session_scope = "user_guild"
            session_policy = "reject"

        class _StrictSub(StatefulView):
            async def update_from_state(self, state):
                pass

        store = get_store()
        hub = _StrictHub(interaction=_make_interaction())
        await hub.send()
        sub = await hub.push(_StrictSub)
        sub._message = MagicMock()
        await sub.send()

        # Second command should be rejected
        hub2 = _StrictHub(interaction=_make_interaction())
        with pytest.raises(SessionLimitError):
            await hub2.send()

    async def test_origin_chains_through_multiple_pushes(self):
        """Pushing A -> B -> C should track C under A's class name."""

        class _Root(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

        class _Middle(StatefulView):
            async def update_from_state(self, state):
                pass

        class _Deep(StatefulView):
            async def update_from_state(self, state):
                pass

        store = get_store()
        root = _Root(interaction=_make_interaction())
        await root.send()

        mid = await root.push(_Middle)
        mid._message = MagicMock()
        await mid.send()

        deep = await mid.push(_Deep)
        deep._message = MagicMock()
        await deep.send()

        # Deep view should be tracked under _Root
        assert len(store.get_active_views("_Root", "user_guild:100:200")) == 1
        assert deep._session_origin == "_Root"

    async def test_pop_preserves_origin(self):
        """After pop(), the restored parent view should still carry the root origin."""

        class _PopRoot(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

        class _PopChild(StatefulView):
            async def update_from_state(self, state):
                pass

        store = get_store()
        root = _PopRoot(interaction=_make_interaction())
        await root.send()

        child = await root.push(_PopChild)
        child._message = MagicMock()
        await child.send()

        # Pop back to root
        restored = await child.pop()
        restored._message = MagicMock()
        await restored.send()

        # Restored view should be tracked under _PopRoot
        assert len(store.get_active_views("_PopRoot", "user_guild:100:200")) == 1
        # The restored view IS a _PopRoot — origin is cleared back to None
        assert restored._session_origin is None

    async def test_replace_does_not_set_origin(self):
        """replace() is a one-way transition — should NOT propagate session origin."""

        class _SourceView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

        class _DestView(StatefulView):
            session_limit = 1
            session_scope = "user_guild"

            async def update_from_state(self, state):
                pass

        store = get_store()
        source = _SourceView(interaction=_make_interaction())
        await source.send()

        dest = await source.replace(_DestView)
        dest._message = MagicMock()
        await dest.send()

        # replace() is one-way — dest view should be independent, tracked under
        # its own class name with origin cleared.
        assert dest._session_origin is None
        assert len(store.get_active_views("_DestView", "user_guild:100:200")) == 1
        assert len(store.get_active_views("_SourceView", "user_guild:100:200")) == 0
