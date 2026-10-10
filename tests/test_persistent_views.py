"""Tests for persistent views: class registry, custom_id validation, and reducers."""

import asyncio
import json
import logging
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cascadeui import RenderOutcome
from cascadeui.components.base import StatefulButton
from cascadeui.state.reducers import (
    reduce_persistent_view_registered,
    reduce_persistent_view_unregistered,
)
from cascadeui.views.base import _StatefulMixin
from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.persistent import (
    PersistentLayoutView,
    PersistentView,
    _persistent_view_classes,
    _PersistentMixin,
)
from cascadeui.views.view import StatefulView


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
    """PersistentView subclasses auto-register in the class registry."""

    def test_subclass_auto_registered(self):
        """Subclassing PersistentView should auto-register the class."""

        class _TestPanel(PersistentView):
            pass

        key = _TestPanel._class_session_key()
        assert key in _persistent_view_classes
        assert _persistent_view_classes[key] is _TestPanel
        # Qualified path keeps unrelated cogs from colliding on bare class name.
        assert key.endswith("._TestPanel")

    def test_nested_subclass_registered(self):
        """Subclasses of subclasses should also be registered."""

        class _BasePanel(PersistentView):
            pass

        class _ChildPanel(_BasePanel):
            pass

        assert _ChildPanel._class_session_key() in _persistent_view_classes

    def test_two_classes_sharing_a_pin_are_refused(self):
        """The registry holds one class per key, so the second definition
        displaced the first and every row written by either reattached as
        whichever was defined last, silently and in the wrong class."""

        class _PinnedA(PersistentView):
            session_class_key = "fam.SharedPanel"

        with pytest.raises(ValueError, match="both resolve to the persistent class key"):

            class _PinnedB(PersistentView):
                session_class_key = "fam.SharedPanel"

        assert _persistent_view_classes["fam.SharedPanel"] is _PinnedA
        # Rejection precedes every registry write, so a refused class is
        # not resolvable afterwards. The check runs before the super() call
        # that registers the class path.
        from cascadeui.views.base import _view_class_registry

        assert not [k for k in _view_class_registry if k.endswith("._PinnedB")]

    @pytest.mark.parametrize(
        "pin", [123, ["a"], {"k": 1}, ""], ids=["int", "list", "dict", "empty"]
    )
    def test_a_wrong_pin_shape_is_refused_before_it_keys_a_registry(self, pin):
        """The persistent registry keys on the pin, so its shape settles first.

        An unhashable value reached ``dict.get`` and raised from inside the
        lookup, naming neither the attribute nor the fix -- on the one class
        family the pin exists for.
        """
        from cascadeui.views.base import _view_class_registry

        before = set(_view_class_registry)
        with pytest.raises(ValueError, match="session_class_key must be a non-empty string"):

            class _BadPin(PersistentView):
                session_class_key = pin

        # A refused definition is never left resolvable by a later lookup.
        assert not [k for k in set(_view_class_registry) - before if "_BadPin" in k]

    def test_redefining_the_same_class_still_overwrites(self):
        """A cog reload rebuilds the class object under the same path. That
        is the intended overwrite, and must not read as a collision."""
        source = (
            "from cascadeui import PersistentView\n"
            "class _Reloadable(PersistentView):\n"
            "    session_class_key = 'fam.Reloadable'\n"
        )
        first, second = {"__name__": "acog"}, {"__name__": "acog"}
        exec(compile(source, "acog.py", "exec"), first)
        exec(compile(source, "acog.py", "exec"), second)

        assert _persistent_view_classes["fam.Reloadable"] is second["_Reloadable"]

    async def test_persistence_key_required(self):
        """PersistentView should raise ValueError without persistence_key."""

        class _NoKeyView(PersistentView):
            pass

        with pytest.raises(ValueError, match="persistence_key"):
            _NoKeyView()

    async def test_timeout_forced_to_none(self):
        """PersistentView should force timeout=None."""

        class _TimeoutView(PersistentView):
            pass

        view = _TimeoutView(persistence_key="test:timeout")
        assert view.timeout is None


# // ========================================( Validation )======================================== // #


class TestCustomIdValidation:
    """PersistentView validates that all interactive components have explicit custom_ids."""

    async def test_missing_custom_id_raises(self):
        """Components without custom_id should fail validation."""

        class _BadView(PersistentView):
            pass

        view = _BadView(persistence_key="test:bad")
        # Add a button without custom_id
        view.add_item(StatefulButton(label="No ID", callback=AsyncMock()))

        with pytest.raises(ValueError, match="custom_id"):
            view._validate_custom_ids()

    async def test_valid_custom_ids_pass(self):
        """Components with explicit custom_id should pass validation."""

        class _GoodView(PersistentView):
            pass

        view = _GoodView(persistence_key="test:good")
        view.add_item(
            StatefulButton(
                label="Has ID",
                custom_id="test:button",
                callback=AsyncMock(),
            )
        )

        # Should not raise
        view._validate_custom_ids()

    async def test_link_button_accepted(self):
        """Link buttons carry no custom_id and need none; they pass validation."""
        import discord

        class _View(PersistentView):
            pass

        view = _View(persistence_key="test:link")
        view.add_item(StatefulButton(label="Go", custom_id="go", callback=AsyncMock()))
        view.add_item(discord.ui.Button(label="Docs", url="https://example.com"))

        # Must not raise despite the link button's custom_id being None.
        view._validate_custom_ids()

    async def test_a_tree_built_in_on_load_is_checked_at_send(self):
        """send() checked before on_load ran, so a button built there went out
        with an id no restart could reattach."""
        from helpers import make_interaction

        from cascadeui.state.singleton import get_store

        class _Panel(PersistentLayoutView):
            async def on_load(self):
                self.clear_items()
                self.add_item(
                    discord.ui.ActionRow(StatefulButton(label="Go", callback=AsyncMock()))
                )

        interaction = make_interaction()
        view = _Panel(interaction=interaction, persistence_key="test:on_load_ids")

        with pytest.raises(ValueError, match="is missing a custom_id"):
            await view.send()

        interaction.response.send_message.assert_not_called()
        assert view.id not in get_store()._active_views


class TestComposedPanelsCheckNestedIds:
    """The persistent leaderboard and roles panels compose the persistent
    mixin with a V2 pattern and never pass through ``PersistentLayoutView``,
    so the id check read only their top-level containers and a nested
    control with a per-run id reached a panel that dies on restart."""

    @staticmethod
    async def _leaderboard(**attrs):
        from helpers import make_interaction

        from cascadeui import PersistentLeaderboardLayoutView

        async def get_entries(self):
            return [(1, {"score": 1})]

        cls = type(
            "_Board", (PersistentLeaderboardLayoutView,), {"get_entries": get_entries, **attrs}
        )
        view = cls(interaction=make_interaction(), persistence_key="board:ids")
        await view.on_load()
        return view

    @staticmethod
    async def _roles(**attrs):
        from cascadeui import PersistentRolesLayoutView, RoleCategory

        cls = type(
            "_Roles",
            (PersistentRolesLayoutView,),
            {"categories": [RoleCategory(name="NestedIdCat", roles={"X": 8801})], **attrs},
        )
        return cls(persistence_key="roles:ids")

    @pytest.mark.parametrize("factory", ["_leaderboard", "_roles"])
    async def test_a_nested_control_without_an_id_is_refused(self, factory):
        async def noop(interaction):
            pass

        view = await getattr(self, factory)()
        view.add_item(discord.ui.ActionRow(StatefulButton(label="Extra", callback=noop)))

        with pytest.raises(ValueError, match="missing a custom_id"):
            view._validate_custom_ids()

    @pytest.mark.parametrize("factory", ["_leaderboard", "_roles"])
    async def test_the_panel_as_built_passes(self, factory):
        view = await getattr(self, factory)()

        view._validate_custom_ids()

    async def test_auto_exit_button_passes_with_its_fixed_id(self):
        view = await self._leaderboard(auto_exit_button=True)

        view._validate_custom_ids()


# // ========================================( Reducers )======================================== // #


class TestPersistentViewReducers:
    """PERSISTENT_VIEW_REGISTERED and UNREGISTERED reducer behavior."""

    async def test_register_adds_entry(self):
        state = base_state()
        action = make_action(
            "PERSISTENT_VIEW_REGISTERED",
            {
                "persistence_key": "panel:main",
                "class_name": "RoleSelectorView",
                "message_id": "111",
                "channel_id": "222",
                "guild_id": "333",
                "user_id": "444",
            },
        )

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

        action = make_action(
            "PERSISTENT_VIEW_REGISTERED",
            {
                "persistence_key": "panel:main",
                "class_name": "New",
                "message_id": "new",
                "channel_id": "222",
            },
        )

        new_state = await reduce_persistent_view_registered(action, state)
        assert new_state["persistent_views"]["panel:main"]["message_id"] == "new"

    async def test_register_no_persistence_key_is_noop(self):
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

        action = make_action(
            "PERSISTENT_VIEW_UNREGISTERED",
            {
                "persistence_key": "panel:main",
            },
        )

        new_state = await reduce_persistent_view_unregistered(action, state)
        assert "panel:main" not in new_state["persistent_views"]
        assert "panel:other" in new_state["persistent_views"]

    async def test_unregister_missing_key_is_noop(self):
        state = base_state()
        action = make_action(
            "PERSISTENT_VIEW_UNREGISTERED",
            {
                "persistence_key": "nonexistent",
            },
        )

        new_state = await reduce_persistent_view_unregistered(action, state)
        assert new_state is state


# // ========================================( Session Re-derivation )======================================== // #


class TestSessionRederivation:
    """Verify that session_id is re-derived when user_id is set after __init__."""

    def test_session_id_none_without_user_id(self):
        """PersistentView constructed with no user_id has no session."""

        class _Panel(PersistentView):
            pass

        view = _Panel(persistence_key="panel:test")
        assert view.session_id is None

    def test_session_id_derived_when_user_id_set_before_init(self):
        """PersistentView with user_id at construction gets a session."""

        class _Panel(PersistentView):
            pass

        view = _Panel(persistence_key="panel:test", user_id=12345)
        assert view.session_id is not None
        assert "user_12345" in view.session_id

    def test_late_user_id_assignment_needs_manual_rederivation(self):
        """Setting user_id after __init__ does NOT auto-derive session_id -- PersistenceMiddleware.initialize re-derives it explicitly during restore."""

        class _Panel(PersistentView):
            pass

        view = _Panel(persistence_key="panel:test")
        assert view.session_id is None

        # Simulate the restore path: set user_id + re-derive
        view.user_id = 12345
        assert view.session_id is None  # still None - no auto-derivation

        # Manual re-derivation (what the restore code does)
        if view.user_id and not view.session_id:
            view.session_id = f"{type(view)._class_session_key()}:user_{view.user_id}"
        assert view.session_id is not None
        assert "user_12345" in view.session_id


