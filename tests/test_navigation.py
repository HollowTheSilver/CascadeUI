"""Tests for navigation reducers, view-local nav stack, and forward-transfer."""

import copy
from unittest.mock import AsyncMock, MagicMock

import pytest
from helpers import make_interaction as _make_interaction

from cascadeui.state.reducers import (
    reduce_navigation_pop,
    reduce_navigation_push,
    reduce_navigation_replace,
)
from cascadeui.state.singleton import get_store
from cascadeui.views.view import StatefulView

# // ========================================( Reducer No-Op Verification )======================================== // #


class TestNavigationPushReducer:
    """NAVIGATION_PUSH reducer is a no-op -- nav stack is view-local."""

    async def test_push_is_noop(self):
        """NAVIGATION_PUSH reducer returns state unchanged (nav stack is view-local)."""
        state = {
            "sessions": {
                "user_1": {
                    "id": "user_1",
                    "members": [],
                    "history": [],
                    "shared_data": {},
                }
            },
            "views": {},
            "application": {},
        }

        action = {
            "type": "NAVIGATION_PUSH",
            "payload": {
                "session_id": "user_1",
                "class_name": "HomeView",
                "module": "test.views",
                "kwargs": {},
                "state_snapshot": None,
            },
            "source": None,
            "timestamp": "2026-01-01T00:00:00",
        }

        new_state = await reduce_navigation_push(action, state)
        assert new_state is state

    async def test_push_does_not_modify_session(self):
        """Push should not add nav_stack to the session."""
        state = {
            "sessions": {
                "s1": {"id": "s1", "members": [], "history": [], "shared_data": {}},
            },
            "views": {},
        }
        original = copy.deepcopy(state)

        for name in ["ViewA", "ViewB", "ViewC"]:
            action = {
                "type": "NAVIGATION_PUSH",
                "payload": {
                    "session_id": "s1",
                    "class_name": name,
                    "module": "test",
                    "kwargs": {},
                    "state_snapshot": None,
                },
                "source": None,
                "timestamp": "2026-01-01T00:00:00",
            }
            state = await reduce_navigation_push(action, state)

        # Session should be untouched -- no nav_stack key added
        assert "nav_stack" not in state["sessions"]["s1"]


class TestNavigationPopReducer:
    """NAVIGATION_POP reducer is a no-op -- nav stack is view-local."""

    async def test_pop_is_noop(self):
        """NAVIGATION_POP reducer returns state unchanged (nav stack is view-local)."""
        state = {
            "sessions": {
                "s1": {"id": "s1"},
            },
        }

        action = {
            "type": "NAVIGATION_POP",
            "payload": {"session_id": "s1"},
            "source": None,
            "timestamp": "2026-01-01T00:00:00",
        }

        new_state = await reduce_navigation_pop(action, state)
        assert new_state is state


# // ========================================( Store Dispatch )======================================== // #


class TestNavigationStackIntegration:
    """Push and pop dispatches complete without error through the store."""

    async def test_push_pop_dispatches_succeed(self):
        """Push and pop dispatches through the store complete without error."""
        store = get_store()

        await store.dispatch(
            "SESSION_CREATED",
            {"session_id": "nav_test", "user_id": 1},
        )

        # Dispatching NAVIGATION_PUSH should succeed (no-op reducer)
        await store.dispatch(
            "NAVIGATION_PUSH",
            {
                "session_id": "nav_test",
                "class_name": "PageA",
                "module": "test",
                "kwargs": {},
                "state_snapshot": None,
            },
        )

        # Session should have no nav_stack (view-local now)
        assert "nav_stack" not in store.state["sessions"]["nav_test"]

        # Pop dispatch should also succeed
        await store.dispatch("NAVIGATION_POP", {"session_id": "nav_test"})


# // ========================================( Replace Reducer )======================================== // #


class TestNavigationReplaceReducer:
    """NAVIGATION_REPLACE records history and handles missing source views."""

    async def test_replace_records_history(self):
        """NAVIGATION_REPLACE should append to session history."""
        state = {
            "sessions": {
                "s1": {"id": "s1", "members": [], "history": [], "shared_data": {}},
            },
            "views": {
                "view_1": {"session_id": "s1"},
            },
            "application": {},
        }

        action = {
            "type": "NAVIGATION_REPLACE",
            "payload": {"destination": "SettingsView", "params": {}},
            "source": "view_1",
            "timestamp": "2026-01-01T00:00:00",
        }

        new_state = await reduce_navigation_replace(action, state)
        history = new_state["sessions"]["s1"]["history"]
        assert len(history) == 1
        assert history[0]["to_view_type"] == "SettingsView"
        assert history[0]["from_view"] == "view_1"

    async def test_replace_no_source_is_noop(self):
        """Replace without a source view should return state unchanged."""
        state = {"sessions": {"s1": {"id": "s1"}}, "views": {}}

        action = {
            "type": "NAVIGATION_REPLACE",
            "payload": {"destination": "SomeView"},
            "source": None,
            "timestamp": "2026-01-01T00:00:00",
        }

        new_state = await reduce_navigation_replace(action, state)
        assert new_state is state


