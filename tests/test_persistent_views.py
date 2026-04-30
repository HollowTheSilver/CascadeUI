"""Tests for persistent views: class registry, custom_id validation, and reducers."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from cascadeui.components.base import StatefulButton
from cascadeui.state.reducers import (
    reduce_persistent_view_registered,
    reduce_persistent_view_unregistered,
)
from cascadeui.views.base import _StatefulMixin
from cascadeui.views.persistent import (
    PersistentLayoutView,
    PersistentView,
    _persistent_view_classes,
    _PersistentMixin,
)


def make_action(action_type, payload, source=None):
    return {
        "type": action_type,
        "payload": payload,
        "source": source,
        "timestamp": datetime.now().isoformat(),
    }


def base_state():
    return {"sessions": {}, "views": {}, "components": {}, "application": {}}


# // ========================================( Registry )======================================== // #


class TestClassRegistry:
    """PersistentView subclasses auto-register in the class registry."""

    def test_subclass_auto_registered(self):
        """Subclassing PersistentView should auto-register the class."""

        class _TestPanel(PersistentView):
            pass

        key = _TestPanel._class_session_key()
        assert key in _persistent_view_classes
        assert _persistent_view_classes[key] is _TestPanel
        # Qualified path keeps unrelated cogs from colliding on bare class name.
        assert key.endswith("._TestPanel")

    def test_nested_subclass_registered(self):
        """Subclasses of subclasses should also be registered."""

        class _BasePanel(PersistentView):
            pass

        class _ChildPanel(_BasePanel):
            pass

        assert _ChildPanel._class_session_key() in _persistent_view_classes

    async def test_persistence_key_required(self):
        """PersistentView should raise ValueError without persistence_key."""

        class _NoKeyView(PersistentView):
            pass

        with pytest.raises(ValueError, match="persistence_key"):
            _NoKeyView()

    async def test_timeout_forced_to_none(self):
        """PersistentView should force timeout=None."""

        class _TimeoutView(PersistentView):
            pass

        view = _TimeoutView(persistence_key="test:timeout")
        assert view.timeout is None


# // ========================================( Validation )======================================== // #


class TestCustomIdValidation:
    """PersistentView validates that all interactive components have explicit custom_ids."""

    async def test_missing_custom_id_raises(self):
        """Components without custom_id should fail validation."""

        class _BadView(PersistentView):
            pass

        view = _BadView(persistence_key="test:bad")
        # Add a button without custom_id
        view.add_item(StatefulButton(label="No ID", callback=AsyncMock()))

        with pytest.raises(ValueError, match="custom_id"):
            view._validate_custom_ids()

    async def test_valid_custom_ids_pass(self):
        """Components with explicit custom_id should pass validation."""

        class _GoodView(PersistentView):
            pass

        view = _GoodView(persistence_key="test:good")
        view.add_item(
            StatefulButton(
                label="Has ID",
                custom_id="test:button",
                callback=AsyncMock(),
            )
        )

        # Should not raise
        view._validate_custom_ids()


# // ========================================( Reducers )======================================== // #


class TestPersistentViewReducers:
    """PERSISTENT_VIEW_REGISTERED and UNREGISTERED reducer behavior."""

    async def test_register_adds_entry(self):
        state = base_state()
        action = make_action(
            "PERSISTENT_VIEW_REGISTERED",
            {
                "persistence_key": "panel:main",
                "class_name": "RoleSelectorView",
                "message_id": "111",
                "channel_id": "222",
                "guild_id": "333",
                "user_id": "444",
            },
        )

        new_state = await reduce_persistent_view_registered(action, state)

        assert "persistent_views" in new_state
        entry = new_state["persistent_views"]["panel:main"]
        assert entry["class_name"] == "RoleSelectorView"
        assert entry["message_id"] == "111"
        assert entry["channel_id"] == "222"
        assert entry["guild_id"] == "333"
        assert entry["user_id"] == "444"

    async def test_register_overwrites_existing(self):
        state = base_state()
        state["persistent_views"] = {
            "panel:main": {"message_id": "old", "class_name": "Old"},
        }

        action = make_action(
            "PERSISTENT_VIEW_REGISTERED",
            {
                "persistence_key": "panel:main",
                "class_name": "New",
                "message_id": "new",
                "channel_id": "222",
            },
        )

        new_state = await reduce_persistent_view_registered(action, state)
        assert new_state["persistent_views"]["panel:main"]["message_id"] == "new"

    async def test_register_no_persistence_key_is_noop(self):
        state = base_state()
        action = make_action("PERSISTENT_VIEW_REGISTERED", {})

        new_state = await reduce_persistent_view_registered(action, state)
        assert new_state is state  # unchanged

    async def test_unregister_removes_entry(self):
        state = base_state()
        state["persistent_views"] = {
            "panel:main": {"message_id": "111"},
            "panel:other": {"message_id": "222"},
        }

        action = make_action(
            "PERSISTENT_VIEW_UNREGISTERED",
            {
                "persistence_key": "panel:main",
            },
        )

        new_state = await reduce_persistent_view_unregistered(action, state)
        assert "panel:main" not in new_state["persistent_views"]
        assert "panel:other" in new_state["persistent_views"]

    async def test_unregister_missing_key_is_noop(self):
        state = base_state()
        action = make_action(
            "PERSISTENT_VIEW_UNREGISTERED",
            {
                "persistence_key": "nonexistent",
            },
        )

        new_state = await reduce_persistent_view_unregistered(action, state)
        assert new_state is state


# // ========================================( Session Re-derivation )======================================== // #


class TestSessionRederivation:
    """Verify that session_id is re-derived when user_id is set after __init__."""

    def test_session_id_none_without_user_id(self):
        """PersistentView constructed with no user_id has no session."""

        class _Panel(PersistentView):
            pass

        view = _Panel(persistence_key="panel:test")
        assert view.session_id is None

    def test_session_id_derived_when_user_id_set_before_init(self):
        """PersistentView with user_id at construction gets a session."""

        class _Panel(PersistentView):
            pass

        view = _Panel(persistence_key="panel:test", user_id=12345)
        assert view.session_id is not None
        assert "user_12345" in view.session_id

    def test_late_user_id_assignment_needs_manual_rederivation(self):
        """Setting user_id after __init__ does NOT auto-derive session_id -- PersistenceMiddleware.initialize re-derives it explicitly during restore."""

        class _Panel(PersistentView):
            pass

        view = _Panel(persistence_key="panel:test")
        assert view.session_id is None

        # Simulate the restore path: set user_id + re-derive
        view.user_id = 12345
        assert view.session_id is None  # still None - no auto-derivation

        # Manual re-derivation (what the restore code does)
        if view.user_id and not view.session_id:
            view.session_id = f"{type(view)._class_session_key()}:user_{view.user_id}"
        assert view.session_id is not None
        assert "user_12345" in view.session_id


# // ========================================( Send Composition )======================================== // #


class TestSendComposition:
    """``send()`` must live on ``_PersistentMixin`` so composed persistent
    views (``_PersistentMixin + ConcreteLayoutView`` shape used by
    ``PersistentRolesLayoutView`` and ``PersistentLeaderboardLayoutView``)
    route through the mixin and dispatch ``PERSISTENT_VIEW_REGISTERED``.
    Locating ``send()`` on the leaf classes (``PersistentView`` /
    ``PersistentLayoutView``) caused composed subclasses to silently
    skip registration because their MRO bypassed the leaf override.
    """

    def test_persistent_layout_view_send_owned_by_mixin(self):
        owner = next(c for c in PersistentLayoutView.__mro__ if "send" in c.__dict__)
        assert (
            owner is _PersistentMixin
        ), f"PersistentLayoutView.send resolves to {owner.__name__}, expected _PersistentMixin"

    def test_persistent_view_send_owned_by_mixin(self):
        owner = next(c for c in PersistentView.__mro__ if "send" in c.__dict__)
        assert (
            owner is _PersistentMixin
        ), f"PersistentView.send resolves to {owner.__name__}, expected _PersistentMixin"

    def test_composed_pattern_send_owned_by_mixin(self):
        """Loaded lazily because the leaderboard / roles pattern modules
        register module-level state when imported."""
        from cascadeui.views.patterns.leaderboard import PersistentLeaderboardLayoutView
        from cascadeui.views.patterns.roles import PersistentRolesLayoutView

        for cls in (PersistentRolesLayoutView, PersistentLeaderboardLayoutView):
            owner = next(c for c in cls.__mro__ if "send" in c.__dict__)
            assert owner is _PersistentMixin, (
                f"{cls.__name__}.send resolves to {owner.__name__}, expected _PersistentMixin "
                "-- this means composed persistent views skip registration silently."
            )
