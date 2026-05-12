"""Tests for scoped state across all four scope values."""

import pytest

from cascadeui import StateStore
from cascadeui.state.singleton import get_store
from cascadeui.state.slots import read_slot

# // ========================================( Store-level round-trip )======================================== // #


class TestStateScopingUser:
    """User-scoped state isolates and merges per user_id."""

    async def test_user_scope_isolation(self):
        store = get_store()

        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 111}, "data": {"theme": "dark"}},
        )
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 222}, "data": {"theme": "light"}},
        )

        assert store.get_scoped("user", user_id=111) == {"theme": "dark"}
        assert store.get_scoped("user", user_id=222) == {"theme": "light"}

    async def test_user_scope_merges(self):
        store = get_store()

        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 1}, "data": {"a": 1}},
        )
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 1}, "data": {"b": 2}},
        )

        assert store.get_scoped("user", user_id=1) == {"a": 1, "b": 2}


class TestStateScopingGuild:
    """Guild-scoped state isolates per guild_id."""

    async def test_guild_scope_isolation(self):
        store = get_store()

        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "guild", "identifiers": {"guild_id": 1001}, "data": {"prefix": "!"}},
        )
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "guild", "identifiers": {"guild_id": 1002}, "data": {"prefix": "?"}},
        )

        assert store.get_scoped("guild", guild_id=1001) == {"prefix": "!"}
        assert store.get_scoped("guild", guild_id=1002) == {"prefix": "?"}


class TestStateScopingUserGuild:
    """Composite user_guild scope isolates per (user_id, guild_id) pair."""

    async def test_user_guild_composite_isolation(self):
        """A user's preference is independent across different guilds."""
        store = get_store()

        await store.dispatch(
            "SCOPED_UPDATE",
            {
                "scope": "user_guild",
                "identifiers": {"user_id": 7, "guild_id": 100},
                "data": {"nick_visible": True},
            },
        )
        await store.dispatch(
            "SCOPED_UPDATE",
            {
                "scope": "user_guild",
                "identifiers": {"user_id": 7, "guild_id": 200},
                "data": {"nick_visible": False},
            },
        )

        assert store.get_scoped("user_guild", user_id=7, guild_id=100) == {"nick_visible": True}
        assert store.get_scoped("user_guild", user_id=7, guild_id=200) == {"nick_visible": False}

    async def test_user_guild_key_format(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {
                "scope": "user_guild",
                "identifiers": {"user_id": 42, "guild_id": 99},
                "data": {"k": "v"},
            },
        )
        assert "user_guild:42:99" in read_slot(store.state, "scoped")

    def test_user_guild_missing_identifier_raises(self):
        store = get_store()
        with pytest.raises(ValueError, match="user_id and guild_id"):
            store.get_scoped("user_guild", user_id=1)
        with pytest.raises(ValueError, match="user_id and guild_id"):
            store.get_scoped("user_guild", guild_id=1)


class TestStateScopingGlobal:
    """Global scope is a single shared slot visible to all callers."""

    async def test_global_scope_single_slot(self):
        """Global scope is a single shared slot visible to all callers."""
        store = get_store()

        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "global", "identifiers": {}, "data": {"maintenance": True}},
        )

        assert store.get_scoped("global") == {"maintenance": True}

    async def test_global_scope_merges(self):
        store = get_store()

        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "global", "identifiers": {}, "data": {"a": 1}},
        )
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "global", "identifiers": {}, "data": {"b": 2}},
        )

        assert store.get_scoped("global") == {"a": 1, "b": 2}

    async def test_global_scope_key_format(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "global", "identifiers": {}, "data": {"k": "v"}},
        )
        assert "global" in read_slot(store.state, "scoped")


class TestScopeEdgeCasesAndValidation:
    """Scoped dispatch does not affect flat state; invalid scopes and missing identifiers raise."""

    async def test_flat_state_unaffected(self):
        store = get_store()
        store.state["application"]["global_val"] = 42

        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 1}, "data": {"x": 1}},
        )

        assert store.state["application"]["global_val"] == 42

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

    async def test_malformed_payload_is_noop(self):
        """Reducer returns state unchanged when scope is missing or invalid."""
        store = get_store()
        before = read_slot(store.state, "scoped").copy()

        # Missing scope
        await store.dispatch("SCOPED_UPDATE", {"identifiers": {"user_id": 1}, "data": {"x": 1}})
        # Bad identifiers for the declared scope
        await store.dispatch(
            "SCOPED_UPDATE", {"scope": "user", "identifiers": {}, "data": {"x": 1}}
        )

        assert read_slot(store.state, "scoped") == before


