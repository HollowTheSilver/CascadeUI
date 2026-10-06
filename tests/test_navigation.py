"""Tests for navigation reducers, view-local nav stack, and forward-transfer."""

import asyncio
import copy
import io
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from discord.ui import ActionRow
from helpers import RenderableLayoutView
from helpers import make_interaction as _make_interaction
from helpers import until

from cascadeui.components.base import StatefulButton
from cascadeui.state.actions import ActionCreators
from cascadeui.state.middleware import UndoMiddleware
from cascadeui.state.reducers import (
    reduce_navigation_pop,
    reduce_navigation_push,
    reduce_navigation_replace,
)
from cascadeui.state.singleton import get_store
from cascadeui.views.base import _class_path
from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.view import StatefulView


def _tree_text(view) -> str:
    """Every TextDisplay in a V2 view's tree, joined.

    A pop restores a cursor and rebuilds the body from it. Asserting only
    that a body exists cannot tell the restored tab's content from the
    first tab's, so these tests read what the tree actually says.
    """
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )


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
        assert child._nav_stack[0]["class_name"] == _class_path(_ViewA)

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
        assert d._nav_stack[0]["class_name"] == _class_path(_A)
        assert d._nav_stack[1]["class_name"] == _class_path(_B)
        assert d._nav_stack[2]["class_name"] == _class_path(_C)

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

    async def test_pop_resolves_a_class_that_pins_its_session_key(self):
        """A session_class_key pin must not break pop's class resolution.

        The entry records the class path, which is what _view_class_registry
        is keyed on. Recording the session key instead made the lookup miss,
        so Back died as a warning and a None rather than an exception.
        """

        class _PinnedHub(StatefulView):
            session_class_key = "legacy.app.Hub"

            async def on_state_changed(self, state):
                pass

        class _Page(StatefulView):
            async def on_state_changed(self, state):
                pass

        hub = _PinnedHub(interaction=_make_interaction())
        await hub.send()
        page = await hub.push(_Page)

        restored = await page.pop()
        assert restored is not None, "pop() could not resolve the pinned parent class"
        assert type(restored) is _PinnedHub
        # The mechanism, asserted after the symptom so a regression reports the
        # dead Back button rather than the entry format that caused it.
        assert page._nav_stack[0]["class_name"] == _class_path(_PinnedHub)
        # The pin still does its own job: a session key it no longer shares
        # with the nav entry is not a pin that stopped applying.
        assert restored.session_id.startswith("legacy.app.Hub:")


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


