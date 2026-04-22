"""Tests for StatefulView initialization: _init_kwargs capture and subscribed_actions override."""

from unittest.mock import MagicMock

import pytest

from cascadeui.views.view import StatefulView

# // ========================================( Snowflake Coercion )======================================== // #


class TestSnowflakeIdCoercion:
    """``user_id`` and ``guild_id`` accept ``int`` or any
    ``Snowflake``-shaped object and coerce silently. Invalid input
    raises ``TypeError`` at the call site instead of polluting the
    state store with non-int scope keys.
    """

    async def test_user_id_member_object_coerced(self):
        member = MagicMock()
        member.id = 99
        view = StatefulView(user_id=member)
        assert view.user_id == 99
        assert isinstance(view.user_id, int)

    async def test_guild_id_object_coerced(self):
        guild = MagicMock()
        guild.id = 555
        view = StatefulView(user_id=1, guild_id=guild)
        assert view.guild_id == 555

    async def test_user_id_string_raises_typeerror(self):
        with pytest.raises(TypeError, match="Snowflake"):
            StatefulView(user_id="123")


class TestInitKwargs:
    """_init_kwargs captures reconstructible kwargs and excludes internal keys."""
    async def test_captures_theme(self):
        """_init_kwargs should include theme when provided."""
        view = StatefulView(theme="dark")
        assert view._init_kwargs == {"theme": "dark"}

    async def test_captures_persistence_key(self):
        """_init_kwargs should include persistence_key when provided."""
        view = StatefulView(persistence_key="counter:user_1")
        assert view._init_kwargs == {"persistence_key": "counter:user_1"}

    async def test_captures_both(self):
        """_init_kwargs should include both theme and persistence_key."""
        view = StatefulView(theme="light", persistence_key="settings:main")
        assert view._init_kwargs == {"theme": "light", "persistence_key": "settings:main"}

    async def test_empty_when_no_reconstructible_kwargs(self):
        """_init_kwargs should be empty when no theme or persistence_key is given."""
        view = StatefulView()
        assert view._init_kwargs == {}

    async def test_excludes_internal_keys(self):
        """_init_kwargs should not contain session_id, user_id, guild_id, state_store."""
        view = StatefulView(
            session_id="sess_1",
            user_id=12345,
            guild_id=67890,
            theme="dark",
            persistence_key="test",
        )
        assert "session_id" not in view._init_kwargs
        assert "user_id" not in view._init_kwargs
        assert "guild_id" not in view._init_kwargs
        assert "state_store" not in view._init_kwargs
        assert view._init_kwargs == {"theme": "dark", "persistence_key": "test"}


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
        """theme and persistence_key should be captured by the wrapper when using a subclass."""

        class MyView(StatefulView):
            def __init__(self, *args, custom="val", **kwargs):
                self.custom = custom
                super().__init__(*args, **kwargs)

        view = MyView(custom="x", theme="dark", persistence_key="key:1")
        assert view._init_kwargs == {"custom": "x", "theme": "dark", "persistence_key": "key:1"}

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


class TestClassAttributeValidation:
    """``__init_subclass__`` validates subclass overrides of CascadeUI
    class attributes at class-definition time, so typos and type
    mistakes raise ``ValueError`` at import instead of failing silently
    or surfacing as confusing runtime errors.
    """

    def test_invalid_instance_policy_raises(self):
        with pytest.raises(ValueError, match="instance_policy"):

            class _Bad(StatefulView):
                instance_policy = "rejct"

    def test_invalid_instance_scope_raises(self):
        with pytest.raises(ValueError, match="instance_scope"):

            class _Bad(StatefulView):
                instance_scope = "user_only"

    def test_invalid_exit_policy_raises(self):
        with pytest.raises(ValueError, match="exit_policy"):

            class _Bad(StatefulView):
                exit_policy = "freeze"

    def test_instance_limit_string_raises(self):
        with pytest.raises(ValueError, match="instance_limit"):

            class _Bad(StatefulView):
                instance_limit = "5"

    def test_instance_limit_zero_raises(self):
        with pytest.raises(ValueError, match="instance_limit"):

            class _Bad(StatefulView):
                instance_limit = 0

    def test_instance_limit_none_allowed(self):
        class _Ok(StatefulView):
            instance_limit = None

        assert _Ok.instance_limit is None

    def test_owner_only_non_bool_raises(self):
        with pytest.raises(ValueError, match="owner_only"):

            class _Bad(StatefulView):
                owner_only = "yes"

    def test_auto_defer_delay_negative_raises(self):
        with pytest.raises(ValueError, match="auto_defer_delay"):

            class _Bad(StatefulView):
                auto_defer_delay = -1.0

    def test_subscribed_actions_non_set_raises(self):
        with pytest.raises(ValueError, match="subscribed_actions"):

            class _Bad(StatefulView):
                subscribed_actions = ["VIEW_DESTROYED"]

    def test_subscribed_actions_none_allowed(self):
        class _Ok(StatefulView):
            subscribed_actions = None

        assert _Ok.subscribed_actions is None

    def test_valid_overrides_accepted(self):
        class _Ok(StatefulView):
            instance_policy = "reject"
            instance_scope = "global"
            exit_policy = "delete"
            instance_limit = 3
            owner_only = False
            auto_defer_delay = 1.5
            subscribed_actions = {"FOO", "BAR"}

        assert _Ok.instance_limit == 3


