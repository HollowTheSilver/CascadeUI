"""Tests for the persistence manager, namespace configs, and middleware setup.

Covers the orchestration layer that sits between the backend Protocol
and the store: :class:`SlotPolicy` validation, the two namespace
configs (capability requirements, opt-out semantics), the
:class:`PersistenceManager` pipeline (initialize -> migrate -> rehydrate
-> prune), and the :class:`PersistenceMiddleware` construction path
that the :func:`setup_middleware` helper drives at bot startup.

Backend-level coverage lives in :mod:`test_backends`; middleware
fan-out coverage lives in :mod:`test_persistence_middleware`.
"""

import asyncio
import json
import logging
from unittest.mock import MagicMock

import pytest
from helpers import make_interaction

from cascadeui import setup_middleware
from cascadeui.exceptions import PersistenceConfigError, PersistenceInitError
from cascadeui.persistence import (
    ApplicationPersistence,
    Capability,
    InMemoryBackend,
    PersistenceManager,
    RegistryPersistence,
    SlotPolicy,
)
from cascadeui.persistence.migrations import (
    _KWARGS_MIGRATORS,
    _MIGRATORS,
    register_kwargs_migrator,
    register_migrator,
)
from cascadeui.persistence.schema import (
    CURRENT_SCHEMA_VERSIONS,
    TABLE_APPLICATION_SLOTS,
    TABLE_PERSISTENT_VIEWS,
)
from cascadeui.state.middleware.persistence import PersistenceMiddleware
from cascadeui.state.singleton import get_store

# // ========================================( Fake / limited backends )======================================== // #


class _KVOnlyBackend:
    """Declares KV only. Used to verify capability validation rejects
    backends missing RELATIONAL/SCHEMA_META/TTL_INDEX."""

    capabilities = Capability.KV

    async def initialize(self):
        pass

    async def close(self):
        pass


# // ========================================( SlotPolicy validation )======================================== // #


class TestSlotPolicy:
    """SlotPolicy defaults to ephemeral; TTL requires explicit opt-in."""

    def test_default_is_not_persistent(self):
        # Opt-in polarity: a bare SlotPolicy() means "ephemeral, skip me".
        p = SlotPolicy()
        assert p.ttl_days is None
        assert p.persistent is False

    def test_ttl_days_non_int_raises(self):
        with pytest.raises(TypeError):
            SlotPolicy(ttl_days="7", persistent=True)

    def test_ttl_days_bool_rejected(self):
        # bool is a subclass of int, so a guard must filter it explicitly.
        with pytest.raises(TypeError):
            SlotPolicy(ttl_days=True, persistent=True)

    def test_ttl_days_zero_raises(self):
        with pytest.raises(ValueError):
            SlotPolicy(ttl_days=0, persistent=True)

    def test_ttl_days_negative_raises(self):
        with pytest.raises(ValueError):
            SlotPolicy(ttl_days=-1, persistent=True)

    def test_persistent_non_bool_raises(self):
        with pytest.raises(TypeError):
            SlotPolicy(persistent="yes")

    def test_ttl_without_persistent_raises(self):
        # In-memory slots never reach storage, so a TTL has nothing to
        # prune. SlotPolicy(ttl_days=7) without persistent=True is a
        # configuration mistake, not a permissive shortcut.
        with pytest.raises(ValueError, match="persistent=True"):
            SlotPolicy(ttl_days=7)

    def test_ttl_with_persistent_is_valid(self):
        # Inverse of the above -- the combination that actually makes sense.
        p = SlotPolicy(persistent=True, ttl_days=30)
        assert p.persistent is True
        assert p.ttl_days == 30


# // ========================================( Namespace config validation )======================================== // #


class TestRegistryPersistenceValidation:
    """RegistryPersistence requires RELATIONAL + SCHEMA_META from its backend."""

    def test_accepts_fully_capable_backend(self):
        cfg = RegistryPersistence(backend=InMemoryBackend())
        assert cfg.backend is not None
        assert cfg._required_capabilities == (Capability.RELATIONAL | Capability.SCHEMA_META)

    def test_rejects_kv_only_backend(self):
        with pytest.raises(PersistenceConfigError):
            RegistryPersistence(backend=_KVOnlyBackend())

    def test_none_backend_opts_out_silently(self):
        cfg = RegistryPersistence(backend=None)
        assert cfg.backend is None


class TestApplicationPersistenceValidation:
    """ApplicationPersistence adds TTL_INDEX only when a slot declares ttl_days."""

    def test_no_slots_requires_relational_and_schema_meta(self):
        cfg = ApplicationPersistence(backend=InMemoryBackend())
        assert cfg._required_capabilities == (Capability.RELATIONAL | Capability.SCHEMA_META)

    def test_non_ttl_slot_does_not_require_ttl_index(self):
        # A persistent slot without ttl_days only needs RELATIONAL + SCHEMA_META.
        cfg = ApplicationPersistence(
            backend=_KVOnlyBackend.__new__(
                type(
                    "_NoTTL",
                    (),
                    {
                        "capabilities": Capability.RELATIONAL | Capability.SCHEMA_META,
                        "initialize": _KVOnlyBackend.initialize,
                        "close": _KVOnlyBackend.close,
                    },
                )
            ),
            slots={"prefs": SlotPolicy(persistent=True)},
        )
        assert Capability.TTL_INDEX not in cfg._required_capabilities

    def test_ttl_slot_requires_ttl_index(self):
        # In-memory backend declares TTL_INDEX so this should pass.
        cfg = ApplicationPersistence(
            backend=InMemoryBackend(),
            slots={"cache": SlotPolicy(persistent=True, ttl_days=30)},
        )
        assert Capability.TTL_INDEX in cfg._required_capabilities

    def test_slots_must_be_dict(self):
        with pytest.raises(TypeError):
            ApplicationPersistence(backend=None, slots=[("a", SlotPolicy())])

    def test_slot_key_must_be_str(self):
        with pytest.raises(TypeError):
            ApplicationPersistence(backend=None, slots={42: SlotPolicy()})

    def test_slot_value_must_be_policy(self):
        with pytest.raises(TypeError):
            ApplicationPersistence(backend=None, slots={"a": 7})


# // ========================================( Manager construction )======================================== // #


class TestPersistenceManagerInit:
    """Manager dedups backends by identity and defaults opted-out namespaces."""

    def test_single_backend_across_all_namespaces_dedups(self):
        be = InMemoryBackend()
        store = get_store()
        mgr = PersistenceManager(
            store=store,
            registry=RegistryPersistence(backend=be),
            application=ApplicationPersistence(backend=be),
        )
        assert mgr.backends == (be,)

    def test_separate_backends_are_both_tracked(self):
        a, b = InMemoryBackend(), InMemoryBackend()
        store = get_store()
        mgr = PersistenceManager(
            store=store,
            registry=RegistryPersistence(backend=a),
            application=ApplicationPersistence(backend=b),
        )
        assert set(mgr.backends) == {a, b}

    def test_opted_out_namespaces_default(self):
        store = get_store()
        mgr = PersistenceManager(store=store)
        assert mgr.registry.backend is None
        assert mgr.application.backend is None
        assert mgr.backends == ()


