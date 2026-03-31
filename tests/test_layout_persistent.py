"""Tests for PersistentLayoutView (V2 persistent views)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from discord.ui import LayoutView

from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.persistent import PersistentLayoutView, _persistent_view_classes
from helpers import make_interaction as _make_interaction


class TestPersistentLayoutViewInit:
    """Init, registry, and validation tests."""

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(PersistentLayoutView, StatefulLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(PersistentLayoutView, LayoutView)

    def test_subclass_auto_registered(self):
        class _TestV2Panel(PersistentLayoutView):
            pass

        assert "_TestV2Panel" in _persistent_view_classes
        assert _persistent_view_classes["_TestV2Panel"] is _TestV2Panel

    def test_state_key_required(self):
        with pytest.raises(ValueError, match="state_key"):
            class _NoKeyV2(PersistentLayoutView):
                pass

            _NoKeyV2()

    def test_timeout_forced_to_none(self):
        class _TimeoutV2(PersistentLayoutView):
            pass

        view = _TimeoutV2(state_key="test:v2:timeout")
        assert view.timeout is None

    def test_owner_only_defaults_false(self):
        class _OwnerV2(PersistentLayoutView):
            pass

        view = _OwnerV2(state_key="test:v2:owner")
        assert view.owner_only is False

    def test_persistent_marker(self):
        class _MarkerV2(PersistentLayoutView):
            pass

        view = _MarkerV2(state_key="test:v2:marker")
        assert view._persistent is True


class TestPersistentLayoutViewSend:
    """Send method validation tests."""

    async def test_ephemeral_raises(self):
        class _EphV2(PersistentLayoutView):
            pass

        interaction = _make_interaction()
        view = _EphV2(interaction=interaction, state_key="test:v2:eph")

        with pytest.raises(ValueError, match="cannot be sent as ephemeral"):
            await view.send(ephemeral=True)