# // ========================================( get_scoped_from staticmethod )======================================== // #


class TestGetScopedFrom:
    """``get_scoped_from(state, scope, **ids)`` reads scoped slices from an
    explicit state dict rather than ``self.state``. Intended for use inside
    ``@computed`` selectors and custom reducers, where the state to read from
    is passed in as an argument.
    """

    def test_reads_from_explicit_state_user_scope(self):
        state = {"application": {"scoped": {"user:42": {"credits": 7}}}}
        result = get_store().get_scoped_from(state, "user", user_id=42)
        assert result == {"credits": 7}

    def test_reads_from_explicit_state_guild_scope(self):
        state = {"application": {"scoped": {"guild:500": {"prefix": "!"}}}}
        result = get_store().get_scoped_from(state, "guild", guild_id=500)
        assert result == {"prefix": "!"}

    def test_reads_user_guild_composite(self):
        state = {"application": {"scoped": {"user_guild:1:2": {"nickname": "Ada"}}}}
        result = get_store().get_scoped_from(state, "user_guild", user_id=1, guild_id=2)
        assert result == {"nickname": "Ada"}

    def test_reads_global(self):
        state = {"application": {"scoped": {"global": {"tz": "UTC"}}}}
        result = get_store().get_scoped_from(state, "global")
        assert result == {"tz": "UTC"}

    def test_missing_slice_returns_empty_dict(self):
        state = {"application": {"scoped": {}}}
        assert get_store().get_scoped_from(state, "user", user_id=9) == {}

    def test_missing_scoped_namespace_returns_empty_dict(self):
        assert get_store().get_scoped_from({}, "user", user_id=9) == {}

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError, match="Unknown scope"):
            get_store().get_scoped_from({}, "channel", channel_id=1)

    def test_missing_identifier_raises(self):
        with pytest.raises(ValueError, match="user_id is required"):
            get_store().get_scoped_from({}, "user")

    def test_callable_as_staticmethod_on_class(self):
        # No instance required: can be called from inside a reducer or
        # selector that only has a state dict.

        state = {"application": {"scoped": {"user:1": {"x": 1}}}}
        result = StateStore.get_scoped_from(state, "user", user_id=1)
        assert result == {"x": 1}

    def test_independent_of_self_state(self):
        # Reads ONLY the passed state, ignores the store's own state.
        store = get_store()
        store.state.setdefault("application", {})["scoped"] = {"user:1": {"from": "store"}}

        other_state = {"application": {"scoped": {"user:1": {"from": "argument"}}}}
        assert store.get_scoped_from(other_state, "user", user_id=1) == {"from": "argument"}


# // ========================================( iter_scoped staticmethod )======================================== // #


