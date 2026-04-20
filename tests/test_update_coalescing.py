"""Tests for update coalescing and ViewStore interaction preservation.

When multiple dispatches converge on the same subscriber view concurrently,
the second notification should be coalesced into the first rather than
producing concurrent on_state_changed / refresh / message.edit() calls.

The clear_items() override tests verify that old items keep a valid _view
reference so discord.py's ViewStore can still route interactions during the
async gap between build_ui() and message.edit().
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cascadeui.components.base import StatefulButton
from cascadeui.state.store import StateStore
from cascadeui.views.view import StatefulView
from cascadeui.views.layout import StatefulLayoutView


# // ========================================( Helpers )======================================== // #


def _make_view(store, *, cls=None):
    """Create a minimal StatefulView for testing."""
    klass = cls or StatefulView
    view = klass(user_id=100, guild_id=200, state_store=store)
    view._message = MagicMock(id=999)
    view._message.edit = AsyncMock()
    return view


# // ========================================( Coalescing )======================================== // #


class TestUpdateCoalescing:
    """Concurrent state notifications coalesce into a single on_state_changed call."""
    async def test_single_notification_runs_normally(self):
        store = StateStore()
        view = _make_view(store)
        calls = []

        async def _track(state):
            calls.append("update")

        view.on_state_changed = _track

        action = {"type": "TEST", "payload": {}, "source": None, "timestamp": "t"}
        await view._handle_state_notification(store.state, action)

        assert calls == ["update"]

    async def test_concurrent_notifications_coalesce(self):
        """Second notification returns immediately when the first is in progress."""
        store = StateStore()
        view = _make_view(store)
        call_count = 0
        barrier = asyncio.Event()

        async def _slow_update(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: wait for the second notification to arrive
                await barrier.wait()

        view.on_state_changed = _slow_update

        action = {"type": "TEST", "payload": {}, "source": None, "timestamp": "t"}

        # Start the first notification (will block at the barrier)
        task1 = asyncio.create_task(view._handle_state_notification(store.state, action))
        await asyncio.sleep(0)  # Let task1 acquire the lock

        # Fire the second notification while the first is running
        task2 = asyncio.create_task(view._handle_state_notification(store.state, action))
        await asyncio.sleep(0)  # Let task2 run and set _update_pending

        # Task2 should have returned immediately (pending flag set)
        assert task2.done()
        assert view._update_pending is True

        # Release the barrier so task1 can finish and re-run
        barrier.set()
        await task1

        # First call + one re-run from the pending flag = 2 total
        assert call_count == 2

    async def test_multiple_concurrent_notifications_coalesce_to_one_rerun(self):
        """Three concurrent notifications produce exactly 2 on_state_changed calls."""
        store = StateStore()
        view = _make_view(store)
        call_count = 0
        barrier = asyncio.Event()

        async def _slow_update(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await barrier.wait()

        view.on_state_changed = _slow_update

        action = {"type": "TEST", "payload": {}, "source": None, "timestamp": "t"}

        task1 = asyncio.create_task(view._handle_state_notification(store.state, action))
        await asyncio.sleep(0)

        # Fire two more while the first is running
        task2 = asyncio.create_task(view._handle_state_notification(store.state, action))
        task3 = asyncio.create_task(view._handle_state_notification(store.state, action))
        await asyncio.sleep(0)

        assert task2.done()
        assert task3.done()

        barrier.set()
        await task1

        # Still only 2 calls: the initial run + one coalesced re-run
        assert call_count == 2

    async def test_coalesced_rerun_uses_latest_state(self):
        """The re-run after coalescing reads the current store state, not a stale snapshot."""
        store = StateStore()
        view = _make_view(store)
        observed_states = []
        barrier = asyncio.Event()

        async def _track_state(state):
            observed_states.append(state.get("version"))
            if len(observed_states) == 1:
                await barrier.wait()

        view.on_state_changed = _track_state

        # Initial state
        store.state = {"version": 1}

        action = {"type": "TEST", "payload": {}, "source": None, "timestamp": "t"}
        task1 = asyncio.create_task(view._handle_state_notification(store.state, action))
        await asyncio.sleep(0)

        # Mutate state before the second notification
        store.state = {"version": 2}

        task2 = asyncio.create_task(view._handle_state_notification(store.state, action))
        await asyncio.sleep(0)

        barrier.set()
        await task1

        # First run saw version 1, re-run saw version 2 (latest)
        assert observed_states == [1, 2]

    async def test_no_coalescing_when_sequential(self):
        """Sequential notifications both run fully (no lock contention)."""
        store = StateStore()
        view = _make_view(store)
        call_count = 0

        async def _fast_update(state):
            nonlocal call_count
            call_count += 1

        view.on_state_changed = _fast_update

        action = {"type": "TEST", "payload": {}, "source": None, "timestamp": "t"}

        await view._handle_state_notification(store.state, action)
        await view._handle_state_notification(store.state, action)

        assert call_count == 2

    async def test_pending_flag_cleared_after_rerun(self):
        """After the coalesced re-run completes, the pending flag is False."""
        store = StateStore()
        view = _make_view(store)
        barrier = asyncio.Event()

        async def _wait_once(state):
            if not barrier.is_set():
                barrier_copy = asyncio.Event()
                barrier.set()
                await asyncio.sleep(0.01)

        view.on_state_changed = _wait_once

        action = {"type": "TEST", "payload": {}, "source": None, "timestamp": "t"}
        task1 = asyncio.create_task(view._handle_state_notification(store.state, action))
        await asyncio.sleep(0)

        task2 = asyncio.create_task(view._handle_state_notification(store.state, action))
        await asyncio.sleep(0)

        await task1

        assert view._update_pending is False
        assert not view._update_lock.locked()


# // ========================================( ViewStore Preservation )======================================== // #


class TestClearItemsViewPreservation:
    """Verify clear_items() preserves _view on old items after send()."""

    def test_preserves_view_on_v1_items_after_send(self):
        """V1 buttons keep _view after clear_items() when view has a message."""
        store = StateStore()
        view = _make_view(store)
        btn = StatefulButton(label="Click")
        view.add_item(btn)
        assert btn.view is view

        view.clear_items()

        # Old button's _view preserved because view has _message
        assert btn.view is view
        # But the button is no longer in the view's children
        assert btn not in view.children

    def test_no_preservation_before_send(self):
        """Before send() (_message is None), clear_items() nulls _view normally."""
        store = StateStore()
        view = StatefulView(user_id=100, guild_id=200, state_store=store)
        btn = StatefulButton(label="Click")
        view.add_item(btn)

        view.clear_items()

        assert btn.view is None

    def test_preserves_view_on_v2_nested_items(self):
        """V2 buttons nested in Container > ActionRow keep _view after clear_items()."""
        store = StateStore()
        view = StatefulLayoutView(user_id=100, guild_id=200, state_store=store)
        view._message = MagicMock(id=999)
        view._message.edit = AsyncMock()

        btn = StatefulButton(label="Regenerate")
        action_row = discord.ui.ActionRow(btn)
        container = discord.ui.Container(action_row)
        view.add_item(container)

        # Container propagates _update_view to all children
        assert btn.view is view

        view.clear_items()

        # Nested button still has valid _view
        assert btn.view is view

    def test_new_items_get_correct_view_after_rebuild(self):
        """After clear + add, new items have _view=self and old items are orphaned."""
        store = StateStore()
        view = _make_view(store)
        old_btn = StatefulButton(label="Old")
        view.add_item(old_btn)

        view.clear_items()
        new_btn = StatefulButton(label="New")
        view.add_item(new_btn)

        # Both point to the same view
        assert old_btn.view is view
        assert new_btn.view is view
        # Only new button is in children
        assert new_btn in view.children
        assert old_btn not in view.children

    def test_returns_self_for_fluent_chaining(self):
        """clear_items() returns the view instance for fluent chaining."""
        store = StateStore()
        view = _make_view(store)
        result = view.clear_items()
        assert result is view
