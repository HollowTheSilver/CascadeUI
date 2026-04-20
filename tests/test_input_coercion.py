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

from cascadeui.utils.coercion import coerce_snowflake_id, coerce_snowflake_id_set

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
