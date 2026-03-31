"""Tests for StatefulLayoutView (V2 base class)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from discord.ui import LayoutView

from cascadeui.state.singleton import get_store
from cascadeui.views.base import _StatefulMixin, _view_class_registry
from cascadeui.views.layout import StatefulLayoutView
from helpers import make_interaction as _make_interaction


class TestStatefulLayoutViewInit:
    """Basic init and inheritance tests."""

    def test_is_subclass_of_layout_view(self):
        assert issubclass(StatefulLayoutView, LayoutView)

    def test_is_subclass_of_mixin(self):
        assert issubclass(StatefulLayoutView, _StatefulMixin)

    def test_init_with_required_kwargs(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.state_store is not None
        assert view.user_id == 100
        assert view.guild_id == 200
        assert view.session_id == "StatefulLayoutView:user_100"

    def test_subscribes_to_state_on_init(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        store = get_store()
        assert view.id in store.subscribers

    def test_default_subscribed_actions(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.subscribed_actions == {"VIEW_DESTROYED", "SESSION_UPDATED"}

    def test_auto_defer_defaults(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.auto_defer is True
        assert view.auto_defer_delay == 2.5

    def test_owner_only_defaults(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.owner_only is True


class TestStatefulLayoutViewSubclass:
    """Subclass registration and kwargs auto-capture."""

    def test_subclass_registered_in_view_class_registry(self):
        class _TestLayoutPanel(StatefulLayoutView):
            pass

        assert "_TestLayoutPanel" in _view_class_registry
        assert _view_class_registry["_TestLayoutPanel"] is _TestLayoutPanel

    def test_init_kwargs_auto_captured(self):
        class _CustomLayout(StatefulLayoutView):
            def __init__(self, *args, title="default", **kwargs):
                self.title = title
                super().__init__(*args, **kwargs)

        interaction = _make_interaction()
        view = _CustomLayout(interaction=interaction, title="Dashboard")

        assert view.title == "Dashboard"
        assert view._init_kwargs == {"title": "Dashboard"}

    def test_non_reconstructible_kwargs_excluded(self):
        class _AnotherLayout(StatefulLayoutView):
            def __init__(self, *args, label="x", **kwargs):
                self.label = label
                super().__init__(*args, **kwargs)

        interaction = _make_interaction()
        view = _AnotherLayout(interaction=interaction, label="test")

        # interaction is non-reconstructible, should be excluded
        assert "interaction" not in view._init_kwargs
        assert view._init_kwargs == {"label": "test"}


class TestStatefulLayoutViewDispatch:
    """State dispatch and batch tests."""

    async def test_dispatch_forwards_to_store(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        result = await view.dispatch("VIEW_UPDATED", {"view_id": view.id})
        assert result is not None

    async def test_batch_returns_store_batch(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        batch = view.batch()
        assert batch is not None

    async def test_scoped_state_empty_without_scope(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.scoped_state == {}


class TestStatefulLayoutViewSend:
    """Send method tests."""

    async def test_send_via_interaction(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        message = await view.send()

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args
        assert call_kwargs.kwargs["view"] is view

    async def test_send_registers_view(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        await view.send()

        assert view.id in store._active_views

    async def test_send_no_content_embed_params(self):
        """V2 send() has no content/embed/embeds params."""
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        # Only ephemeral is accepted
        message = await view.send(ephemeral=False)
        assert message is not None

    async def test_send_rollback_on_failure(self):
        interaction = _make_interaction()
        interaction.response.send_message = AsyncMock(side_effect=Exception("fail"))
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        with pytest.raises(Exception, match="fail"):
            await view.send()

        assert view.id not in store._active_views

    async def test_send_requires_context_or_interaction(self):
        view = StatefulLayoutView()

        with pytest.raises(RuntimeError, match="requires either"):
            await view.send()


class TestStatefulLayoutViewInteraction:
    """Interaction check and owner_only tests."""

    async def test_owner_only_rejects_other_user(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)

        other_interaction = _make_interaction(user_id=999)
        result = await view.interaction_check(other_interaction)

        assert result is False

    async def test_owner_only_allows_owner(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)

        same_interaction = _make_interaction(user_id=100)
        result = await view.interaction_check(same_interaction)

        assert result is True

    async def test_owner_only_disabled(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)
        view.owner_only = False

        other_interaction = _make_interaction(user_id=999)
        result = await view.interaction_check(other_interaction)

        assert result is True


class TestStatefulLayoutViewCleanup:
    """Exit and cleanup tests."""

    async def test_exit_unregisters_view(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()
        store.register_view(view)

        assert view.id in store._active_views

        await view.exit()

        assert view.id not in store._active_views

    async def test_exit_unsubscribes(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        assert view.id in store.subscribers

        await view.exit()

        assert view.id not in store.subscribers