# // ========================================( Send Composition )======================================== // #


class TestSendComposition:
    """``send()`` must live on ``_PersistentMixin`` so composed persistent
    views (``_PersistentMixin + ConcreteLayoutView`` shape used by
    ``PersistentRolesLayoutView`` and ``PersistentLeaderboardLayoutView``)
    route through the mixin and dispatch ``PERSISTENT_VIEW_REGISTERED``.
    Locating ``send()`` on the leaf classes (``PersistentView`` /
    ``PersistentLayoutView``) caused composed subclasses to silently
    skip registration because their MRO bypassed the leaf override.
    """

    def test_persistent_layout_view_send_owned_by_mixin(self):
        owner = next(c for c in PersistentLayoutView.__mro__ if "send" in c.__dict__)
        assert (
            owner is _PersistentMixin
        ), f"PersistentLayoutView.send resolves to {owner.__name__}, expected _PersistentMixin"

    def test_persistent_view_send_owned_by_mixin(self):
        owner = next(c for c in PersistentView.__mro__ if "send" in c.__dict__)
        assert (
            owner is _PersistentMixin
        ), f"PersistentView.send resolves to {owner.__name__}, expected _PersistentMixin"

    def test_composed_pattern_send_owned_by_mixin(self):
        """Loaded lazily because the leaderboard / roles pattern modules
        register module-level state when imported."""
        from cascadeui.views.patterns.leaderboard import PersistentLeaderboardLayoutView
        from cascadeui.views.patterns.roles import PersistentRolesLayoutView

        for cls in (PersistentRolesLayoutView, PersistentLeaderboardLayoutView):
            owner = next(c for c in cls.__mro__ if "send" in c.__dict__)
            assert owner is _PersistentMixin, (
                f"{cls.__name__}.send resolves to {owner.__name__}, expected _PersistentMixin "
                "-- this means composed persistent views skip registration silently."
            )


class TestExitComposition:
    """``exit()`` must resolve to ``_PersistentMixin`` for the same reason
    ``send()`` does (see ``TestSendComposition``): a leaf-class or pattern
    override shadowing it would silently skip both
    ``PERSISTENT_VIEW_UNREGISTERED`` and the dynamic-item registry re-drive.
    """

    def test_persistent_layout_view_exit_owned_by_mixin(self):
        owner = next(c for c in PersistentLayoutView.__mro__ if "exit" in c.__dict__)
        assert (
            owner is _PersistentMixin
        ), f"PersistentLayoutView.exit resolves to {owner.__name__}, expected _PersistentMixin"

    def test_persistent_view_exit_owned_by_mixin(self):
        owner = next(c for c in PersistentView.__mro__ if "exit" in c.__dict__)
        assert (
            owner is _PersistentMixin
        ), f"PersistentView.exit resolves to {owner.__name__}, expected _PersistentMixin"

    def test_composed_pattern_exit_owned_by_mixin(self):
        """Loaded lazily because the leaderboard / roles pattern modules
        register module-level state when imported."""
        from cascadeui.views.patterns.leaderboard import PersistentLeaderboardLayoutView
        from cascadeui.views.patterns.roles import PersistentRolesLayoutView

        for cls in (PersistentRolesLayoutView, PersistentLeaderboardLayoutView):
            owner = next(c for c in cls.__mro__ if "exit" in c.__dict__)
            assert owner is _PersistentMixin, (
                f"{cls.__name__}.exit resolves to {owner.__name__}, expected _PersistentMixin "
                "-- this means composed persistent views skip unregistration and the "
                "dynamic-item repair silently."
            )


# // ========================================( Exit Dynamic-Item Repair )======================================== // #


class TestExitDynamicItemRepair:
    """Exiting one persistent view must not leave a dynamic item dead on
    every OTHER live message sharing its class.

    discord.py's ``ViewStore._dynamic_items`` is keyed by compiled template,
    not by view instance or message, and ``remove_view`` pops that key with
    no refcount against other live views using the same pattern. Any
    ``exit()`` on a persistent view carrying a ``DynamicPersistentButton``
    reaches ``stop()`` -> ``remove_view``, which wipes the pattern for every
    OTHER message using the same button class: whether the exit came from
    a repost's duplicate-key cleanup, an admin's Exit click, or a
    programmatic teardown. ``_StatefulMixin.stop()`` re-drives the full
    registry via ``bot.add_dynamic_items`` immediately afterward (the same
    repair ``PersistenceManager.reattach`` already performs on every pass),
    so the gap closes before any click can land in it.
    """

    @pytest.fixture
    def clean_registry(self):
        from cascadeui.components.base import _dynamic_button_classes

        snapshot = dict(_dynamic_button_classes)
        yield
        _dynamic_button_classes.clear()
        _dynamic_button_classes.update(snapshot)

    async def test_reposting_over_a_live_old_view_redrives_dynamic_items(self, clean_registry):
        """The reported shape: a repost's duplicate-key cleanup exits the
        stale instance while a new message already carries the same button
        class -- the exit must not leave the new message's button dead."""
        from cascadeui.components.base import DynamicPersistentButton
        from cascadeui.state.singleton import get_store

        class _RepostButton(
            DynamicPersistentButton,
            template=r"repost:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x", custom_id=f"repost:{n}", style=discord.ButtonStyle.primary
                    )
                )

        class _Panel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(discord.ui.ActionRow(_RepostButton(n=1)))

        store = get_store()
        bot = MagicMock(spec=discord.Client)

        old_view = _Panel(persistence_key="repost:panel")
        old_view.build_ui()
        old_view.context = types.SimpleNamespace(bot=bot)
        store._register_view(old_view)
        await store.dispatch(
            "PERSISTENT_VIEW_REGISTERED",
            {
                "persistence_key": "repost:panel",
                "view_class": "P",
                "message_id": "111",
                "channel_id": "222",
                "guild_id": None,
                "user_id": None,
            },
        )

        new_view = _Panel(persistence_key="repost:panel")
        new_view.build_ui()
        new_view.context = types.SimpleNamespace(bot=bot)

        fake_message = MagicMock()
        fake_message.id = 999
        fake_message.guild = None
        fake_message.channel.id = 222

        await new_view._register_persistent(fake_message)

        # The old instance must actually have been torn down through the
        # live-instance branch (not the message-only fallback) for this
        # repair to be under test at all.
        assert old_view.id not in store._active_views

        bot.add_dynamic_items.assert_called_once()
        assert _RepostButton in bot.add_dynamic_items.call_args.args

    async def test_exit_on_one_panel_redrives_for_a_live_sibling(self, clean_registry):
        """The general shape the filed report's mechanism also implies: an
        admin closing ONE panel (a plain ``exit()``, no repost involved)
        must not leave a sibling panel's identical button class dead."""
        from cascadeui.components.base import DynamicPersistentButton

        class _SharedButton(
            DynamicPersistentButton,
            template=r"shared:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x", custom_id=f"shared:{n}", style=discord.ButtonStyle.primary
                    )
                )

        class _SharedPanel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(discord.ui.ActionRow(_SharedButton(n=1)))

        bot = MagicMock(spec=discord.Client)

        panel_being_closed = _SharedPanel(persistence_key="shared:one")
        panel_being_closed.build_ui()
        panel_being_closed.context = types.SimpleNamespace(bot=bot)

        # No repost, no duplicate key -- just a direct teardown, as an
        # admin's Exit button or a moderation command would trigger.
        await panel_being_closed.exit(delete_message=False)

        bot.add_dynamic_items.assert_called_once()
        assert _SharedButton in bot.add_dynamic_items.call_args.args

    async def test_exit_on_a_restored_view_redrives_via_bot_fallback(self, clean_registry):
        """A view restored after a bot restart (``PersistenceManager._reattach_one``)
        has neither ``.context`` nor ``.interaction`` (both default to ``None``
        and nothing on the restore path sets them), so ``._bot`` (assigned by
        the restore path) is its ONLY route to a live client. This is exactly
        the population most likely to trigger the repair in practice: a panel
        posted before a restart, closed weeks later by an admin or a moderation
        command, long after any interaction or context ever existed for it.
        """
        from cascadeui.components.base import DynamicPersistentButton

        class _RestoredButton(
            DynamicPersistentButton,
            template=r"restored:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x", custom_id=f"restored:{n}", style=discord.ButtonStyle.primary
                    )
                )

        class _RestoredPanel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(discord.ui.ActionRow(_RestoredButton(n=1)))

        bot = MagicMock(spec=discord.Client)

        panel = _RestoredPanel(persistence_key="restored:one")
        panel.build_ui()
        # Matches PersistenceManager._reattach_one's own restored-view shape:
        # no context, no interaction, only the bot handle stashed as _bot.
        panel.context = None
        panel.interaction = None
        panel._bot = bot

        await panel.exit(delete_message=False)

        bot.add_dynamic_items.assert_called_once()
        assert _RestoredButton in bot.add_dynamic_items.call_args.args

    async def test_no_live_old_view_does_not_redrive(self, clean_registry):
        """The message-only fallback path (no live instance) has nothing to
        repair -- no ``exit()`` ran, so no dynamic-item pattern was removed,
        and the fix must not fire needlessly on every repost."""
        from cascadeui.components.base import DynamicPersistentButton
        from cascadeui.state.singleton import get_store

        class _RepostButton2(
            DynamicPersistentButton,
            template=r"repost2:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x", custom_id=f"repost2:{n}", style=discord.ButtonStyle.primary
                    )
                )

        class _Panel2(PersistentLayoutView):
            pass

        store = get_store()
        bot = MagicMock(spec=discord.Client)

        # No live instance registered -- only a stale registry row, so the
        # cleanup takes the message-only fallback branch.
        await store.dispatch(
            "PERSISTENT_VIEW_REGISTERED",
            {
                "persistence_key": "repost2:panel",
                "view_class": "P",
                "message_id": "111",
                "channel_id": "222",
                "guild_id": None,
                "user_id": None,
            },
        )

        new_view = _Panel2(persistence_key="repost2:panel")
        new_view.context = types.SimpleNamespace(bot=bot)

        fake_message = MagicMock()
        fake_message.id = 999
        fake_message.guild = None
        fake_message.channel.id = 222

        # bot.get_channel(222) returns a MagicMock by default (not a real
        # Messageable), so the fallback's isinstance check declines the
        # fetch cleanly without any network reach. No exit() ran, so no
        # redrive is owed.
        await new_view._register_persistent(fake_message)

        bot.add_dynamic_items.assert_not_called()

    async def test_unresolvable_bot_logs_debug_instead_of_failing_silently(
        self, clean_registry, caplog
    ):
        """When none of the four bot-resolution tiers yields a real
        ``discord.Client``, the repair does not run -- and must say so at
        debug rather than leave the symptom indistinguishable from the
        original defect. Matches the ``PersistenceManager`` no-op, which
        documents the same shape."""
        from cascadeui.components.base import DynamicPersistentButton

        class _UnresolvableButton(
            DynamicPersistentButton,
            template=r"unresolvable:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x",
                        custom_id=f"unresolvable:{n}",
                        style=discord.ButtonStyle.primary,
                    )
                )

        class _Panel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(discord.ui.ActionRow(_UnresolvableButton(n=1)))

        panel = _Panel(persistence_key="unresolvable:one")
        panel.build_ui()
        # No interaction, no context, no _bot. The singleton store's
        # persistence_manager is saved and cleared for the duration of the
        # test rather than assumed empty -- a real bot installed there by
        # an unrelated test earlier in the run would otherwise make this
        # test order-dependent.
        panel.context = None
        panel.interaction = None
        store = panel.state_store
        saved_manager = getattr(store, "persistence_manager", None)
        store.persistence_manager = None
        try:
            with caplog.at_level(logging.DEBUG, logger="cascadeui"):
                panel._redrive_dynamic_items()
        finally:
            store.persistence_manager = saved_manager

        messages = [r.getMessage() for r in caplog.records]
        assert any("could not resolve a discord.Client" in m for m in messages)


