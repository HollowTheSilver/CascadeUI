"""Tests for PersistentLayoutView (V2 persistent views)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ui import LayoutView
from helpers import make_interaction as _make_interaction

from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.persistent import PersistentLayoutView, _persistent_view_classes


class TestPersistentLayoutViewInit:
    """Init, registry, and validation tests."""

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(PersistentLayoutView, StatefulLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(PersistentLayoutView, LayoutView)

    def test_subclass_auto_registered(self):
        class _TestV2Panel(PersistentLayoutView):
            pass

        key = _TestV2Panel._class_session_key()
        assert key in _persistent_view_classes
        assert _persistent_view_classes[key] is _TestV2Panel
        # Qualified path keeps unrelated cogs from colliding on bare class name.
        assert key.endswith("._TestV2Panel")

    def test_persistence_key_required(self):
        with pytest.raises(ValueError, match="persistence_key"):

            class _NoKeyV2(PersistentLayoutView):
                pass

            _NoKeyV2()

    def test_timeout_forced_to_none(self):
        class _TimeoutV2(PersistentLayoutView):
            pass

        view = _TimeoutV2(persistence_key="test:v2:timeout")
        assert view.timeout is None

    def test_owner_only_defaults_false(self):
        class _OwnerV2(PersistentLayoutView):
            pass

        view = _OwnerV2(persistence_key="test:v2:owner")
        assert view.owner_only is False

    def test_persistent_marker(self):
        class _MarkerV2(PersistentLayoutView):
            pass

        view = _MarkerV2(persistence_key="test:v2:marker")
        assert view._persistent is True


class TestPersistentLayoutViewSend:
    """Send method validation tests."""

    async def test_ephemeral_raises(self):
        class _EphV2(PersistentLayoutView):
            pass

        interaction = _make_interaction()
        view = _EphV2(interaction=interaction, persistence_key="test:v2:eph")

        with pytest.raises(ValueError, match="cannot be sent as ephemeral"):
            await view.send(ephemeral=True)
