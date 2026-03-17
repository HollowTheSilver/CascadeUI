"""Tests for StatefulView initialization: _init_kwargs capture and subscribed_actions override."""

import pytest

from cascadeui.views.base import StatefulView


class TestInitKwargs:
    async def test_captures_theme(self):
        """_init_kwargs should include theme when provided."""
        view = StatefulView(theme="dark")
        assert view._init_kwargs == {"theme": "dark"}

    async def test_captures_state_key(self):
        """_init_kwargs should include state_key when provided."""
        view = StatefulView(state_key="counter:user_1")
        assert view._init_kwargs == {"state_key": "counter:user_1"}

    async def test_captures_both(self):
        """_init_kwargs should include both theme and state_key."""
        view = StatefulView(theme="light", state_key="settings:main")
        assert view._init_kwargs == {"theme": "light", "state_key": "settings:main"}

    async def test_empty_when_no_reconstructible_kwargs(self):
        """_init_kwargs should be empty when no theme or state_key is given."""
        view = StatefulView()
        assert view._init_kwargs == {}

    async def test_excludes_internal_keys(self):
        """_init_kwargs should not contain session_id, user_id, guild_id, state_store."""
        view = StatefulView(
            session_id="sess_1", user_id=12345, guild_id=67890,
            theme="dark", state_key="test",
        )
        assert "session_id" not in view._init_kwargs
        assert "user_id" not in view._init_kwargs
        assert "guild_id" not in view._init_kwargs
        assert "state_store" not in view._init_kwargs
        assert view._init_kwargs == {"theme": "dark", "state_key": "test"}


class TestSubscribedActionsOverride:
    async def test_default_subscribed_actions(self):
        """Views without a class-level override should get the default set."""
        view = StatefulView()
        assert view.subscribed_actions == {
            "VIEW_UPDATED", "VIEW_DESTROYED",
            "COMPONENT_INTERACTION", "SESSION_UPDATED",
        }

    async def test_class_level_override_respected(self):
        """A subclass with subscribed_actions at class level should keep its set."""
        class CustomView(StatefulView):
            subscribed_actions = {"MY_CUSTOM_ACTION", "OTHER_ACTION"}

        view = CustomView()
        assert view.subscribed_actions == {"MY_CUSTOM_ACTION", "OTHER_ACTION"}

    async def test_none_override_means_all_actions(self):
        """Setting subscribed_actions = None at class level means receive all."""
        class AllActionsView(StatefulView):
            subscribed_actions = None

        view = AllActionsView()
        assert view.subscribed_actions is None

    async def test_subclass_without_override_gets_default(self):
        """A subclass that doesn't declare subscribed_actions gets the default."""
        class PlainSubclass(StatefulView):
            pass

        view = PlainSubclass()
        assert view.subscribed_actions == {
            "VIEW_UPDATED", "VIEW_DESTROYED",
            "COMPONENT_INTERACTION", "SESSION_UPDATED",
        }