class TestRealViewStoreRepair:
    """End-to-end against a REAL discord.py ``ViewStore`` (``StubClient``),
    not a mocked bot.

    Every case above proves this view's code CALLS ``bot.add_dynamic_items``
    -- it does not prove discord.py's own registry was ever actually broken
    for the shape under test. It is not, for a view stored under its own
    distinct message id (every view CascadeUI sends): ``ViewStore.add_view``
    only stores a message's dispatch table when the view carries at least
    one NON-dynamic dispatchable item (``dispatch_info`` stays an empty,
    falsy dict for a purely-dynamic view, so ``self._views[message_id]`` is
    never set); ``remove_view`` then reads ``if dispatch_info and snapshot:``
    and, finding no stored entry, never reaches the loop that pops the
    dynamic-item pattern. A view built from nothing but a
    ``DynamicPersistentButton`` (the shape every other test in this file
    uses) never triggers the underlying discord.py defect at all under that
    condition. The reachable shape is a MIXED view: a dynamic item alongside
    at least one ordinary button (an Exit button, in practice), which every
    real persistent panel with more than one control has. Verified here
    against the actual mechanism rather than trusting either claim.
    """

    @pytest.fixture
    def clean_registry(self):
        from cascadeui.components.base import _dynamic_button_classes

        snapshot = dict(_dynamic_button_classes)
        yield
        _dynamic_button_classes.clear()
        _dynamic_button_classes.update(snapshot)

    async def test_pure_dynamic_view_never_triggers_the_defect(self, clean_registry):
        """Confirms the negative before trusting the positive: a view with
        ONLY a dynamic item never enters the real ViewStore's dispatch
        table, so removing it cannot wipe the shared pattern."""
        from cascadeui.components.base import DynamicPersistentButton
        from cascadeui.testing import stub_client

        class _PureButton(
            DynamicPersistentButton,
            template=r"pure:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x", custom_id=f"pure:{n}", style=discord.ButtonStyle.primary
                    )
                )

        bot = stub_client()
        a = discord.ui.View(timeout=None)
        a.add_item(_PureButton(n=1))
        b = discord.ui.View(timeout=None)
        b.add_item(_PureButton(n=2))

        bot.add_view(a, message_id=111)
        bot.add_view(b, message_id=222)

        store = bot._connection._view_store
        pattern = _PureButton.__discord_ui_compiled_template__
        assert pattern in store._dynamic_items

        store.remove_view(a)

        # The pattern survives -- discord.py's own guard never reached the
        # pop for a purely-dynamic view.
        assert pattern in store._dynamic_items

    async def test_mixed_view_reproduces_the_defect_and_the_fix_repairs_it(self, clean_registry):
        """The reachable shape: a persistent panel carrying a dynamic
        button ALONGSIDE a plain button (an Exit button, in every real
        panel with more than one control). Confirms the defect is real
        against a real ViewStore, then confirms exit() repairs it.

        The reachability probe runs against a separate THROWAWAY view
        rather than ``old_view`` itself: ``ViewStore.remove_view`` deletes
        the target's own ``dispatch_info`` entry outright, so probing on
        ``old_view`` first would consume the very state the real
        ``exit()`` call below needs to reproduce the defect a second time
        -- the closing assertion would then pass whether or not the fix's
        redrive ever ran, which is exactly what happened before this test
        was corrected.
        """
        from cascadeui.components.base import DynamicPersistentButton
        from cascadeui.testing import stub_client

        class _MixedButton(
            DynamicPersistentButton,
            template=r"mixed:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x", custom_id=f"mixed:{n}", style=discord.ButtonStyle.primary
                    )
                )

        class _MixedPanel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(discord.ui.ActionRow(_MixedButton(n=1)))
                self.add_item(
                    discord.ui.ActionRow(discord.ui.Button(label="Exit", custom_id="exit:panel"))
                )

        bot = stub_client()
        store = bot._connection._view_store
        pattern = _MixedButton.__discord_ui_compiled_template__

        # Throwaway view: proves the shape is genuinely reachable without
        # touching any state the real exit() call below depends on.
        throwaway = _MixedPanel(persistence_key="mixed:throwaway")
        throwaway.build_ui()
        bot.add_view(throwaway, message_id=999)
        assert pattern in store._dynamic_items
        store.remove_view(throwaway)
        assert pattern not in store._dynamic_items, (
            "mixed-view removal did not wipe the shared pattern -- this test's "
            "premise about the reachable shape no longer holds"
        )
        bot.add_dynamic_items(_MixedButton)
        assert pattern in store._dynamic_items

        # Now the real path, with fresh views neither probe has touched.
        old_view = _MixedPanel(persistence_key="mixed:one")
        old_view.build_ui()
        old_view.context = types.SimpleNamespace(bot=bot)

        sibling_view = _MixedPanel(persistence_key="mixed:two")
        sibling_view.build_ui()

        bot.add_view(old_view, message_id=111)
        bot.add_view(sibling_view, message_id=222)

        await old_view.exit(delete_message=False)

        # exit() -> stop() wiped it again, then _redrive_dynamic_items()
        # repaired it -- the sibling's button is routable.
        assert pattern in store._dynamic_items

    async def test_live_view_edit_dropping_a_dynamic_item_wipes_and_refresh_repairs(
        self, clean_registry
    ):
        """A SECOND, independent discord.py wipe mechanism, unrelated to
        stop()/remove_view entirely: every live-view edit re-runs
        ``ViewStore.add_view``, which diffs the view's previous component
        snapshot against its new one and pops any dynamic-item pattern no
        longer present. A live view whose rebuilt tree drops a
        ``DynamicPersistentButton`` class it used to carry breaks that
        class for every OTHER live message sharing it -- the concrete,
        already-reachable trigger is the ephemeral refresh-handoff timer
        swapping a view down to a bare "Continue Session" button.

        Confirms the raw mechanism against the real store first (calling
        ``store_view`` directly, the same call discord.py issues after any
        successful edit), then confirms ``_redrive_dynamic_items()`` (the
        same repair ``refresh()``'s three successful-edit returns and
        navigation's destination edit now call) restores it.
        """
        from cascadeui.components.base import DynamicPersistentButton
        from cascadeui.testing import stub_client

        class _LiveButton(
            DynamicPersistentButton,
            template=r"live:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x", custom_id=f"live:{n}", style=discord.ButtonStyle.primary
                    )
                )

        class _LiveMixedView(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(discord.ui.ActionRow(_LiveButton(n=1)))
                self.add_item(
                    discord.ui.ActionRow(discord.ui.Button(label="Static", custom_id="static:x"))
                )

        bot = stub_client()
        store = bot._connection._view_store
        pattern = _LiveButton.__discord_ui_compiled_template__

        live_view = _LiveMixedView()
        live_view.build_ui()
        live_view.context = types.SimpleNamespace(bot=bot)
        bot._connection.store_view(live_view, message_id=777)

        sibling = _LiveMixedView()
        sibling.build_ui()
        bot._connection.store_view(sibling, message_id=888)

        assert pattern in store._dynamic_items

        # Simulate the edit that drops the dynamic button -- rebuild
        # without it, then re-register through the exact call discord.py
        # issues internally after any successful edit (Message.edit,
        # InteractionResponse.edit_message, etc. all call this).
        live_view.clear_items()
        live_view.add_item(
            discord.ui.ActionRow(discord.ui.Button(label="Continue", custom_id="continue:x"))
        )
        bot._connection.store_view(live_view, message_id=777)

        assert (
            pattern not in store._dynamic_items
        ), "the render-path wipe did not reproduce -- this test's premise no longer holds"

        # The repair refresh() now calls after every successful edit.
        live_view._redrive_dynamic_items()

        assert pattern in store._dynamic_items

    async def test_refresh_repairs_the_registry_through_its_own_edit_path(self, clean_registry):
        """The sibling test above calls ``_redrive_dynamic_items()`` directly
        -- it proves the helper repairs the mechanism, but not that
        ``refresh()`` actually wires the call correctly. This drives
        ``refresh()`` itself through its plain channel-endpoint path, with
        an edit double whose side effect is the exact ``store_view()`` call
        discord.py issues after any real edit. Deleting any of the three
        ``self._redrive_dynamic_items()`` lines inside ``refresh()`` would
        fail this test; it would NOT fail the sibling test above.
        """
        from cascadeui.components.base import DynamicPersistentButton
        from cascadeui.testing import stub_client

        class _RefreshButton(
            DynamicPersistentButton,
            template=r"refresh:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x", custom_id=f"refresh:{n}", style=discord.ButtonStyle.primary
                    )
                )

        class _RefreshMixedView(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(discord.ui.ActionRow(_RefreshButton(n=1)))
                self.add_item(
                    discord.ui.ActionRow(
                        discord.ui.Button(label="Static", custom_id="static:refresh")
                    )
                )

        bot = stub_client()
        store = bot._connection._view_store
        pattern = _RefreshButton.__discord_ui_compiled_template__

        live_view = _RefreshMixedView()
        live_view.build_ui()
        live_view.context = types.SimpleNamespace(bot=bot)
        bot._connection.store_view(live_view, message_id=901)

        sibling = _RefreshMixedView()
        sibling.build_ui()
        bot._connection.store_view(sibling, message_id=902)

        assert pattern in store._dynamic_items

        async def _edit_drops_the_dynamic_button(*, view, **kwargs):
            # Simulate the caller having rebuilt the tree without the
            # dynamic button before calling refresh() -- the ephemeral
            # refresh-handoff shape -- then perform the real store_view()
            # call discord.py issues after any successful edit.
            view.clear_items()
            view.add_item(
                discord.ui.ActionRow(discord.ui.Button(label="Continue", custom_id="continue:x"))
            )
            bot._connection.store_view(view, message_id=901)

        live_view._message = MagicMock()
        live_view._message.edit = AsyncMock(side_effect=_edit_drops_the_dynamic_button)
        live_view._last_tree_digest = None

        outcome = await live_view.refresh()

        assert outcome == RenderOutcome.RENDERED
        assert pattern in store._dynamic_items

    async def test_non_persistent_view_timeout_repairs_a_persistent_siblings_button(
        self, clean_registry
    ):
        """The concrete shape this repair generalizes to cover: a plain,
        non-persistent view (default timeout, no persistence_key) sharing a
        DynamicPersistentButton class with a live PersistentLayoutView
        sibling -- e.g. RolesLayoutView pushed onto a nav stack (gaining an
        auto back button, which makes it a mixed view) alongside a
        PersistentRolesLayoutView panel using the same toggle-button class.

        discord.py's own internal timeout dispatch (``_dispatch_timeout``)
        calls the cancel callback directly, bypassing ``stop()`` entirely,
        so the repair rides ``_StatefulMixin``'s own ``_dispatch_timeout``
        override rather than ``on_timeout()`` -- a subclass overriding the
        public ``on_timeout`` hook without calling ``super()`` must not be
        able to skip the repair. Drives the real method directly rather
        than waiting on a real asyncio timeout.
        """
        from cascadeui.components.base import DynamicPersistentButton
        from cascadeui.testing import stub_client

        class _SharedRoleButton(
            DynamicPersistentButton,
            template=r"role:(?P<n>[0-9]+)",
        ):
            def __init__(self, *, n: int):
                super().__init__(
                    discord.ui.Button(
                        label="x", custom_id=f"role:{n}", style=discord.ButtonStyle.primary
                    )
                )

        class _PlainMixedView(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(discord.ui.ActionRow(_SharedRoleButton(n=1)))
                # Stands in for the auto back button a pushed RolesLayoutView
                # gains -- any non-dynamic dispatchable item makes the view
                # "mixed" and reachable by the underlying discord.py defect.
                self.add_item(
                    discord.ui.ActionRow(discord.ui.Button(label="Back", custom_id="back:x"))
                )

        class _PersistentSiblingPanel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(discord.ui.ActionRow(_SharedRoleButton(n=2)))

        bot = stub_client()

        timing_out_view = _PlainMixedView()
        timing_out_view.build_ui()
        timing_out_view.context = types.SimpleNamespace(bot=bot)

        persistent_sibling = _PersistentSiblingPanel(persistence_key="role:panel")
        persistent_sibling.build_ui()

        # timing_out_view carries the library's default 180s timeout, so
        # it fails add_view's is_persistent() guard even though a normal
        # interaction response send would register it the same way via
        # the lower-level store_view -- which is what actually happens for
        # an ordinary (non-persistent) timed view sent through discord.py.
        bot._connection.store_view(timing_out_view, message_id=555)
        bot.add_view(persistent_sibling, message_id=666)

        store = bot._connection._view_store
        pattern = _SharedRoleButton.__discord_ui_compiled_template__
        assert pattern in store._dynamic_items

        # Drive discord.py's real internal timeout dispatch directly --
        # it calls the cancel callback (the real wipe) and then schedules
        # on_timeout() as a background task, exactly as the library's own
        # timer does. _dispatch_timeout is sync; the repair runs inline,
        # right after on_timeout has been scheduled but before that task
        # gets a chance to run.
        timing_out_view._dispatch_timeout()

        # The persistent sibling's role-toggle button is routable again,
        # immediately -- no need to await the scheduled on_timeout() task.
        assert pattern in store._dynamic_items

        # Let the scheduled on_timeout() task run to completion so it
        # doesn't leak past the test as a pending task.
        await asyncio.sleep(0)


# _setup() below builds a fresh PersistenceMiddleware per test and spawns a
# registry-namespace flush task on every register/unregister. Left uncancelled,
# that task is bound to the test's own event loop, which pytest-asyncio closes
# at teardown -- the task can never resume, and the coroutine it awaits is
# collected as "never awaited" whenever the garbage collector next runs,
# which can land during an unrelated, later test. Draining here, not in each
# test body, covers every test that reaches _setup() (including the ones in
# TestPanelExitAndNavigation and TestPanelExitThatFails that call it through a
# throwaway TestRetirePreviousOnSend() instance).
_pending_persistence_middleware = []


@pytest.fixture(autouse=True)
async def _drain_persistence_middleware():
    yield
    while _pending_persistence_middleware:
        await _pending_persistence_middleware.pop().flush_all()


class TestRetirePreviousOnSend:
    """A send under a key another panel holds retires that panel by default.

    ``retire_previous_on_send = False`` leaves the predecessor to the caller,
    for a swap that confirms the new panel before the old one comes down. Two
    things keep that safe: the registry row is built from the panel that
    registered, and a superseded panel's ``exit()`` removes only the
    registration its own message holds.
    """

    KEY = "swap:panel"

    @staticmethod
    def _message(message_id):
        message = MagicMock()
        message.id = message_id
        message.guild = None
        message.channel.id = 222
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        return message

    @staticmethod
    def _panel_class(retire):
        class _SwapPanel(PersistentLayoutView):
            retire_previous_on_send = retire

            def __init__(self, *, label, **kwargs):
                super().__init__(**kwargs)
                self.add_item(
                    discord.ui.ActionRow(discord.ui.Button(label=label, custom_id="swap:go"))
                )

        return _SwapPanel

    async def _setup(self, retire):
        from cascadeui import setup_middleware
        from cascadeui.persistence import InMemoryBackend
        from cascadeui.state.middleware.persistence import PersistenceMiddleware
        from cascadeui.state.singleton import get_store

        backend = InMemoryBackend()
        await backend.initialize()
        middleware = PersistenceMiddleware(backend=backend)
        await setup_middleware(middleware)
        _pending_persistence_middleware.append(middleware)
        store = get_store()
        cls = self._panel_class(retire)

        old = cls(label="old", persistence_key=self.KEY)
        old._message = self._message(111)
        store._register_view(old)
        await old._register_persistent(old._message)

        new = cls(label="new", persistence_key=self.KEY)
        new._message = self._message(999)
        # _send_pipeline registers the view before _register_persistent runs.
        store._register_view(new)
        return store, backend, middleware, old, new

    async def _row(self, backend, middleware):
        from cascadeui.persistence.schema import TABLE_PERSISTENT_VIEWS

        await middleware.flush_all()
        return await backend.row_select(TABLE_PERSISTENT_VIEWS, {"persistence_key": self.KEY})

    async def test_a_swap_rolled_back_mid_push_hands_the_key_to_the_panel_on_screen(self):
        """The hand-back picked the view the push was still bringing in, so the
        stored row carried its kwargs while the old panel stayed on screen."""
        store, backend, middleware, old, new = await self._setup(retire=False)
        gate, entered = asyncio.Event(), asyncio.Event()

        class _Loading(type(old)):
            async def on_load(self):
                entered.set()
                await gate.wait()

        pushing = asyncio.create_task(old.push(_Loading, label="dest", persistence_key=self.KEY))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await new._register_persistent(new._message)
            await new.exit()

            assert store.get_active_view(persistence_key=self.KEY) is old
            rows = await self._row(backend, middleware)
            assert json.loads(rows[0]["init_kwargs"])["label"] == "old"
            assert rows[0]["view_class"] == type(old)._class_session_key()
        finally:
            gate.set()
            await pushing

    async def test_rolling_back_a_swap_restores_a_panel_showing_a_pushed_view(self):
        """The hand-back looked only for a panel holding the key, and a panel
        that had pushed holds none, so the registration was lost and a restart
        reattached nothing to the panel's message."""
        from helpers import RenderableLayoutView

        class _Detail(RenderableLayoutView):
            def __init__(self, *, detail="d", **kwargs):
                super().__init__(**kwargs)

        store, backend, middleware, old, new = await self._setup(retire=False)
        child = await old.push(_Detail, detail="sub")
        await new._register_persistent(new._message)

        await new.exit()

        entry = store.state["persistent_views"][self.KEY]
        assert entry["message_id"] == "111"
        assert entry["class_name"] == type(old)._class_session_key()
        rows = await self._row(backend, middleware)
        assert rows[0]["view_class"] == type(old)._class_session_key()
        # The panel's own arguments, not the pushed view's.
        assert json.loads(rows[0]["init_kwargs"]) == {"label": "old"}
        assert store.get_active_view(persistence_key=self.KEY) is child

        # Closing the pushed view then closes the panel, as it would have
        # before the swap.
        await child.exit(delete_message=False)
        assert self.KEY not in store.state["persistent_views"]

    async def test_rolling_back_after_the_pushed_view_moved_restores_nothing(self):
        """Sent again to a new message, the view the old panel pushed to still
        carried the old message's registration, so rolling the swap back put
        the key on the message its send had just frozen: a restart restored a
        panel there with nothing to click."""
        from helpers import RenderableLayoutView, make_interaction

        store, backend, middleware, old, new = await self._setup(retire=False)
        child = await old.push(RenderableLayoutView)
        await new._register_persistent(new._message)
        child.interaction = make_interaction()
        child.interaction.original_response.return_value.id = 777
        assert await child.send() is not None

        await new.exit()

        assert self.KEY not in store.state["persistent_views"]
        assert await self._row(backend, middleware) == []

    async def test_rolling_back_with_nothing_on_the_old_message_restores_nothing(self):
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=False)
        child = await old.push(RenderableLayoutView)
        await new._register_persistent(new._message)
        await child.exit(delete_message=True)

        await new.exit()

        assert self.KEY not in store.state["persistent_views"]
        assert await self._row(backend, middleware) == []

    async def test_closing_a_view_the_new_panel_pushed_hands_the_key_back(self):
        """Closing it closes the panel, so it hands the registration back as the
        panel's own exit() does; it removed the registration and left the old
        panel with no row to restore from."""
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=False)
        await new._register_persistent(new._message)
        child = await new.push(RenderableLayoutView)

        await child.exit(delete_message=False)

        assert store.state["persistent_views"][self.KEY]["message_id"] == "111"
        rows = await self._row(backend, middleware)
        assert json.loads(rows[0]["init_kwargs"]) == {"label": "old"}

    async def test_a_panel_sent_again_keeps_the_record_of_the_panel_it_superseded(self):
        """Moving its own row recorded that row as the one superseded, so the
        hand-back went to the panel's own previous message and was lost."""
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=False)
        await old.push(RenderableLayoutView)
        await new._register_persistent(new._message)
        moved_to = self._message(555)
        new._message = moved_to
        await new._register_persistent(moved_to)

        await new.exit()

        assert store.state["persistent_views"][self.KEY]["message_id"] == "111"

    async def test_default_still_exits_the_previous_panel(self):
        store, backend, middleware, old, new = await self._setup(retire=True)

        await new._register_persistent(new._message)

        assert old.id not in store._active_views

    async def test_default_retires_a_view_the_previous_panel_pushed(self):
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=True)
        child = await old.push(RenderableLayoutView)
        assert old.id not in store._active_views

        await new._register_persistent(new._message)

        assert child.is_finished()
        assert child.id not in store._active_views
        assert store.get_active_view(persistence_key=self.KEY) is new

    async def test_a_pushed_view_retires_the_registration_when_its_message_goes(self):
        """After a push the panel is gone and its registration rides the
        destination, so that view is the only one left to retire it."""
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=True)
        child = await old.push(RenderableLayoutView)
        assert child._registry_message_id == "111"

        await child.exit(delete_message=True)
        await middleware.flush_all()

        assert self.KEY not in store.state.get("persistent_views", {})
        assert await self._row(backend, middleware) == []

    async def test_a_persistent_destination_retires_the_carried_registration_too(self):
        """A persistent view unregisters its OWN key, so one pushed under a
        different key leaves the carried registration behind."""
        store, backend, middleware, old, new = await self._setup(retire=True)
        child_cls = self._panel_class(True)
        child = await old.push(child_cls, label="child", persistence_key="other:key")
        assert child._persistent and child._registry_message_id == "111"

        await child.exit(delete_message=True)
        await middleware.flush_all()

        assert self.KEY not in store.state.get("persistent_views", {})
        assert await self._row(backend, middleware) == []

    async def test_a_deleted_message_does_not_rebuild_the_view_mid_teardown(self):
        """The retirement dispatch runs after the teardown: before it, the view
        is still subscribed and its own tree rebuilds against a nulled message.
        """
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=True)
        rebuilds = []

        class _Reactive(RenderableLayoutView):
            subscribed_actions = None

            def build_ui(self):
                # Records rather than raises: a tree that read self.message.id
                # here would raise inside the subscriber wrapper, and the
                # append that proves the rebuild happened would never run.
                rebuilds.append(self._message)

        child = await old.push(_Reactive)
        rebuilds.clear()

        await child.on_message_delete()
        await store._flush_notifications()

        assert rebuilds == []
        assert self.KEY not in store.state.get("persistent_views", {})

    async def test_a_panel_exiting_after_its_push_keeps_the_registration(self):
        """The panel kept the registration id it handed on, so its exit removed
        the row the view now on its message carries."""
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=True)
        child = await old.push(RenderableLayoutView)

        await old.exit(delete_message=False)
        await middleware.flush_all()

        assert store.state["persistent_views"][self.KEY]["message_id"] == "111"
        assert len(await self._row(backend, middleware)) == 1
        assert not child.is_finished()

    async def test_a_panel_exiting_during_its_push_waits_for_the_hand_off(self):
        from helpers import RenderableLayoutView, make_interaction

        store, backend, middleware, old, new = await self._setup(retire=True)
        gate = asyncio.Event()

        async def deferred_edit(**kwargs):
            await gate.wait()

        nav = make_interaction(is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        push = asyncio.create_task(old.push(RenderableLayoutView, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        closing = asyncio.create_task(old.exit(delete_message=False))
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        child = await push
        await closing
        await middleware.flush_all()

        assert store.state["persistent_views"][self.KEY]["message_id"] == "111"
        assert len(await self._row(backend, middleware)) == 1
        assert not child.is_finished()

    @pytest.mark.parametrize("close", ["freeze", "replace"])
    async def test_closing_a_pushed_view_retires_the_panel(self, close):
        """Frozen in place, the panel kept its row, so the next restart put it
        back on the message it had been closed on, beside any replacement."""
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=True)
        child = await old.push(RenderableLayoutView)

        if close == "freeze":
            await child.exit(delete_message=False)
        else:
            await child.replace(RenderableLayoutView)
        await middleware.flush_all()

        assert self.KEY not in store.state.get("persistent_views", {})
        assert await self._row(backend, middleware) == []

    async def test_a_pushed_view_that_times_out_keeps_the_panel_restorable(self):
        """A pushed view that times out (its own on_timeout() calls super(), or
        its return to the panel failed) is not a close: the panel stays
        registered, so a restart brings it back on its message."""
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=True)
        child = await old.push(RenderableLayoutView)

        await child.on_timeout()
        await middleware.flush_all()

        assert store.state["persistent_views"][self.KEY]["message_id"] == "111"
        assert len(await self._row(backend, middleware)) == 1

    async def test_an_exit_cut_off_while_closing_renders_what_it_declined(self):
        """A closing view declines state renders; one whose exit is cut off
        stays live and must still show the state that arrived meanwhile."""
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._setup(retire=True)
        closing = asyncio.Event()
        renders = []

        class _Slow(RenderableLayoutView):
            async def exit(self, delete_message=None):
                closing.set()
                await asyncio.Event().wait()

        async def on_state_changed(state):
            renders.append(state)

        old.on_state_changed = on_state_changed
        old.set_class_attribute("subscribed_actions", None)
        old.attach_child(_Slow())
        task = asyncio.create_task(old.exit())
        await asyncio.wait_for(closing.wait(), 1)

        await store.dispatch("CLOSING_TICK", {})
        await store._flush_notifications()
        assert renders == []  # declined while closing

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(5):
            await asyncio.sleep(0)

        assert not old._closed()
        assert len(renders) == 1  # replayed once the exit was cut off

    async def test_a_raising_hand_back_does_not_escape_exit(self):
        """The teardown still has to run: a caller rolling a swap back would
        otherwise be left with both panels live and neither registered."""
        store, backend, middleware, old, new = await self._setup(retire=False)
        await new._register_persistent(new._message)

        async def _boom(key):
            raise RuntimeError("registry write blew up")

        new._reregister_live_predecessor = _boom

        await new.exit(delete_message=False)

        assert new.is_finished()
        assert new.id not in store._active_views

    async def test_exiting_a_panel_that_never_sent_leaves_the_live_row(self):
        """A panel whose send raised, or was refused, owns no registration.

        Its unregister carries no message id, which is the remove-whatever-
        holds-the-key contract, so it would retire the panel on screen.
        """
        store, backend, middleware, old, new = await self._setup(retire=True)
        never_sent = self._panel_class(True)(label="never", persistence_key=self.KEY)

        await never_sent.exit(delete_message=False)

        assert store.state["persistent_views"][self.KEY]["message_id"] == "111"
        assert store.get_active_view(persistence_key=self.KEY) is old
        assert not old.is_finished()

    async def test_a_holder_retired_by_another_holder_is_not_exited_twice(self):
        """The holder list predates the first exit, and one panel's exit can
        retire another (paired panels retire together)."""
        store, backend, middleware, old, new = await self._setup(retire=True)
        partner = self._panel_class(False)(label="partner", persistence_key=self.KEY)
        partner._message = self._message(333)
        store._register_view(partner)
        exits = []

        async def _old_exit(delete_message=None, _orig=old.exit):
            exits.append("old")
            await _orig(delete_message=delete_message)

        old.exit = _old_exit

        async def _partner_exit(delete_message=None, _orig=partner.exit):
            exits.append("partner")
            if not old.is_finished():
                await old.exit(delete_message=False)
            await _orig(delete_message=delete_message)

        partner.exit = _partner_exit

        await new._register_persistent(new._message)

        assert exits == ["partner", "old"]
        assert old.is_finished() and partner.is_finished()

    async def test_opt_out_leaves_the_previous_panel_live(self):
        store, backend, middleware, old, new = await self._setup(retire=False)

        await new._register_persistent(new._message)

        assert old.id in store._active_views
        assert not old.is_finished()
        old._message.edit.assert_not_called()
        old._message.delete.assert_not_called()
        assert store.state["persistent_views"][self.KEY]["message_id"] == "999"

    async def test_opt_out_writes_the_row_from_the_panel_that_registered(self):
        _, backend, middleware, old, new = await self._setup(retire=False)

        await new._register_persistent(new._message)
        rows = await self._row(backend, middleware)

        assert [r["message_id"] for r in rows] == [999]
        assert json.loads(rows[0]["init_kwargs"])["label"] == "new"

    async def test_superseded_panel_exit_keeps_the_successors_registration(self):
        store, backend, middleware, old, new = await self._setup(retire=False)
        await new._register_persistent(new._message)

        # The caller deletes the old message; the deletion listener nulls
        # _message and exits the superseded view.
        await old.on_message_delete()
        rows = await self._row(backend, middleware)

        assert old.is_finished()
        assert store.state["persistent_views"][self.KEY]["message_id"] == "999"
        assert [r["message_id"] for r in rows] == [999]

    async def test_rolling_back_by_exiting_the_new_panel_restores_the_old_registration(self):
        """A swap that fails its confirmation exits the new panel; the old one
        is still live, so it must still be the panel a restart reattaches."""
        store, backend, middleware, old, new = await self._setup(retire=False)
        await new._register_persistent(new._message)

        await new.exit(delete_message=True)
        rows = await self._row(backend, middleware)

        assert store.state["persistent_views"][self.KEY]["message_id"] == "111"
        assert [r["message_id"] for r in rows] == [111]
        assert json.loads(rows[0]["init_kwargs"])["label"] == "old"

    async def test_exiting_the_new_panel_after_the_old_one_stood_down_removes_the_row(self):
        """A predecessor the caller already stopped is retired, so the owner's
        exit removes the registration as it would with no predecessor at all."""
        store, backend, middleware, old, new = await self._setup(retire=False)
        await new._register_persistent(new._message)
        old.stop()

        await new.exit(delete_message=False)
        rows = await self._row(backend, middleware)

        assert self.KEY not in store.state["persistent_views"]
        assert rows == []

    def test_a_non_bool_value_is_refused_at_class_definition(self):
        with pytest.raises(ValueError, match="retire_previous_on_send must be a bool"):

            class _Bad(PersistentLayoutView):
                retire_previous_on_send = "no"


class TestPanelExitAndNavigation:
    """A panel's retire and exit against a push from it in flight."""

    @staticmethod
    async def _panels():
        return await TestRetirePreviousOnSend()._setup(retire=True)

    async def test_a_failed_push_hands_back_a_view_whose_exit_keeps_the_registration(self):
        """The discarded view still carried the panel's registration id, so its
        exit() removed the registration of the panel that stayed live."""
        from unittest.mock import MagicMock

        from helpers import make_interaction

        store, backend, middleware, old, new = await self._panels()
        response = MagicMock(status=429, reason="Too Many Requests", headers={"Retry-After": "1"})
        nav = make_interaction(user_id=1, guild_id=100, message=old._message)
        nav.response.edit_message = AsyncMock(
            side_effect=discord.HTTPException(response, {"message": "rate limited", "code": 0})
        )

        discarded = await old.push(
            type(old), interaction=nav, label="next", persistence_key=TestRetirePreviousOnSend.KEY
        )
        await discarded.exit(delete_message=True)

        assert not old.is_finished()
        assert store.state["persistent_views"][TestRetirePreviousOnSend.KEY]["message_id"] == "111"
        assert len(await TestRetirePreviousOnSend()._row(backend, middleware)) == 1

    async def test_a_pushed_view_whose_message_goes_retires_every_row_naming_it(self):
        """A key renamed in place left the old row naming the same message, and
        only the first match was retired, so the other kept claiming a deleted
        message until the next restart."""
        from helpers import RenderableLayoutView

        from cascadeui.state.actions import ActionCreators

        store, backend, middleware, old, new = await self._panels()
        await store.dispatch(
            "PERSISTENT_VIEW_REGISTERED",
            ActionCreators.persistent_view_registered("swap:old-name", "_SwapPanel", "111", "222"),
        )
        child = await old.push(RenderableLayoutView)

        await child.exit(delete_message=True)

        registry = store.state["persistent_views"]
        assert TestRetirePreviousOnSend.KEY not in registry
        assert "swap:old-name" not in registry

    async def test_replacing_a_panel_removes_its_registration(self):
        """replace() stopped the panel and left its row, so the next restart
        attached the replaced panel to its old message again."""
        from helpers import RenderableLayoutView

        store, backend, middleware, old, new = await self._panels()

        await old.replace(RenderableLayoutView)

        assert TestRetirePreviousOnSend.KEY not in store.state["persistent_views"]
        assert not await TestRetirePreviousOnSend()._row(backend, middleware)

    async def test_retiring_a_panel_exits_the_view_its_push_in_flight_hands_over_to(self, caplog):
        """The retire exited the panel it collected, which did nothing once
        the panel's push landed, and the pushed view stayed live on the old
        message. The retire follows the push itself, so it warns about no
        stale reference."""
        from helpers import RenderableLayoutView, make_interaction

        class _Child(RenderableLayoutView):
            pass

        store, backend, middleware, old, new = await self._panels()
        release = asyncio.Event()

        async def blocked(**kwargs):
            if kwargs.get("view") is old:
                await release.wait()
            return old._message

        # An edit of the panel in flight holds its push before the pushed
        # view exists, so the retire collects the panel alone.
        old._message.edit = AsyncMock(side_effect=blocked)
        refreshing = asyncio.create_task(old.refresh())
        for _ in range(5):
            await asyncio.sleep(0)
        pushing = asyncio.create_task(
            old.push(_Child, interaction=make_interaction(user_id=1, guild_id=100))
        )
        for _ in range(5):
            await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            retiring = asyncio.create_task(new._register_persistent(new._message))
            for _ in range(5):
                await asyncio.sleep(0)
            release.set()
            await refreshing
            child = await pushing
            await retiring

        assert old._successor is child
        assert child._torn_down()
        assert not [r for r in caplog.records if "handed its panel on" in r.getMessage()]

    async def test_a_push_while_the_panel_exit_unregisters_raises(self):
        """The persistent exit dispatched its unregister before stopping the
        panel, and a push in that window ran on the panel being closed."""
        from helpers import RenderableLayoutView, make_interaction

        class _Child(RenderableLayoutView):
            pass

        store, backend, middleware, old, new = await self._panels()
        suspended, release = asyncio.Event(), asyncio.Event()

        async def slow_unregister(action, state, next_fn):
            if action["type"] == "PERSISTENT_VIEW_UNREGISTERED":
                suspended.set()
                await release.wait()
            return await next_fn(action, state)

        store._add_middleware(slow_unregister)
        try:
            exiting = asyncio.create_task(old.exit())
            await suspended.wait()
            with pytest.raises(RuntimeError, match="has closed"):
                await old.push(_Child, interaction=make_interaction(user_id=1, guild_id=100))
            release.set()
            await exiting
        finally:
            store._remove_middleware(slow_unregister)

        assert not any(isinstance(v, _Child) for v in store._active_views.values())


class TestPanelExitThatFails:
    @pytest.mark.parametrize("close", ["exit", "replace"])
    async def test_a_close_cut_off_while_children_close_keeps_the_registration(self, close):
        """The registration went before the attached views closed, so a cancel
        there left the panel live and answering clicks, with no row to
        reattach it after a restart."""
        from helpers import RenderableLayoutView

        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        closing = asyncio.Event()

        class _Slow(RenderableLayoutView):
            async def exit(self, delete_message=None):
                closing.set()
                await asyncio.Event().wait()

        old.attach_child(_Slow())
        task = asyncio.create_task(
            old.exit() if close == "exit" else old.replace(RenderableLayoutView)
        )
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not old._closed()
        assert store.state["persistent_views"][setup.KEY]["message_id"] == "111"
        assert len(await setup._row(backend, middleware)) == 1

    async def test_a_second_exit_does_not_unregister_again(self):
        """The second call dispatched PERSISTENT_VIEW_UNREGISTERED for a key it
        no longer held, so a store.on() hook saw the panel retired twice."""
        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        unregistered = []

        async def hook(action, state):
            unregistered.append(action["payload"]["persistence_key"])

        store.on("PERSISTENT_VIEW_UNREGISTERED", hook)

        await old.exit()
        await old.exit()

        assert unregistered == [setup.KEY]

    async def test_an_exit_after_a_pushed_views_timeout_still_retires_the_panel(self):
        """A timeout keeps the panel restorable; an exit afterwards closes it,
        which is what the exit would have done on its own."""
        from helpers import RenderableLayoutView

        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        child = await old.push(RenderableLayoutView)

        await child.on_timeout()
        assert setup.KEY in store.state["persistent_views"]
        await child.exit()
        await middleware.flush_all()

        assert setup.KEY not in store.state.get("persistent_views", {})
        assert await setup._row(backend, middleware) == []

    async def test_a_panel_closed_while_it_posts_is_not_registered(self):
        """The registration ran after the send returned, so a panel closed
        while it posted still wrote a row and came back after a restart."""
        from discord.ui import ActionRow, TextDisplay
        from helpers import make_interaction

        from cascadeui import PersistentLayoutView, StatefulButton

        class _Panel(PersistentLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(TextDisplay("panel"))
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="closing_go")))

        posting, gate = asyncio.Event(), asyncio.Event()
        interaction = make_interaction(user_id=1, guild_id=100)
        real_send = interaction.response.send_message

        async def send_slowly(*args, **kwargs):
            posting.set()
            await gate.wait()
            return await real_send(*args, **kwargs)

        interaction.response.send_message = send_slowly
        panel = _Panel(persistence_key="closing:panel", interaction=interaction)
        registered = []

        async def hook(action, state):
            registered.append(action["payload"]["persistence_key"])

        panel.state_store.on("PERSISTENT_VIEW_REGISTERED", hook)
        try:
            sending = asyncio.create_task(panel.send())
            await asyncio.wait_for(posting.wait(), 1)
            await asyncio.wait_for(panel.exit(), 1)
            gate.set()
            assert await sending is None
        finally:
            panel.state_store.off("PERSISTENT_VIEW_REGISTERED", hook)

        assert registered == []

    async def test_a_panel_closed_while_on_bind_runs_posts_nothing(self):
        """The bind ran before the send held the panel, so the close ran on
        its own and the send then raised for a panel it had not begun."""
        from discord.ui import ActionRow, TextDisplay
        from helpers import make_interaction

        from cascadeui import PersistentLayoutView, StatefulButton

        binding, gate = asyncio.Event(), asyncio.Event()

        class _Panel(PersistentLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(TextDisplay("panel"))
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="binding_go")))

            async def on_bind(self, bot):
                binding.set()
                await gate.wait()

        interaction = make_interaction(user_id=1, guild_id=100)
        panel = _Panel(persistence_key="binding:panel", interaction=interaction)
        sending = asyncio.create_task(panel.send())
        await asyncio.wait_for(binding.wait(), 1)
        await asyncio.wait_for(panel.exit(), 1)
        gate.set()

        assert await sending is None
        interaction.response.send_message.assert_not_awaited()
        assert "binding:panel" not in panel.state_store.state.get("persistent_views", {})

    async def test_a_panel_stopped_while_it_is_sent_is_not_registered(self):
        from discord.ui import ActionRow, TextDisplay
        from helpers import make_interaction

        from cascadeui import PersistentLayoutView, StatefulButton

        class _Panel(PersistentLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(TextDisplay("panel"))
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="stopping_go")))

            async def seed_initial_state(self, state):
                self.stop()

        panel = _Panel(
            persistence_key="stopping:panel", interaction=make_interaction(user_id=1, guild_id=100)
        )

        assert await panel.send() is None
        assert panel._torn_down()
        assert "stopping:panel" not in panel.state_store.state.get("persistent_views", {})
        assert "closing:panel" not in panel.state_store.state.get("persistent_views", {})
        assert panel._torn_down()

    async def test_a_panel_exit_that_raises_leaves_the_panel_usable(self):
        """The unregister dispatch raised after exit() had marked the panel
        closing, and the panel then dropped every click until a restart."""
        from discord.ui import ActionRow, TextDisplay
        from helpers import make_interaction

        from cascadeui.views.persistent import PersistentLayoutView

        ran = []

        class _Panel(PersistentLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

                async def go(interaction):
                    ran.append(interaction)

                self.button = StatefulButton(label="Go", custom_id="panel_go", callback=go)
                self.add_item(TextDisplay("panel"))
                self.add_item(ActionRow(self.button))

        panel = _Panel(
            persistence_key="flaky:panel", interaction=make_interaction(user_id=1, guild_id=100)
        )
        await panel.send()
        store = panel.state_store

        async def flaky(action, state, next_fn):
            if action["type"] == "PERSISTENT_VIEW_UNREGISTERED":
                raise RuntimeError("backend blip")
            return await next_fn(action, state)

        store._add_middleware(flaky)
        try:
            with pytest.raises(RuntimeError, match="backend blip"):
                await panel.exit()
        finally:
            store._remove_middleware(flaky)

        assert not panel._closed()
        await panel._scheduled_task(panel.button, make_interaction(user_id=1, guild_id=100))
        assert len(ran) == 1


class TestKwargsSchemaVersionValidation:
    """Read with int() at every registry write, so a bad value failed there."""

    def test_a_per_instance_version_is_refused(self):
        """The restore compares a row against the class's version, so a value
        set on one instance was stored and then never matched."""
        from cascadeui.views.persistent import PersistentLayoutView

        class _Panel(PersistentLayoutView):
            kwargs_schema_version = 2

        panel = _Panel(persistence_key="version:instance")
        with pytest.raises(ValueError, match="read from the class"):
            panel.set_class_attribute("kwargs_schema_version", 5)

    async def test_the_stored_version_is_the_classs(self):
        """The row build read the instance while the restore read the class."""
        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        type(new).kwargs_schema_version = 3
        new.kwargs_schema_version = 9

        await new._register_persistent(new._message)

        rows = await setup._row(backend, middleware)
        assert [row["kwargs_schema_version"] for row in rows] == [3]

    @pytest.mark.parametrize("value", ["two", None, 0, True])
    def test_a_version_that_is_not_a_positive_int_is_refused(self, value):
        from cascadeui.views.persistent import PersistentLayoutView

        with pytest.raises(ValueError, match="kwargs_schema_version must be a positive int"):

            class _Panel(PersistentLayoutView):
                kwargs_schema_version = value

    def test_a_positive_version_is_accepted(self):
        from cascadeui.views.persistent import PersistentLayoutView

        class _Panel(PersistentLayoutView):
            kwargs_schema_version = 3

        assert _Panel.kwargs_schema_version == 3


class TestUnregisterReducerScope:
    """The unregister reducer removes a key only when it still points at the
    message that asked; a payload without a message id keeps the old meaning."""

    @staticmethod
    def _state(message_id):
        return {"persistent_views": {"k": {"persistence_key": "k", "message_id": message_id}}}

    async def test_matching_message_removes_the_key(self):
        action = {"payload": {"persistence_key": "k", "message_id": "1"}}
        state = await reduce_persistent_view_unregistered(action, self._state("1"))
        assert state["persistent_views"] == {}

    async def test_a_different_message_leaves_state_untouched(self):
        before = self._state("2")
        action = {"payload": {"persistence_key": "k", "message_id": "1"}}
        assert await reduce_persistent_view_unregistered(action, before) is before

    async def test_no_message_id_removes_the_key(self):
        action = {"payload": {"persistence_key": "k"}}
        state = await reduce_persistent_view_unregistered(action, self._state("2"))
        assert state["persistent_views"] == {}


class TestRegistrationOwnershipSeams:
    """Where a persistent panel's registration changes hands: a default
    replacement, several live predecessors, and a push/pop round trip that
    rebuilds the panel."""

    async def test_default_replacement_registers_once_without_a_fallback(self, monkeypatch):
        """The new panel registers before the old one exits, so the old
        panel's unregister removes nothing and no fallback re-enters the new
        panel's own registration."""
        from cascadeui.state.singleton import get_store

        store = get_store()
        registered = []
        fallbacks = []
        store.on("PERSISTENT_VIEW_REGISTERED", lambda action, state: registered.append(action))
        original = _StatefulMixin._reregister_live_predecessor

        async def spy(self, key):
            fallbacks.append(self.id)
            return await original(self, key)

        monkeypatch.setattr(_StatefulMixin, "_reregister_live_predecessor", spy)
        make = TestRetirePreviousOnSend._message

        old = TestRetirePreviousOnSend._panel_class(True)(label="old", persistence_key="own:1")
        old._message = make(111)
        store._register_view(old)
        await old._register_persistent(old._message)
        registered.clear()

        new = type(old)(label="new", persistence_key="own:1")
        new._message = make(999)
        store._register_view(new)
        await new._register_persistent(new._message)

        assert len(registered) == 1
        assert fallbacks == []
        assert old.is_finished()

    async def test_default_replacement_exits_every_live_predecessor(self):
        from cascadeui.state.singleton import get_store

        store = get_store()
        make = TestRetirePreviousOnSend._message
        keeper = TestRetirePreviousOnSend._panel_class(False)
        first = keeper(label="first", persistence_key="own:2")
        first._message = make(111)
        store._register_view(first)
        await first._register_persistent(first._message)
        second = keeper(label="second", persistence_key="own:2")
        second._message = make(222)
        store._register_view(second)
        await second._register_persistent(second._message)

        third = TestRetirePreviousOnSend._panel_class(True)(label="third", persistence_key="own:2")
        third._message = make(333)
        store._register_view(third)
        await third._register_persistent(third._message)

        assert first.is_finished() and second.is_finished()
        assert store.state["persistent_views"]["own:2"]["message_id"] == "333"

    async def test_pop_back_to_a_persistent_panel_keeps_its_registration_owner(self):
        """pop() rebuilds the panel; the rebuilt instance still owns the
        registration of the message it shows."""
        from helpers import make_interaction

        class _OwnedPanel(PersistentView):
            pass

        class _Child(StatefulView):
            async def on_state_changed(self, state):
                pass

        panel = _OwnedPanel(interaction=make_interaction(), persistence_key="own:nav")
        await panel.send()
        owner = panel._registry_message_id
        assert owner is not None

        child = await panel.push(_Child)
        restored = await child.pop()

        assert restored is not panel
        assert restored._registry_message_id == owner

    @staticmethod
    def _gone(view):
        view._last_tree_digest = None
        view._check_placement = lambda: None
        view._message.edit = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
        )
        return view

    async def test_an_edit_finding_the_message_gone_removes_the_panels_row(self):
        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, _ = await setup._setup(retire=True)

        await self._gone(old).refresh()
        await old._message_gone_task

        assert old.is_finished()
        assert await setup._row(backend, middleware) == []

    async def test_a_superseded_panels_gone_message_leaves_the_successors_row(self):
        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=False)
        await new._register_persistent(new._message)

        await self._gone(old).refresh()
        await old._message_gone_task

        assert old.is_finished()
        assert [r["message_id"] for r in await setup._row(backend, middleware)] == [999]


