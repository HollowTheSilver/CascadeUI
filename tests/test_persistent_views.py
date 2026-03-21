"""Tests for persistent views: class registry, custom_id validation, and reducers."""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock

from cascadeui.state.reducers import (
    reduce_persistent_view_registered,
    reduce_persistent_view_unregistered,
)
from cascadeui.views.persistent import (
    PersistentView,
    _persistent_view_classes,
)
from cascadeui.components.base import StatefulButton


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
    def test_subclass_auto_registered(self):
        """Subclassing PersistentView should auto-register the class."""

        class _TestPanel(PersistentView):
            pass

        assert "_TestPanel" in _persistent_view_classes
        assert _persistent_view_classes["_TestPanel"] is _TestPanel

    def test_nested_subclass_registered(self):
        """Subclasses of subclasses should also be registered."""

        class _BasePanel(PersistentView):
            pass

        class _ChildPanel(_BasePanel):
            pass

        assert "_ChildPanel" in _persistent_view_classes

    async def test_state_key_required(self):
        """PersistentView should raise ValueError without state_key."""

        class _NoKeyView(PersistentView):
            pass

        with pytest.raises(ValueError, match="state_key"):
            _NoKeyView()

    async def test_timeout_forced_to_none(self):
        """PersistentView should force timeout=None."""

        class _TimeoutView(PersistentView):
            pass

        view = _TimeoutView(state_key="test:timeout")
        assert view.timeout is None


# // ========================================( Validation )======================================== // #


class TestCustomIdValidation:
    async def test_missing_custom_id_raises(self):
        """Components without custom_id should fail validation."""

        class _BadView(PersistentView):
            pass

        view = _BadView(state_key="test:bad")
        # Add a button without custom_id
        view.add_item(StatefulButton(label="No ID", callback=AsyncMock()))

        with pytest.raises(ValueError, match="custom_id"):
            view._validate_custom_ids()

    async def test_valid_custom_ids_pass(self):
        """Components with explicit custom_id should pass validation."""

        class _GoodView(PersistentView):
            pass

        view = _GoodView(state_key="test:good")
        view.add_item(StatefulButton(
            label="Has ID",
            custom_id="test:button",
            callback=AsyncMock(),
        ))

        # Should not raise
        view._validate_custom_ids()


# // ========================================( Reducers )======================================== // #


class TestPersistentViewReducers:
    async def test_register_adds_entry(self):
        state = base_state()
        action = make_action("PERSISTENT_VIEW_REGISTERED", {
            "state_key": "panel:main",
            "class_name": "RoleSelectorView",
            "message_id": "111",
            "channel_id": "222",
            "guild_id": "333",
            "user_id": "444",
        })

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

        action = make_action("PERSISTENT_VIEW_REGISTERED", {
            "state_key": "panel:main",
            "class_name": "New",
            "message_id": "new",
            "channel_id": "222",
        })

        new_state = await reduce_persistent_view_registered(action, state)
        assert new_state["persistent_views"]["panel:main"]["message_id"] == "new"

    async def test_register_no_state_key_is_noop(self):
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

        action = make_action("PERSISTENT_VIEW_UNREGISTERED", {
            "state_key": "panel:main",
        })

        new_state = await reduce_persistent_view_unregistered(action, state)
        assert "panel:main" not in new_state["persistent_views"]
        assert "panel:other" in new_state["persistent_views"]

    async def test_unregister_missing_key_is_noop(self):
        state = base_state()
        action = make_action("PERSISTENT_VIEW_UNREGISTERED", {
            "state_key": "nonexistent",
        })

        new_state = await reduce_persistent_view_unregistered(action, state)
        assert new_state is state