# // ========================================( View-Local Nav Stack Integration )======================================== // #


class TestNavStackForwardTransfer:
    """Integration tests for view-local nav_stack through push/pop chains."""

    async def test_push_builds_nav_stack(self):
        """Pushing A -> B should give B a nav_stack with one entry pointing to A."""

        class _ViewA(StatefulView):
            pass

        class _ViewB(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _ViewA(interaction=_make_interaction())
        await root.send()
        assert root._nav_stack == []

        child = await root.push(_ViewB)
        assert len(child._nav_stack) == 1
        assert child._nav_stack[0]["class_name"] == _ViewA._class_session_key()

    async def test_deep_push_chain(self):
        """Pushing A -> B -> C -> D should give D a nav_stack with 3 entries."""

        class _A(StatefulView):
            pass

        class _B(StatefulView):
            async def on_state_changed(self, state):
                pass

        class _C(StatefulView):
            async def on_state_changed(self, state):
                pass

        class _D(StatefulView):
            async def on_state_changed(self, state):
                pass

        a = _A(interaction=_make_interaction())
        await a.send()
        b = await a.push(_B)
        c = await b.push(_C)
        d = await c.push(_D)

        assert len(d._nav_stack) == 3
        assert d._nav_stack[0]["class_name"] == _A._class_session_key()
        assert d._nav_stack[1]["class_name"] == _B._class_session_key()
        assert d._nav_stack[2]["class_name"] == _C._class_session_key()

    async def test_pop_shrinks_nav_stack(self):
        """Popping from C (depth 2) should give the restored view a nav_stack of depth 1."""

        class _Root(StatefulView):
            pass

        class _Mid(StatefulView):
            async def on_state_changed(self, state):
                pass

        class _Deep(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction())
        await root.send()
        mid = await root.push(_Mid)
        deep = await mid.push(_Deep)
        assert len(deep._nav_stack) == 2

        popped = await deep.pop()
        assert len(popped._nav_stack) == 1

        popped2 = await popped.pop()
        assert len(popped2._nav_stack) == 0

    async def test_replace_clears_nav_stack(self):
        """replace() is a one-way transition -- nav_stack starts empty."""

        class _Old(StatefulView):
            pass

        class _New(StatefulView):
            async def on_state_changed(self, state):
                pass

        old = _Old(interaction=_make_interaction())
        await old.send()

        new = await old.replace(_New)
        assert new._nav_stack == []

    async def test_nav_stack_not_on_session(self):
        """After push/pop, session should never have a nav_stack key."""
        store = get_store()

        class _Hub(StatefulView):
            pass

        class _Page(StatefulView):
            async def on_state_changed(self, state):
                pass

        hub = _Hub(interaction=_make_interaction())
        await hub.send()
        session_id = hub.session_id

        page = await hub.push(_Page)
        session = store.state["sessions"].get(session_id, {})
        assert "nav_stack" not in session


# // ========================================( Kwargs Round-Trip )======================================== // #


class TestKwargsRoundTrip:
    """Push records kwargs so pop can reconstruct the parent view."""

    async def test_init_kwargs_captured(self):
        """Subclass constructor kwargs are captured automatically."""

        class _Config(StatefulView):
            def __init__(self, *, color="red", **kwargs):
                self.color = color
                super().__init__(**kwargs)

        v = _Config(interaction=_make_interaction(), color="blue")
        await v.send()
        assert v._init_kwargs.get("color") == "blue"

    async def test_kwargs_survive_push_pop(self):
        """kwargs captured before push are used to reconstruct on pop."""

        class _Parent(StatefulView):
            def __init__(self, *, label="default", **kwargs):
                self.label = label
                super().__init__(**kwargs)

        class _Child(StatefulView):
            async def on_state_changed(self, state):
                pass

        parent = _Parent(interaction=_make_interaction(), label="custom")
        await parent.send()
        assert parent.label == "custom"

        child = await parent.push(_Child)
        restored = await child.pop()

        assert isinstance(restored, _Parent)
        assert restored.label == "custom"

    async def test_non_reconstructible_kwargs_excluded(self):
        """Context, interaction, state_store, session_id, user_id, guild_id
        are excluded from captured kwargs (supplied at reconstruction time)."""

        class _View(StatefulView):
            pass

        v = _View(interaction=_make_interaction())
        await v.send()

        for key in (
            "context",
            "interaction",
            "message",
            "state_store",
            "session_id",
            "user_id",
            "guild_id",
        ):
            assert key not in v._init_kwargs


# // ========================================( Participant Propagation )======================================== // #


class TestParticipantPropagation:
    """Participants carry through push/pop but not replace."""

    async def test_push_propagates_participants(self):

        class _Game(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Game(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        await root.register_participant(2)
        await root.register_participant(3)

        child = await root.push(_Sub)
        assert 2 in child._participants
        assert 3 in child._participants

    async def test_pop_propagates_participants(self):

        class _Root(StatefulView):
            pass

        class _Child(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        await root.register_participant(2)

        child = await root.push(_Child)
        assert 2 in child._participants

        restored = await child.pop()
        assert 2 in restored._participants

    async def test_replace_drops_participants(self):

        class _Old(StatefulView):
            pass

        class _New(StatefulView):
            async def on_state_changed(self, state):
                pass

        old = _Old(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send()
        await old.register_participant(2)

        new = await old.replace(_New)
        assert 2 not in new._participants


class TestNavigationMessageState:
    """Push/pop targets inherit the parent's message; the state row must
    carry message_id and channel_id so tooling (inspector, persistence,
    admin commands) can locate the Discord message without the live
    view object."""

    async def test_push_populates_new_view_message_state(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        root_state = get_store().state["views"][root.id]
        assert root_state["message_id"] is not None
        assert root_state["channel_id"] is not None

        child = await root.push(_Sub)

        child_state = get_store().state["views"][child.id]
        assert child_state["message_id"] == root_state["message_id"]
        assert child_state["channel_id"] == root_state["channel_id"]

    async def test_pop_populates_restored_view_message_state(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        original_msg_id = get_store().state["views"][root.id]["message_id"]

        child = await root.push(_Sub)
        restored = await child.pop()

        restored_state = get_store().state["views"][restored.id]
        assert restored_state["message_id"] == original_msg_id
        assert restored_state["channel_id"] is not None


# // ========================================( Instance navigation )======================================== // #


class TestNavigationInstanceForm:
    """``push()`` and ``replace()`` accept a pre-constructed view instance
    in addition to a class. The instance form unblocks composition with
    async classmethod constructors (``from_data``, ``from_cursor``)
    where the view is built before the navigation call.
    """

    async def test_push_accepts_pre_constructed_instance(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        parent_session = root.session_id

        # Construct the child outside the navigation call -- mirrors the
        # ``await CategoryListView.from_data(...)`` path users follow.
        child = _Sub(interaction=_make_interaction(user_id=1, guild_id=100))

        # Child is not in the state row registry until push runs -- _register_view
        # and _register_state fire from the navigation path, not __init__.
        store = get_store()
        assert child.id not in store.state["views"]
        assert child.id not in store._active_views

        # The instance arrives with its own auto-derived session_id.
        assert child.session_id != parent_session

        pushed = await root.push(child)

        # After push, the child is fully registered: state row exists and
        # the active-view index includes it. Without these calls, the child
        # is invisible to the state inspector, instance limit enforcement,
        # and any code reading from state["views"].
        assert pushed.id in store.state["views"]
        assert pushed.id in store._active_views

        # Session inherits from the parent. Class-path push behaves
        # the same way; instance-path push must match so shared_data
        # and the session lifecycle stay consistent across navigation.
        assert pushed.session_id == parent_session

    async def test_push_instance_preserves_shared_data(self):
        """Parent's ``shared_data`` must survive instance-push so the
        child sees the same session state. Without session inheritance,
        the parent's session would be deleted when the parent is
        destroyed (last-member rule) and ``shared_data`` would be lost.
        """

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=2, guild_id=100))
        await root.send()
        await root.update_session(my_data="hello from parent")

        child = _Sub(interaction=_make_interaction(user_id=2, guild_id=100))
        pushed = await root.push(child)

        assert pushed.shared_data == {"my_data": "hello from parent"}

        assert pushed is child
        # Nav stack on the pushed view records the parent.
        assert len(pushed._nav_stack) == 1
        assert pushed._nav_stack[0]["class_name"] == _Root._class_session_key()
        # Message reference propagates from parent.
        assert pushed._message is root._message

    async def test_push_class_form_still_works(self):
        """Backward compat: push(class) routes through the original
        construction path."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        child = await root.push(_Sub)

        assert isinstance(child, _Sub)
        assert child is not root
        assert len(child._nav_stack) == 1

    async def test_push_instance_with_kwargs_raises(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        child = _Sub(interaction=_make_interaction(user_id=1, guild_id=100))

        with pytest.raises(TypeError, match="pre-constructed view instance"):
            await root.push(child, user_id=999)

    async def test_replace_accepts_pre_constructed_instance(self):
        class _Origin(StatefulView):
            pass

        class _Destination(StatefulView):
            async def on_state_changed(self, state):
                pass

        origin = _Origin(interaction=_make_interaction(user_id=1, guild_id=100))
        await origin.send()

        dest = _Destination(interaction=_make_interaction(user_id=1, guild_id=100))
        replaced = await origin.replace(dest)

        assert replaced is dest
        # Replace clears the nav stack -- it is a one-way transition.
        assert replaced._nav_stack == []

    async def test_pop_back_to_root_via_instance_push(self):
        """End-to-end: push an instance, then pop, and verify the parent
        is restored from the saved nav-stack entry."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        original_id = root.id

        child = _Sub(interaction=_make_interaction(user_id=1, guild_id=100))
        pushed = await root.push(child)
        assert pushed is child

        restored = await child.pop()
        assert isinstance(restored, _Root)
        # New _Root instance reconstructed via the registry, not the original.
        assert restored.id != original_id