class TestNavigationAttachmentTransfer:
    """Push/pop migrate attachment tracking onto the destination.

    A navigating parent's attached children re-parent onto the new view so
    the cleanup cascade survives the hop, and a navigating child's own
    parent tracks the destination in its place. replace() is a one-way
    transition -- attachments do not carry over.
    """

    async def test_push_reparents_children_onto_destination(self):
        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        parent = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await parent.send()
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100))
        parent.attach_child(child)

        sub = await parent.push(_Sub)

        assert child._attached_to is sub
        assert child in sub._attached_children
        assert child not in parent._attached_children

    async def test_navigating_child_stays_under_parent_tracking(self):
        class _Game(StatefulView):
            pass

        class _Panel(StatefulView):
            pass

        class _SubPanel(StatefulView):
            async def on_state_changed(self, state):
                pass

        game = _Game(interaction=_make_interaction(user_id=1, guild_id=100))
        await game.send()
        panel = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await panel.send()
        game.attach_child(panel)

        sub = await panel.push(_SubPanel)

        # The parent's cleanup cascade now covers the destination, not the
        # torn-down source.
        assert sub in game._attached_children
        assert sub._attached_to is game
        assert panel not in game._attached_children
        assert panel._attached_to is None

    async def test_pop_reparents_children_onto_restored_view(self):
        class _Root(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        sub = await root.push(_Sub)

        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100))
        sub.attach_child(child)

        restored = await sub.pop()

        assert child._attached_to is restored
        assert child in restored._attached_children

    async def test_rollback_keeps_source_tracking_intact(self):
        """A failed destination edit rolls the navigation back. The
        migration waits for the confirmed edit, so the recovered source
        still tracks its child and the torn-down destination adopted
        nothing."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        parent = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await parent.send()
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100))
        parent.attach_child(child)

        # Every edit endpoint fails -> _roll_back_navigation.
        err = discord.HTTPException(MagicMock(status=503), "boom")
        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=err)
        nav.response.defer = AsyncMock(side_effect=err)
        nav.edit_original_response = AsyncMock(side_effect=err)
        parent._message.edit = AsyncMock(side_effect=err)

        sub = await parent.push(_Sub, interaction=nav)

        assert child._attached_to is parent
        assert child in parent._attached_children
        assert child not in sub._attached_children

    async def test_replace_does_not_carry_attachments(self):
        class _Old(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        class _New(StatefulView):
            async def on_state_changed(self, state):
                pass

        old = _Old(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send()
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100))
        old.attach_child(child)

        new = await old.replace(_New)

        # Not carried to the new view: it closes with the view it was
        # attached to, as that view's exit() closes it.
        assert new._attached_children == []
        assert child._torn_down()

    async def test_parent_reads_before_and_after_the_attaching_send(self):
        """A child constructed with parent= holds it from construction.

        The attach itself happens in the send pipeline, so a child that
        reads its parent during on_load or build_ui runs before that, and
        one reading it in a callback runs after. Both resolve here.
        """

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await parent.send()

        child = _Child(
            interaction=_make_interaction(user_id=1, guild_id=100),
            parent=parent,
        )
        assert child.parent is parent  # pending, before the send attaches it

        await child.send()
        assert child.parent is parent  # attached
        assert parent.parent is None  # a root has none

    async def test_parent_clears_when_the_parent_tears_down(self):
        """A child that finished on its own still gets the back-pointer
        cleared: the parent's list is emptied either way, so keeping it
        would name a parent that no longer tracks this child."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await parent.send()
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100))
        parent.attach_child(child)
        child.stop()  # the child finished on its own first

        await parent._cleanup_attached_children()

        assert child.parent is None

    async def test_parent_clears_when_the_send_that_would_attach_it_fails(self):
        """The attach is the last stage of a successful send, so a failed
        one leaves no parent to report."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            async def on_pre_send(self, interaction):
                return False

        parent = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await parent.send()
        child = _Child(
            interaction=_make_interaction(user_id=1, guild_id=100),
            parent=parent,
        )
        assert child.parent is parent

        assert await child.send() is None

        assert child.parent is None

    async def test_a_notification_queued_before_teardown_does_not_rebuild(self):
        """A state notification landing after teardown must not rebuild.

        Subscriber fan-out runs as tasks, so a dispatch can outlive the
        view it targets, and teardown clears the parent link before the
        child exits. Before the guard, the rebuild read ``parent`` as
        ``None`` and raised inside the subscriber wrapper. Seen live: a
        fleet panel logged ``'NoneType' object has no attribute
        'player_1'`` as its game view tore down.
        """
        rebuilds = []

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            def build_ui(self):
                rebuilds.append(1)
                # What the panel does: read something off the parent.
                assert self.parent is not None

        parent = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await parent.send()
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100))
        parent.attach_child(child)

        await parent._cleanup_attached_children()
        assert child.is_finished()

        # The queued notification lands now, after the teardown.
        await child._handle_state_notification(child.state_store.state, {"type": "BATCH_COMPLETE"})

        assert rebuilds == []

    async def test_a_finished_child_orphaned_by_the_cascade_does_not_rebuild(self):
        """A child that stopped without tearing down is torn down on the way past.

        The cascade nulls the parent link and then skips a finished child,
        and ``exit()`` is the only thing that unsubscribes. Left subscribed,
        the child rebuilds on the next notification and reads a parent that
        is no longer there, which is the same crash as the sibling above
        reached by a different route.
        """
        rebuilds = []

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            def build_ui(self):
                rebuilds.append(1)
                assert self.parent is not None

        parent = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await parent.send()
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100))
        parent.attach_child(child)
        # Finished on its own -- a bare stop(), or an on_timeout override that
        # never delegated -- so the cascade skips it instead of exiting it.
        child.stop()

        await parent._cleanup_attached_children()
        assert child.parent is None

        await child._handle_state_notification(child.state_store.state, {"type": "BATCH_COMPLETE"})

        assert rebuilds == []

    async def test_a_finished_child_leaves_both_registries_with_the_cascade(self):
        """Unsubscribing it alone is not enough: that is what answers "torn
        down", so a child left in the registries reads as torn down while
        still holding its message, and every later cleanup path skips it.
        """

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await parent.send()
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100))
        await child.send()
        parent.attach_child(child)
        child.stop()
        store = child.state_store
        assert child.id in store._active_views

        await parent._cleanup_attached_children()

        assert child.id not in store._active_views
        assert child.id not in store.state["views"]

    async def test_parent_follows_a_re_parent(self):
        class _First(StatefulView):
            pass

        class _Second(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        first = _First(interaction=_make_interaction(user_id=1, guild_id=100))
        second = _Second(interaction=_make_interaction(user_id=1, guild_id=100))
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100))

        first.attach_child(child)
        assert child.parent is first

        second.attach_child(child)
        assert child.parent is second


class TestExitChildren:
    """``exit_children()`` runs the attachment cascade on a parent that stays open."""

    @staticmethod
    async def _sent(cls, message_id, **kwargs):
        interaction = _make_interaction(user_id=1, guild_id=100)
        interaction.original_response.return_value.id = message_id
        view = cls(interaction=interaction, **kwargs)
        await view.send()
        return view

    async def test_children_close_and_the_parent_stays_open(self):
        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = await self._sent(_Parent, 1001)
        first = await self._sent(_Child, 1002, parent=parent)
        second = await self._sent(_Child, 1003, parent=parent)
        messages = (first._message, second._message)

        await parent.exit_children()

        assert first.is_finished() and second.is_finished()
        for message in messages:
            message.delete.assert_awaited_once()
        parent._message.delete.assert_not_awaited()
        assert not parent.is_finished()
        assert parent.id in parent.state_store._active_views
        assert parent._attached_children == []
        assert first.parent is None

    @staticmethod
    def _park_first(message, method):
        """Make ``message.<method>`` hang on its first call; later calls return."""
        parked = asyncio.Event()

        async def call(*args, **kwargs):
            if not parked.is_set():
                parked.set()
                await asyncio.Event().wait()
            return message

        setattr(message, method, AsyncMock(side_effect=call))
        return parked

    @pytest.mark.parametrize("delete_message, method", [(True, "delete"), (False, "edit")])
    async def test_a_child_close_cut_off_in_its_edit_is_made_again(self, delete_message, method):
        """A view-owned task running the call is cancelled by the view's own
        push or exit. The child was torn down by then, and its message stayed
        up with buttons that answered nothing."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = await self._sent(_Parent, 1041)
        child = await self._sent(_Child, 1042, parent=parent)
        message = child._message
        parked = self._park_first(message, method)
        closing = asyncio.create_task(parent.exit_children(delete_message=delete_message))
        await asyncio.wait_for(parked.wait(), 1)

        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        for _ in range(40):
            await asyncio.sleep(0)

        assert child._torn_down()
        assert getattr(message, method).await_count == 2

    async def test_a_push_during_the_call_leaves_the_closed_child_behind(self):
        """The push's hand-off re-attached a child already torn down, so the
        closed child named the pushed screen as its parent."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        class _Screen(StatefulView):
            pass

        parent = await self._sent(_Parent, 1051)
        closed = await self._sent(_Child, 1052, parent=parent)
        kept = await self._sent(_Child, 1053, parent=parent)
        parked = self._park_first(closed._message, "delete")
        parent.create_task(parent.exit_children())
        await asyncio.wait_for(parked.wait(), 1)

        screen = await parent.push(
            _Screen, _make_interaction(user_id=1, guild_id=100, message=parent._message)
        )

        assert closed._torn_down()
        assert closed.parent is None
        assert closed not in screen._attached_children
        assert kept.parent is screen

    async def test_delete_message_false_freezes_each_child(self):
        """The cascade used to hard-code a delete, so the argument was never read."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = await self._sent(_Parent, 1011)
        child = await self._sent(_Child, 1012, parent=parent)
        message = child._message

        await parent.exit_children(delete_message=False)

        assert child.is_finished()
        message.delete.assert_not_awaited()
        message.edit.assert_awaited()

    async def test_delete_message_none_follows_each_childs_exit_policy(self):
        class _Parent(StatefulView):
            pass

        class _Deleting(StatefulView):
            exit_policy = "delete"

        class _Freezing(StatefulView):
            exit_policy = "disable"

        parent = await self._sent(_Parent, 1021)
        deleting = await self._sent(_Deleting, 1022, parent=parent)
        freezing = await self._sent(_Freezing, 1023, parent=parent)
        deleted, frozen = deleting._message, freezing._message

        await parent.exit_children(delete_message=None)

        deleted.delete.assert_awaited_once()
        frozen.delete.assert_not_awaited()
        frozen.edit.assert_awaited()

    async def test_a_child_attached_afterwards_still_closes_with_the_parent(self):
        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = await self._sent(_Parent, 1031)
        await self._sent(_Child, 1032, parent=parent)
        await parent.exit_children()

        later = await self._sent(_Child, 1033, parent=parent)
        assert later.parent is parent

        await parent.exit()

        assert later.is_finished()

    async def test_a_child_that_already_timed_out_keeps_its_frozen_message(self):
        """delete_message applies to children still open; a closed child's
        message is what its own close left, which may be a card kept on
        purpose."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = await self._sent(_Parent, 1081)
        closed = await self._sent(_Child, 1082, parent=parent)
        open_child = await self._sent(_Child, 1083, parent=parent)
        closed_message, open_message = closed._message, open_child._message
        await closed.on_timeout()

        await parent.exit_children()

        closed_message.delete.assert_not_awaited()
        open_message.delete.assert_awaited_once()

    @staticmethod
    def _stall_delete(view):
        """Hold the view's message delete until the returned event is set."""
        release = asyncio.Event()
        started = asyncio.Event()

        async def delete(*args, **kwargs):
            started.set()
            await release.wait()

        view._message.delete = AsyncMock(side_effect=delete)
        return started, release

    async def test_a_view_attached_while_it_runs_is_left_for_the_next_set(self):
        """The next round's panel, sent while the last round's were still
        closing, was drained into that cascade and closed with them."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = await self._sent(_Parent, 1041)
        old = await self._sent(_Child, 1042, parent=parent)
        started, release = self._stall_delete(old)
        closing = asyncio.create_task(parent.exit_children())
        await started.wait()

        new = await self._sent(_Child, 1043, parent=parent)
        release.set()
        await closing

        assert old.is_finished()
        assert not new.is_finished()
        assert parent._attached_children == [new]
        assert new.parent is parent

    async def test_a_view_attached_while_the_parent_exits_closes_with_it(self):
        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = await self._sent(_Parent, 1051)
        old = await self._sent(_Child, 1052, parent=parent)
        started, release = self._stall_delete(old)
        exiting = asyncio.create_task(parent.exit())
        await started.wait()

        late = await self._sent(_Child, 1053)
        parent.attach_child(late)
        release.set()
        await exiting

        assert late.is_finished()

    async def test_a_cancelled_call_keeps_the_views_it_had_not_reached(self):
        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = await self._sent(_Parent, 1061)
        first = await self._sent(_Child, 1062, parent=parent)
        second = await self._sent(_Child, 1063, parent=parent)
        started, _ = self._stall_delete(first)
        closing = asyncio.create_task(parent.exit_children())
        await started.wait()

        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert not second.is_finished()
        assert second in parent._attached_children
        assert second.parent is parent

    async def test_an_exit_during_exit_children_closes_every_child(self):
        """The parent's exit cancels its own tasks, the exit_children() one
        included; a child that call had not reached was hidden from the exit
        and stayed live under a torn-down parent."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        parent = await self._sent(_Parent, 1091)
        first = await self._sent(_Child, 1092, parent=parent)
        second = await self._sent(_Child, 1093, parent=parent)
        started, release = self._stall_delete(first)
        parent.create_task(parent.exit_children())
        await started.wait()

        exiting = asyncio.create_task(parent.exit())
        await asyncio.sleep(0)
        release.set()
        await exiting

        assert parent.is_finished()
        assert first.is_finished() and second.is_finished()

    async def test_a_cancel_while_waiting_on_a_child_keeps_it_tracked(self):
        """The child the cascade was waiting on had been taken off the list,
        so an exit cancelled there, with the child's push then rolling back,
        left the child live and reachable by no later exit."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        gate = asyncio.Event()

        class _Dest(StatefulView):
            async def on_load(self):
                await gate.wait()
                raise RuntimeError("destination load failed")

        parent = await self._sent(_Parent, 1101)
        child = await self._sent(_Child, 1102, parent=parent)
        pushing = asyncio.create_task(child.push(_Dest, _make_interaction(message=child._message)))
        await until(lambda: child._away_for_navigation)
        exiting = asyncio.create_task(parent.exit())
        for _ in range(5):
            await asyncio.sleep(0)
        exiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiting
        gate.set()
        with pytest.raises(RuntimeError, match="destination load failed"):
            await pushing
        assert not child._torn_down()

        await parent.exit()

        assert child._torn_down()

    async def test_a_failed_send_closes_a_view_attached_while_it_rolls_back(self):
        """Every cleanup but exit_children() closes what was attached while
        it ran; the rollback of a failed send is not closing its view yet, so
        a rule keyed on the view being closed left the companion live."""

        class _Companion(StatefulView):
            pass

        holder = {}

        class _Panel(StatefulView):
            async def on_load(self):
                holder["first"] = await_first = _Companion(
                    interaction=_make_interaction(user_id=1, guild_id=100), parent=self
                )
                await await_first.send()
                holder["release"] = self._stall_first(await_first)

            def _stall_first(self, view):
                started, release = TestExitChildren._stall_delete(view)
                holder["started"] = started
                return release

        failing = _make_interaction(user_id=1, guild_id=100)
        response = MagicMock(status=500, reason="boom")
        failing.response.send_message = AsyncMock(
            side_effect=discord.HTTPException(response, "boom")
        )
        panel = _Panel(interaction=failing)
        sending = asyncio.create_task(panel.send())
        await until(lambda: "started" in holder)
        await asyncio.wait_for(holder["started"].wait(), timeout=5)

        late = await self._sent(_Companion, 1112)
        panel.attach_child(late)
        holder["release"].set()
        with pytest.raises(discord.HTTPException):
            await sending

        assert holder["first"].is_finished()
        assert late.is_finished()

    async def test_a_call_from_inside_a_childs_navigation_closes_nothing(self):
        """Refused before the cascade starts, as exit() is; it used to close
        the children ahead of the navigating one first."""

        class _Parent(StatefulView):
            pass

        class _Child(StatefulView):
            pass

        raised = []
        parent = await self._sent(_Parent, 1071)
        ahead = await self._sent(_Child, 1072, parent=parent)
        pushing = await self._sent(_Child, 1073, parent=parent)

        class _Dest(StatefulView):
            async def on_load(self):
                with pytest.raises(RuntimeError, match="exit_children"):
                    await parent.exit_children()
                raised.append(True)

        await pushing.push(_Dest, _make_interaction(message=pushing._message))

        assert raised == [True]
        assert not ahead.is_finished()
        assert ahead in parent._attached_children


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


class TestNavigationState:
    """get_nav_state / restore_nav_state carry a view's selection across pop.

    pop reconstructs the parent from its construction kwargs and re-runs
    on_load, so data comes back fresh but anything selected SINCE
    construction (a page, a tab, a tier) silently reverts to the
    constructor's default. These hooks are what carry it.
    """

    async def test_selection_survives_pop(self):
        class _Root(StatefulView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.severity = "high"  # the default

            def get_nav_state(self):
                return {"severity": self.severity}

            def restore_nav_state(self, state):
                self.severity = state.get("severity", self.severity)

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        root.severity = "low"  # selected after construction; not a kwarg

        child = await root.push(_Sub)
        restored = await child.pop()

        assert restored is not root  # pop reconstructs, it does not restore
        assert restored.severity == "low"

    async def test_restore_runs_before_on_load(self):
        """The ordering is the whole point: a preload that reads the
        selection must see the restored value, not the default. Restoring
        after on_load would force a second fetch to correct it.
        """
        seen = []

        class _Root(StatefulView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.severity = "high"

            def get_nav_state(self):
                return {"severity": self.severity}

            def restore_nav_state(self, state):
                self.severity = state.get("severity", self.severity)

            async def on_load(self):
                seen.append(self.severity)

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        assert seen == ["high"]  # the initial send
        root.severity = "low"

        child = await root.push(_Sub)
        await child.pop()

        # The restored parent's preload read the restored tier, not the default.
        assert seen == ["high", "low"]

    async def test_view_without_override_captures_nothing(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        child = await root.push(_Sub)

        assert child._nav_stack[-1]["view_state"] == {}
        assert await child.pop() is not None

    async def test_raising_get_nav_state_does_not_break_navigation(self):
        """A broken override costs the restore, never the navigation."""

        class _Root(StatefulView):
            def get_nav_state(self):
                raise RuntimeError("boom")

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        child = await root.push(_Sub)
        assert child._nav_stack[-1]["view_state"] == {}
        assert await child.pop() is not None

    async def test_raising_restore_nav_state_does_not_break_navigation(self):
        class _Root(StatefulView):
            def get_nav_state(self):
                return {"severity": "low"}

            def restore_nav_state(self, state):
                raise RuntimeError("boom")

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        child = await root.push(_Sub)
        assert await child.pop() is not None  # the user still gets back

    async def test_non_dict_return_is_discarded(self):
        class _Root(StatefulView):
            def get_nav_state(self):
                return ["not", "a", "dict"]

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        child = await root.push(_Sub)
        assert child._nav_stack[-1]["view_state"] == {}

    async def test_nav_state_is_captured_per_push(self):
        """Each push snapshots the selection as it stands at that moment."""

        class _Root(StatefulView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.page = 0

            def get_nav_state(self):
                return {"page": self.page}

            def restore_nav_state(self, state):
                self.page = state.get("page", self.page)

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        root.page = 3
        child = await root.push(_Sub)
        back = await child.pop()
        assert back.page == 3

        back.page = 7
        child2 = await back.push(_Sub)
        back2 = await child2.pop()
        assert back2.page == 7


class TestNavigationOnLoad:
    """push/pop run on_load on the destination view before the edit, so a
    view re-reads its data source on navigation -- the reload-on-render
    contract that supersedes rebuild=lambda v: v.load_and_build()."""

    async def test_push_runs_on_load_on_new_view(self):
        loads = []

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_load(self):
                loads.append("sub")

            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        await root.push(_Sub)

        assert loads == ["sub"]

    async def test_pop_runs_on_load_on_restored_view(self):
        loads = []

        class _Root(StatefulView):
            async def on_load(self):
                loads.append("root")

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        # on_load fired once on the initial send.
        assert loads == ["root"]

        child = await root.push(_Sub)
        await child.pop()

        # The restored root re-ran on_load before the pop edit.
        assert loads == ["root", "root"]

    async def test_push_runs_on_load_without_rebuild_callback(self):
        # on_load fires even when no rebuild= is passed, so a consumer drops
        # rebuild=lambda v: v.load_and_build() and just defines on_load.
        loads = []

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_load(self):
                loads.append("sub")

            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        await root.push(_Sub)  # no rebuild=

        assert loads == ["sub"]

    async def test_on_load_runs_before_explicit_rebuild(self):
        # When both are present, on_load (data load) runs before the
        # rebuild hook (post-construction setup).
        order = []

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_load(self):
                order.append("on_load")

            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        def _rebuild(v):
            order.append("rebuild")

        await root.push(_Sub, rebuild=_rebuild)

        assert order == ["on_load", "rebuild"]


class TestNavigationFastPath:
    """Push/pop edit + ack in one call via interaction.response.edit_message
    when the response slot is open; defer + edit_original_response otherwise."""

    async def test_push_uses_edit_message_when_slot_open(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        dest = await root.push(_Sub, interaction=nav)

        # One round-trip: edit + ack together, no separate defer.
        nav.response.edit_message.assert_awaited_once()
        nav.response.defer.assert_not_called()
        # The fast-path landing is a real render: a teardown right after
        # must not read it as "never rendered" (see _teardown_edit_target).
        assert dest._has_rendered is True

    async def test_push_falls_back_to_deferred_edit_when_acked(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        # Interaction already acknowledged (e.g. the auto-defer timer fired).
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(
            return_value=MagicMock(id=777, channel=MagicMock(id=666))
        )
        dest = await root.push(_Sub, interaction=nav)

        nav.response.edit_message.assert_not_called()
        nav.edit_original_response.assert_awaited_once()
        assert dest._has_rendered is True

    async def test_push_non_component_interaction_skips_fast_path(self):
        """A non-component interaction (e.g. a slash command) is ineligible for
        the edit_message fast path -- it silently no-ops there in discord.py.
        It carries no message, so its original response need not be the
        view's: the navigation acks it and edits the view's own message."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        message = root._message

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.type = discord.InteractionType.application_command
        nav.edit_original_response = AsyncMock(
            return_value=MagicMock(id=5, channel=MagicMock(id=6))
        )
        sub = await root.push(_Sub, interaction=nav)

        nav.response.edit_message.assert_not_called()
        nav.edit_original_response.assert_not_awaited()
        nav.response.defer.assert_awaited_once()
        assert message.edit.await_args.kwargs["view"] is sub

    async def test_a_push_from_a_followup_panel_edits_its_own_message(self):
        """A second panel the same command sent as a followup was edited
        through the command's original response, which is the first panel."""

        class _First(RenderableLayoutView):
            pass

        class _Second(RenderableLayoutView):
            pass

        class _Next(RenderableLayoutView):
            pass

        command = _make_interaction(user_id=1, guild_id=100)
        command.type = discord.InteractionType.application_command
        first = MagicMock(id=111, channel=MagicMock(id=888))
        first.edit = AsyncMock(return_value=first)
        second = MagicMock(id=222, channel=MagicMock(id=888))
        second.edit = AsyncMock(return_value=second)
        command.original_response = AsyncMock(return_value=first)
        command.followup.send = AsyncMock(return_value=second)
        command.channel.fetch_message = AsyncMock(
            side_effect=lambda message_id: {111: first, 222: second}[message_id]
        )
        await _First(interaction=command).send()
        panel = _Second(interaction=command)
        await panel.send()
        command.edit_original_response = AsyncMock(return_value=first)
        first.edit.reset_mock()
        second.edit.reset_mock()

        moved = await panel.push(_Next)

        command.edit_original_response.assert_not_awaited()
        first.edit.assert_not_awaited()
        assert second.edit.await_args.kwargs["view"] is moved

    async def test_push_fast_path_handles_ack_race(self):
        """The auto-defer timer can ack in the window between the is_done()
        guard and edit_message's own internal guard, so edit_message raises
        InteractionResponded -- a sibling of HTTPException, not a subclass, so
        the HTTP handler would miss it. The fast path must catch it and fall
        through to the deferred edit rather than crash the navigation."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=discord.InteractionResponded(MagicMock()))
        nav.edit_original_response = AsyncMock(
            return_value=MagicMock(id=7, channel=MagicMock(id=8))
        )

        sub = await root.push(_Sub, interaction=nav)

        # Raced ack -> fell through to the deferred edit, navigation completed.
        nav.edit_original_response.assert_awaited_once()
        assert sub is not None

    @staticmethod
    def _consumed_file_harness():
        """A file a first request streams, and a record of what a second one uploads."""
        payload = b"x" * 3000
        uploaded = []

        def attach(view):
            return {"attachments": [discord.File(io.BytesIO(payload), filename="map.png")]}

        def consume(kwargs):
            for f in kwargs.get("attachments", []):
                f.fp.read()

        def upload(kwargs):
            f = kwargs["attachments"][0]
            f.reset(seek=0)  # what discord.py's request() does on its first attempt
            uploaded.append(len(f.fp.read()))

        return payload, uploaded, attach, consume, upload

    async def test_a_failed_fast_path_sends_the_rebuild_file_from_the_start(self):
        payload, uploaded, attach, consume, upload = self._consumed_file_harness()

        class _ServerError(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "500")
                self.status = 500
                self.code = 0

        async def fail(*args, **kwargs):
            consume(kwargs)
            raise _ServerError()

        async def edit_original(**kwargs):
            upload(kwargs)
            return MagicMock(id=7, channel=MagicMock(id=8))

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=fail)
        nav.edit_original_response = AsyncMock(side_effect=edit_original)

        await root.push(_Sub, interaction=nav, rebuild=attach)

        assert uploaded == [len(payload)]

    async def test_the_channel_fallback_sends_the_rebuild_file_from_the_start(self):
        payload, uploaded, attach, consume, upload = self._consumed_file_harness()

        class _Expired(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "401")
                self.status = 401
                self.code = 50027

        async def expire(**kwargs):
            consume(kwargs)
            raise _Expired()

        async def channel_edit(**kwargs):
            upload(kwargs)

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        root._message.edit = AsyncMock(side_effect=channel_edit)
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=expire)

        await root.push(_Sub, interaction=nav, rebuild=attach)

        assert uploaded == [len(payload)]

    async def test_a_rebuild_file_sent_before_goes_whole_on_the_fast_path(self):
        """The hook handed back a file an earlier request had sent, and the
        one-request edit uploaded it from where that read stopped."""
        payload = b"x" * 3000
        chart = discord.File(io.BytesIO(payload), filename="map.png")
        chart.fp.read()  # an earlier request streamed it,
        chart.close()  # and discord.py closed it when that request ended
        uploaded = []

        async def edit_message(**kwargs):
            f = kwargs["attachments"][0]
            f.reset(seek=0)
            uploaded.append(len(f.fp.read()))

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=edit_message)

        await root.push(_Sub, interaction=nav, rebuild=lambda v: {"attachments": [chart]})

        assert uploaded == [len(payload)]

    async def test_clear_on_empty_back_leaves_the_panel_alone_v1(self):
        """A Back press with nowhere to go acks and changes nothing.

        The render seams disable a Back button whose stack is empty, so
        reaching here means the click beat that state onto the wire. Tearing
        the message down would destroy a working panel on a press that asked
        for nothing."""
        view = StatefulView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.edit_original_response = AsyncMock()

        await view._clear_on_empty_back(nav)

        nav.response.defer.assert_awaited_once()
        nav.edit_original_response.assert_not_awaited()

    async def test_clear_on_empty_back_survives_the_ack_race_v1(self):
        """The auto-defer timer can take the slot between the guard and the
        call, so ``defer`` raises ``InteractionResponded`` -- a sibling of
        ``HTTPException``, not a subclass. Already acked is the whole job, so
        it is swallowed rather than escaping to on_error."""
        view = StatefulView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.defer = AsyncMock(side_effect=discord.InteractionResponded(MagicMock()))
        nav.edit_original_response = AsyncMock()

        await view._clear_on_empty_back(nav)  # must not raise

        nav.edit_original_response.assert_not_awaited()

    async def test_clear_on_empty_back_does_not_freeze_v2(self):
        """The V2 path used to freeze every component, which left the panel
        rendered but dead. It now shares the base's ack-only behaviour.

        The view needs a real interactive item before the freeze claim can
        be tested at all: ``RenderableLayoutView`` renders one
        ``TextDisplay``, nothing in that tree carries ``disabled``, and an
        assertion over the empty list passes whether or not anything was
        frozen.
        """
        view = RenderableLayoutView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send()
        view._add_back_button()
        interactive = [c for c in view.walk_children() if hasattr(c, "disabled")]
        assert interactive, "fixture must carry something freezable for this to mean anything"

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.edit_original_response = AsyncMock()

        await view._clear_on_empty_back(nav)

        nav.response.defer.assert_awaited_once()
        nav.edit_original_response.assert_not_awaited()
        assert not any(c.disabled for c in interactive)

    async def test_back_button_disabled_when_stack_is_empty(self):
        """A Back button is only meaningful with somewhere to go back to.

        Synced at the render seams rather than at construction: a pushed view
        is built before ``_navigate_to`` assigns its stack, so a build-time
        check would disable a button that works."""
        view = RenderableLayoutView(interaction=_make_interaction(user_id=1, guild_id=100))
        back = view.make_back_button()
        view.add_item(discord.ui.ActionRow(back))

        view._sync_back_buttons()
        assert back.disabled is True

        view._nav_stack = [{"view_class": "Something"}]
        view._sync_back_buttons()
        assert back.disabled is False


class TestNavigationForeignInteraction:
    """A with_confirmation wrapper runs the navigation callback with the
    confirm-button interaction, whose message is the ephemeral prompt --
    a different message than the view's dashboard. Navigation must edit the
    view's own message via the channel endpoint, not the foreign prompt."""

    async def test_push_with_foreign_interaction_edits_view_message(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        # Pin a concrete dashboard message id for the foreign comparison.
        dashboard = MagicMock(id=555, channel=MagicMock(id=2))
        dashboard.edit = AsyncMock()
        root._message = dashboard

        # Confirm-button click on the ephemeral prompt (different message id),
        # slot already consumed by the prompt edit -- the with_confirmation shape.
        prompt = MagicMock(id=999)
        confirm = _make_interaction(user_id=1, guild_id=100, is_done=True, message=prompt)
        confirm.edit_original_response = AsyncMock()

        sub = await root.push(_Sub, interaction=confirm)

        # The view's own message is edited; the foreign prompt is untouched.
        dashboard.edit.assert_awaited()
        confirm.edit_original_response.assert_not_called()
        confirm.response.edit_message.assert_not_called()
        assert sub._message is dashboard

    async def test_pop_with_foreign_interaction_edits_view_message(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        dashboard = MagicMock(id=555, channel=MagicMock(id=2))
        dashboard.edit = AsyncMock()
        root._message = dashboard

        sub = await root.push(_Sub)

        prompt = MagicMock(id=999)
        confirm = _make_interaction(user_id=1, guild_id=100, is_done=True, message=prompt)
        confirm.edit_original_response = AsyncMock()

        restored = await sub.pop(confirm)

        assert restored is not None
        dashboard.edit.assert_awaited()
        confirm.edit_original_response.assert_not_called()
        confirm.response.edit_message.assert_not_called()


class TestNavigationEditFailureContainment:
    """A failed navigation edit (dead ack, expired token, transient HTTP error)
    must never escape push()/pop() into the callback's error path: the
    navigation completes without raising even when every edit/ack endpoint
    fails."""

    @staticmethod
    def _dead_ack():
        return discord.NotFound(MagicMock(), "")

    @staticmethod
    def _http_error(status=500):
        return discord.HTTPException(MagicMock(status=status), "boom")

    async def test_pop_survives_dead_ack_10062(self):
        """The reported incident: a Back/pop whose ack and edit both 10062.
        The deferred-path ack routes through _safe_defer, which must absorb
        the dead interaction rather than propagate it."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        # The reused message also rejects edits (the channel-endpoint fallback).
        root._message.edit = AsyncMock(side_effect=self._dead_ack())

        sub = await root.push(_Sub)

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=self._dead_ack())
        nav.response.defer = AsyncMock(side_effect=self._dead_ack())
        nav.edit_original_response = AsyncMock(side_effect=self._dead_ack())

        # Must complete without raising despite every endpoint failing.
        restored = await sub.pop(interaction=nav)
        assert restored is not None

    async def test_push_survives_dead_ack_10062(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        root._message.edit = AsyncMock(side_effect=self._dead_ack())

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=self._dead_ack())
        nav.response.defer = AsyncMock(side_effect=self._dead_ack())
        nav.edit_original_response = AsyncMock(side_effect=self._dead_ack())

        sub = await root.push(_Sub, interaction=nav)
        assert sub is not None

    async def test_foreign_msg_edit_failure_is_contained(self):
        """The with_confirmation shape: the foreign-message branch routes through
        refresh(), whose non-429 re-raise must be contained at the nav seam."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        dashboard = MagicMock(id=555, channel=MagicMock(id=2))
        dashboard.edit = AsyncMock(side_effect=self._http_error(500))
        root._message = dashboard

        prompt = MagicMock(id=999)
        confirm = _make_interaction(user_id=1, guild_id=100, is_done=True, message=prompt)
        confirm.edit_original_response = AsyncMock()

        sub = await root.push(_Sub, interaction=confirm)
        assert sub is not None
        dashboard.edit.assert_awaited()  # the view's own message edit was attempted

    async def test_deferred_token_expiry_fallback_is_contained(self):
        """Deferred path: edit_original_response 401s (token expired), the
        channel-endpoint fallback refresh() then 500s -- both contained."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        root._message.edit = AsyncMock(side_effect=self._http_error(500))

        # Already acked -> fast path skipped, deferred path taken.
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=self._http_error(401))

        sub = await root.push(_Sub, interaction=nav)
        assert sub is not None


class TestNavigationEditFailureRecovery:
    """When the destination edit genuinely cannot land (Discord 5xx on every
    endpoint), the deferred source teardown rolls back: the source view stays
    live and re-clickable, and the unshown destination is torn down. A
    successful edit commits the source teardown as before."""

    @staticmethod
    def _http_error(status=503):
        return discord.HTTPException(MagicMock(status=status), "boom")

    def _break_all_edits(self, interaction, source_message):
        interaction.response.edit_message = AsyncMock(side_effect=self._http_error())
        interaction.response.defer = AsyncMock(side_effect=self._http_error())
        interaction.edit_original_response = AsyncMock(side_effect=self._http_error())
        if source_message is not None:
            source_message.edit = AsyncMock(side_effect=self._http_error())

    async def test_push_failed_edit_keeps_source_live(self):
        store = get_store()

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        self._break_all_edits(nav, root._message)

        sub = await root.push(_Sub, interaction=nav)

        # Source stays live + re-clickable (never stopped), subscribed, and
        # registered. The destination never reached the screen -> torn down.
        assert root.is_finished() is False
        assert root.id in store.subscribers
        assert root.id in store._active_views
        assert root.id in store.state["views"]
        assert sub.id not in store._active_views
        assert sub.id not in store.state["views"]

    async def test_rolled_back_destination_cannot_edit_the_source_message(self):
        """push() still returns the destination after a rollback. It carried
        the source's message in, so without unbinding it an exit() on the
        returned view would overwrite the recovered source on screen."""

        class _Root(RenderableLayoutView):
            pass

        class _Sub(RenderableLayoutView):
            pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        source_message = root._message

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        self._break_all_edits(nav, source_message)
        sub = await root.push(_Sub, interaction=nav)
        assert sub._message is None

        source_message.edit = AsyncMock()
        await sub.exit(delete_message=False)

        source_message.edit.assert_not_called()

    async def test_pop_failed_edit_keeps_source_live(self):
        store = get_store()

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        sub = await root.push(_Sub)  # now on the child

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        self._break_all_edits(nav, sub._message)

        restored = await sub.pop(interaction=nav)

        # The child (the pop's source) stays live; the restored parent
        # (the destination) is rolled back.
        assert sub.is_finished() is False
        assert sub.id in store.subscribers
        assert sub.id in store._active_views
        assert sub.id in store.state["views"]
        assert restored.id not in store._active_views

    async def test_push_successful_edit_commits_source_teardown(self):
        store = get_store()

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        sub = await root.push(_Sub, interaction=nav)

        # Happy path: the edit confirmed, so the source teardown commits and
        # the destination is the live view.
        assert root.is_finished() is True
        assert root.id not in store.subscribers
        assert root.id not in store._active_views
        assert sub.id in store._active_views
        assert sub.id in store.state["views"]


class TestNavigationUndoTransfer:
    """Push/pop forward-transfer the undo/redo stacks to the new view's state
    row so the undo timeline stays continuous across navigation. The transfer
    routes through a VIEW_UPDATED dispatch (the reducer), never an in-place
    write to the live state["views"] row."""

    async def test_push_transfers_undo_and_redo_stacks(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        # Seed the root's state row the way UndoMiddleware would after recorded
        # actions, then push and confirm the stacks land on the child's row.
        store = get_store()
        await store.dispatch(
            "VIEW_UPDATED",
            ActionCreators.view_updated(
                root.id,
                undo_stack=[{"application_slots": {}, "shared_data": {}}],
                redo_stack=[{"application_slots": {}, "shared_data": {}}],
            ),
        )

        child = await root.push(_Sub)

        child_state = store.state["views"][child.id]
        assert len(child_state.get("undo_stack", [])) == 1
        assert len(child_state.get("redo_stack", [])) == 1

    async def test_transfer_routes_through_view_updated_dispatch(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        store = get_store()
        await store.dispatch(
            "VIEW_UPDATED",
            ActionCreators.view_updated(
                root.id, undo_stack=[{"application_slots": {}, "shared_data": {}}]
            ),
        )

        child = await root.push(_Sub)

        # The transfer is a VIEW_UPDATED action carrying the stacks, proving it
        # went through the reducer rather than mutating the row in place.
        transfers = [
            a
            for a in store.history
            if a["type"] == "VIEW_UPDATED"
            and a["payload"].get("view_id") == child.id
            and "undo_stack" in a["payload"]
        ]
        assert len(transfers) == 1

    async def test_push_without_undo_history_emits_no_transfer(self):
        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        store = get_store()
        child = await root.push(_Sub)

        # No undo/redo stacks on the root -> the guard skips the transfer
        # dispatch entirely, so the child's row carries no stack keys.
        transfers = [
            a
            for a in store.history
            if a["type"] == "VIEW_UPDATED"
            and a["payload"].get("view_id") == child.id
            and ("undo_stack" in a["payload"] or "redo_stack" in a["payload"])
        ]
        assert transfers == []

    @staticmethod
    async def _undo_store():
        store = get_store()
        undo_mw = UndoMiddleware()
        store._add_middleware(undo_mw)
        await undo_mw.initialize(store)

        async def set_a(action, state):
            app = state["application"]
            data = {**app.get("data", {}), "a": action["payload"]["v"]}
            return {**state, "application": {**app, "data": data}}

        store._register_reducer("SET_A", set_a)
        return store

    @staticmethod
    def _a(store):
        return store.state["application"].get("data", {}).get("a", "absent")

    @staticmethod
    def _nav():
        return _make_interaction(user_id=1, guild_id=100, is_done=False)

    class _Root(StatefulView):
        enable_undo = True

    class _Sub(StatefulView):
        enable_undo = True

        async def on_state_changed(self, state):
            pass

    async def test_a_batch_that_pushes_keeps_its_step_on_the_new_screen(self):
        # The batch's entry was pushed when the batch ended, onto the view it
        # came from, which the push had already destroyed: the first Undo on
        # the new screen skipped the batch's change.
        store = await self._undo_store()
        root = self._Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        await root.dispatch("SET_A", {"v": 1})
        async with root.batch():
            await root.dispatch("SET_A", {"v": 2})
            sub = await root.push(self._Sub, interaction=self._nav())

        await sub.undo()
        assert self._a(store) == 1
        await sub.undo()
        assert self._a(store) == "absent"

    async def test_a_batch_that_pushes_and_pops_back_keeps_its_step(self):
        # Two hand-offs inside the batch: the entry follows both.
        store = await self._undo_store()
        root = self._Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        async with root.batch():
            await root.dispatch("SET_A", {"v": 1})
            sub = await root.push(self._Sub, interaction=self._nav())
            back = await sub.pop(interaction=self._nav())

        await back.undo()
        assert self._a(store) == "absent"

    async def test_a_batch_with_changes_on_both_screens_leaves_one_step(self):
        store = await self._undo_store()
        root = self._Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        async with root.batch():
            await root.dispatch("SET_A", {"v": 1})
            sub = await root.push(self._Sub, interaction=self._nav())
            await sub.dispatch("SET_A", {"v": 2})

        assert len(store.state["views"][sub.id]["undo_stack"]) == 1
        await sub.undo()
        assert self._a(store) == "absent"

    async def test_a_batch_whose_push_is_refused_keeps_its_step_on_the_source(self):
        class _Root(RenderableLayoutView):
            enable_undo = True

        class _Dst(RenderableLayoutView):
            enable_undo = True

        store = await self._undo_store()
        root = await _seam_live(_Root)
        await root.dispatch("SET_A", {"v": 1})
        async with root.batch():
            await root.dispatch("SET_A", {"v": 2})
            dest = await root.push(
                _Dst, interaction=TestNavigationAtItsEdges._refusing_nav(_Dst, root._message)
            )

        assert dest.is_finished() and not root.is_finished()
        await root.undo()
        assert self._a(store) == 1


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

    async def test_push_accepts_from_data_instance(self):
        # The literal composition the instance form exists for: a real
        # from_data() result pushed onto a parent, covering the from_data ->
        # push registration path end to end (not just push(instance) alone).
        from cascadeui import PaginatedLayoutView

        class _Root(RenderableLayoutView):
            def build_ui(self):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        child = await PaginatedLayoutView.from_data(
            ["a", "b", "c"],
            per_page=1,
            formatter=lambda chunk: [discord.ui.TextDisplay(str(chunk))],
        )
        store = get_store()
        assert child.id not in store.state["views"]  # unregistered until push

        pushed = await root.push(child)
        assert pushed.id in store.state["views"]
        assert pushed.id in store._active_views
        assert pushed.session_id == root.session_id

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
        message = root._message
        pushed = await root.push(child)

        assert pushed.shared_data == {"my_data": "hello from parent"}

        assert pushed is child
        # Nav stack on the pushed view records the parent.
        assert len(pushed._nav_stack) == 1
        assert pushed._nav_stack[0]["class_name"] == _class_path(_Root)
        # The message moves to the pushed view; the parent lets go of it.
        assert pushed._message is message
        assert root._message is None

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

    async def test_push_instance_binds_acting_interaction(self):
        """The instance path binds the acting interaction, matching the
        class path's ``interaction=current_interaction`` construction.
        Without the bind, a later navigation from the pushed view falls
        back to a stale (or missing) interaction and degrades to the
        no-edit programmatic path."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        child = _Sub(interaction=_make_interaction(user_id=1, guild_id=100))
        nav = _make_interaction(user_id=1, guild_id=100)
        pushed = await root.push(child, interaction=nav)

        assert pushed.interaction is nav

    async def test_replace_instance_binds_acting_interaction(self):
        class _Origin(StatefulView):
            pass

        class _Destination(StatefulView):
            async def on_state_changed(self, state):
                pass

        origin = _Origin(interaction=_make_interaction(user_id=1, guild_id=100))
        await origin.send()

        dest = _Destination(interaction=_make_interaction(user_id=1, guild_id=100))
        nav = _make_interaction(user_id=1, guild_id=100)
        replaced = await origin.replace(dest, interaction=nav)

        assert replaced.interaction is nav


class TestPatternNavigationState:
    """Every stateful pattern carries its cursor across a pop.

    Each sets its cursor in __init__ and none take it as a constructor
    kwarg, so a reconstruction reverted it: the paginator went back to page
    one, tabs to the first tab, the wizard to step one, and the form handed
    back empty fields. All are reachable with no consumer code.
    """

    async def _child(self):
        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        return _Sub

    async def test_paginated_keeps_the_readers_page(self):
        from cascadeui import PaginatedView

        sub = await self._child()
        view = await PaginatedView.from_data(
            items=[f"row{i}" for i in range(20)],
            per_page=5,
            formatter=lambda chunk: "\n".join(chunk),
            interaction=_make_interaction(user_id=1, guild_id=100),
        )
        await view.send()
        view.current_page = 3

        child = await view.push(sub)
        back = await child.pop()

        assert back.current_page == 3
        # The nav follows the cursor: page 4 of 4 cannot go forward.
        assert back._indicator_btn.label == "Page 4/4"

    async def test_paginated_clamps_a_stale_page_index(self):
        """The list can be shorter than it was at push time."""
        from cascadeui import PaginatedView

        sub = await self._child()
        view = await PaginatedView.from_data(
            items=[f"row{i}" for i in range(20)],
            per_page=5,
            formatter=lambda chunk: "\n".join(chunk),
            interaction=_make_interaction(user_id=1, guild_id=100),
        )
        await view.send()
        view.current_page = 3

        child = await view.push(sub)
        # The rebuilt parent renders a shorter list.
        child._nav_stack[-1]["kwargs"]["pages"] = ["only one page"]
        back = await child.pop()

        assert back.current_page == 0  # clamped, not an IndexError

    async def test_tabs_keep_the_open_tab(self):
        """The buttons must follow the cursor, not just the attribute.

        They are built in __init__ against the first tab, so restoring the
        index alone left the third tab's content under a row still
        highlighting the first. A test asserting only `_active_tab`
        stayed green through it.
        """
        from cascadeui import TabView

        sub = await self._child()

        async def builder():
            return {}

        view = TabView(
            tabs={"Overview": builder, "Stats": builder, "Config": builder},
            interaction=_make_interaction(user_id=1, guild_id=100),
        )
        await view.send()
        view._active_tab = 2

        child = await view.push(sub)
        back = await child.pop()

        assert back._active_tab == 2
        styles = [b.style for b in back._tab_buttons]
        assert styles[2] == back.active_tab_style
        assert styles[0] == back.inactive_tab_style

    async def test_wizard_keeps_the_current_step(self):
        """The nav must follow the cursor, not just the attribute.

        Back and the step indicator are built in __init__ against step one,
        so restoring the index alone left step three's content under a
        disabled Back button reading "Step 1/4".
        """
        from cascadeui import WizardView

        sub = await self._child()

        async def builder():
            return {}

        steps = [{"name": f"s{i}", "builder": builder} for i in range(4)]
        view = WizardView(steps=steps, interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send()
        view._current_step = 2

        child = await view.push(sub)
        back = await child.pop()

        assert back._current_step == 2
        assert back._back_btn.disabled is False  # step 3 of 4 can go back
        assert back._step_indicator.label == "Step 3/4"

    async def test_wizard_snaps_to_a_visible_step(self):
        """A step visible at push time can be hidden by the time the user
        comes back, and landing on a hidden one strands them.
        """
        from cascadeui import WizardView

        sub = await self._child()

        async def builder():
            return {}

        steps = [
            {"name": "s0", "builder": builder},
            {"name": "s1", "builder": builder},
            {"name": "s2", "builder": builder, "condition": lambda v: False},
        ]
        view = WizardView(steps=steps, interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send()
        view._current_step = 2  # hidden by the time it is restored

        child = await view.push(sub)
        back = await child.pop()

        assert back._current_step == 1  # nearest visible

    async def test_form_keeps_entered_values(self):
        from cascadeui import FormView

        sub = await self._child()
        fields = [
            {"id": "email", "label": "Email", "type": "text"},
            {"id": "name", "label": "Name", "type": "text"},
        ]
        view = FormView(
            fields=fields, title="Signup", interaction=_make_interaction(user_id=1, guild_id=100)
        )
        await view.send()
        view.values["email"] = "ada@example.com"

        child = await view.push(sub)
        back = await child.pop()

        assert back.values == {"email": "ada@example.com"}

    async def test_form_keeps_pending_drafts(self):
        """A value the user typed that failed to parse survives a pop too.

        The draft lives in _raw_drafts, not values; get_nav_state carries
        both, so an invalid entry the user has not yet fixed is not silently
        lost when they open a picker and press Back.
        """
        from cascadeui import FormView

        sub = await self._child()
        fields = [{"id": "age", "label": "Age", "type": "integer", "min_value": 13}]
        view = FormView(
            fields=fields, title="Signup", interaction=_make_interaction(user_id=1, guild_id=100)
        )
        await view.send()
        view._raw_drafts["age"] = "5"  # out-of-range, held as a draft rather than stored

        child = await view.push(sub)
        back = await child.pop()

        assert back._raw_drafts == {"age": "5"}

    async def test_form_survives_pop_with_its_fields(self):
        """The form's own constructor kwargs were never captured.

        FormView takes its __init__ from _BaseFormMixin, a plain class that
        never triggers __init_subclass__, and the capture wrapper keyed off
        whether a class declared __init__ itself. So a popped form came back
        with no fields and the default title: pressing Back destroyed it.
        """
        from cascadeui import FormView

        sub = await self._child()
        fields = [{"id": "email", "label": "Email", "type": "text"}]
        view = FormView(
            fields=fields, title="Signup", interaction=_make_interaction(user_id=1, guild_id=100)
        )
        await view.send()

        child = await view.push(sub)
        back = await child.pop()

        assert [f["id"] for f in back.fields] == ["email"]
        assert back.title == "Signup"

    async def test_form_drops_values_for_fields_it_no_longer_declares(self):
        from cascadeui import FormView

        sub = await self._child()
        view = FormView(
            fields=[{"id": "email", "label": "Email", "type": "text"}],
            title="Signup",
            interaction=_make_interaction(user_id=1, guild_id=100),
        )
        await view.send()
        view.values["email"] = "ada@example.com"
        view.values["gone"] = "orphan"  # no field declares this

        child = await view.push(sub)
        back = await child.pop()

        assert back.values == {"email": "ada@example.com"}


class TestNavigationDefaultRebuild:
    """A destination view supplies its own edit kwargs when the caller has none.

    pop() passes no rebuild (the back button is library code and has
    nothing to hand it), so a V1 destination could not put its embed on the
    navigation edit. The components swapped and the message kept the CHILD's
    embed underneath the parent's buttons.

    These assert what the EDIT carried, not what the attribute holds: calling
    the lambda directly would pass whether or not navigation ever consults it.
    """

    def _nav_interaction(self, message_id):
        interaction = _make_interaction(user_id=1, guild_id=100)
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.response.is_done.return_value = False
        interaction.response.edit_message = AsyncMock()
        return interaction

    async def test_v2_destination_ships_components_alone(self):
        """V2 views are their component tree; nothing extra rides the edit."""

        class _Root(StatefulLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(ActionRow(StatefulButton(label="Root", custom_id="r")))

        class _Sub(StatefulLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(ActionRow(StatefulButton(label="Sub", custom_id="s")))

        assert _Root.nav_rebuild is None

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        child = await root.push(_Sub)

        interaction = self._nav_interaction(child._message.id)
        await child.pop(interaction=interaction)

        shipped = interaction.response.edit_message.await_args.kwargs
        assert "embed" not in shipped

    async def test_v1_pop_edit_carries_the_parents_embed(self):
        class _Parent(StatefulView):
            nav_rebuild = staticmethod(lambda v: {"embed": v.build_embed()})

            def build_embed(self):
                return discord.Embed(title="PARENT")

        class _Child(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send(embed=root.build_embed())
        child = await root.push(_Child)

        interaction = self._nav_interaction(child._message.id)
        await child.pop(interaction=interaction)

        shipped = interaction.response.edit_message.await_args.kwargs
        assert "embed" in shipped, "the pop edit shipped no embed"
        assert shipped["embed"].title == "PARENT"

    async def test_explicit_rebuild_wins_over_the_default(self):
        """The caller's rebuild is not overridden by the destination's."""

        class _Parent(StatefulView):
            nav_rebuild = staticmethod(lambda v: {"embed": discord.Embed(title="DEFAULT")})

            async def on_state_changed(self, state):
                pass

        class _Child(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        child = await root.push(_Child)

        interaction = self._nav_interaction(child._message.id)
        await child.pop(
            interaction=interaction,
            rebuild=lambda v: {"embed": discord.Embed(title="EXPLICIT")},
        )

        shipped = interaction.response.edit_message.await_args.kwargs
        assert shipped["embed"].title == "EXPLICIT"

    async def test_a_rebuild_returning_a_future_is_awaited(self):
        """Only a coroutine was awaited, so the edit kwargs a Future resolved
        to (a run_in_executor result, say) were dropped."""

        class _Parent(StatefulView):
            async def on_state_changed(self, state):
                pass

        class _Child(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Parent(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        def rebuild(view):
            future = asyncio.get_running_loop().create_future()
            future.set_result({"embed": discord.Embed(title="FROM FUTURE")})
            return future

        interaction = self._nav_interaction(root._message.id)
        await root.push(_Child, interaction=interaction, rebuild=rebuild)

        shipped = interaction.response.edit_message.await_args.kwargs
        assert shipped["embed"].title == "FROM FUTURE"


class TestCompositeNavigationState:
    """A host carries its composites' state through its own nav-state hooks.

    PaginatedRegion and Collapsible hold their cursor on the instance, and
    the host builds them in __init__, so a pop that reconstructs the host
    builds fresh ones on page one, collapsed. They need no hooks of their
    own: their existing public state API (page/set_page, expanded/expand)
    is what the host names in get_nav_state. The host opts in deliberately,
    because only it knows whether returning should resume or start clean.
    """

    async def test_host_carries_region_page_and_collapsible_state(self):
        from cascadeui.components.patterns.v2 import Collapsible, PaginatedRegion, card

        class _Panel(StatefulLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.pager = PaginatedRegion(per_page=5, key="tasks")
                self.filters = Collapsible(label="Filters", reveal=lambda: card("f"), key="filters")
                self.add_item(card("panel"))

            async def on_load(self):
                # Items arrive AFTER restore_nav_state has run.
                self.pager.items = [f"task{i}" for i in range(20)]

            def get_nav_state(self):
                return {"page": self.pager.page, "open": self.filters.expanded}

            def restore_nav_state(self, state):
                self.pager.set_page(state.get("page", 0))
                if state.get("open"):
                    self.filters.expand()

        class _Sub(StatefulLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(card("sub"))

        root = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        root.pager.set_page(3)
        root.filters.expand()

        child = await root.push(_Sub)
        back = await child.pop()

        assert back.pager.page == 3
        assert back.filters.expanded is True
        assert back.pager.page_items == ["task15", "task16", "task17", "task18", "task19"]

    async def test_host_without_hooks_still_resets_its_composites(self):
        """Opting in is the host's call; nothing happens by default."""
        from cascadeui.components.patterns.v2 import PaginatedRegion, card

        class _Panel(StatefulLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.pager = PaginatedRegion(per_page=5, items=list(range(20)), key="t")
                self.add_item(card("panel"))

        class _Sub(StatefulLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(card("sub"))

        root = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()
        root.pager.set_page(3)

        child = await root.push(_Sub)
        back = await child.pop()

        assert back.pager.page == 0


class TestV2PatternPopRendersContent:
    """A popped V2 pattern renders its content, not just its nav row.

    Tabs and wizards build their body from async builders that cannot run in
    __init__, and both built it only in send(). pop() never calls send(), so
    a view returned to from a drill-down rendered its nav row above nothing
    at all. Restoring the cursor without rebuilding the tree made that worse,
    not better: the buttons claimed step three while the body was empty.

    These assert the rendered CHILDREN. Asserting only the cursor is what let
    the gap through: the index restored correctly the whole time.
    """

    def _nav_interaction(self, message_id=7):
        interaction = _make_interaction(user_id=1, guild_id=100)
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.response.is_done.return_value = False
        interaction.response.edit_message = AsyncMock()
        return interaction

    def _prime(self, view):
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.id = 7
        return view

    async def _child(self):
        from cascadeui.components.patterns.v2 import card

        class _Sub(StatefulLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(card("sub"))

        return _Sub

    async def test_tab_layout_view_pop_rebuilds_the_active_tab(self):
        from cascadeui import TabLayoutView
        from cascadeui.components.patterns.v2 import card

        def _tab(name):
            # Content differs per tab, so restoring the cursor while
            # rendering another tab's body fails here.
            async def body():
                return [card(f"TAB-{name}")]

            return body

        sub = await self._child()
        view = TabLayoutView(
            tabs={name: _tab(name) for name in "ABC"},
            interaction=self._nav_interaction(),
        )
        await view.send()
        self._prime(view)
        view._active_tab = 2
        await view._refresh_tabs()

        child = await view.push(sub, interaction=self._nav_interaction())
        back = await child.pop(interaction=self._nav_interaction())

        assert back._active_tab == 2
        assert "TAB-C" in _tree_text(back)

    async def test_wizard_layout_view_pop_rebuilds_the_current_step(self):
        from cascadeui import WizardLayoutView
        from cascadeui.components.patterns.v2 import card

        def _step(index):
            async def body():
                return [card(f"STEP-{index}")]

            return body

        sub = await self._child()
        steps = [{"name": f"s{i}", "builder": _step(i)} for i in range(3)]
        view = WizardLayoutView(steps=steps, interaction=self._nav_interaction())
        await view.send()
        self._prime(view)
        view._current_step = 2
        await view._refresh_wizard()

        child = await view.push(sub, interaction=self._nav_interaction())
        back = await child.pop(interaction=self._nav_interaction())

        assert back._current_step == 2
        assert "STEP-2" in _tree_text(back)
        # The nav buttons agree with the step beside them.
        assert back._back_btn.disabled is False  # step 3 of 3 can go back

    async def test_leaderboard_pop_keeps_the_readers_page(self):
        """Pages are fetched in on_load, which runs AFTER restore_nav_state.

        Treating an empty page list as "nothing to restore" dropped the index
        outright, so the flagship paginated consumer never benefited.
        """
        from cascadeui import LeaderboardLayoutView

        class _Board(LeaderboardLayoutView):
            leaderboard_per_page = 2

            def get_entries(self):
                return [(i, {"score": i}) for i in range(10)]

        sub = await self._child()
        view = _Board(interaction=self._nav_interaction())
        await view.send()
        self._prime(view)
        assert len(view.pages) == 5
        view.current_page = 2

        child = await view.push(sub, interaction=self._nav_interaction())
        back = await child.pop(interaction=self._nav_interaction())

        assert back.current_page == 2
        # Page 2 holds ranks 5 and 6. Asserting the cursor alone would pass
        # on a view that restored the index and rendered page one.
        rendered = _tree_text(back)
        assert "<@4>" in rendered and "<@5>" in rendered
        assert "<@0>" not in rendered

    async def test_restored_page_past_a_shrunk_list_clamps(self):
        """The held index must never reach an indexing read out of range."""
        from cascadeui import LeaderboardLayoutView

        entries = [(i, {"score": i}) for i in range(10)]

        class _Board(LeaderboardLayoutView):
            leaderboard_per_page = 2

            def get_entries(self):
                return list(entries)

        sub = await self._child()
        view = _Board(interaction=self._nav_interaction())
        await view.send()
        self._prime(view)
        view.current_page = 4

        child = await view.push(sub, interaction=self._nav_interaction())
        entries[:] = [(0, {"score": 0})]  # the board shrinks to one page
        back = await child.pop(interaction=self._nav_interaction())

        assert back.current_page == 0  # clamped, not an IndexError


class TestV1PatternPopCarriesTheEmbed:
    """A popped V1 pattern renders its own content, not the child's.

    V1 content lives in the embed, and the navigation edit only carries one
    if the caller supplies a rebuild. pop() has none to supply: the back
    button is library code. So each V1 pattern names its own via
    `nav_rebuild`, and the edit ships the parent's embed beside the
    parent's buttons instead of leaving the child's content on screen.

    These assert what the EDIT carried. Asserting the cursor is what let the
    gap through: the cursor was right the whole time.
    """

    def _nav_interaction(self, message_id):
        interaction = _make_interaction(user_id=1, guild_id=100)
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.response.is_done.return_value = False
        interaction.response.edit_message = AsyncMock()
        return interaction

    async def _child(self):
        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        return _Sub

    async def _popped_edit_kwargs(self, view):
        sub = await self._child()
        child = await view.push(sub)
        interaction = self._nav_interaction(child._message.id)
        await child.pop(interaction=interaction)
        return interaction.response.edit_message.await_args.kwargs

    async def test_tab_view_pop_ships_the_active_tabs_embed(self):
        from cascadeui import TabView

        def _tab(name):
            # Content differs per tab, so shipping the wrong one fails here
            # rather than passing on an embed every tab happens to share.
            async def body():
                return discord.Embed(title=f"TAB-{name}")

            return body

        view = TabView(
            tabs={name: _tab(name) for name in "ABC"},
            interaction=_make_interaction(user_id=1, guild_id=100),
        )
        await view.send()
        view._active_tab = 2

        shipped = await self._popped_edit_kwargs(view)

        assert shipped["embed"].title == "TAB-C"

    async def test_wizard_view_pop_ships_the_current_steps_embed(self):
        from cascadeui import WizardView

        def _step(index):
            async def body():
                return discord.Embed(title=f"STEP-{index}")

            return body

        steps = [{"name": f"s{i}", "builder": _step(i)} for i in range(4)]
        view = WizardView(steps=steps, interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send()
        view._current_step = 2

        shipped = await self._popped_edit_kwargs(view)

        assert shipped["embed"].title == "STEP-2"

    async def test_form_view_pop_ships_the_form_embed(self):
        from cascadeui import FormView

        view = FormView(
            fields=[{"id": "email", "label": "Email", "type": "text"}],
            title="Signup",
            interaction=_make_interaction(user_id=1, guild_id=100),
        )
        await view.send()
        view.values["email"] = "ada@example.com"

        shipped = await self._popped_edit_kwargs(view)

        assert shipped["embed"].title == "Signup"
        # The typed value is the thing a pop dropped. A title-only assertion
        # passes on an embed rendered from a fresh, empty values dict.
        assert "ada@example.com" in str(shipped["embed"].to_dict())

    async def test_paginated_view_pop_ships_the_current_pages_embed(self):
        from cascadeui import PaginatedView

        view = await PaginatedView.from_data(
            items=[f"row{i}" for i in range(20)],
            per_page=5,
            formatter=lambda chunk: discord.Embed(title="PAGE-" + chunk[0]),
            interaction=_make_interaction(user_id=1, guild_id=100),
        )
        await view.send()
        view.current_page = 3

        shipped = await self._popped_edit_kwargs(view)

        assert shipped["embed"].title == "PAGE-row15"  # page 4's first row

    async def test_a_wizard_step_without_a_builder_ships_no_embed(self):
        """A step with nothing to render contributes nothing to the edit."""
        from cascadeui import WizardView

        view = WizardView(
            steps=[{"name": "s0"}, {"name": "s1"}],
            interaction=_make_interaction(user_id=1, guild_id=100),
        )
        await view.send()

        shipped = await self._popped_edit_kwargs(view)

        assert "embed" not in shipped


class TestNavDepth:
    """``nav_depth`` reports how many views sit beneath this one.

    A screen reachable both by a push and by its own command wants Back
    absent on the root entry rather than present and greyed, and deciding
    that needs the stack the library already holds. Without a public read
    the caller carries its own root flag, which duplicates that state and
    lives on whoever constructs the view -- so a class building its nav row
    in two branches has to remember the flag in both.
    """

    async def test_zero_on_a_directly_opened_view(self):
        view = RenderableLayoutView(interaction=_make_interaction(user_id=1))
        await view.send()

        assert view.nav_depth == 0

    async def test_one_after_a_push(self):
        from cascadeui.state.singleton import get_store

        class _Child(RenderableLayoutView):
            owner_only = False

        root = RenderableLayoutView(interaction=_make_interaction(user_id=1))
        await root.send()
        await root.push(_Child, _make_interaction(user_id=1))

        child = next(v for v in get_store().get_active_views().values() if isinstance(v, _Child))
        assert child.nav_depth == 1

    async def test_readable_inside_on_load(self):
        """``_navigate_to`` assigns the stack before running the destination's
        load hook, which is where a view composes its nav row."""
        from cascadeui.state.singleton import get_store

        seen = []

        class _Child(RenderableLayoutView):
            owner_only = False

            async def on_load(self):
                seen.append(self.nav_depth)

        root = RenderableLayoutView(interaction=_make_interaction(user_id=1))
        await root.send()
        await root.push(_Child, _make_interaction(user_id=1))

        assert seen == [1]

    async def test_back_to_zero_after_a_pop(self):
        class _Child(RenderableLayoutView):
            owner_only = False

        root = RenderableLayoutView(interaction=_make_interaction(user_id=1))
        await root.send()
        await root.push(_Child, _make_interaction(user_id=1))

        assert root.nav_depth == 0


class TestAbortedNavigationNeverPaintsTheDestination:
    """The rollback has to land before the batch announces its prefix.

    An aborted batch announces what it committed, and the flush awards the
    inline notification slot to ``source_id`` -- rebound to the destination.
    A rollback placed outside the ``async with`` therefore lets the
    destination render itself onto the live message and only then be
    unsubscribed and destroyed, leaving the user clicking a view that no
    longer exists.
    """

    async def test_a_raise_inside_the_batch_leaves_the_message_untouched(self):
        class BrokenDestination(RenderableLayoutView):
            # Raises AFTER _register_state has run, so the destination is a
            # live subscriber holding the source's message when the batch
            # unwinds -- the window this ordering exists to close.
            async def _update_message_state(self, *args, **kwargs):
                raise RuntimeError("destination blew up after registration")

        source = RenderableLayoutView(
            interaction=_make_interaction(user_id=7), user_id=7, guild_id=8
        )
        await source.send()
        store = source.state_store
        source._message.edit = AsyncMock()

        with pytest.raises(RuntimeError):
            await source.push(BrokenDestination, _make_interaction(user_id=7))
        await store._flush_notifications()

        source._message.edit.assert_not_called()
        assert source.id in store.subscribers
        assert source.id in store.state["views"]
        assert not source.is_finished()


class TestNavigationConnectTimeoutTakesTheTransportPath:
    """A connect timeout is a transport failure that also answers as a timeout.

    aiohttp's connect and socket timeouts inherit from both ``ClientError``
    and ``asyncio.TimeoutError``, and discord.py builds its session with no
    total timeout, so aiohttp's 30s ``sock_connect`` fires below the 60s
    ``edit_timeout``. Ordering the timeout clause first would route a request
    that never left the host past the branch that re-routes it to the channel
    endpoint.
    """

    async def test_a_connect_timeout_reaches_the_channel_endpoint_fallback(self):
        class _Dest(RenderableLayoutView):
            pass

        source = RenderableLayoutView(
            interaction=_make_interaction(user_id=5), user_id=5, guild_id=6
        )
        await source.send()

        # is_done() True forces the deferred path, where the two clauses sit.
        nav = _make_interaction(user_id=5, guild_id=6, is_done=True)
        nav.edit_original_response = AsyncMock(
            side_effect=aiohttp.ConnectionTimeoutError("connect timed out")
        )
        message = source._message
        message.edit = AsyncMock()

        dest = await source.push(_Dest, interaction=nav)

        message.edit.assert_awaited()
        assert dest is not None
        assert not dest.is_finished(), "the channel fallback landed, so no rollback is owed"

    async def test_a_true_stall_still_rolls_back(self):
        class _Dest(RenderableLayoutView):
            pass

        source = RenderableLayoutView(
            interaction=_make_interaction(user_id=5), user_id=5, guild_id=6
        )
        await source.send()

        nav = _make_interaction(user_id=5, guild_id=6, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=asyncio.TimeoutError())
        shipped = []

        async def channel_edit(**kwargs):
            shipped.append(kwargs.get("view"))
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)

        dest = await source.push(_Dest, interaction=nav)
        await source.task_manager.wait_tasks(source.id)

        assert not source.is_finished(), "a stall rolls back to a live source"
        # The channel fallback is for a request that never left; a stall may
        # have reached Discord, so the destination is never shipped a second
        # time, and the source re-renders over whatever the stall left.
        assert dest not in shipped
        assert shipped == [source]


# // ========================================( Source Across Navigation )======================================== // #


class TestSourceAcrossNavigation:
    """The source of a push or pop keeps its background work until the edit
    lands, then gives its message to the destination.

    A failing push here refuses the destination on every endpoint it tries:
    the deferred edit, then the channel fallback, which carries the
    destination. Channel edits carrying anything else land and are recorded,
    so what the source renders or freezes after a rollback is observable.
    """

    @staticmethod
    def _error():
        return discord.HTTPException(MagicMock(status=503), "boom")

    @staticmethod
    async def _live(cls=RenderableLayoutView):
        view = cls(interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send()
        return view

    def _failing_nav(self, source, destination_cls, gate=None):
        landed = []
        message = source._message

        async def deferred_edit(**kwargs):
            if gate is not None:
                await gate.wait()
            raise self._error()

        async def channel_edit(**kwargs):
            if isinstance(kwargs.get("view"), destination_cls):
                raise self._error()
            landed.append(kwargs.get("view"))
            return message

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        message.edit = AsyncMock(side_effect=channel_edit)
        return nav, landed

    @staticmethod
    def _yielding_nav():
        """A push interaction whose edit lands after a real suspension."""

        async def edit(**kwargs):
            await asyncio.sleep(0)

        nav = _make_interaction(user_id=1, guild_id=100)
        nav.response.edit_message = AsyncMock(side_effect=edit)
        return nav

    async def test_a_failed_push_leaves_the_sources_background_work_running(self):
        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        release = asyncio.Event()
        work = source.create_task(release.wait())
        nav, _ = self._failing_nav(source, _Sub)

        await source.push(_Sub, interaction=nav)
        await asyncio.sleep(0)

        assert not source.is_finished()
        assert not work.done(), "a rolled-back push left the source's work running"
        release.set()
        await work

    async def test_a_landed_push_cancels_the_sources_background_work(self):
        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        work = source.create_task(asyncio.Event().wait())

        await source.push(_Sub, interaction=_make_interaction(user_id=1, guild_id=100))
        await asyncio.sleep(0)

        assert work.cancelled()

    async def test_a_render_during_a_failed_push_lands_after_the_rollback(self):
        from cascadeui import RenderOutcome

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        gate = asyncio.Event()
        nav, landed = self._failing_nav(source, _Sub, gate)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        source.add_item(ActionRow(StatefulButton(label="late", custom_id="late")))
        assert await source.refresh() is RenderOutcome.DEFERRED
        assert landed == [], "nothing edits the message while the push is in flight"
        gate.set()
        await push
        await source.task_manager.wait_tasks(source.id)

        assert landed == [source]

    async def test_a_render_after_a_stalled_push_keeps_its_rules_over_the_redraw(self):
        """A state change made while a push stalled is redrawn by the
        rollback's reclaim, and a render with mention rules landed before the
        redraw did. Numbered as a new render, the redraw came after that
        render, changed the text, and put the view's default rules back,
        though the state change was made first."""
        from cascadeui import RenderOutcome

        users = discord.AllowedMentions(everyone=False, users=True, roles=False)

        class _Counter(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.count = 0
                self.redraw_gate = None

            async def on_state_changed(self, state):
                if self.redraw_gate is not None:
                    await self.redraw_gate.wait()
                self.children[0].content = f"<@5> count={self.count}"
                await self.refresh()

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live(_Counter)
        gate = asyncio.Event()
        shipped = []

        async def stalled(**kwargs):
            await gate.wait()
            raise asyncio.TimeoutError()

        async def recording(**kwargs):
            rules = kwargs.get("allowed_mentions")
            shipped.append((source.children[0].content, rules.to_dict() if rules else None))
            return source._message

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=stalled)
        source._message.edit = AsyncMock(side_effect=recording)
        source.redraw_gate = asyncio.Event()
        try:
            push = asyncio.create_task(source.push(_Sub, interaction=nav))
            await until(lambda: source._away_for_navigation, timeout=2)
            source.count = 1
            await source._render_from_state()
            gate.set()
            await push
            landed = await source.refresh(attachments=[], allowed_mentions=users)
            assert landed is RenderOutcome.RENDERED
            source.redraw_gate.set()
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()
            source.redraw_gate.set()

        assert shipped[-1] == ("<@5> count=1", users.to_dict()), shipped

    @staticmethod
    def _by_context(message):
        context = MagicMock()
        context.author = MagicMock(id=1)
        context.guild = MagicMock(id=100)
        context.send = AsyncMock(return_value=message)
        return context

    @staticmethod
    def _loading_destination(base, loading, gate):
        class _Loading(base):
            async def on_load(self):
                loading.set()
                await gate.wait()

        return _Loading

    async def test_content_asked_of_a_source_during_a_failed_push_lands_after_it(self):
        """A refresh with keywords made while a push was in flight was
        recorded as a state render, so the rollback redrew the source from
        state and the embed the call asked for never reached the message."""
        from cascadeui import RenderOutcome

        loading, gate = asyncio.Event(), asyncio.Event()
        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise self._error()
            embed = kwargs.get("embed")
            shipped.append(embed.title if embed else None)
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = StatefulView(context=self._by_context(message))
        await source.send(embed=discord.Embed(title="S0"))
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            assert await source.refresh(embed=discord.Embed(title="X")) is RenderOutcome.DEFERRED
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        assert not source.is_finished()
        assert shipped[-1:] == ["X"], f"the held embed never reached the message: {shipped}"

    async def test_a_plain_refresh_during_a_failed_push_ships_the_tree_it_was_asked_for(self):
        """A refresh() with no keywords made while a push was in flight was
        replayed through on_state_changed, which rebuilt the tree from state
        over the change the call was made to ship."""
        from cascadeui import RenderOutcome

        loading, gate = asyncio.Event(), asyncio.Event()
        rebuilt = []

        class _Panel(RenderableLayoutView):
            async def on_state_changed(self, state):
                rebuilt.append(state)
                self.children[0].content = "from state"
                await self.refresh()

        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            view = kwargs.get("view")
            if isinstance(view, sub):
                raise self._error()
            shipped.append(view.children[0].content)
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send()
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            source.children[0].content = "asked for"
            assert await source.refresh() is RenderOutcome.DEFERRED
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        assert shipped[-1:] == ["asked for"], shipped
        assert rebuilt == [], "the held refresh() was replayed as a state render"

    async def test_a_call_held_during_a_failed_push_replays_under_its_rules(self):
        """A V2 call with mention rules held while a push was in flight was
        replayed after the push failed without them. A V2 edit's rules ride
        with the view rather than with the held content, and the rollback
        did not put the call's in force, so its attachments went out under
        the view's default."""
        from cascadeui import RenderOutcome

        users = discord.AllowedMentions(everyone=False, users=True, roles=False)
        loading, gate = asyncio.Event(), asyncio.Event()

        class _Panel(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()

        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        rules = []

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise self._error()
            rules.append(kwargs["allowed_mentions"].to_dict())
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send()
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            source.children[0].content = "<@5> changed"
            held = await source.refresh(attachments=[], allowed_mentions=users)
            assert held is RenderOutcome.DEFERRED
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        assert rules == [users.to_dict()], f"the held call replayed under other rules: {rules}"

    async def test_the_later_rules_of_a_late_render_held_during_a_failed_push_win(self):
        """A state change deferred behind a reload was rendered late through
        two refreshes with different mention rules, both held while a push
        from the view was in flight. The two share the late render's number,
        and the second call's rules were ignored as though older, so after the
        push failed the message went out under the first call's."""
        from cascadeui import RenderOutcome

        users = discord.AllowedMentions(everyone=False, users=True, roles=False)
        roles = discord.AllowedMentions(everyone=False, users=False, roles=True)
        loading, gate = asyncio.Event(), asyncio.Event()
        load_gate, in_render, render_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
        outcomes = []

        class _Panel(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()
            gate_load = False
            go = False

            async def on_load(self):
                if self.gate_load:
                    await load_gate.wait()

            async def on_state_changed(self, state):
                if not self.go:
                    return
                self.go = False
                in_render.set()
                await render_gate.wait()
                self.children[0].content = "<@5> x"
                outcomes.append(await self.refresh(allowed_mentions=users))
                self.children[0].content = "<@5> y"
                outcomes.append(await self.refresh(allowed_mentions=roles))

        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        rules = []

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise self._error()
            rules.append(kwargs["allowed_mentions"].to_dict())
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send()
        try:
            source.gate_load = True
            reloading = asyncio.create_task(source.reload())
            await until(lambda: source._reload_task is reloading, timeout=2)
            source.go = True
            await source._render_from_state()
            assert source._deferred_origin, "the state render was not deferred"
            source.gate_load = False
            load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await asyncio.wait_for(in_render.wait(), 2)
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            render_gate.set()
            await until(lambda: len(outcomes) == 2, timeout=2)
            assert outcomes == [RenderOutcome.DEFERRED] * 2, outcomes
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            load_gate.set()
            render_gate.set()
            gate.set()

        assert rules[-1:] == [roles.to_dict()], f"replayed under other rules: {rules}"

    async def test_the_later_rules_of_a_late_render_split_by_a_failed_push_win(self):
        """A state change deferred behind a reload was rendered late through
        two refreshes with different mention rules. The first landed, and a
        push from the view began before the second, which was held. After
        the push failed, the held rules shared the first refresh's number
        and were dropped as though older, so the result went out under the
        first call's rules."""
        from cascadeui import RenderOutcome

        users = discord.AllowedMentions(everyone=False, users=True, roles=False)
        roles = discord.AllowedMentions(everyone=False, users=False, roles=True)
        loading, gate = asyncio.Event(), asyncio.Event()
        load_gate, first_done, render_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
        outcomes = []

        class _Panel(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()
            gate_load = False
            go = False

            async def on_load(self):
                if self.gate_load:
                    await load_gate.wait()

            async def on_state_changed(self, state):
                if not self.go:
                    return
                self.go = False
                self.children[0].content = "<@5> x"
                outcomes.append(await self.refresh(allowed_mentions=users))
                first_done.set()
                await render_gate.wait()
                self.children[0].content = "<@5> y"
                outcomes.append(await self.refresh(allowed_mentions=roles))

        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        rules = []

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise self._error()
            rules.append(kwargs["allowed_mentions"].to_dict())
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send()
        try:
            source.gate_load = True
            reloading = asyncio.create_task(source.reload())
            await until(lambda: source._reload_task is reloading, timeout=2)
            source.go = True
            await source._render_from_state()
            assert source._deferred_origin, "the state render was not deferred"
            source.gate_load = False
            load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await asyncio.wait_for(first_done.wait(), 2)
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            render_gate.set()
            await until(lambda: len(outcomes) == 2, timeout=2)
            assert outcomes == [RenderOutcome.RENDERED, RenderOutcome.DEFERRED], outcomes
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            load_gate.set()
            render_gate.set()
            gate.set()

        assert rules[-1:] == [roles.to_dict()], f"replayed under other rules: {rules}"

    async def test_a_redraw_leaves_a_render_deferred_behind_a_reload_to_its_release(self):
        """A state change was deferred while a reload held the source's turn,
        and then a push stalled. The rollback's redraw took the deferred
        render without making it, so the reload's release found nothing to
        replay and the message kept the state from before the change."""
        load_gate = asyncio.Event()

        class _Texted(RenderableLayoutView):
            text = "t0"
            gate_load = False

            async def on_load(self):
                if self.gate_load:
                    await load_gate.wait()

            async def on_state_changed(self, state):
                self.children[0].content = self.text
                await self.refresh()

        class _Sub(RenderableLayoutView):
            pass

        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            view = kwargs.get("view")
            if isinstance(view, _Sub):
                raise asyncio.TimeoutError()
            shipped.append(view.children[0].content)
            return message

        message.edit = AsyncMock(side_effect=edit)
        # Sent from a context, so the push edits through the channel.
        source = _Texted(context=self._by_context(message))
        await source.send()
        source.gate_load = True
        try:
            reloading = asyncio.create_task(source.reload())
            await until(lambda: source._reload_task is reloading, timeout=2)
            source.text = "s"
            await source._render_from_state()
            await asyncio.wait_for(source.push(_Sub), 5)
            load_gate.set()
            await asyncio.wait_for(reloading, 5)
            await source.task_manager.wait_tasks(source.id)
        finally:
            load_gate.set()

        assert shipped[-1:] == ["s"], f"the deferred state change never rendered: {shipped}"

    async def test_content_asked_of_a_source_during_a_stalled_push_lands_with_the_redraw(self):
        """With a push's outcome unknown, the rollback redraws the source. A
        refresh with keywords made while the push was in flight was not part
        of that redraw, so its embed never reached the message."""
        from cascadeui import RenderOutcome

        loading, gate = asyncio.Event(), asyncio.Event()
        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise asyncio.TimeoutError()
            embed = kwargs.get("embed")
            shipped.append(embed.title if embed else None)
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = StatefulView(context=self._by_context(message))
        await source.send(embed=discord.Embed(title="S0"))
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            assert await source.refresh(embed=discord.Embed(title="X")) is RenderOutcome.DEFERRED
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        assert shipped == ["X"], f"the held embed did not reach the message: {shipped}"

    async def test_content_asked_of_a_source_whose_timer_ran_out_during_a_failed_push_lands(self):
        """The source's own timeout fired while its push was in flight, and
        the push failed. The rollback left the stopped source to that timeout
        and scheduled no replay, so an embed asked of it during the push never
        reached the message."""
        from cascadeui import RenderOutcome

        loading, gate = asyncio.Event(), asyncio.Event()
        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise self._error()
            embed = kwargs.get("embed")
            shipped.append(embed.title if embed else None)
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = StatefulView(context=self._by_context(message))
        await source.send(embed=discord.Embed(title="S0"))
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            assert await source.refresh(embed=discord.Embed(title="X")) is RenderOutcome.DEFERRED
            source._dispatch_timeout()
            gate.set()
            await push
            await until(source._torn_down, timeout=5)
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        assert source.is_finished()
        assert [t for t in shipped if t][-1:] == ["X"], f"the held embed never shipped: {shipped}"

    async def test_content_asked_of_a_source_that_exits_during_a_failed_push_lands(self):
        """An exit() was called while the source's push was in flight, and
        the push failed. The exit waited out the push and then cancelled the
        rollback's replay, so an embed asked of the source during the push
        never reached the message the exit froze."""
        from cascadeui import RenderOutcome

        loading, gate = asyncio.Event(), asyncio.Event()
        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise self._error()
            embed = kwargs.get("embed")
            shipped.append(embed.title if embed else None)
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = StatefulView(context=self._by_context(message))
        await source.send(embed=discord.Embed(title="S0"))
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            assert await source.refresh(embed=discord.Embed(title="X")) is RenderOutcome.DEFERRED
            closing = asyncio.create_task(source.exit())
            for _ in range(10):
                await asyncio.sleep(0)
            assert not closing.done(), "the exit did not wait out the push"
            gate.set()
            await push
            assert await asyncio.wait_for(closing, 5) is True
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        assert source.is_finished()
        assert [t for t in shipped if t][-1:] == ["X"], f"the held embed never shipped: {shipped}"

    async def test_a_redraw_after_a_quiet_missed_render_puts_the_source_back(self):
        """A push stalled, so the rollback redrew the source, which had missed
        a state change while away. Its on_state_changed shipped nothing for
        that change, and the redraw took the replayed render for its own and
        returned, leaving the discarded view on the message."""
        loading, gate = asyncio.Event(), asyncio.Event()

        class _Quiet(RenderableLayoutView):
            async def on_state_changed(self, state):
                # Nothing this panel shows changed.
                return

        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            view = kwargs.get("view")
            shipped.append(type(view).__name__)
            if isinstance(view, sub):
                raise asyncio.TimeoutError()
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Quiet(context=self._by_context(message))
        await source.send()
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            await source._render_from_state()
            assert source._missed_while_away
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        assert not source.is_finished()
        assert shipped[-1:] == ["_Quiet"], f"the discarded view stayed on the message: {shipped}"

    async def test_a_close_during_the_redraw_runs_the_rebuild_hook_once(self):
        """A push stalled, and an exit() began while the rollback's redraw
        replayed a render the source missed. The close's freeze carries what
        nav_rebuild names; the redraw, left running by the close, ran the hook
        again, or had already taken the request and left the freeze without
        the content, or shipped an embed asked for before the redraw over it."""
        from cascadeui import RenderOutcome

        loading, gate = asyncio.Event(), asyncio.Event()
        rendering, render_gate = asyncio.Event(), asyncio.Event()
        rebuilds = []

        class _Panel(StatefulView):
            nav_rebuild = staticmethod(
                lambda view: rebuilds.append(view) or {"embed": discord.Embed(title="restored")}
            )
            hold_render = False

            async def on_state_changed(self, state):
                if self.hold_render:
                    rendering.set()
                    await render_gate.wait()

        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            embed = kwargs.get("embed")
            shipped.append(embed.title if embed else None)
            if isinstance(kwargs.get("view"), sub):
                raise asyncio.TimeoutError()
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send(embed=discord.Embed(title="S0"))
        try:
            push = asyncio.create_task(
                source.push(sub, rebuild=lambda v: {"embed": discord.Embed(title="dest")})
            )
            await asyncio.wait_for(loading.wait(), 2)
            source.hold_render = True
            await source._render_from_state()
            assert source._missed_while_away
            assert await source.refresh(embed=discord.Embed(title="X")) is RenderOutcome.DEFERRED
            gate.set()
            await push
            await asyncio.wait_for(rendering.wait(), 2)
            assert await asyncio.wait_for(source.exit(delete_message=False), 5) is True
            render_gate.set()
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()
            render_gate.set()

        assert source.is_finished()
        assert len(rebuilds) == 1, f"nav_rebuild ran {len(rebuilds)} times"
        assert [t for t in shipped if t][-1:] == ["restored"], shipped

    async def test_content_asked_during_a_stalled_push_survives_a_close_during_the_redraw(self):
        """A push stalled, the rollback's redraw replayed a render the source
        missed, and an exit() began while that render ran. The close cancelled
        the redraw, which had already taken the embed asked of the source
        during the push, so the embed never reached the frozen message."""
        from cascadeui import RenderOutcome

        loading, gate = asyncio.Event(), asyncio.Event()
        rendering, render_gate = asyncio.Event(), asyncio.Event()

        class _Panel(StatefulView):
            hold_render = False

            async def on_state_changed(self, state):
                if self.hold_render:
                    rendering.set()
                    await render_gate.wait()

        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            embed = kwargs.get("embed")
            shipped.append(embed.title if embed else None)
            if isinstance(kwargs.get("view"), sub):
                raise asyncio.TimeoutError()
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send(embed=discord.Embed(title="S0"))
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            source.hold_render = True
            await source._render_from_state()
            assert source._missed_while_away
            assert await source.refresh(embed=discord.Embed(title="X")) is RenderOutcome.DEFERRED
            gate.set()
            await push
            await asyncio.wait_for(rendering.wait(), 2)
            assert await asyncio.wait_for(source.exit(delete_message=False), 5) is True
            render_gate.set()
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()
            render_gate.set()

        assert source.is_finished()
        assert [t for t in shipped if t][-1:] == ["X"], f"the held embed never shipped: {shipped}"

    @pytest.mark.parametrize("how", ["cut", "stop"])
    async def test_a_redraw_left_to_a_close_that_does_not_happen_still_runs(self, how):
        """A push stalled, so the source was owed a redraw, and the source
        began closing, or user code stopped it, before that redraw ran. The
        redraw left nav_rebuild to the close; the close was cut off, or there
        was none, so the discarded view stayed on the message."""
        loading, gate = asyncio.Event(), asyncio.Event()
        in_child_delete, child_gate = asyncio.Event(), asyncio.Event()
        rebuilds = []

        class _Panel(StatefulView):
            nav_rebuild = staticmethod(
                lambda view: rebuilds.append(view) or {"embed": discord.Embed(title="restored")}
            )

        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            embed = kwargs.get("embed")
            shipped.append((type(kwargs.get("view")).__name__, embed.title if embed else None))
            if isinstance(kwargs.get("view"), sub):
                raise asyncio.TimeoutError()
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send(embed=discord.Embed(title="S0"))
        child_message = MagicMock(id=1500, channel=MagicMock(id=888))

        async def child_delete(*args, **kwargs):
            in_child_delete.set()
            await child_gate.wait()

        child_message.delete = AsyncMock(side_effect=child_delete)
        child = StatefulView(context=self._by_context(child_message), parent=source)
        await child.send()
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            if how == "cut":
                closing = asyncio.create_task(source.exit())
            else:

                async def stop_once_the_push_settles():
                    await source._wait_out_navigation("test")
                    source.stop()

                closing = asyncio.create_task(stop_once_the_push_settles())
            for _ in range(5):
                await asyncio.sleep(0)
            gate.set()
            await push
            if how == "cut":
                await asyncio.wait_for(in_child_delete.wait(), 2)
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing
            else:
                await asyncio.wait_for(closing, 2)
            child_gate.set()
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()
            child_gate.set()

        assert len(rebuilds) == 1, f"nav_rebuild ran {len(rebuilds)} times"
        assert shipped[-1] == ("_Panel", "restored"), shipped

    async def test_a_v2_redraw_ships_the_tree_its_rebuild_hook_builds(self):
        """A push stalled, the source had missed a state change while away,
        and its replayed state render shipped a tree. The redraw then ran
        nav_rebuild, which built a different tree, and skipped its own
        refresh because a tree had already shipped since the rollback."""
        loading, gate = asyncio.Event(), asyncio.Event()

        class _Panel(RenderableLayoutView):
            nav_rebuild = staticmethod(lambda view: setattr(view.children[0], "content", "nr"))

            async def on_state_changed(self, state):
                self.children[0].content = "st"
                await self.refresh()

        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            view = kwargs.get("view")
            if isinstance(view, sub):
                shipped.append("dest")
                raise asyncio.TimeoutError()
            shipped.append(view.children[0].content)
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send()
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            await source._render_from_state()
            assert source._missed_while_away
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        assert not source.is_finished()
        assert shipped[-1:] == ["nr"], f"the rebuilt tree never shipped: {shipped}"

    async def test_a_redraw_a_closed_bot_cannot_send_is_not_an_error(self, caplog):
        """A push stalled, and the bot closed before the source's redraw ran.
        The redraw's edit failed on the closed session and was logged as an
        error with a traceback, which no other edit the library makes after
        its bot closed is."""
        loading, gate = asyncio.Event(), asyncio.Event()
        closed, tried = [], []

        class _Panel(RenderableLayoutView):
            pass

        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                # The bot closes while the push's edit stalls.
                closed.append(True)
                raise asyncio.TimeoutError()
            if closed:
                tried.append(True)
                raise RuntimeError("Session is closed")
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send()
        source._client_closed = lambda: bool(closed)
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        assert tried, "the redraw never reached the closed session"
        errors = [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui") and r.levelno >= logging.ERROR
        ]
        assert not errors, errors

    async def test_a_rebuild_hook_that_raises_after_the_bot_closed_is_still_an_error(self, caplog):
        """A push stalled, the bot closed, and the source's redraw ran a
        nav_rebuild that raised an AttributeError of its own. Because the bot
        had closed, the redraw logged that error at DEBUG without a
        traceback, as though it were the closed session refusing an edit."""
        loading, gate = asyncio.Event(), asyncio.Event()
        closed = []

        def nav_rebuild(view):
            raise AttributeError("'NoneType' object has no attribute 'label'")

        class _Panel(RenderableLayoutView):
            pass

        _Panel.nav_rebuild = staticmethod(nav_rebuild)
        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                closed.append(True)
                raise asyncio.TimeoutError()
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send()
        source._client_closed = lambda: bool(closed)
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            gate.set()
            await push
            await source.task_manager.wait_tasks(source.id)
        finally:
            gate.set()

        errors = [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui") and r.levelno >= logging.ERROR
        ]
        assert any("'label'" in m for m in errors), errors

    async def test_a_source_stopped_during_a_failed_push_closes(self):
        """User code stopped the source while a push from it was in flight,
        and the push failed. Nothing closed the source, which discord.py no
        longer routes clicks to, so the message kept the discarded
        destination's controls, answering nothing."""
        loading, gate = asyncio.Event(), asyncio.Event()
        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))
        shipped = []

        async def edit(**kwargs):
            view = kwargs.get("view")
            if isinstance(view, sub):
                raise asyncio.TimeoutError()
            shipped.append(view)
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = RenderableLayoutView(context=self._by_context(message))
        source.add_item(ActionRow(StatefulButton(label="go", custom_id="src:go")))
        await source.send()
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            source.stop()
            gate.set()
            await push
            try:
                await until(source._torn_down, timeout=2)
            except asyncio.TimeoutError:
                pass
        finally:
            gate.set()

        assert source._torn_down(), "the stopped source was never closed"
        assert shipped and shipped[-1] is source, f"the message was not redrawn: {shipped}"

    async def test_an_on_timeout_that_keeps_the_source_is_honoured_after_a_failed_push(self):
        """The source's timer ran out while its push was in flight, and the
        push failed. A source its own timer stopped is the timeout's to close,
        so an on_timeout() override that keeps the view keeps it."""
        from cascadeui.views.base import _CLOSE_RESUME_TASK_OWNER

        loading, gate = asyncio.Event(), asyncio.Event()
        kept = []

        class _Keeps(StatefulView):
            async def on_timeout(self):
                kept.append(True)

        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise self._error()
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Keeps(context=self._by_context(message))
        await source.send(embed=discord.Embed(title="S0"))
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            source._dispatch_timeout()
            gate.set()
            await push
            await until(lambda: kept, timeout=2)
            await asyncio.wait_for(source.task_manager.wait_tasks(_CLOSE_RESUME_TASK_OWNER), 2)
        finally:
            gate.set()

        assert not source._torn_down(), "the rollback closed a view its on_timeout() kept"

    @pytest.mark.parametrize("freeze_fails", [False, True], ids=["landed", "failed"])
    async def test_a_state_render_finishing_after_a_freeze_keeps_the_redraw_it_carried(
        self, freeze_fails
    ):
        """A push stalled, so the source owed a redraw, and a state change
        reached it while it was away. An exit landed while the redraw's state
        render waited, and its freeze carried nav_rebuild's tree. The state
        render, asked for before that redraw, then shipped its tree over it.
        When the freeze edit fails, the redraw never reached the message, and
        the state render still ships."""
        loading, gate = asyncio.Event(), asyncio.Event()
        in_state, state_gate = asyncio.Event(), asyncio.Event()
        shown = []

        class _Source(RenderableLayoutView):
            state_text = None
            gate_state = False

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._build("src")

            def _build(self, text):
                self.clear_items()
                self.text = discord.ui.TextDisplay(text)
                self.add_item(self.text)
                self.add_item(ActionRow(StatefulButton(label="go", custom_id="src:go")))

            async def on_state_changed(self, state):
                if self.state_text is None:
                    return
                if self.gate_state:
                    in_state.set()
                    await state_gate.wait()
                self.text.content = self.state_text
                await self.refresh()

            @staticmethod
            def nav_rebuild(view):
                view._build("nr")

        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))

        async def edit(**kwargs):
            view = kwargs.get("view")
            if isinstance(view, sub):
                raise asyncio.TimeoutError()
            if view is not None:
                live = [
                    item
                    for item in view.walk_children()
                    if isinstance(item, discord.ui.Button) and not item.disabled
                ]
                if freeze_fails and not live and view.text.content == "nr" and not failed:
                    failed.append(True)
                    raise self._error()
                shown.append((view.text.content, bool(live)))
            return message

        failed = []
        message.edit = AsyncMock(side_effect=edit)
        source = _Source(context=self._by_context(message))
        await source.send()
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            source.state_text = "st"
            await source._render_from_state()
            source.gate_state = True
            gate.set()
            await asyncio.wait_for(in_state.wait(), 2)
            await source.exit()
            state_gate.set()
            await push
            await asyncio.wait_for(source.task_manager.wait_tasks(source.id), 2)
        finally:
            gate.set()
            state_gate.set()

        expected = ("st", False) if freeze_fails else ("nr", False)
        assert failed == ([True] if freeze_fails else []), "the freeze edit was not the one failed"
        assert shown and shown[-1] == expected, f"the message ends wrong: {shown}"

    @pytest.mark.parametrize("close", ["exit", "timeout"])
    async def test_a_redraw_with_its_own_mention_rules_reaches_the_frozen_message(self, close):
        """A push stalled while the source was closing, so the close carried
        the source's redraw, and nav_rebuild named mention rules other than
        the view's own. The freeze passed allowed_mentions twice and raised,
        leaving the discarded screen's buttons on the message."""
        loading, gate = asyncio.Event(), asyncio.Event()
        users = discord.AllowedMentions(everyone=False, users=True, roles=False)
        shown = []

        class _Source(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._build("src")

            def _build(self, text):
                self.clear_items()
                self.text = discord.ui.TextDisplay(text)
                self.add_item(self.text)
                self.add_item(ActionRow(StatefulButton(label="go", custom_id="src:go")))

            @staticmethod
            def nav_rebuild(view):
                view._build("nr")
                return {"allowed_mentions": users}

        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))

        async def edit(**kwargs):
            view = kwargs.get("view")
            if isinstance(view, sub):
                raise asyncio.TimeoutError()
            if view is not None:
                live = [
                    item
                    for item in view.walk_children()
                    if isinstance(item, discord.ui.Button) and not item.disabled
                ]
                text = getattr(getattr(view, "text", None), "content", None)
                shown.append((text, bool(live), kwargs.get("allowed_mentions")))
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Source(context=self._by_context(message))
        await source.send()
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            closing = asyncio.create_task(source.exit() if close == "exit" else source.on_timeout())
            for _ in range(5):
                await asyncio.sleep(0)
            gate.set()
            await push
            await asyncio.wait_for(closing, 5)
            await asyncio.wait_for(source.task_manager.wait_tasks(source.id), 2)
        finally:
            gate.set()

        assert shown, "the close edited nothing: its freeze raised"
        text, live, rules = shown[-1]
        assert (text, live) == ("nr", False), shown
        assert rules is not None and rules.to_dict() == users.to_dict(), shown
        # A later edit rebuilds mentions under the rules recorded for the
        # message, which must be the ones the freeze shipped.
        assert source._mention_rules[0] is users, source._mention_rules

    async def test_a_late_render_ships_after_a_redraw_that_changed_only_content(self):
        """A V1 source owed a redraw whose nav_rebuild changed only the embed,
        and a state change reached it while away. A timeout landed while the
        redraw's state render waited, carrying the embed. The state render,
        which relabelled a button, was skipped as older than the redraw,
        though the redraw had left the components as they were."""
        loading, gate = asyncio.Event(), asyncio.Event()
        in_state, state_gate = asyncio.Event(), asyncio.Event()
        shown = []

        class _Source(StatefulView):
            state_text = None
            gate_state = False

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.button = StatefulButton(label="go", custom_id="src:go")
                self.add_item(self.button)

            async def on_state_changed(self, state):
                if self.state_text is None:
                    return
                if self.gate_state:
                    in_state.set()
                    await state_gate.wait()
                self.button.label = self.state_text
                await self.refresh()

            @staticmethod
            def nav_rebuild(view):
                return {"embed": discord.Embed(title="NR")}

        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))

        async def edit(**kwargs):
            view = kwargs.get("view")
            if isinstance(view, sub):
                raise asyncio.TimeoutError()
            embed = kwargs.get("embed")
            labels = [i.label for i in view.children] if view is not None else None
            shown.append((labels, embed.title if embed is not None else None))
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Source(context=self._by_context(message))
        await source.send(embed=discord.Embed(title="S0"))
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            source.state_text = "st"
            await source._render_from_state()
            source.gate_state = True
            gate.set()
            await asyncio.wait_for(in_state.wait(), 2)
            await source.on_timeout()
            state_gate.set()
            await push
            await asyncio.wait_for(source.task_manager.wait_tasks(source.id), 2)
        finally:
            gate.set()
            state_gate.set()

        assert "NR" in [e for _, e in shown], f"the redraw never shipped: {shown}"
        assert shown[-1][0] == ["st"], f"the late relabel was dropped: {shown}"

    async def test_an_exit_cut_off_in_its_freeze_still_redraws_the_source(self):
        """A push stalled, and an exit begun while it was in flight took the
        source's redraw into its own edit. The exit was cancelled during that
        edit, and the resumed close stripped the controls without the
        redraw's embed, so the message kept the discarded view's."""
        loading, gate = asyncio.Event(), asyncio.Event()
        in_edit, edit_gate = asyncio.Event(), asyncio.Event()
        strips = []

        class _Source(StatefulView):
            nav_rebuild = staticmethod(lambda view: {"embed": discord.Embed(title="NR")})

        sub = self._loading_destination(StatefulView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise asyncio.TimeoutError()
            if "view" in kwargs and kwargs["view"] is None:
                embed = kwargs.get("embed")
                strips.append(embed.title if embed else None)
                if len(strips) == 1:
                    in_edit.set()
                    await edit_gate.wait()
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Source(context=self._by_context(message))
        source.add_item(StatefulButton(label="go", custom_id="src:go"))
        await source.send(embed=discord.Embed(title="S0"))
        try:
            push = asyncio.create_task(
                source.push(sub, rebuild=lambda v: {"embed": discord.Embed(title="D")})
            )
            await asyncio.wait_for(loading.wait(), 2)
            closing = asyncio.create_task(source.exit())
            gate.set()
            await push
            await asyncio.wait_for(in_edit.wait(), 2)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            edit_gate.set()
            try:
                await until(lambda: len(strips) >= 2, timeout=2)
            except asyncio.TimeoutError:
                pass
        finally:
            gate.set()
            edit_gate.set()

        assert strips[-1:] == ["NR"], f"the resumed close dropped the redraw: {strips}"

    async def test_a_rebuild_hook_that_raises_once_its_view_closed_is_not_an_error(self, caplog):
        """A push stalled, and the source's redraw was awaiting its
        nav_rebuild when an exit closed the view. The hook then failed on
        what the close had cleared, and that was logged as an error,
        although the view it would have redrawn was gone."""
        loading, gate = asyncio.Event(), asyncio.Event()
        in_hook, hook_gate = asyncio.Event(), asyncio.Event()
        raised = []

        async def nav_rebuild(view):
            in_hook.set()
            await hook_gate.wait()
            if view._torn_down():
                raised.append(True)
                raise AttributeError("'NoneType' object has no attribute 'label'")

        class _Panel(RenderableLayoutView):
            pass

        _Panel.nav_rebuild = staticmethod(nav_rebuild)
        sub = self._loading_destination(RenderableLayoutView, loading, gate)
        message = MagicMock(id=1000, channel=MagicMock(id=888))

        async def edit(**kwargs):
            if isinstance(kwargs.get("view"), sub):
                raise asyncio.TimeoutError()
            return message

        message.edit = AsyncMock(side_effect=edit)
        source = _Panel(context=self._by_context(message))
        await source.send()
        try:
            push = asyncio.create_task(source.push(sub))
            await asyncio.wait_for(loading.wait(), 2)
            gate.set()
            await push
            await asyncio.wait_for(in_hook.wait(), 2)
            await source.exit()
            hook_gate.set()
            try:
                await until(lambda: raised, timeout=2)
            except asyncio.TimeoutError:
                pass
        finally:
            gate.set()
            hook_gate.set()
        for _ in range(20):
            await asyncio.sleep(0)

        assert raised, "the hook never ran on the closed view"
        errors = [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui") and r.levelno >= logging.ERROR
        ]
        assert not errors, errors

    async def test_a_throttled_render_that_comes_due_during_a_failed_push_lands_after(self):
        from cascadeui import RenderOutcome

        class _Throttled(RenderableLayoutView):
            refresh_cooldown_ms = 50

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live(_Throttled)
        source._message.edit = AsyncMock()
        source.add_item(ActionRow(StatefulButton(label="one", custom_id="one")))
        assert await source.refresh() is RenderOutcome.RENDERED
        source.add_item(ActionRow(StatefulButton(label="two", custom_id="two")))
        assert await source.refresh() is RenderOutcome.DEFERRED

        gate = asyncio.Event()
        nav, landed = self._failing_nav(source, _Sub, gate)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        await asyncio.sleep(0.1)  # the deferred render comes due while away
        assert landed == []
        gate.set()
        await push
        await source.task_manager.wait_tasks(source.id)

        assert landed == [source]

    async def test_a_push_from_a_task_the_source_owns_lands_and_the_task_runs_on(self):
        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        after = []

        async def move():
            child = await source.push(_Sub, interaction=self._yielding_nav())
            await asyncio.sleep(0)
            after.append(child)

        await source.create_task(move())

        [child] = after
        assert source.is_finished()
        assert not child.is_finished()
        assert child.id in get_store().subscribers

    async def test_a_replace_from_a_task_the_source_owns_can_send_the_destination(self):
        class _Next(RenderableLayoutView):
            pass

        source = await self._live()
        sent = []

        async def switch():
            new = await source.replace(_Next, interaction=_make_interaction(user_id=1))
            await new.send()
            await asyncio.sleep(0)
            sent.append(new)

        await source.create_task(switch())

        [new] = sent
        assert new._message is not None
        assert not new.is_finished()

    async def test_a_failed_send_after_replace_leaves_the_task_handling_it_running(self):
        """replace() hands its task to the destination, so the destination's
        failed send cancelled the task that was handling the failure."""

        class _Next(RenderableLayoutView):
            pass

        source = await self._live()
        handled = []

        async def switch():
            interaction = _make_interaction(user_id=1)
            interaction.response.send_message = AsyncMock(side_effect=self._error())
            new = await source.replace(_Next, interaction=interaction)
            try:
                await new.send()
            except discord.HTTPException:
                await asyncio.sleep(0)
                handled.append(new)

        await source.create_task(switch())

        [new] = handled
        assert new.id not in get_store()._active_views

    async def test_a_view_that_pushed_away_no_longer_edits_the_message(self):
        """It painted its own stopped tree over the destination."""
        from cascadeui import RenderOutcome

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        message = source._message
        child = await source.push(_Sub, interaction=_make_interaction(user_id=1, guild_id=100))
        message.edit = AsyncMock()

        source.add_item(ActionRow(StatefulButton(label="late", custom_id="late")))
        assert await source.refresh() is RenderOutcome.NO_MESSAGE
        await source.exit(delete_message=False)

        message.edit.assert_not_called()
        assert source.message is None
        assert child.message is message

    async def test_a_v1_view_that_pushed_away_leaves_the_destinations_buttons(self):
        """A V1 exit strips components with edit(view=None)."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        source = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        message = source._message
        await source.push(_Sub, interaction=_make_interaction(user_id=1, guild_id=100))
        message.edit = AsyncMock()

        await source.exit(delete_message=False)

        message.edit.assert_not_called()

    @pytest.mark.parametrize("method", ["push", "pop"])
    async def test_navigating_from_a_view_that_pushed_away_raises(self, method):
        """A second push shipped over the first destination, which stayed live
        and subscribed with the same message."""

        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live()
        child = await source.push(_Sub, interaction=_make_interaction(user_id=1, guild_id=100))

        call = getattr(source, method)
        args = (_Sub,) if method == "push" else ()
        with pytest.raises(
            RuntimeError, match=r"already handed its panel on \(a push\(\) or pop\(\)"
        ):
            await call(*args, interaction=_make_interaction(user_id=1, guild_id=100))

        assert not child.is_finished()
        assert child.id in store.subscribers

    async def test_a_back_button_that_overflows_the_tree_rolls_the_push_back(self):
        """The back button is added after _navigate_to returns; its raise left
        the source unsubscribed and the destination registered."""
        from discord.ui import TextDisplay

        class _Full(RenderableLayoutView):
            auto_back_button = True

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                for i in range(38):
                    self.add_item(TextDisplay(f"row {i}"))

        store = get_store()
        source = await self._live()

        with pytest.raises(ValueError, match="40-component limit"):
            await source.push(_Full, interaction=_make_interaction(user_id=1, guild_id=100))

        assert source.id in store.subscribers
        assert not source._away_for_navigation
        assert not any(isinstance(v, _Full) for v in store._active_views.values())

    async def test_a_push_cancelled_during_its_edit_rolls_back(self):
        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live()

        async def hang(**kwargs):
            await asyncio.Event().wait()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=hang)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        source._message.edit = AsyncMock(return_value=source._message)
        push.cancel()
        with pytest.raises(asyncio.CancelledError):
            await push
        await source.task_manager.wait_tasks(source.id)

        assert source.id in store.subscribers
        assert not source._away_for_navigation
        assert not any(isinstance(v, _Sub) for v in store._active_views.values())
        # The cancelled edit may have reached Discord, so the source redraws.
        assert source._message.edit.await_args.kwargs["view"] is source

    async def test_an_exit_during_a_failed_push_waits_and_then_freezes(self):
        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        source.add_item(ActionRow(StatefulButton(label="go", custom_id="go")))
        gate = asyncio.Event()
        nav, landed = self._failing_nav(source, _Sub, gate)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        closing = asyncio.create_task(source.exit(delete_message=False))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not closing.done(), "the exit waits for the push in flight"
        gate.set()
        await push
        await closing

        assert source.is_finished()
        assert landed == [source]

    async def test_an_exit_during_a_landed_push_leaves_the_destinations_message(self):
        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        message = source._message
        message.edit = AsyncMock()
        gate = asyncio.Event()

        async def deferred_edit(**kwargs):
            await gate.wait()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        closing = asyncio.create_task(source.exit(delete_message=False))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not closing.done()
        gate.set()
        child = await push
        await closing

        message.edit.assert_not_called()
        assert not child.is_finished()
        assert child.message is message

    async def test_a_timeout_during_a_landed_push_leaves_the_destinations_message(self):
        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        source.add_item(ActionRow(StatefulButton(label="go", custom_id="go")))
        message = source._message
        message.edit = AsyncMock()
        gate = asyncio.Event()

        async def deferred_edit(**kwargs):
            await gate.wait()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        expiring = asyncio.create_task(source.on_timeout())
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        child = await push
        await expiring

        message.edit.assert_not_called()
        assert not child.is_finished()

    @pytest.mark.parametrize("method", ["push", "pop"])
    async def test_a_second_navigation_during_one_in_flight_waits_and_then_raises(self, method):
        """Both stepped away on the one message, and both edited it."""

        class _Sub(RenderableLayoutView):
            pass

        class _Other(RenderableLayoutView):
            pass

        source = await self._live()
        gate = asyncio.Event()

        async def deferred_edit(**kwargs):
            await gate.wait()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        first = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        call = getattr(source, method)
        args = (_Other,) if method == "push" else ()
        second = asyncio.create_task(
            call(*args, interaction=_make_interaction(user_id=1, guild_id=100))
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert not second.done(), "the second navigation waits for the first"
        gate.set()
        child = await first

        with pytest.raises(RuntimeError, match="already handed its panel on"):
            await second
        assert not child.is_finished()
        assert not any(isinstance(v, _Other) for v in get_store()._active_views.values())

    async def test_a_rebuild_hook_that_exits_the_source_is_refused(self):
        """Run inside the navigation, the exit would close the message's owner
        before the navigation had decided which view that is. It raises
        rather than waiting on itself, and the push rolls back."""

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()

        async def close_source(view):
            await source.exit(delete_message=False)

        with pytest.raises(
            RuntimeError, match=r"exit\(\) on .* inside the push\(\) or pop\(\) from it"
        ):
            await asyncio.wait_for(
                source.push(
                    _Sub,
                    interaction=_make_interaction(user_id=1, guild_id=100),
                    rebuild=close_source,
                ),
                timeout=2,
            )

        assert not source.is_finished()
        assert source._message is not None
        assert not any(isinstance(v, _Sub) for v in get_store()._active_views.values())


# // ========================================( Navigation Transaction )======================================== // #


class TestNavigationTransaction:
    """A push or pop is one transaction on the message, settled once on every path.

    While it runs, the source is away: live and subscribed, but nothing it does
    edits the message. The destination arrives muted and owns nothing until its
    edit lands. Callers that act on the view wait for the outcome.
    """

    @staticmethod
    def _error():
        return discord.HTTPException(MagicMock(status=503), "boom")

    @staticmethod
    async def _live(cls=RenderableLayoutView, **kwargs):
        view = cls(interaction=_make_interaction(user_id=1, guild_id=100), **kwargs)
        await view.send()
        return view

    def _refusing(self, destination_cls, message, gate=None):
        """A deferred-path push whose edits refuse ``destination_cls`` and land otherwise."""
        landed = []

        async def deferred_edit(**kwargs):
            if gate is not None:
                await gate.wait()
            raise self._error()

        async def channel_edit(**kwargs):
            if isinstance(kwargs.get("view"), destination_cls):
                raise self._error()
            landed.append(kwargs.get("view"))
            return message

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        message.edit = AsyncMock(side_effect=channel_edit)
        return nav, landed

    async def test_a_push_cancelled_while_it_builds_settles(self):
        """A hook awaiting in the build's flush held the cancel outside every
        rollback, and the source stayed away: exit() and on_timeout() then
        waited forever."""

        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live()

        async def audit(action, state):
            await asyncio.sleep(0.3)

        store.on("view_created", audit)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    source.push(_Sub, interaction=_make_interaction(user_id=1, guild_id=100)),
                    0.05,
                )
        finally:
            store.off("view_created", audit)

        assert not source._away_for_navigation
        assert not any(isinstance(v, _Sub) for v in store._active_views.values())
        await asyncio.wait_for(source.exit(delete_message=False), 1)

    async def test_a_push_cancelled_while_it_rolls_back_settles(self):
        """The rollback awaited the destination's removal before it settled."""

        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live()
        nav, _ = self._refusing(_Sub, source._message)

        async def audit(action, state):
            await asyncio.sleep(0.3)

        store.on("view_destroyed", audit)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(source.push(_Sub, interaction=nav), 0.05)
            assert not source._away_for_navigation
            assert source.id in store.subscribers
            # The removal runs to its end despite the cancel.
            await until(lambda: not any(isinstance(v, _Sub) for v in store._active_views.values()))
        finally:
            store.off("view_destroyed", audit)

        await asyncio.wait_for(source.exit(delete_message=False), 1)

    async def test_a_view_reopens_after_a_cancelled_push(self):
        """Its replacement exited the stuck view and waited on it forever."""

        class _Single(RenderableLayoutView):
            instance_limit = 1
            instance_policy = "replace"

        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live(_Single)

        async def audit(action, state):
            await asyncio.sleep(0.3)

        store.on("view_created", audit)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    source.push(_Sub, interaction=_make_interaction(user_id=1, guild_id=100)),
                    0.05,
                )
        finally:
            store.off("view_created", audit)

        again = _Single(interaction=_make_interaction(user_id=1, guild_id=100))
        assert await asyncio.wait_for(again.send(), 1) is not None
        assert source.is_finished()

    @pytest.mark.parametrize("closing", ["exit", "on_timeout"])
    async def test_a_parent_closing_during_a_childs_push_takes_down_its_successor(self, closing):
        """The cascade cleared the child's parent link before the child's push
        landed, so the hand-off found no parent and the new screen stayed live."""

        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        parent = await self._live()
        child = await self._live()
        parent.attach_child(child)
        gate = asyncio.Event()

        async def deferred_edit(**kwargs):
            await gate.wait()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        push = asyncio.create_task(child.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        if closing == "exit":
            ending = asyncio.create_task(parent.exit(delete_message=False))
        else:
            parent.stop()
            ending = asyncio.create_task(parent.on_timeout())
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        successor = await push
        await ending

        assert successor.is_finished()
        assert successor.id not in store._active_views

    @pytest.mark.parametrize("serialized", [True, False])
    async def test_a_second_click_on_back_is_dropped(self, serialized):
        """The queued second click ran on the view the first had left: a leaked
        second parent, and an error card once navigating from a view that has
        handed its message over raised."""

        class _Parent(RenderableLayoutView):
            pass

        class _Child(RenderableLayoutView):
            auto_back_button = True
            serialize_interactions = serialized

        store = get_store()
        parent = await self._live(_Parent)
        child = await parent.push(_Child, interaction=_make_interaction(user_id=1, guild_id=100))
        back = next(i for i in child.walk_children() if getattr(i, "_cascadeui_back_button", False))
        errors = []

        async def on_error(interaction, error, item):
            errors.append(error)

        child.on_error = on_error

        def click():
            interaction = _make_interaction(user_id=1, guild_id=100, message=parent._message)

            async def edit_message(**kwargs):
                await asyncio.sleep(0.02)
                interaction.response.is_done.return_value = True

            interaction.response.edit_message = AsyncMock(side_effect=edit_message)
            return interaction

        await asyncio.gather(
            child._scheduled_task(back, click()), child._scheduled_task(back, click())
        )
        await asyncio.sleep(0.05)

        assert errors == []
        assert [type(v).__name__ for v in store._active_views.values()] == ["_Parent"]

    @pytest.mark.parametrize("before_312", [False, True], ids=["current", "bounded_as_before_312"])
    async def test_an_edit_the_source_started_lands_before_the_push(self, before_312, monkeypatch):
        """Left running, a source edit queued behind a rate limit was released
        after the push had shipped, and covered the new view. Before 3.12 the
        bound starts the edit a tick after the call, and the push must count
        it from the call."""
        from helpers import schedule_bounded_waits_as_before_312

        if before_312:
            schedule_bounded_waits_as_before_312(monkeypatch)

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        order = []
        released = asyncio.Event()

        async def channel_edit(**kwargs):
            await released.wait()
            order.append(type(kwargs["view"]).__name__)
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)
        source.add_item(ActionRow(StatefulButton(label="late", custom_id="late")))
        pending = source.create_task(source.refresh())
        await asyncio.sleep(0)

        nav = _make_interaction(user_id=1, guild_id=100)

        async def edit_message(**kwargs):
            order.append(type(kwargs["view"]).__name__)
            nav.response.is_done.return_value = True

        nav.response.edit_message = AsyncMock(side_effect=edit_message)
        asyncio.get_running_loop().call_later(0.05, released.set)
        await source.push(_Sub, interaction=nav)
        await asyncio.gather(pending, return_exceptions=True)

        assert order == ["RenderableLayoutView", "_Sub"]

    async def test_a_change_delivered_after_a_rollback_still_renders(self):
        """A notification selected while the source was away and delivered
        after it came back still renders."""
        from discord.ui import TextDisplay

        from cascadeui import cascade_reducer

        @cascade_reducer("NAV_TXN_SET_X")
        async def _set_x(action, state):
            state["application"]["nav_txn_x"] = action["payload"]
            return state

        class _Source(RenderableLayoutView):
            subscribed_actions = {"NAV_TXN_SET_X"}

            def state_selector(self, state):
                return state["application"].get("nav_txn_x")

            def build_ui(self):
                self.clear_items()
                x = self.state_store.state["application"].get("nav_txn_x")
                self.add_item(TextDisplay(f"x={x}"))

        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live(_Source)
        gate = asyncio.Event()
        nav, landed = self._refusing(_Sub, source._message, gate)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        gate.set()
        await store.dispatch("NAV_TXN_SET_X", 7)
        await push
        await store._flush_notifications()
        await source.task_manager.wait_tasks(source.id)

        assert landed and "x=7" in str(landed[-1].to_components())

    async def test_a_message_deleted_during_a_failed_push_tears_the_source_down(self):
        """The gateway cleanup read the away source as torn down and skipped it,
        and the rollback revived it on a message that no longer existed."""

        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live()
        gate = asyncio.Event()
        nav, _ = self._refusing(_Sub, source._message, gate)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        deleted = source._message.id
        cleanup = asyncio.create_task(
            store._clean_up_deleted(lambda message: message.id == deleted)
        )
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        await push
        await cleanup

        assert source.is_finished()
        assert source.id not in store._active_views

    async def test_a_replace_during_a_push_waits_for_it(self):
        """A failing replace() cleared the push's bookkeeping while it was
        still in flight, waking every caller waiting on it."""

        class _Sub(RenderableLayoutView):
            pass

        class _Broken(RenderableLayoutView):
            def __init__(self, **kwargs):
                raise ValueError("bad kwargs")

        source = await self._live()
        gate = asyncio.Event()

        async def deferred_edit(**kwargs):
            await gate.wait()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        replacing = asyncio.create_task(source.replace(_Broken))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not replacing.done()
        assert source._away_for_navigation
        gate.set()
        child = await push

        # The push landed, so the view it left has nothing to replace.
        with pytest.raises(RuntimeError, match="already handed its panel on"):
            await replacing
        assert not child.is_finished()

    async def test_a_destination_that_listens_to_state_ships_once_loaded(self):
        """Handed the build's notification, it rendered its tree before its
        on_load had run, then the navigation shipped the loaded one."""
        from discord.ui import TextDisplay

        class _Dest(RenderableLayoutView):
            subscribed_actions = None

            async def on_load(self):
                self.clear_items()
                self.add_item(TextDisplay("loaded"))

        source = await self._live()
        shipped = []

        async def channel_edit(**kwargs):
            shipped.append(("channel", str(kwargs["view"].to_components())))
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)
        nav = _make_interaction(user_id=1, guild_id=100)
        nav.message = MagicMock(id=source._message.id)

        async def edit_message(**kwargs):
            shipped.append(("interaction", str(kwargs["view"].to_components())))
            nav.response.is_done.return_value = True

        nav.response.edit_message = AsyncMock(side_effect=edit_message)
        from cascadeui.state.store import _CURRENT_INTERACTION

        token = _CURRENT_INTERACTION.set(nav)
        try:
            dest = await source.push(_Dest, interaction=nav)
        finally:
            _CURRENT_INTERACTION.reset(token)
        await dest.task_manager.wait_tasks(dest.id)

        assert shipped and shipped[0][0] == "interaction" and "loaded" in shipped[0][1]
        assert all("loaded" in body for _, body in shipped)

    async def test_a_destination_refresh_from_another_task_waits_for_the_landing(self):
        """Only the navigation edits the message while the destination arrives."""
        from cascadeui import RenderOutcome

        class _Dest(RenderableLayoutView):
            pass

        source = await self._live()
        gate = asyncio.Event()
        outcomes = []

        async def deferred_edit(**kwargs):
            await gate.wait()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        push = asyncio.create_task(source.push(_Dest, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)
        [dest] = [v for v in get_store()._active_views.values() if isinstance(v, _Dest)]
        dest._message.edit = AsyncMock()

        outcomes.append(await dest.refresh())
        gate.set()
        await push
        await dest.task_manager.wait_tasks(dest.id)

        assert outcomes == [RenderOutcome.DEFERRED]

    async def test_a_stalled_v1_push_reclaims_with_the_sources_content(self):
        """A stall may have reached Discord, so the message can show the
        discarded view; the source re-renders the way pop() restores it."""

        class _Source(StatefulView):
            nav_rebuild = staticmethod(lambda v: {"embed": discord.Embed(title="SOURCE")})

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._webhook_message = None
        source._message.edit = AsyncMock(return_value=source._message)
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=asyncio.TimeoutError())

        await source.push(_Sub, interaction=nav)
        await source.task_manager.wait_tasks(source.id)

        assert not source.is_finished()
        sent = source._message.edit.await_args.kwargs
        assert sent["view"] is source
        assert sent["embed"].title == "SOURCE"

    async def test_an_armed_source_reclaims_with_its_refresh_button(self):
        """The reclaim runs nav_rebuild for its content, and that hook
        rebuilt the tree: the redraw shipped a live-looking panel over the
        refresh button, which the token's end then left with no way on."""
        from discord.ui import Container, TextDisplay

        class _Source(StatefulLayoutView):
            nav_rebuild = staticmethod(lambda v: v.build_ui())

            def build_ui(self):
                self.clear_items()
                self.add_item(Container(TextDisplay("live panel")))

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        source.build_ui()
        await source.send()
        source._refresh_armed = True
        source._install_refresh_button(source.build_refresh_button())
        armed = list(source.children)
        source._message.edit = AsyncMock(return_value=source._message)
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=asyncio.TimeoutError())

        await source.push(RenderableLayoutView, interaction=nav)
        await source.task_manager.wait_tasks(source.id)

        assert source._message.edit.await_args.kwargs["view"] is source
        assert source.children == armed

    async def test_an_exit_owed_a_redraw_whose_nav_rebuild_raises_still_closes(self, caplog):
        """The hook's raise escaped the exit's message edit, so the message kept
        the discarded view's content and its controls."""

        def broken(view):
            raise RuntimeError("template missing")

        class _Source(StatefulView):
            nav_rebuild = staticmethod(broken)

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._webhook_message = None
        source._message.edit = AsyncMock(return_value=source._message)
        # What a push that failed with an unknown outcome leaves owed.
        source._reclaim_pending = True

        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            await source.exit(delete_message=False)

        assert source._torn_down()
        assert source._message.edit.await_count == 1, "the exit shipped no teardown edit"
        assert source._message.edit.await_args.kwargs == {"view": None}
        assert any("teardown edit raised" in r.getMessage() for r in caplog.records)

    async def test_an_exit_owed_a_redraw_by_a_hook_that_returns_nothing_logs_nothing(self, caplog):
        """A nav_rebuild that rebuilds the tree and returns None was read as
        edit arguments, so every teardown owed a redraw logged an error."""

        async def noop(interaction):
            pass

        class _Source(StatefulLayoutView):
            nav_rebuild = staticmethod(lambda v: v.build_ui())

            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="go", callback=noop)))

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        source.build_ui()
        await source.send()
        source._message.edit = AsyncMock(return_value=source._message)
        source._reclaim_pending = True

        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            await source.exit(delete_message=False)

        assert source._message.edit.await_count == 1
        assert not [r for r in caplog.records if r.name.startswith("cascadeui")]

    async def test_an_exit_owed_a_redraw_sends_its_file_whole(self):
        """A nav_rebuild that hands back the file the panel went out with
        uploaded it from where that send's read stopped."""
        payload = b"x" * 3000
        chart = discord.File(io.BytesIO(payload), filename="map.png")
        chart.fp.read()  # the panel's send streamed it,
        chart.close()  # and discord.py closed it when that request ended
        uploaded = []

        async def edit(**kwargs):
            f = kwargs["attachments"][0]
            f.reset(seek=0)
            uploaded.append(len(f.fp.read()))

        class _Source(StatefulView):
            nav_rebuild = staticmethod(lambda v: {"attachments": [chart]})

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._webhook_message = None
        source._message.edit = AsyncMock(side_effect=edit)
        source._reclaim_pending = True

        await source.exit(delete_message=False)

        assert uploaded == [len(payload)]

    async def test_a_landing_through_the_original_response_records_its_render(self):
        """A push landed with no render baseline, so the next unchanged
        refresh shipped an edit anyway."""
        from cascadeui import RenderOutcome

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        child = await source.push(_Sub, interaction=nav)
        child._message.edit = AsyncMock()

        assert await child.refresh() is RenderOutcome.SKIPPED
        child._message.edit.assert_not_called()

    async def test_the_sources_state_render_waits_for_its_push(self):
        """A state change reaching the source mid-push runs none of its
        render code then; a failed push renders it once afterwards."""
        from cascadeui import cascade_reducer

        @cascade_reducer("NAV_TXN_BUMP")
        async def _bump(action, state):
            state["application"]["nav_txn_bump"] = action["payload"]
            return state

        renders = []

        class _Source(RenderableLayoutView):
            subscribed_actions = {"NAV_TXN_BUMP"}

            async def on_state_changed(self, state):
                renders.append(state["application"].get("nav_txn_bump"))

        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live(_Source)
        gate = asyncio.Event()
        nav, _ = self._refusing(_Sub, source._message, gate)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        await store.dispatch("NAV_TXN_BUMP", 1)
        await store._flush_notifications()
        assert renders == []
        gate.set()
        await push
        await source.task_manager.wait_tasks(source.id)

        assert renders == [1]

    async def test_a_change_while_the_destination_arrives_renders_once_it_lands(self):
        """The destination defers what it is asked to render until it owns
        the message, then renders the state it missed."""
        from discord.ui import TextDisplay

        from cascadeui import cascade_reducer

        @cascade_reducer("NAV_TXN_ARRIVE")
        async def _arrive(action, state):
            state["application"]["nav_txn_arrive"] = action["payload"]
            return state

        class _Dest(RenderableLayoutView):
            subscribed_actions = {"NAV_TXN_ARRIVE"}

            def build_ui(self):
                self.clear_items()
                value = self.state_store.state["application"].get("nav_txn_arrive")
                self.add_item(TextDisplay(f"arrive={value}"))

        store = get_store()
        source = await self._live()
        gate = asyncio.Event()

        async def deferred_edit(**kwargs):
            await gate.wait()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        push = asyncio.create_task(source.push(_Dest, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)
        [dest] = [v for v in store._active_views.values() if isinstance(v, _Dest)]
        shipped = []

        async def channel_edit(**kwargs):
            shipped.append(str(kwargs["view"].to_components()))
            return dest._message

        dest._message.edit = AsyncMock(side_effect=channel_edit)

        await store.dispatch("NAV_TXN_ARRIVE", 5)
        await store._flush_notifications()
        assert shipped == []
        gate.set()
        await push
        await dest.task_manager.wait_tasks(dest.id)

        assert shipped and "arrive=5" in shipped[-1]

    async def test_a_push_run_by_the_destinations_own_task_survives_a_failed_edit(self):
        """Discarding the destination cancelled its tasks, the running one
        included, so the push raised CancelledError instead of returning."""

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        nav, _ = self._refusing(_Sub, source._message)
        dest = _Sub(interaction=_make_interaction(user_id=1, guild_id=100))

        async def navigate():
            return await source.push(dest, interaction=nav)

        returned = await dest.create_task(navigate())

        assert returned is dest
        assert dest.is_finished()
        assert not source.is_finished()

    @staticmethod
    def _rate_limited():
        class _Response:
            status = 429
            reason = "Too Many Requests"
            headers = {"Retry-After": "0.5"}

        return discord.HTTPException(
            _Response(), {"message": "You are being rate limited.", "code": 0}
        )

    async def test_a_cancel_during_the_discarded_views_removal_still_removes_it(self):
        """Settled, but a second cancel stopped the removal part way and left
        the discarded view holding its registration."""

        class _Sub(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live()
        nav, _ = self._refusing(_Sub, source._message)

        async def slow_removal(action, state, next_fn):
            if action["type"] == "VIEW_DESTROYED":
                await asyncio.sleep(0.2)
            return await next_fn(action, state)

        store._add_middleware(slow_removal)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(source.push(_Sub, interaction=nav), 0.05)
            await asyncio.sleep(0.3)
        finally:
            store._remove_middleware(slow_removal)

        assert not any(isinstance(v, _Sub) for v in store._active_views.values())
        assert not any(row.get("class_name") == "_Sub" for row in store.state["views"].values())

    async def test_a_destination_renders_only_once_it_has_loaded(self, caplog):
        """Handed the build's notification, it ran its render before on_load
        had set the data that render reads."""
        import logging

        from discord.ui import TextDisplay

        class _Dest(RenderableLayoutView):
            subscribed_actions = None

            async def on_load(self):
                self.rows = ["loaded"]

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay(", ".join(self.rows)))

        source = await self._live()
        with caplog.at_level(logging.ERROR):
            dest = await source.push(_Dest, interaction=_make_interaction(user_id=1, guild_id=100))
            await dest.task_manager.wait_tasks(dest.id)
            await get_store()._flush_notifications()

        assert [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and r.name.startswith("cascadeui")
        ] == []

    async def test_a_stalled_channel_fallback_redraws_the_source(self):
        """The fallback edit may have reached Discord, so the push rolls back
        and the source redraws rather than committing to a swap it cannot
        confirm."""

        class _Sub(RenderableLayoutView):
            edit_timeout = 0.05

        source = await self._live()
        message = source._message
        shipped = []

        async def channel_edit(**kwargs):
            if isinstance(kwargs.get("view"), _Sub):
                await asyncio.sleep(1)
            shipped.append(kwargs.get("view"))
            return message

        message.edit = AsyncMock(side_effect=channel_edit)
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=self._error())

        await source.push(_Sub, interaction=nav)
        await source.task_manager.wait_tasks(source.id)

        assert not source.is_finished()
        assert shipped == [source]

    async def test_a_rate_limited_channel_fallback_keeps_the_source(self):
        """Nothing was sent, so committing would leave the torn-down source on
        screen for the whole backoff."""

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        message = source._message
        limited = self._rate_limited()

        async def channel_edit(**kwargs):
            if isinstance(kwargs.get("view"), _Sub):
                raise limited
            return message

        message.edit = AsyncMock(side_effect=channel_edit)
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=self._error())

        child = await source.push(_Sub, interaction=nav)

        assert not source.is_finished()
        assert child.is_finished()

    async def test_a_second_click_queued_behind_a_slow_navigation_is_dropped(self):
        """The second click passed the checks before the first had started its
        push, then waited on the lock and ran on the view the first had left."""

        class _Parent(RenderableLayoutView):
            pass

        class _Child(RenderableLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(ActionRow(self.back))

            @property
            def back(self):
                if not hasattr(self, "_slow_back"):

                    async def go_back(interaction):
                        await asyncio.sleep(0.01)
                        await self.pop(interaction=interaction)

                    self._slow_back = StatefulButton(
                        label="Back", custom_id="slow_back", callback=go_back
                    )
                return self._slow_back

        store = get_store()
        parent = await self._live(_Parent)
        child = await parent.push(_Child, interaction=_make_interaction(user_id=1, guild_id=100))
        errors = []

        async def on_error(interaction, error, item):
            errors.append(error)

        child.on_error = on_error

        def click():
            interaction = _make_interaction(user_id=1, guild_id=100, message=parent._message)

            async def edit_message(**kwargs):
                interaction.response.is_done.return_value = True

            interaction.response.edit_message = AsyncMock(side_effect=edit_message)
            return interaction

        await asyncio.gather(
            child._scheduled_task(child.back, click()),
            child._scheduled_task(child.back, click()),
        )

        assert errors == []
        assert [type(v).__name__ for v in store._active_views.values()] == ["_Parent"]

    async def test_a_source_that_timed_out_mid_push_is_not_redrawn(self):
        """A rollback redraw scheduled on a view its own timeout had stopped
        shipped its live buttons over the frozen panel its teardown was
        composing."""
        from discord.ui import Button

        class _Source(RenderableLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))

            async def on_timeout(self):
                await asyncio.sleep(0.02)
                await self.exit(delete_message=False)

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live(_Source)
        message = source._message
        shipped = []

        async def channel_edit(**kwargs):
            view = kwargs.get("view")
            shipped.append([b.disabled for b in view.walk_children() if isinstance(b, Button)])
            return message

        message.edit = AsyncMock(side_effect=channel_edit)
        gate = asyncio.Event()

        async def stalled(**kwargs):
            await gate.wait()
            raise asyncio.TimeoutError()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=stalled)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        source.stop()
        expiring = asyncio.create_task(source.on_timeout())
        gate.set()
        await push
        await expiring

        assert shipped and all(all(flags) for flags in shipped)

    async def test_a_replace_cancelled_while_it_builds_leaves_no_destination(self):
        """replace() tears its source down up front, so its destination is
        discarded inside the batch, before the flush hands it anything."""

        class _Next(RenderableLayoutView):
            pass

        store = get_store()
        source = await self._live()

        async def slow_registration(action, state, next_fn):
            if action["type"] == "VIEW_CREATED":
                await asyncio.sleep(0.3)
            return await next_fn(action, state)

        store._add_middleware(slow_registration)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(source.replace(_Next), 0.05)
        finally:
            store._remove_middleware(slow_registration)

        assert not any(isinstance(v, _Next) for v in store._active_views.values())

    async def test_a_tab_render_asked_of_the_source_mid_push_ships_after_it_fails(self):
        """The render builds while the push is in flight and its edit is held:
        replaying the state render afterwards would not re-run the tab
        builder, so holding the whole render would lose the new content."""
        from discord.ui import TextDisplay

        from cascadeui import TabLayoutView

        version = {"n": 1}

        async def overview():
            return [TextDisplay(f"overview v{version['n']}")]

        class _Sub(RenderableLayoutView):
            pass

        source = TabLayoutView(
            interaction=_make_interaction(user_id=1, guild_id=100), tabs={"Overview": overview}
        )
        await source.send()
        gate = asyncio.Event()
        nav, landed = self._refusing(_Sub, source._message, gate)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        version["n"] = 2
        await source.refresh_content()
        assert landed == [], "nothing edits the message while the push is in flight"
        gate.set()
        await push
        await source.task_manager.wait_tasks(source.id)

        assert landed and "overview v2" in str(landed[-1].to_components())

    async def test_a_wait_on_a_push_that_does_not_finish_is_logged(self, monkeypatch, caplog):
        import logging

        from cascadeui.views import _navigation

        monkeypatch.setattr(_navigation, "_NAVIGATION_WAIT_WARN_SECONDS", 0.01)

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        gate = asyncio.Event()

        async def deferred_edit(**kwargs):
            await gate.wait()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        for _ in range(5):
            await asyncio.sleep(0)

        with caplog.at_level(logging.WARNING, logger="cascadeui.views._navigation"):
            closing = asyncio.create_task(source.exit(delete_message=False))
            await asyncio.sleep(0.05)
            gate.set()
            await push
            await closing

        assert any(
            "exit() on RenderableLayoutView has waited" in record.message
            for record in caplog.records
        )

    async def test_a_push_waiting_on_an_earlier_edit_is_logged(self, monkeypatch, caplog):
        import logging

        from cascadeui.views import _navigation

        monkeypatch.setattr(_navigation, "_NAVIGATION_WAIT_WARN_SECONDS", 0.01)

        class _Sub(RenderableLayoutView):
            pass

        source = await self._live()
        released = asyncio.Event()

        async def channel_edit(**kwargs):
            await released.wait()
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)
        source.add_item(ActionRow(StatefulButton(label="late", custom_id="late")))
        pending = asyncio.create_task(source.refresh())
        await asyncio.sleep(0)

        with caplog.at_level(logging.WARNING, logger="cascadeui.views._navigation"):
            push = asyncio.create_task(
                source.push(_Sub, interaction=_make_interaction(user_id=1, guild_id=100))
            )
            await asyncio.sleep(0.05)
            released.set()
            await push
            await pending

        assert any(
            "has waited" in record.message and "for an edit the view started" in record.message
            for record in caplog.records
        )


# // ========================================( Navigation Seam )======================================== // #


def _seam_error(status=503):
    return discord.HTTPException(MagicMock(status=status, reason="x"), "x")


async def _seam_live(cls=RenderableLayoutView, **kwargs):
    view = cls(interaction=_make_interaction(user_id=1, guild_id=100), **kwargs)
    await view.send()
    return view


def _seam_landing_nav(shipped):
    """A click whose fast-path edit lands and records the view it shipped."""
    nav = _make_interaction(user_id=1, guild_id=100)

    async def edit_message(**kwargs):
        view = kwargs["view"]
        shipped.append(("navigation", type(view).__name__, view.is_finished()))
        nav.response.is_done.return_value = True

    nav.response.edit_message = AsyncMock(side_effect=edit_message)
    return nav


async def _seam_ticks(n=5):
    for _ in range(n):
        await asyncio.sleep(0)


class TestEditsAroundNavigation:
    """Only the navigation edits a message while a push or pop moves it."""

    async def test_a_source_edit_retried_on_the_channel_does_not_follow_the_push(self):
        """The webhook edit failed after the push had started waiting for it,
        and the refresh retried through the channel, over the new view."""
        from cascadeui import RenderOutcome

        class _Src(StatefulView):
            pass

        class _Dst(StatefulView):
            pass

        source = await _seam_live(_Src)
        shipped = []
        started, release = asyncio.Event(), asyncio.Event()

        async def webhook_edit(**kwargs):
            started.set()
            await release.wait()
            raise _seam_error(401)

        source._webhook_message = MagicMock()
        source._webhook_message.edit = AsyncMock(side_effect=webhook_edit)

        async def channel_edit(**kwargs):
            shipped.append(("channel", type(kwargs.get("view")).__name__, False))
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)
        nav = _seam_landing_nav(shipped)

        refreshing = asyncio.create_task(source.refresh(embed=discord.Embed(title="t")))
        await started.wait()
        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await _seam_ticks()
        release.set()
        outcome = await refreshing
        await pushing

        assert outcome is RenderOutcome.DEFERRED
        assert shipped == [("navigation", "_Dst", False)]

    async def test_an_acting_edit_retried_on_the_channel_does_not_follow_the_push(self):
        """The same retry, from a failed fast path."""
        from discord.ui import TextDisplay

        from cascadeui import RenderOutcome
        from cascadeui.state.store import _CURRENT_INTERACTION

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live()
        shipped = []
        started, release = asyncio.Event(), asyncio.Event()
        click = _make_interaction(user_id=1, guild_id=100, message=source._message)

        async def fast_edit(**kwargs):
            started.set()
            await release.wait()
            raise _seam_error(500)

        click.response.edit_message = AsyncMock(side_effect=fast_edit)

        async def channel_edit(**kwargs):
            shipped.append(("channel", type(kwargs.get("view")).__name__, False))
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)
        source.add_item(TextDisplay("changed"))

        async def acting_refresh():
            _CURRENT_INTERACTION.set(click)
            return await source.refresh()

        refreshing = asyncio.create_task(acting_refresh())
        await started.wait()
        pushing = asyncio.create_task(source.push(_Dst, interaction=_seam_landing_nav(shipped)))
        await _seam_ticks()
        release.set()
        outcome = await refreshing
        await pushing

        assert outcome is RenderOutcome.DEFERRED
        assert shipped == [("navigation", "_Dst", False)]

    async def test_an_acting_embed_edit_retried_on_the_webhook_does_not_follow_the_push(self):
        """An embed edit falls back from the fast path to the webhook, the
        route an interaction-owned message needs for embeds."""
        from cascadeui import RenderOutcome
        from cascadeui.state.store import _CURRENT_INTERACTION

        class _Src(StatefulView):
            pass

        class _Dst(StatefulView):
            pass

        source = await _seam_live(_Src)
        shipped = []
        started, release = asyncio.Event(), asyncio.Event()
        click = _make_interaction(user_id=1, guild_id=100, message=source._message)

        async def fast_edit(**kwargs):
            started.set()
            await release.wait()
            raise _seam_error(500)

        click.response.edit_message = AsyncMock(side_effect=fast_edit)

        async def webhook_edit(**kwargs):
            shipped.append(("webhook", type(kwargs.get("view")).__name__, False))
            return source._message

        source._webhook_message = MagicMock()
        source._webhook_message.edit = AsyncMock(side_effect=webhook_edit)

        async def acting_refresh():
            _CURRENT_INTERACTION.set(click)
            return await source.refresh(embed=discord.Embed(title="t"))

        refreshing = asyncio.create_task(acting_refresh())
        await started.wait()
        pushing = asyncio.create_task(source.push(_Dst, interaction=_seam_landing_nav(shipped)))
        await _seam_ticks()
        release.set()
        outcome = await refreshing
        await pushing

        assert outcome is RenderOutcome.DEFERRED
        assert shipped == [("navigation", "_Dst", False)]

    async def test_a_refresh_from_the_new_views_on_load_waits_for_the_navigation(self):
        """It edited the message before the push had decided, so a push that
        then failed left the discarded view on screen."""

        class _Dst(RenderableLayoutView):
            async def on_load(self):
                await self.refresh()

        source = await _seam_live()
        shipped = []

        async def channel_edit(**kwargs):
            shipped.append(("channel", type(kwargs.get("view")).__name__, False))
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)

        def refuse(view):
            raise RuntimeError("rebuild failed")

        with pytest.raises(RuntimeError, match="rebuild failed"):
            await source.push(_Dst, interaction=_seam_landing_nav(shipped), rebuild=refuse)
        await source.task_manager.wait_tasks(source.id)

        assert shipped == []
        assert not source.is_finished()

    async def test_a_refresh_from_the_new_views_on_load_ships_once(self):
        """Held, it rides the navigation's edit, and its replay afterwards
        finds nothing new to send."""

        class _Dst(RenderableLayoutView):
            async def on_load(self):
                await self.refresh()

        source = await _seam_live()
        shipped = []

        async def channel_edit(**kwargs):
            shipped.append(("channel", type(kwargs.get("view")).__name__, False))
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)

        dest = await source.push(_Dst, interaction=_seam_landing_nav(shipped))
        await dest.task_manager.wait_tasks(dest.id)
        await get_store()._flush_notifications()

        assert shipped == [("navigation", "_Dst", False)]

    async def test_a_v1_source_redrawn_after_a_failed_push_gets_its_embed_back(self):
        """The redraw after a stalled push shipped the source's tree alone,
        under the embed the discarded view had put on the message."""

        class _Src(StatefulView):
            nav_rebuild = staticmethod(lambda view: {"embed": discord.Embed(title="SRC")})

        class _Dst(StatefulView):
            nav_rebuild = staticmethod(lambda view: {"embed": discord.Embed(title="DST")})

        source = _Src(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(embed=discord.Embed(title="SRC"))
        source._webhook_message = None
        edits = []
        in_flight, release = asyncio.Event(), asyncio.Event()

        async def channel_edit(**kwargs):
            embed = kwargs.get("embed")
            edits.append((type(kwargs.get("view")).__name__, embed.title if embed else None))
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)
        nav = _make_interaction(user_id=1, guild_id=100)
        nav.response.edit_message = AsyncMock(side_effect=_seam_error(500))

        async def stall(**kwargs):
            # The request left and never answered: the outcome is unknown.
            in_flight.set()
            await release.wait()
            raise asyncio.TimeoutError()

        nav.edit_original_response = AsyncMock(side_effect=stall)

        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await in_flight.wait()
        await source.refresh()
        release.set()
        await pushing
        await source.task_manager.wait_tasks(source.id)

        assert edits[-1] == ("_Src", "SRC")

    async def test_an_armed_source_ships_its_held_edit_after_a_failed_push(self):
        """The edit it could not make while away (the arming edit's retry) was
        replayed as a state render, which an armed view declines, so the
        refresh button never reached the message."""
        from discord.ui import TextDisplay

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live()
        shipped = []

        async def channel_edit(**kwargs):
            view = kwargs.get("view")
            if isinstance(view, _Dst):
                raise _seam_error(500)
            shipped.append(type(view).__name__)
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)
        source._refresh_armed = True
        source.clear_items()
        source.add_item(TextDisplay("refresh button"))
        gate = asyncio.Event()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def refuse(**kwargs):
            await gate.wait()
            raise _seam_error(500)

        nav.edit_original_response = AsyncMock(side_effect=refuse)

        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await _seam_ticks()
        await source.refresh()
        gate.set()
        await pushing
        await source.task_manager.wait_tasks(source.id)

        assert shipped == ["RenderableLayoutView"]

    async def test_an_armed_source_redrawn_after_an_unknown_push_ships_its_tree(self):
        """The redraw ran the state render, which an armed view declines, and
        the discarded view's edit may have been what the message showed."""
        from discord.ui import TextDisplay

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live()
        shipped = []

        async def channel_edit(**kwargs):
            shipped.append(type(kwargs.get("view")).__name__)
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)
        source._refresh_armed = True
        source.clear_items()
        source.add_item(TextDisplay("refresh button"))
        in_flight, release = asyncio.Event(), asyncio.Event()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def stall(**kwargs):
            in_flight.set()
            await release.wait()
            raise asyncio.TimeoutError()

        nav.edit_original_response = AsyncMock(side_effect=stall)

        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await in_flight.wait()
        await source.refresh()
        release.set()
        await pushing
        await source.task_manager.wait_tasks(source.id)

        assert shipped == ["RenderableLayoutView"]

    async def test_a_custom_id_both_views_use_stays_routable_after_a_push(self):
        """discord.py keys a message's buttons by custom_id, and the source
        stopping at the commit removed its keys, the destination's among them."""
        from discord.ui.view import ViewStore

        def help_row():
            async def callback(interaction):
                pass

            return ActionRow(StatefulButton(label="Help", custom_id="help", callback=callback))

        class _Src(RenderableLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(help_row())

        class _Dst(_Src):
            pass

        views = ViewStore(MagicMock())
        source = await _seam_live(_Src)
        views.add_view(source, 555)
        nav = _make_interaction(user_id=1, guild_id=100)

        async def edit_message(**kwargs):
            views.add_view(kwargs["view"], 555)
            nav.response.is_done.return_value = True

        nav.response.edit_message = AsyncMock(side_effect=edit_message)

        dest = await source.push(_Dst, interaction=nav)

        item = views._views.get(555, {}).get((discord.ComponentType.button.value, "help"))
        assert item is not None and item.view is dest
        assert views._synced_message_views.get(555) is dest


class TestCallsInsideNavigation:
    """A push or pop cannot be navigated or closed from inside itself, and a
    closed view cannot be navigated from at all."""

    async def test_navigating_the_source_from_the_new_views_on_load_raises(self):
        """The nested push overwrote the navigation's record of its
        destination, left the destination muted for good, and leaked a view."""

        class _Other(RenderableLayoutView):
            pass

        source = None

        class _Dst(RenderableLayoutView):
            async def on_load(self):
                await source.push(_Other)

        source = await _seam_live()
        shipped = []

        with pytest.raises(
            RuntimeError, match=r"push\(\) on .* inside the push\(\) or pop\(\) from it"
        ):
            await source.push(_Dst, interaction=_seam_landing_nav(shipped))

        assert shipped == []
        assert not source.is_finished()
        assert not source._away_for_navigation
        live = [type(v).__name__ for v in get_store()._active_views.values()]
        assert live == ["RenderableLayoutView"]

    @pytest.mark.parametrize("call", ["push", "pop", "exit"])
    async def test_the_new_view_navigating_or_closing_itself_from_on_load_raises(self, call):
        """The new view shipped finished over the view it had moved on to."""

        class _Grand(RenderableLayoutView):
            pass

        class _Dst(RenderableLayoutView):
            async def on_load(self):
                if call == "push":
                    await self.push(_Grand)
                elif call == "pop":
                    await self.pop()
                else:
                    await self.exit()

        source = await _seam_live()
        shipped = []

        with pytest.raises(RuntimeError, match=rf"{call}\(\) on _Dst .* bringing it in"):
            await source.push(_Dst, interaction=_seam_landing_nav(shipped))

        assert shipped == []
        assert not source.is_finished()
        live = [type(v).__name__ for v in get_store()._active_views.values()]
        assert live == ["RenderableLayoutView"]

    async def test_exit_on_a_new_view_still_loading_waits_for_it_to_arrive(self):
        """Run at once, it froze the view onto the message before it owned it,
        and the navigation then shipped it dead."""
        loading, release = asyncio.Event(), asyncio.Event()

        class _Dst(RenderableLayoutView):
            async def on_load(self):
                loading.set()
                await release.wait()

        source = await _seam_live()
        shipped = []

        async def channel_edit(**kwargs):
            shipped.append(("channel", type(kwargs.get("view")).__name__, False))
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)

        pushing = asyncio.create_task(source.push(_Dst, interaction=_seam_landing_nav(shipped)))
        await loading.wait()
        dest = source._navigation_destination
        exiting = asyncio.create_task(dest.exit())
        await _seam_ticks()

        assert not exiting.done()
        assert shipped == []
        release.set()
        await pushing
        await exiting

        assert shipped == [("navigation", "_Dst", False)]
        assert dest._torn_down()

    @pytest.mark.parametrize("call", ["push", "pop"])
    async def test_navigating_from_a_closed_view_raises(self, call):
        """The push revived the panel exit() had closed."""

        class _Dst(RenderableLayoutView):
            pass

        parent = await _seam_live()
        source = await parent.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))
        await source.exit()

        with pytest.raises(RuntimeError, match=rf"{call}\(\) was called on a view that has closed"):
            if call == "push":
                await source.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))
            else:
                await source.pop(interaction=_make_interaction(user_id=1, guild_id=100))

        assert not any(not v.is_finished() for v in get_store()._active_views.values())

    async def test_a_push_while_exit_closes_the_children_raises(self):
        """exit() stopped the view only after closing its children, and a push
        in that window ran on the view being closed."""
        closing, release = asyncio.Event(), asyncio.Event()

        class _SlowChild(RenderableLayoutView):
            async def exit(self, delete_message=None):
                closing.set()
                await release.wait()
                return await super().exit(delete_message)

        class _Dst(RenderableLayoutView):
            pass

        parent = await _seam_live()
        child = _SlowChild(interaction=_make_interaction(user_id=1, guild_id=100), parent=parent)
        await child.send()
        exiting = asyncio.create_task(parent.exit())
        await closing.wait()

        with pytest.raises(RuntimeError, match="has closed"):
            await parent.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))
        release.set()
        await exiting

        assert not any(isinstance(v, _Dst) for v in get_store()._active_views.values())

    async def test_a_second_click_navigating_beside_the_first_names_the_setting(self):
        """With serialize_interactions = False both callbacks run, and the
        second navigates from a view the first already moved on from."""

        class _Parent(RenderableLayoutView):
            serialize_interactions = False

        class _Child(RenderableLayoutView):
            pass

        parent = await _seam_live(_Parent)
        gate = asyncio.Event()

        async def go(interaction):
            await gate.wait()
            await parent.push(_Child, interaction=interaction)

        button = StatefulButton(label="Go", custom_id="go", callback=go)
        parent.add_item(ActionRow(button))
        errors = []

        async def on_error(interaction, error, item):
            errors.append(error)

        parent.on_error = on_error

        def click():
            interaction = _make_interaction(user_id=1, guild_id=100, message=parent._message)

            async def edit_message(**kwargs):
                interaction.response.is_done.return_value = True

            interaction.response.edit_message = AsyncMock(side_effect=edit_message)
            return interaction

        clicks = asyncio.gather(
            parent._scheduled_task(button, click()),
            parent._scheduled_task(button, click()),
        )
        await _seam_ticks()
        gate.set()
        await clicks

        assert len(errors) == 1
        assert "serialize_interactions = False" in str(errors[0])


