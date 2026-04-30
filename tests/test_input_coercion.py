"""Pure-helper tests for ``cascadeui.utils.coercion``.

These tests cover ``coerce_snowflake_id`` and ``coerce_snowflake_id_set``
in isolation. View-level integration coverage lives in:

    - ``test_view_init.py::TestSnowflakeIdCoercion`` (user_id / guild_id)
    - ``test_owner_only.py::TestAllowedUsers`` (allowed_users coercion)
    - ``test_instance_limit.py::TestParticipantSessions`` (register_participant)
"""

# // ========================================( Modules )======================================== // #


from unittest.mock import MagicMock

import pytest

from cascadeui.utils.coercion import (
    coerce_snowflake_id,
    coerce_snowflake_id_set,
    coerce_snowflake_match,
)

# // ========================================( Helpers )======================================== // #


def _snowflake(snowflake_id: int) -> MagicMock:
    """Build a Member/User-shaped mock with an ``int`` ``.id`` attribute."""
    obj = MagicMock()
    obj.id = snowflake_id
    return obj


# // ========================================( coerce_snowflake_id )======================================== // #


class TestCoerceSnowflakeId:
    """coerce_snowflake_id handles None, int, and .id-bearing objects."""

    def test_none_passthrough(self):
        assert coerce_snowflake_id(None) is None

    def test_int_passthrough(self):
        assert coerce_snowflake_id(42) == 42

    def test_member_shaped_object(self):
        assert coerce_snowflake_id(_snowflake(123456789)) == 123456789

    def test_rejects_string(self):
        with pytest.raises(TypeError, match="Snowflake"):
            coerce_snowflake_id("123")

    def test_rejects_dict(self):
        with pytest.raises(TypeError, match="Snowflake"):
            coerce_snowflake_id({"id": 123})

    def test_rejects_object_with_non_int_id(self):
        bad = MagicMock()
        bad.id = "not-an-int"
        with pytest.raises(TypeError, match="Snowflake"):
            coerce_snowflake_id(bad)

    def test_rejects_bool(self):
        # ``True`` is technically an ``int`` subclass, but a bool flag
        # being passed where an ID is expected is always a mistake.
        with pytest.raises(TypeError, match="Snowflake"):
            coerce_snowflake_id(True)


# // ========================================( coerce_snowflake_id_set )======================================== // #


class TestCoerceSnowflakeIdSet:
    """coerce_snowflake_id_set converts iterables of mixed types to int sets."""

    def test_none_returns_empty_set(self):
        assert coerce_snowflake_id_set(None) == set()

    def test_empty_returns_empty_set(self):
        assert coerce_snowflake_id_set([]) == set()

    def test_mixed_int_and_member(self):
        assert coerce_snowflake_id_set({_snowflake(1), _snowflake(2), 3}) == {1, 2, 3}

    def test_rejects_none_inside_collection(self):
        with pytest.raises(TypeError, match="None is not a valid snowflake"):
            coerce_snowflake_id_set([1, None, 2])

    def test_rejects_invalid_element(self):
        with pytest.raises(TypeError, match="Snowflake"):
            coerce_snowflake_id_set([1, "not-an-id", 3])


# // ========================================( coerce_snowflake_match )======================================== // #


_SNOWFLAKE_KEYS = frozenset({"user_id", "guild_id", "channel_id", "role_id", "message_id"})


class TestCoerceSnowflakeMatch:
    """coerce_snowflake_match coerces named regex-capture groups to int."""

    def test_snowflake_key_coerced_to_int(self):
        out = coerce_snowflake_match({"role_id": "123456789"}, _SNOWFLAKE_KEYS)
        assert out == {"role_id": 123456789}
        assert isinstance(out["role_id"], int)

    def test_non_snowflake_key_preserved_as_string(self):
        out = coerce_snowflake_match({"category": "mod_staff", "role_id": "42"}, _SNOWFLAKE_KEYS)
        assert out == {"category": "mod_staff", "role_id": 42}
        assert isinstance(out["category"], str)
        assert isinstance(out["role_id"], int)

    def test_all_five_snowflake_names_coerced(self):
        raw = {
            "user_id": "1",
            "guild_id": "2",
            "channel_id": "3",
            "role_id": "4",
            "message_id": "5",
        }
        out = coerce_snowflake_match(raw, _SNOWFLAKE_KEYS)
        assert out == {
            "user_id": 1,
            "guild_id": 2,
            "channel_id": 3,
            "role_id": 4,
            "message_id": 5,
        }
        assert all(isinstance(v, int) for v in out.values())

    def test_none_value_preserved(self):
        # Optional regex groups match as None; coercion must not crash.
        out = coerce_snowflake_match({"role_id": None}, _SNOWFLAKE_KEYS)
        assert out == {"role_id": None}

    def test_non_digit_string_raises_value_error(self):
        # A template like ``(?P<role_id>\w+)`` would capture non-digits
        # and trip int(); this is a programmer error the helper surfaces
        # via the natural ValueError rather than silently coercing.
        with pytest.raises(ValueError):
            coerce_snowflake_match({"role_id": "not-digits"}, _SNOWFLAKE_KEYS)

    def test_input_dict_not_mutated(self):
        raw = {"role_id": "42", "category": "mod"}
        out = coerce_snowflake_match(raw, _SNOWFLAKE_KEYS)
        assert raw == {"role_id": "42", "category": "mod"}
        assert out is not raw

    def test_empty_snowflake_keys_passthrough(self):
        # When no keys are designated as snowflakes, everything stays a string.
        out = coerce_snowflake_match({"role_id": "42"}, frozenset())
        assert out == {"role_id": "42"}
        assert isinstance(out["role_id"], str)