class TestIdlePushedScreenReturnsToPanel:
    """A view pushed onto a persistent panel's message timed out and froze the
    shared panel for everyone until a restart. It now returns to the panel."""

    @staticmethod
    async def _settle():
        for _ in range(30):
            await asyncio.sleep(0)

    @staticmethod
    def _screen(name="_Screen", **attrs):
        from discord.ui import ActionRow, TextDisplay

        async def noop(interaction):
            pass

        def __init__(self, **kwargs):
            StatefulLayoutView.__init__(self, **kwargs)
            self.add_item(TextDisplay(name))
            self.add_item(ActionRow(StatefulButton(label="Back", custom_id="back", callback=noop)))

        return type(name, (StatefulLayoutView,), {"__init__": __init__, **attrs})

    async def test_an_idle_pushed_screen_returns_to_the_panel(self):
        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        message = old._message
        screen = await old.push(self._screen())
        message.edit.reset_mock()

        screen._dispatch_timeout()
        await self._settle()

        panel = screen._successor
        assert panel is not None and type(panel) is type(old)
        assert message.edit.await_args.kwargs["view"] is panel
        assert not panel.is_finished() and panel._nav_stack == []
        assert store.state["persistent_views"][setup.KEY]["message_id"] == "111"
        assert store.get_active_view(persistence_key=setup.KEY) is panel

    async def test_a_deeper_screen_returns_to_the_panel_itself(self):
        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        middle = await old.push(self._screen("_Middle"))
        deeper = await middle.push(self._screen("_Deeper"))

        deeper._dispatch_timeout()
        await self._settle()

        panel = deeper._successor
        assert type(panel) is type(old)
        assert panel._nav_stack == []

    async def test_a_screen_whose_panel_row_was_pruned_times_out(self):
        """The operator pruned the panel's row: a return would bring back a
        panel no restart restores, so the screen freezes as before."""
        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        screen = await old.push(self._screen())
        await store.persistence_manager.prune_registry(persistence_keys=[setup.KEY])

        screen._dispatch_timeout()
        await self._settle()

        assert screen._successor is None and screen.is_finished()

    async def test_a_screen_with_its_own_on_timeout_keeps_it(self):
        ran = []

        async def on_timeout(self):
            ran.append(self)
            await StatefulLayoutView.on_timeout(self)

        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        screen = await old.push(self._screen(on_timeout=on_timeout))

        screen._dispatch_timeout()
        await self._settle()

        assert ran == [screen]
        assert screen._successor is None and screen.is_finished()

    async def test_a_return_that_fails_times_out_as_before(self):
        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        message = old._message
        screen = await old.push(self._screen())
        response = MagicMock(status=500, reason="Server Error")
        message.edit = AsyncMock(
            side_effect=[discord.HTTPException(response, "down"), message, message]
        )

        screen._dispatch_timeout()
        await self._settle()

        assert screen._successor is None and screen.is_finished()
        frozen = message.edit.await_args.kwargs["view"]
        assert all(
            button.disabled
            for button in frozen.walk_children()
            if isinstance(button, discord.ui.Button)
        )
        assert store.state["persistent_views"][setup.KEY]["message_id"] == "111"

    async def test_a_return_that_raises_times_out_as_before(self, caplog):
        """A raise from the return (the panel could not be rebuilt) ended the
        timeout's task, so the screen stayed live with its timer spent."""
        from helpers import until

        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        screen = await old.push(self._screen())
        entry = screen._nav_stack[-1]
        entry["kwargs"] = {**entry["kwargs"], "unknown_option": 1}

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            screen._dispatch_timeout()
            try:
                await until(screen.is_finished)
            except asyncio.TimeoutError:
                pytest.fail("the screen stayed live with its timer spent")
            await self._settle()

        assert screen._successor is None
        assert any("could not return to its panel" in r.getMessage() for r in caplog.records)

    async def test_a_screen_on_an_ordinary_view_times_out_as_before(self):
        from helpers import make_interaction

        root = self._screen("_Root")(interaction=make_interaction())
        await root.send()
        screen = await root.push(self._screen())

        screen._dispatch_timeout()
        await self._settle()

        assert screen._successor is None and screen.is_finished()

    async def test_a_push_that_lands_while_the_timeout_waits_is_left_alone(self):
        """The screen handed its message on while the return waited for the
        navigation, so returning would navigate a view that owns nothing."""
        from helpers import RenderableLayoutView

        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        screen = await old.push(self._screen())
        gate, entered = asyncio.Event(), asyncio.Event()

        class _Loading(RenderableLayoutView):
            async def on_load(self):
                entered.set()
                await gate.wait()

        pushing = asyncio.create_task(screen.push(_Loading))
        await asyncio.wait_for(entered.wait(), 1)
        screen._dispatch_timeout()
        await self._settle()
        gate.set()
        loaded = await pushing
        await self._settle()

        assert screen._successor is loaded
        assert not loaded.is_finished()
        assert loaded._successor is None  # nothing returned it to the panel

    async def test_a_timeout_during_an_exit_takes_the_ordinary_path(self):
        """A timeout arriving while an exit closes the screen does not return
        it to the panel, and still times it out if that exit is cut off."""
        from helpers import RenderableLayoutView

        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        screen = await old.push(self._screen())
        closing = asyncio.Event()

        class _Slow(RenderableLayoutView):
            async def exit(self, delete_message=None):
                closing.set()
                await asyncio.Event().wait()

        screen.attach_child(_Slow())
        exiting = asyncio.create_task(screen.exit())
        await asyncio.wait_for(closing.wait(), 1)
        screen._dispatch_timeout()
        exiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiting
        await self._settle()

        assert screen.is_finished()  # the timeout still ran

    async def test_an_exit_that_begins_while_the_return_waits_still_times_out(self):
        """The return gave up on a closing screen without timing it out, and
        discord.py's timer had already fired, so an exit cut off afterwards
        left the screen live with nothing left to close it."""
        from helpers import RenderableLayoutView

        setup = TestRetirePreviousOnSend()
        store, backend, middleware, old, new = await setup._setup(retire=True)
        screen = await old.push(self._screen())
        gate, entered, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class _Failing(RenderableLayoutView):
            async def on_load(self):
                entered.set()
                await gate.wait()
                raise RuntimeError("load failed")

        class _Slow(RenderableLayoutView):
            async def exit(self, delete_message=None):
                closing.set()
                await asyncio.Event().wait()

        screen.attach_child(_Slow())
        pushing = asyncio.create_task(screen.push(_Failing))
        await asyncio.wait_for(entered.wait(), 1)
        exiting = asyncio.create_task(screen.exit())  # waits out the push first
        await self._settle()
        screen._dispatch_timeout()  # the return queues behind the exit
        await self._settle()
        gate.set()
        with pytest.raises(RuntimeError, match="load failed"):
            await pushing
        await asyncio.wait_for(closing.wait(), 1)
        await self._settle()
        exiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiting
        await self._settle()

        assert screen._successor is None
        assert screen.is_finished()