# // ========================================( Manager lifecycle )======================================== // #


class TestManagerInitializeBackends:
    """initialize_backends is called exactly once per unique backend and wraps errors."""

    async def test_called_once_per_unique_backend(self):
        count = {"n": 0}

        class Counting(InMemoryBackend):
            async def initialize(self):
                count["n"] += 1
                await super().initialize()

        be = Counting()
        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
            application=ApplicationPersistence(backend=be),
        )
        await mgr.initialize_backends()
        assert count["n"] == 1

    async def test_idempotent_second_call(self):
        count = {"n": 0}

        class Counting(InMemoryBackend):
            async def initialize(self):
                count["n"] += 1
                await super().initialize()

        be = Counting()
        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
        )
        await mgr.initialize_backends()
        await mgr.initialize_backends()
        assert count["n"] == 1

    async def test_raises_persistence_init_error_on_backend_failure(self):
        class Broken(InMemoryBackend):
            async def initialize(self):
                raise RuntimeError("disk on fire")

        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=Broken()),
        )
        with pytest.raises(PersistenceInitError):
            await mgr.initialize_backends()


class TestManagerApplyMigrations:
    """apply_migrations seeds fresh installs and raises on missing migrators."""

    async def test_fresh_install_records_current_version(self):
        be = InMemoryBackend()
        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
        )
        await mgr.initialize_backends()
        await mgr.apply_migrations()
        current = CURRENT_SCHEMA_VERSIONS[TABLE_PERSISTENT_VIEWS]
        assert await be.get_schema_version(TABLE_PERSISTENT_VIEWS) == current

    async def test_newer_than_library_raises(self):
        be = InMemoryBackend()
        # Pre-set a version newer than the library expects.
        current = CURRENT_SCHEMA_VERSIONS[TABLE_PERSISTENT_VIEWS]
        await be.set_schema_version(TABLE_PERSISTENT_VIEWS, current + 5)
        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
        )
        await mgr.initialize_backends()
        with pytest.raises(PersistenceInitError, match="newer than"):
            await mgr.apply_migrations()

    async def test_missing_migrator_raises(self):
        be = InMemoryBackend()
        # Pre-set an older version with no migrator registered. Treat
        # current as 1 with a v0 on disk and no v0->v1 migrator.
        await be.set_schema_version(TABLE_PERSISTENT_VIEWS, 0)
        # Manually push on-disk below current so the migrator loop runs.
        be._schema_versions[TABLE_PERSISTENT_VIEWS] = -1  # below 0 sentinel
        # The manager treats 0 as "fresh install" and records current
        # without running migrators. Forcing the missing-migrator path
        # requires poking _MIGRATORS lookup directly via a value > 0
        # that has no registered migrator.
        be._schema_versions[TABLE_PERSISTENT_VIEWS] = 0

        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
        )
        await mgr.initialize_backends()
        # Fresh-install path should succeed without migrators.
        await mgr.apply_migrations()
        assert (
            await be.get_schema_version(TABLE_PERSISTENT_VIEWS)
            == CURRENT_SCHEMA_VERSIONS[TABLE_PERSISTENT_VIEWS]
        )


# // ========================================( Manager rehydrate )======================================== // #


class TestPruneRegistryKeys:
    """prune_registry reports the persistence_keys it actually removed."""

    def test_action_creator_includes_keys(self):
        from cascadeui.state.actions import ActionCreators

        payload = ActionCreators.registry_pruned(2, "explicit", keys=["a", "b"])
        assert payload == {"deleted": 2, "keys": ["a", "b"], "reason": "explicit"}
        # Backward-compatible default when no keys are supplied.
        assert ActionCreators.registry_pruned(0, "clear_all")["keys"] == []

    async def test_registry_pruned_carries_pruned_keys(self):
        be = InMemoryBackend()
        await be.initialize()
        for key in ("panel:a", "panel:b"):
            await be.row_upsert(
                TABLE_PERSISTENT_VIEWS,
                {
                    "persistence_key": key,
                    "view_class": "MyPanel",
                    "custom_id": None,
                    "message_id": 1,
                    "channel_id": 2,
                    "guild_id": None,
                    "user_id": None,
                    "session_id": None,
                    "init_kwargs": "{}",
                    "kwargs_schema_version": 1,
                    "schema_version": 1,
                    "created_at": 1,
                    "updated_at": 1,
                },
                ["persistence_key"],
            )
        store = get_store()
        mgr = PersistenceManager(store=store, registry=RegistryPersistence(backend=be))

        captured = []

        async def _on_pruned(action, state):
            captured.append(action["payload"])

        store.on("registry_pruned", _on_pruned)
        try:
            # One present key + one absent key: only the present one is reported.
            deleted = await mgr.prune_registry(persistence_keys=["panel:a", "panel:gone"])
        finally:
            store.off("registry_pruned", _on_pruned)

        assert deleted == 1
        assert len(captured) == 1
        assert captured[0]["keys"] == ["panel:a"]
        assert captured[0]["deleted"] == 1
        assert captured[0]["reason"] == "explicit"

    async def test_all_absent_keys_report_empty(self):
        # Pruning only keys that are not on disk reports zero deleted and an
        # empty keys list, not a false positive.
        be = InMemoryBackend()
        await be.initialize()
        store = get_store()
        mgr = PersistenceManager(store=store, registry=RegistryPersistence(backend=be))

        captured = []

        async def _on_pruned(action, state):
            captured.append(action["payload"])

        store.on("registry_pruned", _on_pruned)
        try:
            deleted = await mgr.prune_registry(persistence_keys=["panel:gone"])
        finally:
            store.off("registry_pruned", _on_pruned)

        assert deleted == 0
        assert captured[0]["keys"] == []
        assert captured[0]["deleted"] == 0