class TestSetClassAttribute:
    """``set_class_attribute`` lets a view instance override a class-level
    policy attribute with a per-invocation value while running the same
    validator pipeline as ``__init_subclass__``. Reuses the lookup tables
    so the two paths cannot drift apart.
    """

    def test_valid_int_override_applied(self):
        view = StatefulView()
        view.set_class_attribute("participant_limit", 4)
        assert view.participant_limit == 4

    def test_valid_enum_override_applied(self):
        view = StatefulView()
        view.set_class_attribute("instance_policy", "reject")
        assert view.instance_policy == "reject"

    def test_valid_bool_override_applied(self):
        view = StatefulView()
        view.set_class_attribute("auto_register_participants", True)
        assert view.auto_register_participants is True

    def test_invalid_enum_value_raises(self):
        view = StatefulView()
        with pytest.raises(ValueError, match="instance_policy"):
            view.set_class_attribute("instance_policy", "rejct")

    def test_invalid_int_value_raises(self):
        view = StatefulView()
        with pytest.raises(ValueError, match="participant_limit"):
            view.set_class_attribute("participant_limit", 0)

    def test_int_attr_string_raises(self):
        view = StatefulView()
        with pytest.raises(ValueError, match="instance_limit"):
            view.set_class_attribute("instance_limit", "3")

    def test_bool_attr_int_raises(self):
        view = StatefulView()
        with pytest.raises(ValueError, match="owner_only"):
            view.set_class_attribute("owner_only", 1)

    def test_unknown_attribute_raises(self):
        view = StatefulView()
        with pytest.raises(ValueError, match="no attribute named"):
            view.set_class_attribute("not_a_real_attr", 7)

    def test_instance_data_attribute_rejected(self):
        view = StatefulView()
        with pytest.raises(ValueError, match="instance data"):
            view.set_class_attribute("user_id", 12345)

    def test_allowed_users_rejected_as_instance_data(self):
        view = StatefulView()
        with pytest.raises(ValueError, match="instance data"):
            view.set_class_attribute("allowed_users", frozenset({1, 2}))

    def test_freeform_message_attr_accepted_without_validation(self):
        view = StatefulView()
        view.set_class_attribute("participant_limit_message", "Lobby is closed.")
        assert view.participant_limit_message == "Lobby is closed."

    def test_int_attr_none_allowed(self):
        view = StatefulView()
        view.set_class_attribute("instance_limit", None)
        assert view.instance_limit is None

    def test_override_does_not_mutate_class(self):
        """Per-instance override must not bleed into the class default."""

        class _OkView(StatefulView):
            participant_limit = 2

        view = _OkView()
        view.set_class_attribute("participant_limit", 8)
        assert view.participant_limit == 8
        assert _OkView.participant_limit == 2


    def test_method_rejected(self):
        view = StatefulView()
        with pytest.raises(ValueError, match="method or property"):
            view.set_class_attribute("dispatch", "oops")

    def test_property_rejected(self):
        view = StatefulView()
        with pytest.raises(ValueError, match="method or property"):
            view.set_class_attribute("shared_data", {"bad": True})


class TestSubscribedActionsOverride:
    """Class-level subscribed_actions overrides control which actions notify the view."""
    async def test_default_subscribed_actions(self):
        """Views without a class-level override should get an empty set."""
        view = StatefulView()
        assert view.subscribed_actions == set()

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
        """A subclass that doesn't declare subscribed_actions gets an empty set."""

        class PlainSubclass(StatefulView):
            pass

        view = PlainSubclass()
        assert view.subscribed_actions == set()