class TestPanelSentAgain:
    async def test_a_panel_sent_again_freezes_the_message_it_left(self):
        # The duplicate-key cleanup took the message the panel had just left
        # for an orphan from an earlier run and deleted it, whatever the
        # panel's exit_policy said.
        from discord.components import _component_factory
        from discord.ui import ActionRow, TextDisplay
        from helpers import make_interaction

        from cascadeui import PersistentLayoutView

        class _Panel(PersistentLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(TextDisplay("panel"))
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="moving_go")))

        panel = _Panel(persistence_key="moving:panel", interaction=make_interaction())
        await panel.send()
        old = panel._message
        # Real components, so an orphan cleanup would have something to freeze.
        old.components = [_component_factory(payload) for payload in panel.to_components()]
        again = make_interaction()
        again.original_response.return_value.id = 4321
        again.original_response.return_value.components = [
            _component_factory(payload) for payload in panel.to_components()
        ]
        # A client whose channel lookup finds the old message, so the orphan
        # cleanup would run if it took the message for one.
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=old)
        again.client = MagicMock()
        again.client.get_channel = MagicMock(return_value=channel)
        panel.interaction = again

        assert await panel.send() is not None

        channel.fetch_message.assert_not_awaited()
        old.delete.assert_not_awaited()
        old.edit.assert_awaited_once()
        assert old.edit.await_args.kwargs["view"].is_finished()
        entry = panel.state_store.state["persistent_views"]["moving:panel"]
        assert entry["message_id"] == "4321"

    @staticmethod
    def _board(custom_id):
        from discord.ui import ActionRow, TextDisplay

        from cascadeui import PersistentLayoutView

        class _Panel(PersistentLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(TextDisplay("panel"))
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id=custom_id)))

        return _Panel

    async def test_a_message_posted_without_its_channel_still_registers(self):
        # discord.py leaves the channel off a message posted in a channel type
        # it cannot build, and registering read it and raised: the panel was
        # live and would not come back after a restart.
        from helpers import make_interaction

        interaction = make_interaction()
        interaction.channel_id = 555
        interaction.original_response.return_value.id = 100
        interaction.original_response.return_value.channel = None
        panel = self._board("nochannel_go")(persistence_key="nochannel", interaction=interaction)

        assert await panel.send() is not None

        state = panel.state_store.state
        assert state["persistent_views"]["nochannel"]["channel_id"] == "555"
        assert state["views"][panel.id]["channel_id"] == "555"

    async def test_a_panel_stopped_once_its_registration_moved_retires_it(self):
        # Closed as a timeout leaves a view, the stopped panel kept its row,
        # and a restart restored it on a message whose buttons were disabled.
        from helpers import make_interaction

        panel = self._board("stopped_go")(persistence_key="stopped", interaction=make_interaction())
        await panel.send()
        old = panel._message

        async def stop_while_leaving(**kwargs):
            panel.stop()
            return old

        old.edit = AsyncMock(side_effect=stop_while_leaving)
        again = make_interaction()
        again.original_response.return_value.id = 200
        panel.interaction = again

        assert await panel.send() is None

        assert panel._torn_down()
        assert "stopped" not in panel.state_store.state["persistent_views"]