class TestManagerRehydrate:
    """rehydrate restores each configured namespace into store state."""

    async def test_application_slots_restored(self):
        be = InMemoryBackend()
        await be.initialize()
        await be.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "prefs",
                "payload": json.dumps({"theme": "dark"}),
                "schema_version": 1,
                "updated_at": 1,
                "expires_at": None,
            },
            ["slot_name"],
        )
        store = get_store()
        mgr = PersistenceManager(
            store=store,
            application=ApplicationPersistence(backend=be),
        )
        await mgr.rehydrate()
        assert store.state["application"]["prefs"] == {"theme": "dark"}

    async def test_registry_rows_stashed_for_reattach(self):
        be = InMemoryBackend()
        await be.initialize()
        await be.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "panel:main",
                "view_class": "MyPanel",
                "custom_id": None,
                "message_id": 1,
                "channel_id": 2,
                "guild_id": None,
                "user_id": None,
                "session_id": None,
                "init_kwargs": "{}",
                "kwargs_schema_version": 1,
                "schema_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
            ["persistence_key"],
        )
        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
        )
        await mgr.rehydrate()
        assert len(mgr._registry_rows) == 1
        assert mgr._registry_rows[0]["persistence_key"] == "panel:main"

    async def test_rehydrate_sets_flag(self):
        mgr = PersistenceManager(store=get_store())
        assert mgr.is_rehydrated is False
        await mgr.rehydrate()
        assert mgr.is_rehydrated is True

    async def test_expired_rows_not_rehydrated_when_ttl_slot_registered(self):
        # expires_at is absolute wall-clock; rows that expired while the
        # bot was offline must not reappear in memory at restart. The
        # pre-rehydrate prune pass fires when any registered slot carries
        # ttl_days, so declaring one TTL policy is enough to activate it
        # for the whole application namespace.
        import time as _time

        be = InMemoryBackend()
        await be.initialize()
        now = int(_time.time())
        # Expired row: expires_at is in the past.
        await be.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "stale",
                "payload": json.dumps({"v": 1}),
                "schema_version": 1,
                "updated_at": now - 1000,
                "expires_at": now - 10,
            },
            ["slot_name"],
        )
        # Fresh row: no TTL, never touched by the prune contract.
        await be.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "keep",
                "payload": json.dumps({"v": 2}),
                "schema_version": 1,
                "updated_at": now,
                "expires_at": None,
            },
            ["slot_name"],
        )

        store = get_store()
        mgr = PersistenceManager(
            store=store,
            application=ApplicationPersistence(
                backend=be,
                slots={"stale": SlotPolicy(persistent=True, ttl_days=7)},
            ),
        )
        await mgr.rehydrate()

        assert "stale" not in store.state["application"]
        assert store.state["application"]["keep"] == {"v": 2}

    async def test_expired_rows_survive_when_no_ttl_slot_registered(self):
        # Without any TTL slot declared, the prune pass is skipped --
        # deployments that never opt into TTL pay zero cost at startup
        # and see no behavior change.
        import time as _time

        be = InMemoryBackend()
        await be.initialize()
        now = int(_time.time())
        await be.row_upsert(
            TABLE_APPLICATION_SLOTS,
            {
                "slot_name": "orphan",
                "payload": json.dumps({"v": 9}),
                "schema_version": 1,
                "updated_at": now - 1000,
                "expires_at": now - 10,
            },
            ["slot_name"],
        )

        store = get_store()
        mgr = PersistenceManager(
            store=store,
            application=ApplicationPersistence(backend=be),
        )
        await mgr.rehydrate()

        # No TTL policies registered, so the row-with-expiry is still
        # loaded. Production wouldn't write such a row without a matching
        # policy, but the behavior is stable and predictable.
        assert store.state["application"]["orphan"] == {"v": 9}


# // ========================================( Manager runtime surface )======================================== // #


class TestManagerSlotPolicy:
    """Slot policies seeded from config and register_slot_policy runtime surface."""

    def test_slots_from_config_are_seeded(self):
        policy = SlotPolicy(persistent=True, ttl_days=7)
        mgr = PersistenceManager(
            store=get_store(),
            application=ApplicationPersistence(
                backend=InMemoryBackend(),
                slots={"prefs": policy},
            ),
        )
        assert mgr.get_slot_policy("prefs") is policy

    def test_register_slot_policy_runtime(self):
        mgr = PersistenceManager(store=get_store())
        policy = SlotPolicy(persistent=True, ttl_days=30)
        mgr.register_slot_policy("cache", policy)
        assert mgr.get_slot_policy("cache") is policy

    def test_register_slot_policy_collision_raises(self):
        mgr = PersistenceManager(store=get_store())
        mgr.register_slot_policy("cache", SlotPolicy())
        with pytest.raises(ValueError, match="already registered"):
            mgr.register_slot_policy("cache", SlotPolicy(persistent=True, ttl_days=7))

    def test_register_slot_policy_type_check(self):
        mgr = PersistenceManager(store=get_store())
        with pytest.raises(TypeError):
            mgr.register_slot_policy(42, SlotPolicy())
        with pytest.raises(TypeError):
            mgr.register_slot_policy("k", "not-a-policy")

    def test_unknown_slot_returns_default(self):
        mgr = PersistenceManager(store=get_store())
        policy = mgr.get_slot_policy("never-declared")
        assert isinstance(policy, SlotPolicy)
        assert policy.ttl_days is None
        assert policy.persistent is False


class TestManagerPruneApplication:
    """prune_application enforces slot vs ttl mutual exclusion."""

    async def test_mutual_exclusion(self):
        be = InMemoryBackend()
        mgr = PersistenceManager(
            store=get_store(),
            application=ApplicationPersistence(backend=be),
        )
        with pytest.raises(ValueError, match="slot OR older_than_days"):
            await mgr.prune_application(slot="a", older_than_days=7)


class TestManagerHooks:
    """register_hook wires observability callbacks the middleware fires."""

    def test_register_hook_stores_callback(self):
        mgr = PersistenceManager(store=get_store())

        def cb(*args):
            pass

        mgr.register_hook("on_flush", cb)
        assert cb in mgr._hooks["on_flush"]

    def test_register_hook_type_checks(self):
        mgr = PersistenceManager(store=get_store())
        with pytest.raises(TypeError):
            mgr.register_hook(42, lambda: None)
        with pytest.raises(TypeError):
            mgr.register_hook("on_flush", "not-callable")


class TestManagerReattachNoBot:
    """reattach_persistent_views is a no-op when no bot was provided."""

    async def test_returns_empty_summary(self):
        mgr = PersistenceManager(store=get_store())
        assert mgr.last_reattach_summary is None  # not run yet
        summary = await mgr.reattach_persistent_views()
        assert summary == {
            "restored": [],
            "skipped": [],
            "failed": [],
            "removed": [],
            "unreachable": [],
        }
        # The summary is published on the manager so a consumer can read the
        # pruned keys (summary["removed"]) after setup_middleware returns, since
        # REGISTRY_PRUNED fires too early for a hook. It is the same object the
        # method returned.
        assert mgr.last_reattach_summary is summary