class TestCleanupFollowsNavigation:
    """exit() on a view that navigated away does nothing; the library's own
    cleanup closes the view that took its place."""

    @staticmethod
    def _blocked_edit(view):
        """An edit of ``view``'s message in flight until the returned event is set.

        A push from ``view`` begun meanwhile waits for it before building its
        destination, so a cleanup that collects views then sees no destination.
        """
        release = asyncio.Event()
        message = view._message

        async def channel_edit(**kwargs):
            if kwargs.get("view") is view:
                await release.wait()
            return message

        message.edit = AsyncMock(side_effect=channel_edit)
        return release

    async def test_a_queued_push_is_not_overtaken_by_an_exit_queued_behind_it(self):
        """Every waiter woke when the first push settled, the queued push
        started, and the exit ran beside it on a view already away again."""
        from discord.ui import Button

        class _Failing(RenderableLayoutView):
            pass

        class _Landing(RenderableLayoutView):
            pass

        source = await _seam_live()
        source.add_item(ActionRow(Button(label="b", custom_id="b")))
        message = source._message
        shipped = []

        async def channel_edit(**kwargs):
            view = kwargs.get("view")
            if isinstance(view, _Failing):
                raise _seam_error(500)
            shipped.append(("channel", type(view).__name__))
            return message

        message.edit = AsyncMock(side_effect=channel_edit)
        first_gate, second_gate = asyncio.Event(), asyncio.Event()
        first_nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def refuse(**kwargs):
            await first_gate.wait()
            raise _seam_error(500)

        first_nav.edit_original_response = AsyncMock(side_effect=refuse)
        second_nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def land(**kwargs):
            shipped.append(("navigation", type(kwargs["view"]).__name__))
            await second_gate.wait()
            return message

        second_nav.edit_original_response = AsyncMock(side_effect=land)

        first = asyncio.create_task(source.push(_Failing, interaction=first_nav))
        await _seam_ticks()
        second = asyncio.create_task(source.push(_Landing, interaction=second_nav))
        await _seam_ticks(1)
        exiting = asyncio.create_task(source.exit())
        await _seam_ticks()
        first_gate.set()
        await _seam_ticks(10)

        assert not exiting.done()
        second_gate.set()
        await first
        landed = await second
        await exiting

        assert shipped == [("navigation", "_Landing")]
        assert not landed.is_finished()

    async def test_an_instance_limit_replacement_exits_the_view_a_push_hands_over_to(self):
        """The replacement exited the view it collected, which did nothing once
        its push landed, and the new view joined the pushed one past the limit."""

        class _Panel(RenderableLayoutView):
            instance_limit = 1
            instance_policy = "replace"
            replace_policy = "disable"

        class _Next(RenderableLayoutView):
            pass

        store = get_store()
        old = await _seam_live(_Panel)
        release = self._blocked_edit(old)
        old.add_item(ActionRow(StatefulButton(label="x", custom_id="x")))
        refreshing = asyncio.create_task(old.refresh())
        await _seam_ticks()
        pushing = asyncio.create_task(
            old.push(_Next, interaction=_make_interaction(user_id=1, guild_id=100))
        )
        await _seam_ticks()

        new = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        sending = asyncio.create_task(new.send())
        await _seam_ticks()
        release.set()
        await refreshing
        pushed = await pushing
        await sending

        assert old._successor is pushed
        assert pushed._torn_down()
        assert [v for v in store._active_views.values() if not v.is_finished()] == [new]

    async def test_a_parent_cascade_exits_the_view_a_childs_later_push_hands_over_to(self):
        """The cascade followed the child's first hand-off, then exited a view
        whose own push landed during that exit, and missed its destination."""

        class _Second(RenderableLayoutView):
            pass

        class _Third(RenderableLayoutView):
            pass

        parent = await _seam_live()
        child = await _seam_live(parent=parent)
        release = self._blocked_edit(child)
        child.add_item(ActionRow(StatefulButton(label="x", custom_id="x")))
        refreshing = asyncio.create_task(child.refresh())
        await _seam_ticks()
        pushing = asyncio.create_task(
            child.push(_Second, interaction=_make_interaction(user_id=1, guild_id=100))
        )
        await _seam_ticks()
        chained = {}
        third_nav = _make_interaction(user_id=1, guild_id=100)
        third_gate = asyncio.Event()

        async def third_edit(**kwargs):
            await third_gate.wait()
            third_nav.response.is_done.return_value = True

        third_nav.response.edit_message = AsyncMock(side_effect=third_edit)

        async def push_again():
            # Queued on the child's settle ahead of the cascade, so the view
            # it lands on is already navigating when the cascade reaches it,
            # and still is while the cascade's exit of it waits.
            await child._navigation_settled.wait()
            chained["third"] = await child._successor.push(_Third, interaction=third_nav)

        again = asyncio.create_task(push_again())
        await _seam_ticks()
        exiting = asyncio.create_task(parent.exit())
        await _seam_ticks()
        release.set()
        await refreshing
        second = await pushing
        await _seam_ticks(20)

        assert second._away_for_navigation
        assert not exiting.done()
        third_gate.set()
        await again
        await exiting

        assert chained["third"]._torn_down()

    async def test_a_child_sent_while_its_parent_pushes_attaches_to_the_new_view(self):
        """It attached to the parent that had just handed its message over, a
        view that never exits again, so nothing closed it."""
        loading = asyncio.Event()

        class _Child(RenderableLayoutView):
            async def on_load(self):
                await loading.wait()

        class _Dst(RenderableLayoutView):
            pass

        parent = await _seam_live()
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100), parent=parent)
        sending = asyncio.create_task(child.send())
        await _seam_ticks()
        dest = await parent.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))
        assert child.parent is dest
        loading.set()
        await sending

        assert child._attached_to is dest
        await dest.exit()
        assert child._torn_down()

    async def test_a_parent_that_pushed_is_checked_for_cycles_before_the_send(self):
        """The check read the parent that had handed its message over, whose
        chain was empty, and the cycle surfaced after the message went out."""
        loading = asyncio.Event()

        class _Child(RenderableLayoutView):
            async def on_load(self):
                await loading.wait()

        class _Dst(RenderableLayoutView):
            pass

        parent = await _seam_live()
        interaction = _make_interaction(user_id=1, guild_id=100)
        child = _Child(interaction=interaction, parent=parent)
        sending = asyncio.create_task(child.send())
        await _seam_ticks()
        dest = await parent.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))
        child.attach_child(dest)
        loading.set()

        with pytest.raises(ValueError, match="Circular attachment"):
            await sending
        interaction.response.send_message.assert_not_awaited()

    async def test_attaching_a_child_that_navigated_away_attaches_the_view_it_became(self):
        class _Dst(RenderableLayoutView):
            pass

        parent = await _seam_live()
        child = await _seam_live()
        moved = await child.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))

        parent.attach_child(child)

        assert moved.parent is parent
        await parent.exit()
        assert moved._torn_down()

    async def test_exit_on_a_view_that_navigated_away_leaves_the_new_view(self):
        """A caller holding the old view refers to the view that handed its message on."""

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live()
        message = source._message
        dest = await source.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))

        await source.exit(delete_message=True)

        assert not dest.is_finished()
        message.delete.assert_not_awaited()

    async def test_replace_on_a_view_that_navigated_away_raises(self):
        """The screen on the message was another view's: replacing from the
        old one closed nothing and handed back a view whose send posted a
        second live panel beside it."""

        class _Dst(RenderableLayoutView):
            pass

        class _Other(RenderableLayoutView):
            pass

        source = await _seam_live()
        dest = await source.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))

        with pytest.raises(RuntimeError, match="already handed its panel on"):
            await source.replace(_Other, _make_interaction(user_id=1, guild_id=100))
        assert not dest.is_finished()

    async def test_a_view_closed_by_its_timeout_can_still_be_replaced(self):
        """An on_timeout() override swaps the panel for a new message this way."""

        class _Other(RenderableLayoutView):
            pass

        replaced = []

        class _Swaps(RenderableLayoutView):
            async def on_timeout(self):
                await super().on_timeout()
                replaced.append(
                    await self.replace(_Other, _make_interaction(user_id=1, guild_id=100))
                )

        source = await _seam_live(_Swaps)
        source._dispatch_timeout()
        await until(lambda: replaced)

        assert isinstance(replaced[0], _Other)

    @pytest.mark.parametrize("call", ["replace", "push", "send"])
    async def test_the_view_a_failed_push_returned_refuses_to_take_the_panel(self, call):
        """replace() from it posted a second live panel while the view the push
        started from was still live on the message."""

        class _Dst(RenderableLayoutView):
            pass

        class _Other(RenderableLayoutView):
            pass

        source = await _seam_live()
        dest = await source.push(
            _Dst, interaction=TestNavigationAtItsEdges._refusing_nav(_Dst, source._message)
        )
        assert source.current_view is source

        with pytest.raises(RuntimeError, match=r"failed push\(\) or pop\(\) returned"):
            if call == "replace":
                await dest.replace(_Other, _make_interaction(user_id=1, guild_id=100))
            elif call == "push":
                await dest.push(_Other, interaction=_make_interaction(user_id=1, guild_id=100))
            else:
                await dest.send()
        assert not source.is_finished()

    async def test_current_view_follows_each_hand_off(self):
        class _Mid(RenderableLayoutView):
            pass

        class _Top(RenderableLayoutView):
            pass

        root = await _seam_live()
        assert root.current_view is root
        mid = await root.push(_Mid, interaction=_make_interaction(user_id=1, guild_id=100))
        top = await mid.push(_Top, interaction=_make_interaction(user_id=1, guild_id=100))

        assert root.current_view is top
        assert mid.current_view is top
        assert top.current_view is top

    async def test_a_kept_reference_closes_the_panel_through_current_view(self):
        class _Dst(RenderableLayoutView):
            pass

        panel = await _seam_live()
        dest = await panel.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))

        await panel.current_view.exit()

        assert dest._torn_down()

    @staticmethod
    def _handed_over_warnings(caplog):
        return [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui") and "handed its panel on" in r.getMessage()
        ]

    @pytest.mark.parametrize(
        "call",
        ["refresh", "reload", "load", "exit", "exit_children"],
    )
    async def test_a_call_that_does_nothing_on_a_kept_reference_says_so(self, call, caplog):
        """A cog holding the panel it sent found its calls did nothing once a
        user clicked into another screen, and nothing said why."""

        class _Dst(RenderableLayoutView):
            pass

        panel = await _seam_live()
        await panel.push(_Dst, interaction=_make_interaction(user_id=1, guild_id=100))

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            await getattr(panel, call)()
            await getattr(panel, call)()

        warnings = self._handed_over_warnings(caplog)
        assert len(warnings) == 1
        assert f"{call}()" in warnings[0] and "_Dst" in warnings[0]
        assert "current_view" in warnings[0]

    async def test_an_exit_whose_push_lands_while_it_waits_says_so(self, caplog):
        """The exit waited out the push, found the panel handed on, and did
        nothing without a word."""

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live()
        message = source._message
        gate = asyncio.Event()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def edit_original(**kwargs):
            await gate.wait()
            return message

        nav.edit_original_response = AsyncMock(side_effect=edit_original)
        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await _seam_ticks()
        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            closing = asyncio.create_task(source.exit())
            await _seam_ticks()
            gate.set()
            dest = await pushing
            await closing

        assert not dest.is_finished()
        warnings = self._handed_over_warnings(caplog)
        assert len(warnings) == 1 and "exit()" in warnings[0]

    async def test_a_view_that_has_not_navigated_does_not_warn(self, caplog):
        panel = await _seam_live()

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            await panel.refresh()
            await panel.exit()

        assert self._handed_over_warnings(caplog) == []

    async def test_a_timeout_during_a_push_leaves_the_new_view(self, caplog):
        """The click and the expiry raced; the click opened the new view, which
        has a timeout of its own, and the old view's on_timeout must not close
        it."""
        expired = asyncio.Event()

        class _Expiring(RenderableLayoutView):
            async def on_timeout(self):
                await self.exit(delete_message=True)
                expired.set()

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live(_Expiring)
        message = source._message
        gate = asyncio.Event()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def edit_original(**kwargs):
            await gate.wait()
            return message

        nav.edit_original_response = AsyncMock(side_effect=edit_original)
        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await _seam_ticks()
        source._dispatch_timeout()
        await _seam_ticks()
        gate.set()
        dest = await pushing
        await expired.wait()

        assert not dest.is_finished()
        message.delete.assert_not_awaited()
        # The timeout belonged to the view it closed; nothing to warn about.
        assert self._handed_over_warnings(caplog) == []

    @staticmethod
    async def _timeout_racing_a_push(expiring_cls):
        """Time ``expiring_cls`` out while a push from it is landing."""

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live(expiring_cls)
        gate = asyncio.Event()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def edit_original(**kwargs):
            await gate.wait()
            return source._message

        nav.edit_original_response = AsyncMock(side_effect=edit_original)
        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await _seam_ticks()
        source._dispatch_timeout()
        await _seam_ticks()
        gate.set()
        dest = await pushing
        assert source.current_view is dest
        return source, dest

    @pytest.mark.parametrize("call", ["exit", "refresh", "reload", "exit_children"])
    async def test_a_timeout_that_acts_after_the_push_landed_does_not_warn(self, call, caplog):
        """An on_timeout() override that awaits before acting reaches the view
        after the push has landed; what it does belongs to the timed-out view,
        and pointing it at current_view would act on the user's new screen."""
        may_act = asyncio.Event()
        acted = asyncio.Event()

        class _Expiring(RenderableLayoutView):
            async def on_timeout(self):
                await may_act.wait()
                if call == "exit":
                    await self.exit(delete_message=True)
                else:
                    await getattr(self, call)()
                acted.set()

        source, dest = await self._timeout_racing_a_push(_Expiring)

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            may_act.set()
            await asyncio.wait_for(acted.wait(), 5)

        assert not dest.is_finished()
        assert self._handed_over_warnings(caplog) == []

    async def test_replace_from_a_timeout_that_raced_a_push_names_the_race(self):
        refused = []
        may_replace = asyncio.Event()

        class _Other(RenderableLayoutView):
            pass

        class _Expiring(RenderableLayoutView):
            async def on_timeout(self):
                await may_replace.wait()
                try:
                    await self.replace(_Other, _make_interaction(user_id=1, guild_id=100))
                except RuntimeError as error:
                    refused.append(str(error))

        source, dest = await self._timeout_racing_a_push(_Expiring)
        may_replace.set()
        await until(lambda: refused)

        assert "keeps its own timeout" in refused[0]
        assert not dest.is_finished()

    async def test_a_timeout_that_exits_after_the_push_landed_does_not_warn(self, caplog):
        """An on_timeout() override that awaits before its exit() reaches it
        after the push has landed; that exit belongs to the timed-out view."""
        may_exit = asyncio.Event()
        exited = asyncio.Event()

        class _Expiring(RenderableLayoutView):
            async def on_timeout(self):
                await may_exit.wait()
                await self.exit(delete_message=True)
                exited.set()

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live(_Expiring)
        gate = asyncio.Event()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def edit_original(**kwargs):
            await gate.wait()
            return source._message

        nav.edit_original_response = AsyncMock(side_effect=edit_original)
        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await _seam_ticks()
        source._dispatch_timeout()
        await _seam_ticks()
        gate.set()
        dest = await pushing
        assert source.current_view is dest

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            may_exit.set()
            await asyncio.wait_for(exited.wait(), 5)

        assert not dest.is_finished()
        assert self._handed_over_warnings(caplog) == []


