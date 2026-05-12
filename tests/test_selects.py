"""Tests for default-value coercion in the specialized select wrappers."""

# // ========================================( Modules )======================================== // #


from unittest.mock import MagicMock

import discord
import pytest

from cascadeui.components.selects import (
    ChannelSelect,
    MentionableSelect,
    RoleSelect,
    UserSelect,
    _wrap_default_values,
    _wrap_mentionable_defaults,
)

# // ========================================( Helpers )======================================== // #


def _snowflake(snowflake_id: int) -> MagicMock:
    """Build a minimal Snowflake-shaped mock with an ``int`` ``.id``."""
    obj = MagicMock()
    obj.id = snowflake_id
    return obj


def _ids_and_types(default_values):
    """Reduce a SelectDefaultValue list to ``[(id, type_str), ...]`` for asserting."""
    return [(dv.id, dv.type if isinstance(dv.type, str) else dv.type.name) for dv in default_values]


# // ========================================( _wrap_default_values )======================================== // #


class TestWrapDefaultValues:
    """_wrap_default_values handles ints, Snowflakes, and SelectDefaultValue."""

    def test_none_returns_empty_list(self):
        assert _wrap_default_values(None, "role") == []

    def test_empty_returns_empty_list(self):
        assert _wrap_default_values([], "role") == []

    def test_int_wrapped_with_type(self):
        out = _wrap_default_values([111, 222], "role")
        assert _ids_and_types(out) == [(111, "role"), (222, "role")]

    def test_snowflake_object_coerced(self):
        out = _wrap_default_values([_snowflake(333), 444], "user")
        assert _ids_and_types(out) == [(333, "user"), (444, "user")]

    def test_pre_built_passthrough(self):
        # Pre-built SelectDefaultValue passes through with its declared type
        existing = discord.SelectDefaultValue(id=999, type="user")
        out = _wrap_default_values([existing, 111], "role")
        # Existing kept its 'user' type even though default_type='role'
        assert _ids_and_types(out) == [(999, "user"), (111, "role")]

    def test_rejects_unrecognized_type(self):
        with pytest.raises(TypeError):
            _wrap_default_values(["not-a-snowflake"], "role")


# // ========================================( _wrap_mentionable_defaults )======================================== // #


class TestWrapMentionableDefaults:
    """_wrap_mentionable_defaults infers type from object class."""

    def test_none_returns_empty_list(self):
        assert _wrap_mentionable_defaults(None) == []

    def test_role_inferred_as_role_type(self):
        role = MagicMock(spec=discord.Role)
        role.id = 555
        out = _wrap_mentionable_defaults([role])
        assert _ids_and_types(out) == [(555, "role")]

    def test_member_inferred_as_user_type(self):
        member = MagicMock(spec=discord.Member)
        member.id = 666
        out = _wrap_mentionable_defaults([member])
        assert _ids_and_types(out) == [(666, "user")]

    def test_user_inferred_as_user_type(self):
        user = MagicMock(spec=discord.User)
        user.id = 777
        out = _wrap_mentionable_defaults([user])
        assert _ids_and_types(out) == [(777, "user")]

    def test_mixed_member_and_role(self):
        member = MagicMock(spec=discord.Member)
        member.id = 100
        role = MagicMock(spec=discord.Role)
        role.id = 200
        out = _wrap_mentionable_defaults([member, role])
        assert _ids_and_types(out) == [(100, "user"), (200, "role")]

    def test_pre_built_passthrough(self):
        existing = discord.SelectDefaultValue(id=999, type="role")
        out = _wrap_mentionable_defaults([existing])
        assert _ids_and_types(out) == [(999, "role")]

    def test_raw_int_rejected(self):
        # Type cannot be inferred from a bare int, so reject
        with pytest.raises(TypeError, match="Member, User, Role"):
            _wrap_mentionable_defaults([12345])


# // ========================================( RoleSelect )======================================== // #