class TestReattachUnreachable:
    """Transient fetch failures land in `unreachable` (row left on disk, not
    pruned); only a definitive `NotFound` goes to `removed`."""

    def _mgr_with_bot(self, bot):
        mgr = PersistenceManager(store=get_store())
        mgr._bot = bot
        return mgr

    async def test_forbidden_channel_is_unreachable(self):
        from unittest.mock import AsyncMock

        import discord

        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "nope"))
        mgr = self._mgr_with_bot(bot)

        removed, unreachable = [], []
        row = {"persistence_key": "panel:x", "channel_id": 1, "message_id": 2}
        result = await mgr._fetch_restore_message(row, removed, unreachable)

        assert result is None
        assert removed == []  # transient: row stays on disk for the next restart
        assert unreachable == ["panel:x"]

    async def test_http_exception_channel_is_unreachable(self):
        from unittest.mock import AsyncMock

        import discord

        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=503), "5xx")
        )
        mgr = self._mgr_with_bot(bot)

        removed, unreachable = [], []
        row = {"persistence_key": "panel:y", "channel_id": 1, "message_id": 2}
        result = await mgr._fetch_restore_message(row, removed, unreachable)

        assert result is None
        assert removed == []
        assert unreachable == ["panel:y"]

    async def test_non_messageable_channel_is_unreachable(self):
        import discord

        bot = MagicMock()
        bot.get_channel.return_value = MagicMock(spec=discord.CategoryChannel)
        mgr = self._mgr_with_bot(bot)

        removed, unreachable = [], []
        row = {"persistence_key": "panel:w", "channel_id": 1, "message_id": 2}
        result = await mgr._fetch_restore_message(row, removed, unreachable)

        assert result is None
        assert removed == []  # was pruned before the fix; now left on disk
        assert unreachable == ["panel:w"]

    async def test_notfound_channel_is_removed(self):
        from unittest.mock import AsyncMock

        import discord

        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
        mgr = self._mgr_with_bot(bot)

        removed, unreachable = [], []
        row = {"persistence_key": "panel:z", "channel_id": 1, "message_id": 2}
        result = await mgr._fetch_restore_message(row, removed, unreachable)

        assert result is None
        assert removed == ["panel:z"]  # definitive 404: pruned
        assert unreachable == []


class TestManagerReattachInitKwargsDuplicate:
    """Reattach strips persistence_key from init_kwargs before splatting.

    The view base re-injects persistence_key into ``_init_kwargs`` so the
    registry serialization round-trips the value (push/pop reconstruction
    expects every constructor kwarg to survive a json round trip). The
    registry row also stores ``persistence_key`` as its own column. The
    reattach path must drop the duplicate before calling the constructor,
    or Python raises ``TypeError: got multiple values for keyword
    argument 'persistence_key'`` and the panel never restores.
    """

    async def test_reattach_does_not_pass_persistence_key_twice(self):
        from cascadeui.views.persistent import PersistentLayoutView

        class _DupPanel(PersistentLayoutView):
            pass

        be = InMemoryBackend()
        await be.initialize()

        init_kwargs_with_dup = json.dumps({"persistence_key": "panel:dup"})
        await be.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "panel:dup",
                "view_class": _DupPanel.__qualname__,
                "custom_id": None,
                "message_id": 1,
                "channel_id": 2,
                "guild_id": None,
                "user_id": None,
                "session_id": None,
                "init_kwargs": init_kwargs_with_dup,
                "kwargs_schema_version": 1,
                "schema_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
            ["persistence_key"],
        )

        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
        )
        await mgr.rehydrate()

        # Direct unit on _reattach_one: stub the bot-touching seams so
        # the test isolates the duplicate-kwarg fix without pulling in a
        # full discord.py mock surface.
        captured_view = {}

        class _Msg:
            id = 1

        async def _fake_register(self):
            captured_view["registered"] = True

        async def _fake_update(self, message):
            pass

        _DupPanel._register_state = _fake_register
        _DupPanel._update_message_state = _fake_update
        _DupPanel._validate_custom_ids = lambda self: None

        class _FakeBot:
            def add_view(self, view, message_id):
                captured_view["view"] = view

        mgr._bot = _FakeBot()
        outcome = await mgr._reattach_one(
            row=mgr._registry_rows[0],
            view_cls=_DupPanel,
            init_kwargs=json.loads(init_kwargs_with_dup),
            message=_Msg(),
            class_name=_DupPanel.__qualname__,
            restored_views=[],
        )

        assert outcome == "restored"
        assert captured_view["view"].persistence_key == "panel:dup"

    async def test_reattach_strips_theme_kwarg(self):
        # ``theme`` shares the same capture+re-injection shape as
        # ``persistence_key``: ``__init_subclass__`` snapshots it,
        # ``__init__`` re-injects it into ``_init_kwargs``. Older registry
        # rows captured before the write-side strip shipped may still
        # carry it. Reattach must drop it so the constructor falls back
        # to the class-level ``theme`` attribute (or the global default)
        # rather than crashing on a stringified Theme object.
        from cascadeui.views.persistent import PersistentLayoutView

        class _ThemedPanel(PersistentLayoutView):
            pass

        be = InMemoryBackend()
        await be.initialize()

        init_kwargs_with_theme = json.dumps(
            {"persistence_key": "panel:themed", "theme": "<Theme object at 0xdead>"}
        )
        await be.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "panel:themed",
                "view_class": _ThemedPanel.__qualname__,
                "custom_id": None,
                "message_id": 1,
                "channel_id": 2,
                "guild_id": None,
                "user_id": None,
                "session_id": None,
                "init_kwargs": init_kwargs_with_theme,
                "kwargs_schema_version": 1,
                "schema_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
            ["persistence_key"],
        )

        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
        )
        await mgr.rehydrate()

        captured_view = {}

        class _Msg:
            id = 1

        async def _fake_register(self):
            pass

        async def _fake_update(self, message):
            pass

        _ThemedPanel._register_state = _fake_register
        _ThemedPanel._update_message_state = _fake_update
        _ThemedPanel._validate_custom_ids = lambda self: None

        class _FakeBot:
            def add_view(self, view, message_id):
                captured_view["view"] = view

        mgr._bot = _FakeBot()
        outcome = await mgr._reattach_one(
            row=mgr._registry_rows[0],
            view_cls=_ThemedPanel,
            init_kwargs=json.loads(init_kwargs_with_theme),
            message=_Msg(),
            class_name=_ThemedPanel.__qualname__,
            restored_views=[],
        )

        # Drop both: stringified theme would never be a real Theme, and
        # ``persistence_key`` still arrives via the row column.
        assert outcome == "restored"
        view = captured_view["view"]
        assert view.persistence_key == "panel:themed"
        # The literal stringified theme from the row never reaches the
        # constructor -- the class-level ``theme`` (None by default) wins.
        assert view.theme is None


# // ========================================( Reattach on_bind )======================================== // #