class TestIterScoped:
    """``iter_scoped(state, scope, *, slot_name, **filter_ids)`` walks a named
    bucket under ``state["application"]`` and yields ``(identifiers_dict,
    value)`` pairs for every key matching the scope and supplied filters.
    Unsupplied identifiers act as wildcards, so a leaderboard can pass only
    ``guild_id=`` and discover every participating ``user_id``.
    """

    def test_iterates_user_scope(self):

        state = {
            "application": {
                "scoped": {
                    "user:1": {"credits": 10},
                    "user:2": {"credits": 20},
                }
            }
        }
        results = sorted(StateStore.iter_scoped(state, "user"), key=lambda pair: pair[0]["user_id"])
        assert results == [
            ({"user_id": 1}, {"credits": 10}),
            ({"user_id": 2}, {"credits": 20}),
        ]

    def test_iterates_guild_scope(self):

        state = {
            "application": {
                "scoped": {
                    "guild:100": {"prefix": "!"},
                    "guild:200": {"prefix": "?"},
                }
            }
        }
        results = sorted(
            StateStore.iter_scoped(state, "guild"), key=lambda pair: pair[0]["guild_id"]
        )
        assert results == [
            ({"guild_id": 100}, {"prefix": "!"}),
            ({"guild_id": 200}, {"prefix": "?"}),
        ]

    def test_iterates_user_guild_scope(self):

        state = {
            "application": {
                "scoped": {
                    "user_guild:1:500": {"wins": 3},
                    "user_guild:2:500": {"wins": 1},
                }
            }
        }
        results = sorted(
            StateStore.iter_scoped(state, "user_guild"), key=lambda pair: pair[0]["user_id"]
        )
        assert results == [
            ({"user_id": 1, "guild_id": 500}, {"wins": 3}),
            ({"user_id": 2, "guild_id": 500}, {"wins": 1}),
        ]

    def test_iterates_global_scope(self):

        state = {"application": {"scoped": {"global": {"tz": "UTC"}}}}
        results = list(StateStore.iter_scoped(state, "global"))
        assert results == [({}, {"tz": "UTC"})]

    def test_user_guild_filter_by_guild_discovers_users(self):
        # Leaderboard case: guild_id known, user_id wildcarded.

        state = {
            "application": {
                "scoped": {
                    "user_guild:1:500": {"wins": 3},
                    "user_guild:2:500": {"wins": 1},
                    "user_guild:1:999": {"wins": 7},
                }
            }
        }
        results = sorted(
            StateStore.iter_scoped(state, "user_guild", guild_id=500),
            key=lambda pair: pair[0]["user_id"],
        )
        assert results == [
            ({"user_id": 1, "guild_id": 500}, {"wins": 3}),
            ({"user_id": 2, "guild_id": 500}, {"wins": 1}),
        ]

    def test_user_guild_filter_by_user_discovers_guilds(self):

        state = {
            "application": {
                "scoped": {
                    "user_guild:1:500": {"wins": 3},
                    "user_guild:1:999": {"wins": 7},
                    "user_guild:2:500": {"wins": 1},
                }
            }
        }
        results = sorted(
            StateStore.iter_scoped(state, "user_guild", user_id=1),
            key=lambda pair: pair[0]["guild_id"],
        )
        assert results == [
            ({"user_id": 1, "guild_id": 500}, {"wins": 3}),
            ({"user_id": 1, "guild_id": 999}, {"wins": 7}),
        ]

    def test_user_guild_full_filter_returns_single_entry(self):

        state = {
            "application": {
                "scoped": {
                    "user_guild:1:500": {"wins": 3},
                    "user_guild:2:500": {"wins": 1},
                }
            }
        }
        results = list(StateStore.iter_scoped(state, "user_guild", user_id=1, guild_id=500))
        assert results == [({"user_id": 1, "guild_id": 500}, {"wins": 3})]

    def test_custom_slot_name_is_scanned(self):

        state = {
            "application": {
                "battleship_stats": {
                    "user_guild:1:500": {"games": 5},
                },
                "scoped": {
                    "user_guild:1:500": {"other": True},
                },
            }
        }
        results = list(
            StateStore.iter_scoped(
                state,
                "user_guild",
                slot_name="battleship_stats",
                guild_id=500,
            )
        )
        assert results == [({"user_id": 1, "guild_id": 500}, {"games": 5})]

    def test_missing_slot_yields_nothing(self):

        results = list(StateStore.iter_scoped({}, "user"))
        assert results == []

    def test_skips_keys_with_wrong_prefix(self):

        state = {
            "application": {
                "scoped": {
                    "user:1": {"ok": True},
                    "guild:1": {"should_be_skipped": True},
                    "user_guild:1:2": {"also_skipped": True},
                }
            }
        }
        results = list(StateStore.iter_scoped(state, "user"))
        assert results == [({"user_id": 1}, {"ok": True})]

    def test_skips_malformed_segment_count(self):

        state = {
            "application": {
                "scoped": {
                    "user_guild:1:2:extra": {"skip": True},
                    "user_guild:1:2": {"keep": True},
                }
            }
        }
        results = list(StateStore.iter_scoped(state, "user_guild"))
        assert results == [({"user_id": 1, "guild_id": 2}, {"keep": True})]

    def test_skips_non_integer_ids(self):

        state = {
            "application": {
                "scoped": {
                    "user:abc": {"skip": True},
                    "user:42": {"keep": True},
                }
            }
        }
        results = list(StateStore.iter_scoped(state, "user"))
        assert results == [({"user_id": 42}, {"keep": True})]

    def test_global_missing_value_yields_nothing(self):

        state = {"application": {"scoped": {}}}
        results = list(StateStore.iter_scoped(state, "global"))
        assert results == []

    def test_unknown_scope_raises(self):

        with pytest.raises(ValueError, match="Unknown scope"):
            list(StateStore.iter_scoped({}, "channel"))


# // ========================================( scoped_state_for helper )======================================== // #


