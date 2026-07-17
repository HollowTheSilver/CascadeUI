"""Tests for navigation reducers, view-local nav stack, and forward-transfer."""

import copy
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import ActionRow
from helpers import RenderableLayoutView
from helpers import make_interaction as _make_interaction

from cascadeui.components.base import StatefulButton
from cascadeui.state.actions import ActionCreators
from cascadeui.state.reducers import (
    reduce_navigation_pop,
    reduce_navigation_push,
    reduce_navigation_replace,
)
from cascadeui.state.singleton import get_store
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
        await root.push(_Sub, interaction=nav)

        # One round-trip: edit + ack together, no separate defer.
        nav.response.edit_message.assert_awaited_once()
        nav.response.defer.assert_not_called()

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
        await root.push(_Sub, interaction=nav)

        nav.response.edit_message.assert_not_called()
        nav.edit_original_response.assert_awaited_once()

    async def test_push_non_component_interaction_skips_fast_path(self):
        """A non-component interaction (e.g. a slash command) is ineligible for
        the edit_message fast path -- it silently no-ops there in discord.py, so
        navigation must fall to the deferred edit instead."""

        class _Root(StatefulView):
            pass

        class _Sub(StatefulView):
            async def on_state_changed(self, state):
                pass

        root = _Root(interaction=_make_interaction(user_id=1, guild_id=100))
        await root.send()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.type = discord.InteractionType.application_command
        nav.edit_original_response = AsyncMock(
            return_value=MagicMock(id=5, channel=MagicMock(id=6))
        )
        await root.push(_Sub, interaction=nav)

        nav.response.edit_message.assert_not_called()
        nav.edit_original_response.assert_awaited_once()

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

    async def test_clear_on_empty_back_handles_ack_race_v1(self):
        """_clear_on_empty_back's fast-path edit hits the same auto-defer ack
        race as the navigation fast path: edit_message can raise
        InteractionResponded (a sibling of HTTPException, not a subclass). The
        empty-stack clear must catch it and fall through to the deferred edit
        rather than escape to on_error."""
        view = StatefulView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=discord.InteractionResponded(MagicMock()))
        nav.edit_original_response = AsyncMock()

        await view._clear_on_empty_back(nav)  # must not raise

        # V1 fallback edits to view=None (strips buttons, keeps embed).
        nav.edit_original_response.assert_awaited_once_with(view=None)

    async def test_clear_on_empty_back_handles_ack_race_v2(self):
        """Same race on the V2 freeze path (edits view=self, not view=None)."""
        view = RenderableLayoutView(interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send()

        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=discord.InteractionResponded(MagicMock()))
        nav.edit_original_response = AsyncMock()

        await view._clear_on_empty_back(nav)  # must not raise

        # V2 fallback edits with the frozen view, not view=None (50006 guard).
        nav.edit_original_response.assert_awaited_once_with(view=view)


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
            {"id": "email", "label": "Email", "type": "string"},
            {"id": "name", "label": "Name", "type": "string"},
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
        fields = [{"id": "email", "label": "Email", "type": "string"}]
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
            fields=[{"id": "email", "label": "Email", "type": "string"}],
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

        interaction = self._nav_interaction(root._message.id)
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

        interaction = self._nav_interaction(root._message.id)
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

        interaction = self._nav_interaction(root._message.id)
        await child.pop(
            interaction=interaction,
            rebuild=lambda v: {"embed": discord.Embed(title="EXPLICIT")},
        )

        shipped = interaction.response.edit_message.await_args.kwargs
        assert shipped["embed"].title == "EXPLICIT"


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
        interaction = self._nav_interaction(view._message.id)
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
            fields=[{"id": "email", "label": "Email", "type": "string"}],
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
