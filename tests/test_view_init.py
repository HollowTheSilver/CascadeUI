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


class TestInitKwargsAutoCapture:
    """Tests for the __init_subclass__ auto-capture of subclass kwargs."""

    async def test_subclass_kwargs_auto_captured(self):
        """Custom kwargs consumed by a subclass should appear in _init_kwargs."""

        class MyView(StatefulView):
            def __init__(self, *args, pages=None, title="default", **kwargs):
                self.pages = pages
                self.title = title
                super().__init__(*args, **kwargs)

        view = MyView(pages=["a", "b"], title="hello")
        assert view._init_kwargs["pages"] == ["a", "b"]
        assert view._init_kwargs["title"] == "hello"

    async def test_subclass_excludes_non_reconstructible(self):
        """Non-reconstructible kwargs (context, interaction, etc.) are excluded."""

        class MyView(StatefulView):
            def __init__(self, *args, label="btn", **kwargs):
                self.label = label
                super().__init__(*args, **kwargs)

        view = MyView(label="test", context=None, interaction=None, user_id=123)
        assert view._init_kwargs == {"label": "test"}
        assert "context" not in view._init_kwargs
        assert "interaction" not in view._init_kwargs
        assert "user_id" not in view._init_kwargs

    async def test_deep_subclass_captures_all_kwargs(self):
        """Kwargs from the most-derived class should be captured, even when
        intermediate classes consume some of them."""

        class MiddleView(StatefulView):
            def __init__(self, *args, pages=None, **kwargs):
                self.pages = pages
                super().__init__(*args, **kwargs)

        class LeafView(MiddleView):
            def __init__(self, *args, ticket_data=None, **kwargs):
                self.ticket_data = ticket_data
                super().__init__(*args, **kwargs)

        view = LeafView(pages=["p1"], ticket_data=["t1"], theme="dark")
        assert view._init_kwargs["pages"] == ["p1"]
        assert view._init_kwargs["ticket_data"] == ["t1"]
        assert view._init_kwargs["theme"] == "dark"

    async def test_subclass_without_init_still_works(self):
        """A subclass that doesn't define __init__ should still work."""

        class PlainView(StatefulView):
            pass

        view = PlainView(theme="light")
        # No wrapper ran, falls back to base class explicit capture
        assert view._init_kwargs == {"theme": "light"}

    async def test_base_class_kwargs_included_via_wrapper(self):
        """theme and state_key should be captured by the wrapper when using a subclass."""

        class MyView(StatefulView):
            def __init__(self, *args, custom="val", **kwargs):
                self.custom = custom
                super().__init__(*args, **kwargs)

        view = MyView(custom="x", theme="dark", state_key="key:1")
        assert view._init_kwargs == {"custom": "x", "theme": "dark", "state_key": "key:1"}


    async def test_positional_args_rejected(self):
        """Positional args cannot be captured for push/pop, so they raise immediately."""

        class BadView(StatefulView):
            def __init__(self, label, **kwargs):
                self.label = label
                super().__init__(**kwargs)

        with pytest.raises(TypeError, match="positional arguments"):
            BadView("hello")

    async def test_positional_args_as_kwargs_still_work(self):
        """The same parameter passed as a kwarg should work fine."""

        class OkView(StatefulView):
            def __init__(self, label="default", **kwargs):
                self.label = label
                super().__init__(**kwargs)

        view = OkView(label="hello")
        assert view._init_kwargs == {"label": "hello"}


class TestSubscribedActionsOverride:
    async def test_default_subscribed_actions(self):
        """Views without a class-level override should get the default set."""
        view = StatefulView()
        assert view.subscribed_actions == {
            "VIEW_DESTROYED", "SESSION_UPDATED",
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
            "VIEW_DESTROYED", "SESSION_UPDATED",
        }