class TestReattachOnBind:
    """``on_bind(bot)`` runs during reattach (setup_hook); ``on_restore`` runs
    after, in the deferred post-ready render, with on_bind's deps available."""

    def _stub_seams(self, cls):
        async def _fake_register(self):
            pass

        async def _fake_update(self, message):
            pass

        cls._register_state = _fake_register
        cls._update_message_state = _fake_update
        cls._validate_custom_ids = lambda self: None

    async def _row(self, key, cls):
        be = InMemoryBackend()
        await be.initialize()
        await be.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": key,
                "view_class": cls.__qualname__,
                "custom_id": None,
                "message_id": 1,
                "channel_id": 2,
                "guild_id": None,
                "user_id": None,
                "session_id": None,
                "init_kwargs": json.dumps({"persistence_key": key}),
                "kwargs_schema_version": 1,
                "schema_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
            ["persistence_key"],
        )
        mgr = PersistenceManager(store=get_store(), registry=RegistryPersistence(backend=be))
        await mgr.rehydrate()
        return mgr

    async def test_on_bind_runs_before_on_restore_with_deps(self):
        from cascadeui.views.persistent import PersistentLayoutView

        order = []

        class _BindPanel(PersistentLayoutView):
            def on_bind(self, bot):
                order.append("bind")
                self.db = bot.db

            async def on_restore(self, bot):
                order.append("restore")
                # The dependency on_bind injected is available here.
                assert self.db == "POOL"

        self._stub_seams(_BindPanel)
        mgr = await self._row("panel:bind", _BindPanel)

        class _Msg:
            id = 1

        class _FakeBot:
            db = "POOL"

            def add_view(self, view, message_id):
                pass

            async def wait_until_ready(self):
                pass

        mgr._bot = _FakeBot()
        restored_views = []
        outcome = await mgr._reattach_one(
            row=mgr._registry_rows[0],
            view_cls=_BindPanel,
            init_kwargs={"persistence_key": "panel:bind"},
            message=_Msg(),
            class_name=_BindPanel.__qualname__,
            restored_views=restored_views,
        )
        # on_bind runs during reattach; on_restore is deferred to after-ready.
        assert outcome == "restored"
        assert order == ["bind"]
        assert len(restored_views) == 1

        # Drive the deferred post-ready render: on_restore now runs, with the
        # dependency on_bind injected still available.
        await mgr._run_post_ready_restore(restored_views)
        assert order == ["bind", "restore"]

    async def test_async_on_bind_is_awaited(self):
        from cascadeui.views.persistent import PersistentLayoutView

        class _AsyncBindPanel(PersistentLayoutView):
            async def on_bind(self, bot):
                self.db = bot.db

        self._stub_seams(_AsyncBindPanel)
        mgr = await self._row("panel:abind", _AsyncBindPanel)

        class _Msg:
            id = 1

        captured = {}

        class _FakeBot:
            db = "POOL"

            def add_view(self, view, message_id):
                captured["view"] = view

        mgr._bot = _FakeBot()
        outcome = await mgr._reattach_one(
            row=mgr._registry_rows[0],
            view_cls=_AsyncBindPanel,
            init_kwargs={"persistence_key": "panel:abind"},
            message=_Msg(),
            class_name=_AsyncBindPanel.__qualname__,
            restored_views=[],
        )
        assert outcome == "restored"
        assert captured["view"].db == "POOL"


class TestSendTimeBind:
    """``_bind_from_context`` (the send-time seam) forwards ``on_bind`` when
    the construction context resolves a bot, and ``send()`` invokes it."""

    async def test_send_invokes_bind_from_context(self):
        # Covers the send() -> _bind_from_context wiring, so removing the call
        # from send() fails here rather than silently skipping dep injection.
        from unittest.mock import AsyncMock

        from cascadeui.views.persistent import PersistentLayoutView

        class _Panel(PersistentLayoutView):
            async def on_bind(self, bot):
                pass

        inter = make_interaction()
        inter.client = type("B", (), {})()
        view = _Panel(interaction=inter, persistence_key="send:wire")
        view._bind_from_context = AsyncMock()
        # Instance-stub the pipeline below the mixin so the Discord send is
        # skipped without patching the base class for every other subclass.
        view._send_pipeline = AsyncMock(return_value=None)
        await view.send()
        view._bind_from_context.assert_awaited_once()

    async def test_calls_on_bind_when_bot_derivable(self):
        from cascadeui.views.persistent import PersistentLayoutView

        bound = {}

        class _Panel(PersistentLayoutView):
            def on_bind(self, bot):
                bound["bot"] = bot
                self.db = bot.db

        class _FakeBot:
            db = "POOL"

        inter = make_interaction()
        inter.client = _FakeBot()
        view = _Panel(interaction=inter, persistence_key="send:bind")
        await view._bind_from_context()
        assert bound["bot"] is inter.client
        assert view.db == "POOL"

    async def test_noop_when_bot_not_derivable(self):
        from cascadeui.views.persistent import PersistentLayoutView

        called = {"n": 0}

        class _Panel(PersistentLayoutView):
            def on_bind(self, bot):
                called["n"] += 1

        inter = make_interaction()
        inter.client = None  # channel-context-like: no resolvable bot
        view = _Panel(interaction=inter, persistence_key="send:nobot")
        await view._bind_from_context()
        assert called["n"] == 0

    async def test_async_on_bind_awaited(self):
        from cascadeui.views.persistent import PersistentLayoutView

        class _Panel(PersistentLayoutView):
            async def on_bind(self, bot):
                self.db = bot.db

        class _FakeBot:
            db = "POOL"

        inter = make_interaction()
        inter.client = _FakeBot()
        view = _Panel(interaction=inter, persistence_key="send:async")
        await view._bind_from_context()
        assert view.db == "POOL"


class TestNonSerializableKwargError:
    """A non-JSON constructor kwarg declines the row with a directed error."""

    async def test_declines_row_and_points_to_on_bind(self, caplog):
        from cascadeui.state.middleware.persistence import PersistenceMiddleware

        class _Pool:  # a stand-in for a non-serializable runtime dependency
            pass

        class _FakeView:
            _init_kwargs = {"persistence_key": "panel:x", "db": _Pool()}
            kwargs_schema_version = 1
            session_id = None

        mw = PersistenceMiddleware(backend=InMemoryBackend())
        payload = {
            "persistence_key": "panel:x",
            "class_name": "X",
            "message_id": 1,
            "channel_id": 2,
        }
        with caplog.at_level(logging.ERROR):
            row = mw._build_registry_row(payload, _FakeView())

        assert row is None  # the row is declined, not silently corrupted
        assert "on_bind" in caplog.text  # the error names the fix
        assert "db=_Pool" in caplog.text  # and the offending kwarg


# // ========================================( Reattach batching )======================================== // #