class _StubView:
    """Minimal stand-in for a _StatefulMixin view.

    ``scoped_state_for`` only reads ``state_store``, ``user_id``, and
    ``guild_id`` off ``self``, so a tiny namespace object is enough to
    exercise the method without spinning up a real View subclass.
    """

    def __init__(self, user_id=None, guild_id=None):
        self.state_store = get_store()
        self.user_id = user_id
        self.guild_id = guild_id

    # Bind the real implementation to the stub.
    from cascadeui.views.base import _StatefulMixin

    scoped_slot = None
    _effective_scoped_slot = _StatefulMixin._effective_scoped_slot
    scoped_state_for = _StatefulMixin.scoped_state_for
    user_scoped_state = _StatefulMixin.user_scoped_state
    guild_scoped_state = _StatefulMixin.guild_scoped_state
    user_guild_scoped_state = _StatefulMixin.user_guild_scoped_state
    global_scoped_state = _StatefulMixin.global_scoped_state


class TestNamedScopedStateAccessors:
    """Four dedicated accessors mirror the four legal ``state_scope`` values.

    They delegate to ``scoped_state_for`` but read like attribute access at
    the call site, replacing six-level ``state.get().get().get()`` chains
    that appeared in example ``state_selector`` methods.
    """

    async def test_user_scoped_state_uses_view_user_id(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 501}, "data": {"theme": "dark"}},
        )
        view = _StubView(user_id=501)
        assert view.user_scoped_state() == {"theme": "dark"}

    async def test_user_scoped_state_explicit_override(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 502}, "data": {"theme": "light"}},
        )
        view = _StubView(user_id=1)
        assert view.user_scoped_state(502) == {"theme": "light"}

    async def test_guild_scoped_state(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "guild", "identifiers": {"guild_id": 42}, "data": {"prefix": "!"}},
        )
        view = _StubView(guild_id=42)
        assert view.guild_scoped_state() == {"prefix": "!"}

    async def test_user_guild_scoped_state(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {
                "scope": "user_guild",
                "identifiers": {"user_id": 7, "guild_id": 9},
                "data": {"nick": "Ace"},
            },
        )
        view = _StubView(user_id=7, guild_id=9)
        assert view.user_guild_scoped_state() == {"nick": "Ace"}

    async def test_global_scoped_state(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "global", "identifiers": {}, "data": {"banner": "hello"}},
        )
        view = _StubView()
        assert view.global_scoped_state() == {"banner": "hello"}

    def test_missing_identifiers_return_empty(self):
        view = _StubView()
        assert view.user_scoped_state() == {}
        assert view.guild_scoped_state() == {}
        assert view.user_guild_scoped_state() == {}


class TestScopedStateFor:
    """``scoped_state_for`` lets hub views read slices from scopes other than their own."""

    async def test_auto_resolves_identifiers_from_view(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 900}, "data": {"theme": "dark"}},
        )
        await store.dispatch(
            "SCOPED_UPDATE",
            {
                "scope": "user_guild",
                "identifiers": {"user_id": 900, "guild_id": 1},
                "data": {"nick": True},
            },
        )

        view = _StubView(user_id=900, guild_id=1)
        assert view.scoped_state_for("user") == {"theme": "dark"}
        assert view.scoped_state_for("user_guild") == {"nick": True}

    async def test_global_ignores_identifiers(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "global", "identifiers": {}, "data": {"banner": "hi"}},
        )
        # Even with no user_id / guild_id, global resolves.
        view = _StubView()
        assert view.scoped_state_for("global") == {"banner": "hi"}

    async def test_override_targets_different_user(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 77}, "data": {"theme": "light"}},
        )
        view = _StubView(user_id=1)
        assert view.scoped_state_for("user", user_id=77) == {"theme": "light"}

    def test_missing_identifier_returns_empty(self):
        view = _StubView()  # no user_id / guild_id
        assert view.scoped_state_for("user") == {}
        assert view.scoped_state_for("guild") == {}
        assert view.scoped_state_for("user_guild") == {}

    def test_unknown_scope_raises(self):
        view = _StubView(user_id=1, guild_id=1)
        with pytest.raises(ValueError, match="Unknown scope"):
            view.scoped_state_for("channel")


# // ========================================( Session Data )======================================== // #


class _SessionStubView:
    """Stand-in for testing ``shared_data`` and ``update_session``.

    Needs ``session_id``, ``id``, ``state_store``, and ``dispatch`` in
    addition to the methods under test.
    """

    def __init__(self, session_id, view_id="stub-view"):
        self.session_id = session_id
        self.id = view_id
        self.state_store = get_store()

    from cascadeui.views.base import _StatefulMixin

    shared_data = _StatefulMixin.shared_data
    update_session = _StatefulMixin.update_session
    dispatch = _StatefulMixin.dispatch


