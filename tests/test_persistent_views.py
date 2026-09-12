"""Tests for persistent views: class registry, custom_id validation, and reducers."""

import asyncio
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