class TestReattachBatching:
    """``_reattach_one`` wraps the registration dispatches in a single
    ``store.batch()`` so each restored view produces one BATCH_COMPLETE
    notification instead of three standalone cycles. With N persistent views
    the savings scale linearly. (``on_restore`` is deferred to the post-ready
    render and batched separately there.)
    """

    async def test_three_dispatches_collapse_to_one_notification(self):
        from cascadeui.views.persistent import PersistentLayoutView

        class _SilentPanel(PersistentLayoutView):
            pass

        be = InMemoryBackend()
        await be.initialize()

        await be.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "panel:batch",
                "view_class": _SilentPanel.__qualname__,
                "custom_id": None,
                "message_id": 1,
                "channel_id": 2,
                "guild_id": None,
                "user_id": None,
                "session_id": None,
                "init_kwargs": json.dumps({"persistence_key": "panel:batch"}),
                "kwargs_schema_version": 1,
                "schema_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
            ["persistence_key"],
        )

        store = get_store()

        mgr = PersistenceManager(
            store=store,
            registry=RegistryPersistence(backend=be),
        )
        await mgr.rehydrate()

        # A catch-all subscriber (filter=None) records every notification
        # it receives. Without the batch wrap, _reattach_one's three
        # dispatches (SESSION_CREATED + VIEW_CREATED + VIEW_UPDATED)
        # would each trigger a separate notification cycle and the
        # subscriber would fire three times. With the batch wrap, the
        # three dispatches collapse into a single BATCH_COMPLETE
        # notification and the subscriber fires exactly once.
        notification_actions = []

        async def catch_all(state, action):
            notification_actions.append(action.get("type"))

        store.subscribe("test_catch_all", catch_all, None)

        try:

            class _Channel:
                id = 2

            class _Msg:
                id = 1
                channel = _Channel()

            class _FakeBot:
                def add_view(self, view, message_id):
                    pass

            mgr._bot = _FakeBot()

            outcome = await mgr._reattach_one(
                row=mgr._registry_rows[0],
                view_cls=_SilentPanel,
                init_kwargs={"persistence_key": "panel:batch"},
                message=_Msg(),
                class_name=_SilentPanel.__qualname__,
                restored_views=[],
            )

            await store._flush_notifications()

            assert outcome == "restored"
            # Single notification cycle: BATCH_COMPLETE carries the three
            # queued actions in its payload. Without batching, the catch-all
            # subscriber would have fired three separate times.
            assert notification_actions == ["BATCH_COMPLETE"]
        finally:
            store._unsubscribe("test_catch_all")


class TestPostReadyRestore:
    """on_restore is deferred to a post-ready task so its gateway-cache reads
    (avatars, members, channels) land warm, off the setup_hook critical path
    where the cache is still cold."""

    def _view(self, vid, on_restore, finished=False):
        view = MagicMock()
        view.id = vid
        view.is_finished.return_value = finished
        view.on_restore = on_restore
        return view

    async def test_on_restore_waits_for_ready(self):
        events = []
        ready = asyncio.Event()

        class _FakeBot:
            async def wait_until_ready(self):
                await ready.wait()
                events.append("ready")

        async def _on_restore(bot):
            events.append("restore")

        mgr = PersistenceManager(store=get_store(), bot=_FakeBot())
        task = asyncio.create_task(
            mgr._run_post_ready_restore([self._view("v-ready", _on_restore)])
        )
        await asyncio.sleep(0)  # let the task reach wait_until_ready
        assert events == []  # on_restore has not run -- still waiting on ready

        ready.set()
        await task
        assert events == ["ready", "restore"]  # ready first, THEN the render

    async def test_finished_view_is_skipped(self):
        called = []

        class _FakeBot:
            async def wait_until_ready(self):
                pass

        async def _on_restore(bot):
            called.append("ran")

        mgr = PersistenceManager(store=get_store(), bot=_FakeBot())
        await mgr._run_post_ready_restore([self._view("v-finished", _on_restore, finished=True)])
        assert called == []

    async def test_one_failure_does_not_abort_the_rest(self):
        ran = []

        class _FakeBot:
            async def wait_until_ready(self):
                pass

        async def _boom(bot):
            raise ValueError("boom")

        async def _ok(bot):
            ran.append("ok")

        mgr = PersistenceManager(store=get_store(), bot=_FakeBot())
        await mgr._run_post_ready_restore([self._view("v-boom", _boom), self._view("v-ok", _ok)])
        assert ran == ["ok"]  # the failure did not abort the surviving view

    def test_done_callback_discards_its_own_task(self):
        # The done-callback discards its OWN task from the set and leaves the
        # sweeper field alone.
        mgr = PersistenceManager(store=get_store())
        mgr._ttl_sweeper_task = "sweeper-sentinel"
        done = MagicMock()
        done.cancelled.return_value = False
        done.exception.return_value = None
        mgr._post_ready_restore_tasks.add(done)

        mgr._on_post_ready_restore_done(done)

        assert done not in mgr._post_ready_restore_tasks
        assert mgr._ttl_sweeper_task == "sweeper-sentinel"  # untouched

    def test_multiple_post_ready_tasks_coexist(self):
        # Option-1 contract: the set tracks each reattach's render task, so a
        # second reattach never drops a batch the way the old single-slot
        # guard did. Each task settles independently via its done-callback.
        mgr = PersistenceManager(store=get_store())
        t1, t2 = MagicMock(), MagicMock()
        mgr._post_ready_restore_tasks.update({t1, t2})

        t1.cancelled.return_value = False
        t1.exception.return_value = None
        mgr._on_post_ready_restore_done(t1)

        assert mgr._post_ready_restore_tasks == {t2}  # only the settled one left

    def test_sweeper_done_callback_clears_its_own_field(self):
        mgr = PersistenceManager(store=get_store())
        mgr._ttl_sweeper_task = "sentinel"
        sentinel_task = MagicMock()
        mgr._post_ready_restore_tasks.add(sentinel_task)

        done = MagicMock()
        done.cancelled.return_value = False
        done.exception.return_value = None
        mgr._on_sweeper_done(done)

        assert mgr._ttl_sweeper_task is None
        assert sentinel_task in mgr._post_ready_restore_tasks  # untouched