class TestSharedData:
    """``shared_data`` reads and ``update_session`` writes the session's
    ``data`` dict, mirroring the scoped state accessors for session-level
    shared metadata.
    """

    async def _seed_session(self, session_id, data=None):
        store = get_store()
        await store.dispatch(
            "SESSION_CREATED",
            {"session_id": session_id, "user_id": 1, "shared_data": data or {}},
        )

    async def test_shared_data_returns_empty_for_missing_session(self):
        view = _SessionStubView(session_id="nonexistent")
        assert view.shared_data == {}

    async def test_shared_data_reads_existing(self):
        await self._seed_session("sd-1", {"lang": "en"})
        view = _SessionStubView(session_id="sd-1")
        assert view.shared_data == {"lang": "en"}

    async def test_update_session_merges_data(self):
        await self._seed_session("sd-2", {"lang": "en"})
        view = _SessionStubView(session_id="sd-2")
        await view.update_session(theme="dark")
        assert view.shared_data == {"lang": "en", "theme": "dark"}

    async def test_update_session_overwrites_key(self):
        await self._seed_session("sd-3", {"lang": "en"})
        view = _SessionStubView(session_id="sd-3")
        await view.update_session(lang="fr")
        assert view.shared_data["lang"] == "fr"

    async def test_update_session_multiple_keys(self):
        await self._seed_session("sd-4")
        view = _SessionStubView(session_id="sd-4")
        await view.update_session(a=1, b=2, c=3)
        assert view.shared_data == {"a": 1, "b": 2, "c": 3}

    async def test_shared_data_empty_without_updates(self):
        await self._seed_session("sd-5")
        view = _SessionStubView(session_id="sd-5")
        assert view.shared_data == {}


# // ========================================( dispatch_scoped / dispatch_scoped_as )======================================== // #


class _DispatchStubView:
    """Stand-in for exercising ``dispatch_scoped`` / ``dispatch_scoped_as``.

    Needs ``state_scope``, ``state_store``, ``user_id``, ``guild_id``,
    ``id`` to satisfy the dispatch pipeline plus the bound methods.
    """

    def __init__(self, state_scope=None, user_id=None, guild_id=None, view_id="stub-view"):
        self.state_scope = state_scope
        self.state_store = get_store()
        self.user_id = user_id
        self.guild_id = guild_id
        self.id = view_id

    from cascadeui.views.base import _StatefulMixin

    scoped_slot = None
    _effective_scoped_slot = _StatefulMixin._effective_scoped_slot
    dispatch = _StatefulMixin.dispatch
    dispatch_scoped = _StatefulMixin.dispatch_scoped
    dispatch_scoped_as = _StatefulMixin.dispatch_scoped_as
    _resolve_scoped_identifiers = _StatefulMixin._resolve_scoped_identifiers
    _resolve_scope_target = _StatefulMixin._resolve_scope_target
    scoped_state = _StatefulMixin.scoped_state


class TestDispatchScopedOverrides:
    """``dispatch_scoped`` falls back to the view's own scope + IDs, but
    explicit kwargs override per-call. Errors fire at the method boundary
    when required identifiers are missing.
    """

    async def test_falls_back_to_view_scope_and_user_id(self):
        store = get_store()
        view = _DispatchStubView(state_scope="user", user_id=404)

        await view.dispatch_scoped({"theme": "dark"})

        assert store.get_scoped("user", user_id=404) == {"theme": "dark"}

    async def test_scope_kwarg_overrides_view_scope(self):
        store = get_store()
        view = _DispatchStubView(state_scope="user", user_id=1, guild_id=77)

        await view.dispatch_scoped({"prefix": "?"}, scope="guild")

        assert store.get_scoped("guild", guild_id=77) == {"prefix": "?"}

    async def test_identifier_kwarg_overrides_view_id(self):
        store = get_store()
        view = _DispatchStubView(state_scope="user", user_id=1)

        await view.dispatch_scoped({"theme": "light"}, user_id=999)

        # Written under the explicit id, not the view's.
        assert store.get_scoped("user", user_id=999) == {"theme": "light"}
        assert store.get_scoped("user", user_id=1) == {}

    async def test_user_guild_composite_with_overrides(self):
        store = get_store()
        view = _DispatchStubView(state_scope="user_guild", user_id=1, guild_id=2)

        await view.dispatch_scoped(
            {"nick": "Ada"},
            scope="user_guild",
            user_id=50,
            guild_id=60,
        )

        assert store.get_scoped("user_guild", user_id=50, guild_id=60) == {"nick": "Ada"}

    async def test_global_scope_ignores_identifiers(self):
        store = get_store()
        view = _DispatchStubView(state_scope="global")

        await view.dispatch_scoped({"banner": "hi"})

        assert store.get_scoped("global") == {"banner": "hi"}

    def test_no_scope_available_raises(self):
        view = _DispatchStubView(state_scope=None)

        with pytest.raises(ValueError, match="no scope given"):
            view._resolve_scoped_identifiers(None, {})

    def test_missing_identifier_raises(self):
        view = _DispatchStubView(state_scope="user", user_id=None)

        with pytest.raises(ValueError, match="missing identifiers"):
            view._resolve_scoped_identifiers("user", {})

    def test_unknown_scope_raises(self):
        view = _DispatchStubView()

        with pytest.raises(ValueError, match="Unknown scope"):
            view._resolve_scoped_identifiers("channel", {})


