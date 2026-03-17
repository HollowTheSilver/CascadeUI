"""Tests for per-user and per-guild state scoping."""

import copy
import pytest

from cascadeui.state.singleton import get_store


class TestStateScooping:
    async def test_user_scope_isolation(self):
        """Different users should have isolated scoped state."""
        store = get_store()

        await store.dispatch("SCOPED_UPDATE", {
            "scope": "user", "scope_id": 111, "data": {"theme": "dark"},
        })
        await store.dispatch("SCOPED_UPDATE", {
            "scope": "user", "scope_id": 222, "data": {"theme": "light"},
        })

        assert store.get_scoped("user", user_id=111) == {"theme": "dark"}
        assert store.get_scoped("user", user_id=222) == {"theme": "light"}

    async def test_guild_scope_isolation(self):
        """Different guilds should have isolated scoped state."""
        store = get_store()

        await store.dispatch("SCOPED_UPDATE", {
            "scope": "guild", "scope_id": 1001, "data": {"prefix": "!"},
        })
        await store.dispatch("SCOPED_UPDATE", {
            "scope": "guild", "scope_id": 1002, "data": {"prefix": "?"},
        })

        assert store.get_scoped("guild", guild_id=1001) == {"prefix": "!"}
        assert store.get_scoped("guild", guild_id=1002) == {"prefix": "?"}

    async def test_flat_state_unaffected(self):
        """Scoped updates should not affect existing flat state."""
        store = get_store()
        store.state["application"]["global_val"] = 42

        await store.dispatch("SCOPED_UPDATE", {
            "scope": "user", "scope_id": 1, "data": {"x": 1},
        })

        assert store.state["application"]["global_val"] == 42

    async def test_scoped_data_merges(self):
        """Multiple scoped updates should merge data."""
        store = get_store()

        await store.dispatch("SCOPED_UPDATE", {
            "scope": "user", "scope_id": 1, "data": {"a": 1},
        })
        await store.dispatch("SCOPED_UPDATE", {
            "scope": "user", "scope_id": 1, "data": {"b": 2},
        })

        assert store.get_scoped("user", user_id=1) == {"a": 1, "b": 2}

    def test_get_scoped_missing_returns_empty(self):
        store = get_store()
        assert store.get_scoped("user", user_id=99999) == {}

    def test_unknown_scope_raises(self):
        store = get_store()
        with pytest.raises(ValueError, match="Unknown scope"):
            store.get_scoped("channel", channel_id=1)

    def test_missing_identifier_raises(self):
        store = get_store()
        with pytest.raises(ValueError, match="user_id is required"):
            store.get_scoped("user")

    def test_set_scoped_direct(self):
        store = get_store()
        store.set_scoped("user", {"direct": True}, user_id=42)
        assert store.get_scoped("user", user_id=42) == {"direct": True}