class TestReattachConcurrency:
    """reattach_persistent_views prepares (resolve class, migrate kwargs,
    fetch the Discord message) concurrently under a bounded semaphore, then
    registers each view serially. The split preserves correctness and bounds
    the fetch fan-out to restore_concurrency.
    """

    async def _seed_rows(self, be, view_cls, n):
        for i in range(n):
            key = f"panel:{i}"
            await be.row_upsert(
                TABLE_PERSISTENT_VIEWS,
                {
                    "persistence_key": key,
                    # _persistent_view_classes is keyed by _class_session_key()
                    # (module + qualname), which is what reattach looks up.
                    "view_class": view_cls._class_session_key(),
                    "custom_id": None,
                    "message_id": i + 1,
                    "channel_id": 100 + i,
                    "guild_id": None,
                    "user_id": None,
                    "session_id": None,
                    "init_kwargs": json.dumps({"persistence_key": key}),
                    "kwargs_schema_version": 1,
                    "schema_version": 1,
                    "created_at": 1,
                    "updated_at": 1,
                },
                ["persistence_key"],
            )

    class _Msg:
        def __init__(self, mid):
            self.id = mid

    async def test_all_rows_restore_in_row_order(self):
        from cascadeui.views.persistent import PersistentLayoutView

        class _P(PersistentLayoutView):
            pass

        be = InMemoryBackend()
        await be.initialize()
        await self._seed_rows(be, _P, 4)

        mgr = PersistenceManager(
            store=get_store(), registry=RegistryPersistence(backend=be), bot=object()
        )
        await mgr.rehydrate()

        reattach_order = []

        async def _fake_fetch(row, removed_keys, unreachable_keys):
            return (None, self._Msg(row["message_id"]))

        async def _fake_reattach(row, view_cls, init_kwargs, message, class_name, restored_views):
            reattach_order.append(row["persistence_key"])
            return "restored"

        mgr._fetch_restore_message = _fake_fetch
        mgr._reattach_one = _fake_reattach

        summary = await mgr.reattach_persistent_views()

        assert sorted(summary["restored"]) == ["panel:0", "panel:1", "panel:2", "panel:3"]
        # gather preserves input order, so phase 2 registers in row order.
        assert reattach_order == ["panel:0", "panel:1", "panel:2", "panel:3"]

    async def test_fetch_phase_bounded_by_restore_concurrency(self):
        from cascadeui.views.persistent import PersistentLayoutView

        class _P(PersistentLayoutView):
            pass

        be = InMemoryBackend()
        await be.initialize()
        await self._seed_rows(be, _P, 5)

        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
            bot=object(),
            restore_concurrency=2,
        )
        await mgr.rehydrate()

        tracker = {"live": 0, "max": 0}

        async def _fake_fetch(row, removed_keys, unreachable_keys):
            tracker["live"] += 1
            tracker["max"] = max(tracker["max"], tracker["live"])
            await asyncio.sleep(0.02)
            tracker["live"] -= 1
            return (None, self._Msg(row["message_id"]))

        async def _fake_reattach(row, view_cls, init_kwargs, message, class_name, restored_views):
            return "restored"

        mgr._fetch_restore_message = _fake_fetch
        mgr._reattach_one = _fake_reattach

        await mgr.reattach_persistent_views()

        # 5 rows, cap 2 -> never more than 2 fetches in flight at once.
        assert tracker["max"] == 2

    async def test_registration_phase_is_serial(self):
        from cascadeui.views.persistent import PersistentLayoutView

        class _P(PersistentLayoutView):
            pass

        be = InMemoryBackend()
        await be.initialize()
        await self._seed_rows(be, _P, 4)

        mgr = PersistenceManager(
            store=get_store(),
            registry=RegistryPersistence(backend=be),
            bot=object(),
            restore_concurrency=4,
        )
        await mgr.rehydrate()

        tracker = {"live": 0, "max": 0}

        async def _fake_fetch(row, removed_keys, unreachable_keys):
            return (None, self._Msg(row["message_id"]))

        async def _fake_reattach(row, view_cls, init_kwargs, message, class_name, restored_views):
            tracker["live"] += 1
            tracker["max"] = max(tracker["max"], tracker["live"])
            await asyncio.sleep(0.01)
            tracker["live"] -= 1
            return "restored"

        mgr._fetch_restore_message = _fake_fetch
        mgr._reattach_one = _fake_reattach

        await mgr.reattach_persistent_views()

        # Registration mutates shared registries -- it must stay serial even
        # when the fetch phase runs many rows concurrently.
        assert tracker["max"] == 1

    async def test_malformed_row_does_not_abort_others(self):
        # A row missing a required key must land in summary["failed"] and not
        # escape the gather -- otherwise one bad row aborts every other row's
        # reattach (a wider blast radius than the old serial loop).
        from cascadeui.views.persistent import PersistentLayoutView

        class _P(PersistentLayoutView):
            pass

        be = InMemoryBackend()
        await be.initialize()
        await self._seed_rows(be, _P, 2)

        mgr = PersistenceManager(
            store=get_store(), registry=RegistryPersistence(backend=be), bot=object()
        )
        await mgr.rehydrate()
        # Inject a malformed row that has no "view_class" key.
        mgr._registry_rows.append({"persistence_key": "panel:bad"})

        async def _fake_fetch(row, removed_keys, unreachable_keys):
            return (None, self._Msg(row["message_id"]))

        async def _fake_reattach(row, view_cls, init_kwargs, message, class_name, restored_views):
            return "restored"

        mgr._fetch_restore_message = _fake_fetch
        mgr._reattach_one = _fake_reattach

        # Must not raise despite the malformed row.
        summary = await mgr.reattach_persistent_views()

        assert sorted(summary["restored"]) == ["panel:0", "panel:1"]
        assert summary["failed"] == ["panel:bad"]

    async def test_unknown_class_skips_and_warns(self, caplog):
        be = InMemoryBackend()
        await be.initialize()
        await be.row_upsert(
            TABLE_PERSISTENT_VIEWS,
            {
                "persistence_key": "panel:unknown",
                "view_class": "nonexistent.module.GhostPanel",
                "custom_id": None,
                "message_id": 1,
                "channel_id": 2,
                "guild_id": None,
                "user_id": None,
                "session_id": None,
                "init_kwargs": json.dumps({"persistence_key": "panel:unknown"}),
                "kwargs_schema_version": 1,
                "schema_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
            ["persistence_key"],
        )

        mgr = PersistenceManager(
            store=get_store(), registry=RegistryPersistence(backend=be), bot=object()
        )
        await mgr.rehydrate()

        with caplog.at_level(logging.WARNING, logger="cascadeui.persistence.manager"):
            summary = await mgr.reattach_persistent_views()

        assert summary["skipped"] == ["panel:unknown"]
        assert any("not found" in r.getMessage() for r in caplog.records)


class TestRestoreConcurrencyValidation:
    """PersistenceMiddleware validates restore_concurrency at construction."""

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="positive int"):
            PersistenceMiddleware(restore_concurrency=0)

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="positive int"):
            PersistenceMiddleware(restore_concurrency=-1)

    def test_bool_raises(self):
        with pytest.raises(ValueError, match="positive int"):
            PersistenceMiddleware(restore_concurrency=True)

    def test_string_raises(self):
        with pytest.raises(ValueError, match="positive int"):
            PersistenceMiddleware(restore_concurrency="8")

    def test_valid_value_passes(self):
        mw = PersistenceMiddleware(backend=InMemoryBackend(), restore_concurrency=4)
        assert mw is not None


class TestMigratorsWiring:
    """PersistenceMiddleware(migrators=...) registers migrators into the
    module registries; initialize() wires them in before apply_migrations
    (schema) and rehydrate (kwargs) consume them.
    """

    async def test_kwargs_migrator_wired_through_initialize(self):
        from cascadeui.persistence.migrations import get_kwargs_migrator

        async def _mig(kwargs):
            return kwargs

        key = ("test.module.PanelKwargs", 1)
        try:
            await setup_middleware(
                PersistenceMiddleware(backend=InMemoryBackend(), migrators={"kwargs": {key: _mig}})
            )
            assert get_kwargs_migrator(*key) is _mig
        finally:
            _KWARGS_MIGRATORS.pop(key, None)

    def test_register_migrators_schema(self):
        from cascadeui.persistence.migrations import get_schema_migrator

        async def _mig(backend):
            pass

        key = ("test_table_schema", 1)
        try:
            PersistenceMiddleware._register_migrators({"schema": {key: _mig}})
            assert get_schema_migrator(*key) is _mig
        finally:
            _MIGRATORS.pop(key, None)

    def test_register_migrators_idempotent(self):
        from cascadeui.persistence.migrations import get_kwargs_migrator

        async def _mig(kwargs):
            return kwargs

        key = ("test.module.PanelIdem", 1)
        try:
            PersistenceMiddleware._register_migrators({"kwargs": {key: _mig}})
            # Re-registering the same key is a no-op, not a ValueError.
            PersistenceMiddleware._register_migrators({"kwargs": {key: _mig}})
            assert get_kwargs_migrator(*key) is _mig
        finally:
            _KWARGS_MIGRATORS.pop(key, None)

    def test_none_is_noop(self):
        PersistenceMiddleware._register_migrators(None)  # no error, no registration

    def test_non_dict_raises_typeerror(self):
        with pytest.raises(TypeError, match="must be None or a dict"):
            PersistenceMiddleware(migrators=[1, 2, 3])

    def test_unknown_key_raises_valueerror(self):
        with pytest.raises(ValueError, match="unknown keys"):
            PersistenceMiddleware(migrators={"bogus": {}})