class TestDispatchScopedAs:
    """``dispatch_scoped_as`` emits the canonical
    ``{"scope", "identifiers", "data"}`` payload under a custom action
    type so custom reducers share one decode path with built-in
    ``SCOPED_UPDATE``.
    """

    async def test_emits_canonical_payload_shape(self):
        store = get_store()
        captured = {}

        async def capture_reducer(action, state):
            captured["payload"] = action["payload"]
            return state

        store._register_reducer("CUSTOM_SCOPED", capture_reducer)

        try:
            view = _DispatchStubView(state_scope="user", user_id=321)
            await view.dispatch_scoped_as("CUSTOM_SCOPED", {"level": 5})
        finally:
            store._custom_reducers.pop("CUSTOM_SCOPED", None)

        assert captured["payload"] == {
            "scope": "user",
            "identifiers": {"user_id": 321},
            "data": {"level": 5},
            "slot_name": "scoped",
        }

    async def test_custom_reducer_can_decode_via_build_scope_key(self):

        store = get_store()

        from cascadeui.state.slots import access_slot

        async def custom_reducer(action, state):
            payload = action["payload"]
            scope_key = StateStore._build_scope_key(payload["scope"], **payload["identifiers"])
            access_slot(state, "scoped", scope_key).update({"ns": payload["data"]})
            return state

        store._register_reducer("NS_UPDATE", custom_reducer)

        try:
            view = _DispatchStubView(state_scope="guild", guild_id=808)
            await view.dispatch_scoped_as("NS_UPDATE", {"prefix": "!"})
        finally:
            store._custom_reducers.pop("NS_UPDATE", None)

        assert store.get_scoped("guild", guild_id=808) == {"ns": {"prefix": "!"}}

    async def test_scope_and_identifier_overrides_apply(self):
        store = get_store()

        async def capture_reducer(action, state):
            state.setdefault("_seen", []).append(action["payload"])
            return state

        store._register_reducer("OVR_ACTION", capture_reducer)

        try:
            view = _DispatchStubView(state_scope="user", user_id=1, guild_id=2)
            await view.dispatch_scoped_as(
                "OVR_ACTION",
                {"x": 1},
                scope="user_guild",
                user_id=10,
                guild_id=20,
            )
        finally:
            store._custom_reducers.pop("OVR_ACTION", None)

        assert store.state["_seen"][-1] == {
            "scope": "user_guild",
            "identifiers": {"user_id": 10, "guild_id": 20},
            "data": {"x": 1},
            "slot_name": "scoped",
        }

    def test_missing_identifier_raises(self):
        view = _DispatchStubView(state_scope="guild", guild_id=None)

        with pytest.raises(ValueError, match="missing identifiers"):
            view._resolve_scoped_identifiers("guild", {})


# // ========================================( Named slot routing )======================================== // #