class TestNavigationAtItsEdges:
    """Where a push or pop meets instance limits, attachments, the refresh
    handoff, timeouts, and teardown."""

    @staticmethod
    def _refusing_nav(destination_cls, message):
        """A push whose every edit endpoint refuses ``destination_cls``."""
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=_seam_error())

        async def channel_edit(**kwargs):
            if isinstance(kwargs.get("view"), destination_cls):
                raise _seam_error()
            return message

        message.edit = AsyncMock(side_effect=channel_edit)
        return nav

    @staticmethod
    def _stalling_nav():
        """A push whose request leaves and never answers, released by hand."""
        started, release = asyncio.Event(), asyncio.Event()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def stall(**kwargs):
            started.set()
            await release.wait()
            raise asyncio.TimeoutError()

        nav.edit_original_response = AsyncMock(side_effect=stall)
        return nav, started, release

    @staticmethod
    def _limited(limit):
        class _Root(RenderableLayoutView):
            instance_limit = limit
            instance_policy = "replace"
            instance_scope = "user_guild"

        return _Root

    async def test_a_push_in_flight_counts_as_one_instance(self):
        """The source and its arriving destination were both counted, so a
        second send under a limit of two replaced the panel mid-push."""

        class _Child(RenderableLayoutView):
            pass

        root_cls = self._limited(2)
        store = get_store()
        root = await _seam_live(root_cls)
        gate = asyncio.Event()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def edit_original(**kwargs):
            await gate.wait()
            return root._message

        nav.edit_original_response = AsyncMock(side_effect=edit_original)
        pushing = asyncio.create_task(root.push(_Child, interaction=nav))
        await _seam_ticks()

        assert root_cls.check_instance_available(user_id=1, guild_id=100)
        second = root_cls(interaction=_make_interaction(user_id=1, guild_id=100))
        await second.send()
        gate.set()
        child = await pushing

        live = {id(v) for v in store._active_views.values() if not v.is_finished()}
        assert live == {id(child), id(second)}

    async def test_a_source_that_handed_over_frees_its_slot_before_its_removal(self):
        """Between the commit and the source's removal, both were counted."""

        class _Child(RenderableLayoutView):
            pass

        root_cls = self._limited(2)
        store = get_store()
        root = await _seam_live(root_cls)
        held, release = asyncio.Event(), asyncio.Event()

        async def slow_removal(action, state, next_fn):
            if action["type"] == "VIEW_DESTROYED":
                held.set()
                await release.wait()
            return await next_fn(action, state)

        store._add_middleware(slow_removal)
        try:
            pushing = asyncio.create_task(
                root.push(_Child, interaction=_make_interaction(user_id=1, guild_id=100))
            )
            await held.wait()
            available = root_cls.check_instance_available(user_id=1, guild_id=100)
            release.set()
            await pushing
        finally:
            store._remove_middleware(slow_removal)

        assert available

    async def test_a_refused_cross_version_push_leaves_the_instance_usable(self):
        """The instance was marked arriving before the version check raised,
        and the rollback then stopped and unsubscribed it."""
        from cascadeui.views.view import StatefulView

        class _V1(StatefulView):
            pass

        source = await _seam_live()
        instance = _V1(interaction=_make_interaction(user_id=1, guild_id=100))

        with pytest.raises(TypeError, match="Cannot push/pop between"):
            await source.push(instance, interaction=_make_interaction(user_id=1, guild_id=100))

        assert not instance.is_finished()
        assert not instance._torn_down()
        assert instance._arriving_from is None

    async def test_a_parent_exit_from_a_childs_navigation_is_refused_before_closing(self):
        """The exit began closing the parent, then the cascade raised on the
        child, leaving the parent closed but still registered."""
        parent = None

        class _Dest(RenderableLayoutView):
            async def on_load(self):
                await parent.exit()

        parent = await _seam_live()
        child = await _seam_live(parent=parent)

        with pytest.raises(
            RuntimeError, match=r"A parent's exit\(\) on .* inside the push\(\) or pop\(\) from it"
        ):
            await child.push(_Dest, interaction=_make_interaction(user_id=1, guild_id=100))

        assert not parent._closed()
        assert not parent._torn_down()
        assert child._attached_to is parent
        await parent.exit()
        assert child._torn_down()

    async def test_an_armed_menu_redrawn_after_an_unknown_push_keeps_its_refresh_button(self):
        """The redraw ran the menu's nav_rebuild, which rebuilt the tree over
        the refresh button, and an armed view takes no render to put it back."""
        from discord.ui import Button

        from cascadeui import MenuLayoutView

        class _Dst(RenderableLayoutView):
            pass

        class _Menu(MenuLayoutView):
            pass

        def labels(view):
            return [b.label for b in view.walk_children() if isinstance(b, Button)]

        menu = _Menu(
            interaction=_make_interaction(user_id=1, guild_id=100),
            categories=[{"label": "Settings", "view": _Dst}],
        )
        await menu.send(ephemeral=True)
        message = menu._message
        await menu._arm_refresh_button()
        armed = labels(menu)
        shipped = []

        async def edit(**kwargs):
            shipped.append(labels(kwargs["view"]))
            return message

        message.edit = AsyncMock(side_effect=edit)
        nav, started, release = self._stalling_nav()
        pushing = asyncio.create_task(menu.push(_Dst, interaction=nav))
        await started.wait()
        release.set()
        await pushing
        await menu.task_manager.wait_tasks(menu.id)

        assert shipped == [armed]
        assert labels(menu) == armed

    async def test_a_hand_off_that_would_close_a_cycle_rolls_the_push_back(self):
        """The commit's carry raised after the edit landed and left the source
        away for good, so every later exit on it waited forever."""

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live()
        child = await _seam_live(parent=source)
        dest = _Dst(interaction=_make_interaction(user_id=1, guild_id=100))
        child.attach_child(dest)
        shipped = []

        with pytest.raises(ValueError, match="Circular attachment"):
            await source.push(dest, interaction=_seam_landing_nav(shipped))

        assert shipped == []
        assert not source._away_for_navigation
        await asyncio.wait_for(source.exit(delete_message=False), 1)

    async def test_a_source_that_timed_out_during_an_unknown_push_redraws_through_its_freeze(self):
        """With nothing to disable, the freeze found the tree equal to the
        last render and shipped nothing, leaving the discarded view up."""

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live()
        message = source._message
        shipped = []

        async def channel_edit(**kwargs):
            shipped.append(type(kwargs.get("view")).__name__)
            return message

        message.edit = AsyncMock(side_effect=channel_edit)
        nav, started, release = self._stalling_nav()
        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await started.wait()
        source._dispatch_timeout()
        release.set()
        await pushing
        expiring = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == f"discord-ui-view-timeout-{source.id}"
        ]
        await asyncio.gather(*expiring)

        assert shipped == ["RenderableLayoutView"]

    async def test_a_companion_panel_opened_by_a_discarded_destination_closes_with_it(self):
        companions = []

        class _Companion(RenderableLayoutView):
            pass

        class _Dst(RenderableLayoutView):
            async def on_load(self):
                companion = _Companion(
                    interaction=_make_interaction(user_id=1, guild_id=100), parent=self
                )
                await companion.send()
                companions.append(companion)

        source = await _seam_live()
        dest = await source.push(_Dst, interaction=self._refusing_nav(_Dst, source._message))

        assert dest.is_finished()
        assert companions and companions[0]._torn_down()

    async def test_wait_returns_once_the_exit_has_closed_the_children(self):
        """exit() stopped the view before closing its children, so wait()
        returned while the view still held its instance slot."""
        closing, release = asyncio.Event(), asyncio.Event()

        class _SlowChild(RenderableLayoutView):
            async def exit(self, delete_message=None):
                closing.set()
                await release.wait()
                return await super().exit(delete_message)

        parent_cls = self._limited(1)
        parent = await _seam_live(parent_cls)
        child = _SlowChild(interaction=_make_interaction(user_id=1, guild_id=100), parent=parent)
        await child.send()
        seen = []

        async def waiter():
            await parent.wait()
            seen.append(parent_cls.check_instance_available(user_id=1, guild_id=100))

        waiting = asyncio.create_task(waiter())
        exiting = asyncio.create_task(parent.exit())
        await closing.wait()
        await _seam_ticks()

        assert seen == []
        release.set()
        await exiting
        await waiting
        assert seen == [True]

    @pytest.mark.parametrize("serialized", [True, False], ids=["serialized", "unserialized"])
    async def test_a_click_while_exit_closes_the_children_is_answered_and_dropped(self, serialized):
        closing, release = asyncio.Event(), asyncio.Event()

        class _SlowChild(RenderableLayoutView):
            async def exit(self, delete_message=None):
                closing.set()
                await release.wait()
                return await super().exit(delete_message)

        ran = []

        async def go(interaction):
            ran.append(interaction)

        class _Parent(RenderableLayoutView):
            serialize_interactions = serialized

        parent = await _seam_live(_Parent)
        button = StatefulButton(label="Go", custom_id="go", callback=go)
        parent.add_item(ActionRow(button))
        child = _SlowChild(interaction=_make_interaction(user_id=1, guild_id=100), parent=parent)
        await child.send()
        exiting = asyncio.create_task(parent.exit())
        await closing.wait()

        click = _make_interaction(user_id=1, guild_id=100, message=parent._message)
        await parent._scheduled_task(button, click)
        release.set()
        await exiting

        assert ran == []
        assert click.response.is_done()

    async def test_a_click_queued_behind_the_lock_while_exit_begins_is_dropped(self):
        """The click passed the first check before the exit began, then took
        the interaction lock while the view was closing but not yet stopped."""
        closing, release, first_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class _SlowChild(RenderableLayoutView):
            async def exit(self, delete_message=None):
                closing.set()
                await release.wait()
                return await super().exit(delete_message)

        ran = []

        async def go(interaction):
            ran.append(interaction)
            if len(ran) == 1:
                await first_gate.wait()

        parent = await _seam_live()
        button = StatefulButton(label="Go", custom_id="go", callback=go)
        parent.add_item(ActionRow(button))
        child = _SlowChild(interaction=_make_interaction(user_id=1, guild_id=100), parent=parent)
        await child.send()

        def click():
            return _make_interaction(user_id=1, guild_id=100, message=parent._message)

        first = asyncio.create_task(parent._scheduled_task(button, click()))
        await _seam_ticks()
        second = asyncio.create_task(parent._scheduled_task(button, click()))
        await _seam_ticks()
        exiting = asyncio.create_task(parent.exit())
        await closing.wait()
        first_gate.set()
        await first
        await second
        release.set()
        await exiting

        assert len(ran) == 1

    async def test_a_discarded_destination_leaves_no_undo_entry(self):
        class _Dst(RenderableLayoutView):
            enable_undo = True

        source = await _seam_live()
        dest = await source.push(_Dst, interaction=self._refusing_nav(_Dst, source._message))

        assert dest.is_finished()
        assert dest.id not in get_store()._undo_enabled_views

    async def test_a_listening_destination_pushed_without_an_interaction_renders_loaded(self):
        """on_load() never ran, and the destination's own render shipped its
        unloaded tree onto the message."""
        from discord.ui import TextDisplay

        class _Dst(RenderableLayoutView):
            subscribed_actions = None

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.rows = None

            async def on_load(self):
                self.rows = ["loaded"]
                self.build_ui()

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay(f"rows={self.rows}"))

        source = await _seam_live()
        source.interaction = None
        texts = []

        async def channel_edit(**kwargs):
            view = kwargs["view"]
            texts.append([c.content for c in view.walk_children() if isinstance(c, TextDisplay)])
            return source._message

        source._message.edit = AsyncMock(side_effect=channel_edit)
        dest = await source.push(_Dst)
        await dest.task_manager.wait_tasks(dest.id)
        await get_store()._flush_notifications()

        assert texts == [["rows=['loaded']"]]

    async def test_a_replace_destination_sent_under_its_limit_is_not_replaced_by_itself(self):
        """replace() registers its destination before the caller sends it, and
        the send counted the view against itself and exited it."""

        class _Src(StatefulView):
            pass

        class _Dest(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        source = _Src(interaction=_make_interaction())
        await source.send()
        dest = await source.replace(_Dest)
        dest._message = MagicMock()
        await dest.send()

        assert not dest.is_finished()

    async def test_pushing_a_views_own_child_clears_the_old_link(self):
        """The carry skipped the link, so the new view kept naming the view it
        replaced as its parent."""

        class _Dst(RenderableLayoutView):
            pass

        source = await _seam_live()
        dest = _Dst(interaction=_make_interaction(user_id=1, guild_id=100))
        source.attach_child(dest)

        pushed = await source.push(dest, interaction=_make_interaction(user_id=1, guild_id=100))

        assert pushed is dest
        assert dest.parent is None
        assert source._attached_children == []

    async def test_an_exit_cut_off_before_the_view_stops_leaves_it_usable(self):
        """exit() marked the view closing and was cancelled while closing its
        children, and every later click was answered and dropped."""
        deleting, release = asyncio.Event(), asyncio.Event()
        parent = await _seam_live()
        child = await _seam_live(parent=parent)

        async def slow_delete():
            deleting.set()
            await release.wait()

        child._message.delete = AsyncMock(side_effect=slow_delete)
        ran = []

        async def go(interaction):
            ran.append(interaction)

        button = StatefulButton(label="Go", custom_id="go", callback=go)
        parent.add_item(ActionRow(button))
        exiting = asyncio.create_task(parent.exit())
        await deleting.wait()
        exiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exiting

        assert not parent._closed()
        click = _make_interaction(user_id=1, guild_id=100, message=parent._message)
        await parent._scheduled_task(button, click)
        assert len(ran) == 1

    async def test_a_link_the_new_views_on_load_makes_is_checked_before_the_edit(self):
        """The hand-off was checked before on_load() ran, and a cycle on_load()
        made raised at the commit, leaving the source away for good."""
        parent = None

        class _Dst(RenderableLayoutView):
            async def on_load(self):
                self.attach_child(parent)

        parent = await _seam_live()
        source = await _seam_live(parent=parent)
        shipped = []

        with pytest.raises(ValueError, match="Circular attachment"):
            await source.push(_Dst, interaction=_seam_landing_nav(shipped))

        assert shipped == []
        assert not source._away_for_navigation
        await asyncio.wait_for(source.exit(delete_message=False), 1)

    async def test_a_child_sent_as_its_parent_exits_closes_with_it(self):
        """It attached to the parent after the parent's cleanup had run, so
        nothing ever closed it."""
        loading = asyncio.Event()

        class _Child(RenderableLayoutView):
            async def on_load(self):
                await loading.wait()

        parent = await _seam_live()
        child = _Child(interaction=_make_interaction(user_id=1, guild_id=100), parent=parent)
        sending = asyncio.create_task(child.send())
        await _seam_ticks()
        await parent.exit()
        loading.set()
        await sending

        assert child._torn_down()
        assert child not in parent._attached_children

    @pytest.mark.parametrize("closer", ["timeout", "exit"])
    async def test_a_v1_source_closed_during_an_unknown_push_keeps_its_embed(self, closer):
        """The redraw the rollback owed never ran, and the teardown edit left
        the discarded view's embed under the frozen source."""

        class _Base(StatefulView):
            nav_rebuild = staticmethod(
                lambda view: {"embed": discord.Embed(title=type(view).__name__)}
            )

            def __init__(self, **kwargs):
                super().__init__(**kwargs)

                async def noop(interaction):
                    pass

                self.add_item(StatefulButton(label="x", custom_id="x", callback=noop))

        class _Src(_Base):
            pass

        class _Dst(_Base):
            pass

        source = _Src(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(embed=discord.Embed(title="_Src"))
        source._webhook_message = None
        message = source._message
        embeds = []

        async def channel_edit(**kwargs):
            if "embed" in kwargs:
                embeds.append(kwargs["embed"].title if kwargs["embed"] else None)
            return message

        message.edit = AsyncMock(side_effect=channel_edit)
        started, release = asyncio.Event(), asyncio.Event()
        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)

        async def stall(**kwargs):
            embeds.append("_Dst")  # Discord applied it; the answer never came
            started.set()
            await release.wait()
            raise asyncio.TimeoutError()

        nav.edit_original_response = AsyncMock(side_effect=stall)
        pushing = asyncio.create_task(source.push(_Dst, interaction=nav))
        await started.wait()
        if closer == "timeout":
            source._dispatch_timeout()
            closing = [
                t
                for t in asyncio.all_tasks()
                if t.get_name() == f"discord-ui-view-timeout-{source.id}"
            ]
        else:
            closing = [asyncio.create_task(source.exit())]
        await _seam_ticks()
        release.set()
        await pushing
        await asyncio.gather(*closing)
        await _seam_ticks(20)

        assert embeds[-1] == "_Src"

    async def test_a_views_own_message_kwarg_survives_a_pop(self):
        """``message`` was stripped from the replayed kwargs as if the library
        took it, and no view does, so a confirm prompt came back defaulted."""

        class _Confirm(RenderableLayoutView):
            def __init__(self, *, message="(default)", **kwargs):
                super().__init__(**kwargs)
                self.prompt = message

        class _Child(RenderableLayoutView):
            pass

        root = await _seam_live(_Confirm, message="Delete the league?")
        child = await root.push(_Child, interaction=_make_interaction(user_id=1, guild_id=100))
        back = await child.pop(interaction=_make_interaction(user_id=1, guild_id=100))

        assert back.prompt == "Delete the league?"


class _ButtonPanel(StatefulLayoutView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        async def noop(interaction):
            pass

        self.add_item(ActionRow(StatefulButton(label="x", custom_id="x", callback=noop)))


class TestReplaceClosesItsSource:
    """replace() closes the view it was called on as that view's exit() would.

    It used to stop the view and leave its message untouched, so the old
    panel kept buttons that answered nothing.
    """

    async def test_the_default_policy_freezes_the_old_message(self):
        source = await _seam_live(_ButtonPanel)
        message = source._message
        message.edit.reset_mock()

        await source.replace(RenderableLayoutView)

        message.delete.assert_not_awaited()
        shipped = message.edit.await_args.kwargs["view"]
        buttons = [i for i in shipped.walk_children() if isinstance(i, discord.ui.Button)]
        assert buttons and all(button.disabled for button in buttons)
        assert source._torn_down()

    async def test_exit_policy_delete_deletes_the_old_message(self):
        class _Deleting(_ButtonPanel):
            exit_policy = "delete"

        source = await _seam_live(_Deleting)
        message = source._message

        await source.replace(RenderableLayoutView)

        message.delete.assert_awaited_once()

    async def test_a_v1_source_has_its_buttons_removed(self):
        class _V1(StatefulView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

                async def noop(interaction):
                    pass

                self.add_item(StatefulButton(label="x", custom_id="x", callback=noop))

        source = _V1(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(embed=discord.Embed(title="Old"))
        message = source._message
        message.edit.reset_mock()

        await source.replace(RenderableLayoutView)

        assert message.edit.await_args.kwargs["view"] is None

    async def test_an_exit_override_runs(self):
        closed = []

        class _Custom(_ButtonPanel):
            async def exit(self, delete_message=None):
                closed.append(delete_message)
                return await super().exit(delete_message=delete_message)

        source = await _seam_live(_Custom)

        await source.replace(RenderableLayoutView)

        assert closed == [None]

    async def test_a_failed_replace_leaves_its_source_working(self):
        """The source was torn down before the swap, so a swap that failed
        left a stopped view registered on a message whose buttons answered
        nothing."""
        store = get_store()
        source = await _seam_live(_ButtonPanel)
        message = source._message
        message.edit.reset_mock()

        async def failing_registration(action, state, next_fn):
            if action["type"] == "VIEW_CREATED":
                raise RuntimeError("registration failed")
            return await next_fn(action, state)

        store._add_middleware(failing_registration)
        try:
            with pytest.raises(RuntimeError, match="registration failed"):
                await source.replace(RenderableLayoutView)
        finally:
            store._remove_middleware(failing_registration)

        assert not source.is_finished() and not source._closed()
        assert source.id in store._active_views
        assert source.id in store.subscribers
        message.edit.assert_not_awaited()
        message.delete.assert_not_awaited()

    async def test_a_push_during_the_replace_is_refused(self):
        """A push that landed while the new view's registration awaited left
        the pushed view and the replacement both live."""

        class _Next(RenderableLayoutView):
            pass

        store = get_store()
        source = await _seam_live(_ButtonPanel)
        gate, entered = asyncio.Event(), asyncio.Event()

        async def slow_registration(action, state, next_fn):
            if action["type"] == "VIEW_CREATED" and action["payload"]["view_type"] == "_Next":
                entered.set()
                await gate.wait()
            return await next_fn(action, state)

        store._add_middleware(slow_registration)
        try:
            replacing = asyncio.create_task(source.replace(_Next))
            await asyncio.wait_for(entered.wait(), 1)
            with pytest.raises(RuntimeError, match="closing"):
                await source.push(
                    RenderableLayoutView, interaction=_make_interaction(user_id=1, guild_id=100)
                )
            gate.set()
            await replacing
        finally:
            store._remove_middleware(slow_registration)

    async def test_a_failed_replace_renders_what_its_source_declined(self):
        """The source declines state renders while the replace closes it; one
        that stays live after the swap fails must still show that state."""

        class _Next(RenderableLayoutView):
            pass

        store = get_store()
        renders = []

        class _Watching(_ButtonPanel):
            # Only the tick: the aborted batch's own flush would render a view
            # listening to everything, which hides whether the replay runs.
            subscribed_actions = {"REPLACE_TICK"}

            async def on_state_changed(self, state):
                renders.append(state)

        source = await _seam_live(_Watching)
        renders.clear()
        gate, entered = asyncio.Event(), asyncio.Event()

        async def failing_registration(action, state, next_fn):
            if action["type"] == "VIEW_CREATED" and action["payload"]["view_type"] == "_Next":
                entered.set()
                await gate.wait()
                raise RuntimeError("registration failed")
            return await next_fn(action, state)

        store._add_middleware(failing_registration)
        try:
            replacing = asyncio.create_task(source.replace(_Next))
            await asyncio.wait_for(entered.wait(), 1)
            await store.dispatch("REPLACE_TICK", {})
            await store._flush_notifications()
            assert renders == []  # declined while the replace was closing it

            gate.set()
            with pytest.raises(RuntimeError, match="registration failed"):
                await replacing
        finally:
            store._remove_middleware(failing_registration)
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(renders) == 1


class TestNavigationAndSendTakeTurns:
    """A push or pop and a send of the same view never run side by side.

    Clicks reach a view once its message is posted, while its send is still
    fetching the message back and attaching the view to its parent. A push
    then moved the message to a new view under the send, which reported the
    torn-down source as sent; a send beside a push in flight posted a view the
    push had already torn down.
    """

    @staticmethod
    def _interaction_fetching_slowly(message_id):
        """An interaction whose sent message is fetched back only once ``gate`` is set."""
        interaction = _make_interaction()
        sent = MagicMock(spec=discord.InteractionMessage)
        sent.id = message_id
        fetched = MagicMock(id=message_id, channel=MagicMock(id=888))
        fetched.edit = AsyncMock(return_value=fetched)
        fetched.delete = AsyncMock()
        fetching, gate = asyncio.Event(), asyncio.Event()

        async def fetch_message(_):
            fetching.set()
            await gate.wait()
            return fetched

        sent.channel = MagicMock()
        sent.channel.fetch_message = fetch_message
        interaction.original_response = AsyncMock(return_value=sent)
        return interaction, fetching, gate

    async def test_a_push_while_the_view_is_still_being_sent_waits_for_the_send(self):
        parent = RenderableLayoutView(interaction=_make_interaction())
        await parent.send()
        interaction, fetching, gate = self._interaction_fetching_slowly(77)
        view = RenderableLayoutView(interaction=interaction, parent=parent)
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(fetching.wait(), 1)

        click = _make_interaction(message=MagicMock(id=77))
        pushing = asyncio.create_task(view.push(RenderableLayoutView, click))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not pushing.done()
        gate.set()

        # The send finished with the view live, then the push moved it on.
        assert await asyncio.wait_for(sending, 1) is not None
        destination = await asyncio.wait_for(pushing, 1)
        assert view._torn_down()
        assert destination._message.id == 77
        assert destination._attached_to is parent

    async def test_a_push_from_inside_the_views_own_send_raises(self):
        class Pushes(RenderableLayoutView):
            async def on_load(self):
                await self.push(RenderableLayoutView)

        view = Pushes(interaction=_make_interaction())

        with pytest.raises(RuntimeError, match="inside its own send"):
            await asyncio.wait_for(view.send(), 1)
        assert view._torn_down()

    async def test_a_send_while_the_views_push_is_in_flight_waits_for_it(self):
        loading, gate = asyncio.Event(), asyncio.Event()

        class Destination(RenderableLayoutView):
            async def on_load(self):
                loading.set()
                await gate.wait()

        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send()
        click = _make_interaction(message=MagicMock(id=view._message.id))
        pushing = asyncio.create_task(view.push(Destination, click))
        await asyncio.wait_for(loading.wait(), 1)
        resend = _make_interaction()
        view.interaction = resend
        sending = asyncio.create_task(view.send())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not sending.done()
        gate.set()
        destination = await asyncio.wait_for(pushing, 1)

        # The push landed, so the view it left posts nothing.
        with pytest.raises(RuntimeError, match="handed its panel on"):
            await asyncio.wait_for(sending, 1)
        resend.response.send_message.assert_not_awaited()
        assert not destination._torn_down()


class TestNavigationTakesAFreshInstance:
    """push() and replace() refuse a view instance that is not fresh.

    A cached instance pushed a second time was a view the first push had
    handed back and torn down: the navigation moved the message to it and
    tore the panel down, leaving nothing on the message that answered.
    """

    async def test_pushing_a_cached_instance_a_second_time_raises(self):
        hub = RenderableLayoutView(interaction=_make_interaction())
        await hub.send()
        message_id = hub._message.id
        cached = RenderableLayoutView(interaction=_make_interaction())
        await hub.push(cached, _make_interaction(message=MagicMock(id=message_id)))
        restored = await cached.pop(_make_interaction(message=MagicMock(id=message_id)))

        with pytest.raises(RuntimeError, match="already been sent, pushed, or closed"):
            await restored.push(cached, _make_interaction(message=MagicMock(id=message_id)))
        assert not restored._torn_down()
        assert restored._message is not None

    async def test_pushing_an_instance_already_sent_raises(self):
        hub = RenderableLayoutView(interaction=_make_interaction())
        await hub.send()
        sent = RenderableLayoutView(interaction=_make_interaction())
        await sent.send()

        with pytest.raises(RuntimeError, match="already been sent"):
            await hub.push(sent, _make_interaction(message=MagicMock(id=hub._message.id)))
        assert not hub._torn_down()
        assert not sent._torn_down()

    async def test_replacing_with_a_closed_instance_raises_and_keeps_the_view(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send()
        closed = RenderableLayoutView(interaction=_make_interaction())
        closed.stop()

        with pytest.raises(RuntimeError, match=r"replace\(\) was given"):
            await view.replace(closed)
        assert not view._torn_down()
        assert not view._closed()