class TestRoleSelect:
    """RoleSelect wires default_values through the role-typed coercion path."""

    def test_constructor_kwarg_coerces_ints(self):
        select = RoleSelect(default_values=[111, 222])
        assert _ids_and_types(select.default_values) == [(111, "role"), (222, "role")]

    def test_constructor_no_default_values(self):
        select = RoleSelect()
        assert list(select.default_values) == []

    def test_set_default_values_replaces(self):
        select = RoleSelect(default_values=[111])
        select.set_default_values([222, 333])
        assert _ids_and_types(select.default_values) == [(222, "role"), (333, "role")]

    def test_set_default_values_clears(self):
        select = RoleSelect(default_values=[111, 222])
        select.set_default_values([])
        assert list(select.default_values) == []

    def test_set_default_values_none_clears(self):
        select = RoleSelect(default_values=[111])
        select.set_default_values(None)
        assert list(select.default_values) == []

    def test_role_object_coerced(self):
        role = MagicMock(spec=discord.Role)
        role.id = 444
        select = RoleSelect(default_values=[role])
        assert _ids_and_types(select.default_values) == [(444, "role")]


# // ========================================( UserSelect )======================================== // #


class TestUserSelect:
    """UserSelect wires default_values through the user-typed coercion path."""

    def test_constructor_kwarg_coerces_ints(self):
        select = UserSelect(default_values=[111, 222])
        assert _ids_and_types(select.default_values) == [(111, "user"), (222, "user")]

    def test_set_default_values_replaces(self):
        select = UserSelect(default_values=[111])
        select.set_default_values([999])
        assert _ids_and_types(select.default_values) == [(999, "user")]

    def test_member_object_coerced(self):
        member = MagicMock(spec=discord.Member)
        member.id = 555
        select = UserSelect(default_values=[member])
        assert _ids_and_types(select.default_values) == [(555, "user")]


# // ========================================( ChannelSelect )======================================== // #


class TestChannelSelect:
    """ChannelSelect wires default_values through the channel-typed coercion path."""

    def test_constructor_kwarg_coerces_ints(self):
        select = ChannelSelect(default_values=[111, 222])
        assert _ids_and_types(select.default_values) == [(111, "channel"), (222, "channel")]

    def test_set_default_values_replaces(self):
        select = ChannelSelect(default_values=[111])
        select.set_default_values([777, 888])
        assert _ids_and_types(select.default_values) == [(777, "channel"), (888, "channel")]


# // ========================================( MentionableSelect )======================================== // #


class TestMentionableSelect:
    """MentionableSelect requires typed objects (no raw int IDs)."""

    def test_member_inferred_as_user(self):
        member = MagicMock(spec=discord.Member)
        member.id = 100
        select = MentionableSelect(default_values=[member])
        assert _ids_and_types(select.default_values) == [(100, "user")]

    def test_role_inferred_as_role(self):
        role = MagicMock(spec=discord.Role)
        role.id = 200
        select = MentionableSelect(default_values=[role])
        assert _ids_and_types(select.default_values) == [(200, "role")]

    def test_mixed_types(self):
        member = MagicMock(spec=discord.Member)
        member.id = 300
        role = MagicMock(spec=discord.Role)
        role.id = 400
        select = MentionableSelect(default_values=[member, role])
        assert _ids_and_types(select.default_values) == [(300, "user"), (400, "role")]

    def test_raw_int_rejected_at_constructor(self):
        with pytest.raises(TypeError, match="Member, User, Role"):
            MentionableSelect(default_values=[12345])

    def test_raw_int_rejected_at_set_default_values(self):
        select = MentionableSelect()
        with pytest.raises(TypeError, match="Member, User, Role"):
            select.set_default_values([12345])

    def test_pre_built_select_default_value_passthrough(self):
        # User who has bare IDs constructs SelectDefaultValue explicitly
        existing = discord.SelectDefaultValue(id=999, type="user")
        select = MentionableSelect(default_values=[existing])
        assert _ids_and_types(select.default_values) == [(999, "user")]