class TestScopedSlotRouting:
    """``scoped_slot`` class attribute routes scoped writes and reads into a
    named bucket under ``state["application"]`` so subsystems stay isolated
    from one another and from the shared ``"scoped"`` default.
    """

    async def test_default_slot_is_scoped(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {"scope": "user", "identifiers": {"user_id": 1}, "data": {"x": 1}},
        )
        assert store.state["application"]["scoped"]["user:1"] == {"x": 1}

    async def test_slot_name_payload_routes_into_named_bucket(self):
        store = get_store()
        await store.dispatch(
            "SCOPED_UPDATE",
            {
                "scope": "user",
                "identifiers": {"user_id": 1},
                "data": {"rank": 5},
                "slot_name": "game_stats",
            },
        )
        assert store.state["application"]["game_stats"]["user:1"] == {"rank": 5}
        assert "user:1" not in store.state["application"].get("scoped", {})

    async def test_get_scoped_reads_named_bucket(self):
        store = get_store()
        store.set_scoped("guild", {"prefix": "!"}, slot_name="settings", guild_id=42)
        assert store.get_scoped("guild", slot_name="settings", guild_id=42) == {"prefix": "!"}
        assert store.get_scoped("guild", guild_id=42) == {}

    async def test_view_scoped_slot_threads_through_dispatch(self):
        store = get_store()
        view = _DispatchStubView(state_scope="user", user_id=7)
        view.scoped_slot = "achievements"

        await view.dispatch_scoped({"earned": 3})

        assert store.state["application"]["achievements"]["user:7"] == {"earned": 3}

    async def test_view_scoped_slot_threads_through_read(self):
        store = get_store()
        store.set_scoped("user", {"coins": 50}, slot_name="economy", user_id=11)

        view = _DispatchStubView(state_scope="user", user_id=11)
        view.scoped_slot = "economy"

        assert view.scoped_state == {"coins": 50}

    async def test_two_slots_are_independent(self):
        store = get_store()
        view_a = _DispatchStubView(state_scope="user", user_id=1)
        view_a.scoped_slot = "slot_a"
        view_b = _DispatchStubView(state_scope="user", user_id=1)
        view_b.scoped_slot = "slot_b"

        await view_a.dispatch_scoped({"from": "a"})
        await view_b.dispatch_scoped({"from": "b"})

        assert view_a.scoped_state == {"from": "a"}
        assert view_b.scoped_state == {"from": "b"}


class TestScopedSlotValidation:
    """``scoped_slot`` is validated at class definition time: must be a
    non-empty string or ``None``. Typos and wrong types fail at import,
    not at the first dispatch.
    """

    def test_none_allowed(self):
        from cascadeui.views.view import StatefulView

        class _Ok(StatefulView):
            scoped_slot = None

        assert _Ok.scoped_slot is None

    def test_string_allowed(self):
        from cascadeui.views.view import StatefulView

        class _Ok(StatefulView):
            scoped_slot = "achievements"

        assert _Ok.scoped_slot == "achievements"

    def test_empty_string_rejected(self):
        from cascadeui.views.view import StatefulView

        with pytest.raises(TypeError, match="scoped_slot"):

            class _Bad(StatefulView):
                scoped_slot = ""

    def test_non_string_rejected(self):
        from cascadeui.views.view import StatefulView

        with pytest.raises(TypeError, match="scoped_slot"):

            class _Bad(StatefulView):
                scoped_slot = 42


class TestSlotCoherenceValidation:
    """``scoped_slot`` and ``persistent_slots`` are cross-checked at class
    definition time. A view with a custom ``scoped_slot`` that still
    persists the default ``"scoped"`` bucket is almost certainly a
    copy-paste mistake -- the view's own writes would never land under
    the declared persistent slot. The validator raises at import so the
    mismatch cannot ship.
    """

    def test_custom_slot_with_scoped_persist_rejected(self):
        from cascadeui.views.view import StatefulView

        with pytest.raises(ValueError, match='persistent_slots includes "scoped"'):

            class _Bad(StatefulView):
                scoped_slot = "my_stats"
                persistent_slots = ("scoped",)

    def test_custom_slot_matching_persist_allowed(self):
        from cascadeui.views.view import StatefulView

        class _Ok(StatefulView):
            scoped_slot = "my_stats"
            persistent_slots = ("my_stats",)

        assert _Ok.scoped_slot == "my_stats"
        assert _Ok.persistent_slots == ("my_stats",)

    def test_default_slot_with_scoped_persist_allowed(self):
        from cascadeui.views.view import StatefulView

        class _Ok(StatefulView):
            persistent_slots = ("scoped",)

        assert _Ok.scoped_slot is None
        assert _Ok.persistent_slots == ("scoped",)

    def test_custom_slot_with_empty_persist_allowed(self):
        from cascadeui.views.view import StatefulView

        class _Ok(StatefulView):
            scoped_slot = "my_stats"
            persistent_slots = ()

        assert _Ok.scoped_slot == "my_stats"
        assert _Ok.persistent_slots == ()

    def test_inherited_scoped_slot_rejected_when_child_adds_scoped_persist(self):
        from cascadeui.views.view import StatefulView

        class _Parent(StatefulView):
            scoped_slot = "my_stats"

        with pytest.raises(ValueError, match='persistent_slots includes "scoped"'):

            class _Child(_Parent):
                persistent_slots = ("scoped",)

    def test_inherited_persist_rejected_when_child_sets_custom_slot(self):
        from cascadeui.views.view import StatefulView

        class _Parent(StatefulView):
            persistent_slots = ("scoped",)

        with pytest.raises(ValueError, match='persistent_slots includes "scoped"'):

            class _Child(_Parent):
                scoped_slot = "my_stats"