class TestPolicyFollowsTheMessage:
    """A message a view leaves or supersedes closes by the view's exit_policy,
    and a registration naming it goes with it."""

    @staticmethod
    def _panel_class(**attrs):
        from discord.ui import ActionRow, TextDisplay

        from cascadeui import PersistentLayoutView

        def __init__(self, **kwargs):
            PersistentLayoutView.__init__(self, **kwargs)
            self.add_item(TextDisplay("panel"))
            self.add_item(ActionRow(StatefulButton(label="Go", custom_id="policy_go")))

        return type("_Panel", (PersistentLayoutView,), {"__init__": __init__, **attrs})

    async def test_a_screen_a_panel_pushed_to_sent_again_takes_the_panel_along(self):
        # The re-send retired the panel's registration, so Back on the new
        # message rebuilt a live panel that no restart would bring back.
        from helpers import RenderableLayoutView, make_interaction

        first = make_interaction()
        first.original_response.return_value.id = 100
        panel = self._panel_class()(persistence_key="policy:panel", interaction=first)
        await panel.send()
        store = panel.state_store
        registered = dict(store.state["persistent_views"]["policy:panel"])
        screen = await panel.push(RenderableLayoutView, make_interaction(message=MagicMock(id=100)))
        again = make_interaction()
        again.original_response.return_value.id = 200
        screen.interaction = again

        await screen.send()

        entry = store.state["persistent_views"]["policy:panel"]
        assert entry["message_id"] == "200"
        assert entry["class_name"] == registered["class_name"]
        assert "policy:panel" in store._live_persistence_keys()
        popped = await screen.pop(make_interaction(message=MagicMock(id=200)))
        assert store.get_active_view(persistence_key="policy:panel") is popped
        assert store.state["persistent_views"]["policy:panel"]["message_id"] == "200"

    async def test_a_screen_sent_again_as_its_bot_closes_keeps_the_panel_restorable(self):
        # The close cleared the screen's registration id, and the re-send then
        # retired the panel's row as if the screen had closed: the restart
        # restored nothing.
        from helpers import RenderableLayoutView, make_interaction

        class _Screen(RenderableLayoutView):
            releasing = False

            async def _update_message_state(self, message):
                await super()._update_message_state(message)
                if self.releasing:
                    # What the store does to each view of a bot that closes.
                    self._release_for_restart()

        first = make_interaction()
        first.original_response.return_value.id = 100
        panel = self._panel_class()(persistence_key="policy:released", interaction=first)
        await panel.send()
        screen = await panel.push(_Screen, make_interaction(message=MagicMock(id=100)))
        again = make_interaction()
        again.original_response.return_value.id = 200
        screen.interaction = again
        screen.releasing = True

        await screen.send()

        entry = screen.state_store.state["persistent_views"]["policy:released"]
        assert entry["message_id"] == "100"

    @pytest.mark.parametrize("policy", ["disable", "delete"])
    async def test_an_earlier_runs_message_closes_by_exit_policy(self, policy):
        # It was deleted whatever the policy said, while the same re-post over a
        # panel restored in this process froze it.
        from discord.components import _component_factory
        from helpers import make_interaction

        from cascadeui.state.actions import ActionCreators

        panel_class = self._panel_class(exit_policy=policy)
        first = make_interaction()
        first.original_response.return_value.id = 300
        panel = panel_class(persistence_key="policy:orphan", interaction=first)
        await panel.state_store.dispatch(
            "PERSISTENT_VIEW_REGISTERED",
            ActionCreators.persistent_view_registered(
                persistence_key="policy:orphan",
                class_name="earlier",
                message_id="999",
                channel_id="888",
                guild_id=None,
                user_id=None,
            ),
        )
        orphan = MagicMock(id=999)
        orphan.edit = AsyncMock()
        orphan.delete = AsyncMock()
        orphan.components = [_component_factory(p) for p in panel.to_components()]
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=orphan)
        first.client = MagicMock()
        first.client.get_channel = MagicMock(return_value=channel)

        await panel.send()

        if policy == "delete":
            orphan.delete.assert_awaited_once()
            orphan.edit.assert_not_awaited()
        else:
            orphan.delete.assert_not_awaited()
            frozen = orphan.edit.await_args.kwargs["view"]
            assert frozen.is_finished()

    async def test_a_panel_sent_again_moves_its_registration_before_closing_the_old_message(
        self,
    ):
        # The registration waited behind closing the old message, so a send
        # cancelled there left the row naming the message it had just frozen.
        from helpers import make_interaction

        first = make_interaction()
        first.original_response.return_value.id = 400
        panel = self._panel_class()(persistence_key="policy:moving", interaction=first)
        await panel.send()
        old = panel._message
        closing = asyncio.Event()

        async def hang(**kwargs):
            closing.set()
            await asyncio.Event().wait()

        old.edit = AsyncMock(side_effect=hang)
        again = make_interaction()
        again.original_response.return_value.id = 500
        panel.interaction = again
        sending = asyncio.create_task(panel.send())
        await asyncio.wait_for(closing.wait(), 1)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending

        entry = panel.state_store.state["persistent_views"]["policy:moving"]
        assert entry["message_id"] == "500"