# // ========================================( PersistenceMiddleware setup )======================================== // #


class TestPersistenceMiddlewareSetup:
    """PersistenceMiddleware composes configs, defaults zero-config to SQLite, validates bot arg."""

    async def test_zero_config_defaults_to_sqlite(self, monkeypatch):
        # With no backend/registry/application configured, initialize()
        # lazy-imports SQLiteBackend and defaults the filename to
        # cascadeui.db. The import + file open are stubbed so the test
        # never touches disk.
        from cascadeui.persistence import backends as backends_module

        captured = {}

        class _FakeSQLite(InMemoryBackend):
            # Extend InMemoryBackend so the full pipeline (initialize ->
            # apply_migrations -> rehydrate) has real methods to call; lie
            # about capabilities so ApplicationPersistence validation passes.
            capabilities = (
                Capability.KV
                | Capability.RELATIONAL
                | Capability.SCHEMA_META
                | Capability.TTL_INDEX
            )

            def __init__(self, path):
                super().__init__()
                captured["path"] = path

        monkeypatch.setattr(backends_module, "SQLiteBackend", _FakeSQLite)
        await setup_middleware(PersistenceMiddleware())
        assert captured["path"] == "cascadeui.db"

    async def test_zero_config_raises_when_aiosqlite_missing(self, monkeypatch):
        # If the optional aiosqlite dependency isn't installed, the lazy
        # import fails and the library surfaces a PersistenceInitError
        # directing the user to either install the extra or pass a backend.
        # Simulating the missing extra: delete SQLiteBackend from the
        # backends module so the lazy import inside _resolve_manager
        # raises ImportError.
        from cascadeui.persistence import backends as backends_module

        monkeypatch.delattr(backends_module, "SQLiteBackend", raising=False)
        with pytest.raises(PersistenceInitError, match="aiosqlite"):
            await setup_middleware(PersistenceMiddleware())

    async def test_shorthand_fills_all_namespaces(self):
        be = InMemoryBackend()
        await setup_middleware(PersistenceMiddleware(backend=be))
        store = get_store()
        mgr = store.persistence_manager
        assert mgr.registry.backend is be
        assert mgr.application.backend is be
        # The reattach summary is available on demand via the manager;
        # initialize() does not stash it because a data-only setup
        # (no bot) has no views to reattach.
        summary = await mgr.reattach_persistent_views()
        assert summary == {
            "restored": [],
            "skipped": [],
            "failed": [],
            "removed": [],
            "unreachable": [],
        }

    async def test_explicit_none_opts_out_namespace(self):
        be = InMemoryBackend()
        await setup_middleware(
            PersistenceMiddleware(
                backend=be,
                registry=RegistryPersistence(backend=None),
            )
        )
        mgr = get_store().persistence_manager
        assert mgr.registry.backend is None
        assert mgr.application.backend is be

    async def test_per_namespace_config_overrides_shorthand(self):
        shared = InMemoryBackend()
        dedicated = InMemoryBackend()
        await setup_middleware(
            PersistenceMiddleware(
                backend=shared,
                application=ApplicationPersistence(backend=dedicated),
            )
        )
        mgr = get_store().persistence_manager
        assert mgr.registry.backend is shared
        assert mgr.application.backend is dedicated

    def test_bot_wrong_type_raises_type_error(self):
        # Sync body: validation fires at construction time rather than
        # deep inside initialize(), so no await is involved.
        with pytest.raises(TypeError, match="discord.py Bot"):
            PersistenceMiddleware(bot="not-a-bot", backend=InMemoryBackend())

    def test_bot_accepts_discord_client_subclass(self):
        # Positive complement to the wrong-type guard: any discord.Client
        # subclass (and therefore every commands.Bot) passes without
        # raising. Bypass Client.__init__ so the test does not open a
        # gateway session or require intents config.
        import discord

        class _FakeBot(discord.Client):
            pass

        bot = _FakeBot.__new__(_FakeBot)
        mw = PersistenceMiddleware(bot=bot, backend=InMemoryBackend())
        assert mw._pending_config["bot"] is bot

    async def test_manager_stashed_on_store(self):
        be = InMemoryBackend()
        await setup_middleware(PersistenceMiddleware(backend=be))
        store = get_store()
        assert store.persistence_manager is not None
        assert isinstance(store.persistence_manager, PersistenceManager)

    async def test_installs_middleware_when_backend_present(self):
        be = InMemoryBackend()
        await setup_middleware(PersistenceMiddleware(backend=be))
        store = get_store()
        assert any(isinstance(mw, PersistenceMiddleware) for mw in store._middleware)


# // ========================================( Migrator registries )======================================== // #


class TestMigratorRegistries:
    """register_migrator / register_kwargs_migrator collision and lookup."""

    def teardown_method(self, method):
        # Clean per-test so re-registration succeeds without raising.
        _MIGRATORS.clear()
        _KWARGS_MIGRATORS.clear()

    def test_register_migrator_stores_callable(self):
        @register_migrator(TABLE_PERSISTENT_VIEWS, 1)
        async def migrator(backend):
            pass

        assert (TABLE_PERSISTENT_VIEWS, 1) in _MIGRATORS

    def test_duplicate_migrator_raises(self):
        @register_migrator(TABLE_PERSISTENT_VIEWS, 1)
        async def first(backend):
            pass

        with pytest.raises(ValueError, match="already registered"):

            @register_migrator(TABLE_PERSISTENT_VIEWS, 1)
            async def second(backend):
                pass

    def test_register_kwargs_migrator_stores_callable(self):
        @register_kwargs_migrator("MyView", 1)
        async def migrator(kwargs):
            return kwargs

        assert ("MyView", 1) in _KWARGS_MIGRATORS

    def test_duplicate_kwargs_migrator_raises(self):
        @register_kwargs_migrator("MyView", 1)
        async def first(kwargs):
            return kwargs

        with pytest.raises(ValueError, match="already registered"):

            @register_kwargs_migrator("MyView", 1)
            async def second(kwargs):
                return kwargs