class TestMergeScoped:
    """``merge_scoped(state, scope, data, *, slot_name, subkey, **identifiers)``
    is the reducer-side writer paired with ``get_scoped_from`` / ``iter_scoped``.
    It decodes the canonical payload shape and merges ``data`` into the scope
    bucket without callers reaching the private ``_build_scope_key``. Falsy
    scope and invalid identifier combos return ``state`` untouched, mirroring
    how the built-in ``SCOPED_UPDATE`` reducer degrades.
    """

    def test_merges_into_scope_root_without_subkey(self):
        state = {"application": {}}
        result = StateStore.merge_scoped(state, "user", {"credits": 10}, user_id=1)
        assert result is state
        assert state["application"]["scoped"]["user:1"] == {"credits": 10}

    def test_merges_under_subkey_when_provided(self):
        state = {"application": {}}
        StateStore.merge_scoped(state, "user", {"theme": "dark"}, subkey="settings", user_id=1)
        assert state["application"]["scoped"]["user:1"] == {"settings": {"theme": "dark"}}

    def test_subkey_setdefault_preserves_existing(self):
        state = {
            "application": {"scoped": {"user:1": {"settings": {"theme": "dark", "language": "en"}}}}
        }
        StateStore.merge_scoped(state, "user", {"theme": "light"}, subkey="settings", user_id=1)
        # Existing "language" key untouched; "theme" overwritten.
        assert state["application"]["scoped"]["user:1"]["settings"] == {
            "theme": "light",
            "language": "en",
        }

    def test_falsy_scope_returns_state_untouched(self):
        state = {"application": {"scoped": {"user:1": {"credits": 10}}}}
        result = StateStore.merge_scoped(state, None, {"credits": 20}, user_id=1)
        assert result is state
        assert state["application"]["scoped"]["user:1"] == {"credits": 10}

    def test_missing_identifier_returns_state_untouched(self):
        state = {"application": {}}
        # "user" scope requires user_id; omitting it triggers ValueError inside
        # _build_scope_key, which merge_scoped swallows.
        result = StateStore.merge_scoped(state, "user", {"credits": 10})
        assert result is state
        assert "scoped" not in state["application"]

    def test_guild_scope(self):
        state = {"application": {}}
        StateStore.merge_scoped(state, "guild", {"prefix": "!"}, guild_id=100)
        assert state["application"]["scoped"]["guild:100"] == {"prefix": "!"}

    def test_user_guild_scope(self):
        state = {"application": {}}
        StateStore.merge_scoped(state, "user_guild", {"wins": 3}, user_id=1, guild_id=100)
        assert state["application"]["scoped"]["user_guild:1:100"] == {"wins": 3}

    def test_global_scope(self):
        state = {"application": {}}
        StateStore.merge_scoped(state, "global", {"version": 2})
        assert state["application"]["scoped"]["global"] == {"version": 2}

    def test_custom_slot_name(self):
        state = {"application": {}}
        StateStore.merge_scoped(
            state,
            "user",
            {"wins": 1},
            slot_name="battleship_stats",
            user_id=1,
        )
        assert state["application"]["battleship_stats"]["user:1"] == {"wins": 1}
        assert "scoped" not in state["application"]

    def test_repeated_merge_accumulates(self):
        state = {"application": {}}
        StateStore.merge_scoped(state, "user", {"a": 1}, subkey="stats", user_id=1)
        StateStore.merge_scoped(state, "user", {"b": 2}, subkey="stats", user_id=1)
        assert state["application"]["scoped"]["user:1"]["stats"] == {"a": 1, "b": 2}
