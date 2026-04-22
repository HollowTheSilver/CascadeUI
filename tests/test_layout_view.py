"""Tests for StatefulLayoutView (V2 base class)."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import ActionRow, LayoutView, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.components.base import StatefulButton
from cascadeui.state.singleton import get_store
from cascadeui.state.store import _CURRENT_INTERACTION
from cascadeui.views.base import _StatefulMixin, _view_class_registry
from cascadeui.views.layout import DisplayLayoutView, StatefulLayoutView


class TestStatefulLayoutViewInit:
    """Basic init and inheritance tests."""

    def test_is_subclass_of_layout_view(self):
        assert issubclass(StatefulLayoutView, LayoutView)

    def test_is_subclass_of_mixin(self):
        assert issubclass(StatefulLayoutView, _StatefulMixin)

    def test_init_with_required_kwargs(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.state_store is not None
        assert view.user_id == 100
        assert view.guild_id == 200
        # Default polarity: session_continuity is False, so the derived
        # session_id carries a per-instance UUID suffix after the user id.
        prefix = f"{StatefulLayoutView._class_session_key()}:user_100:"
        assert view.session_id.startswith(prefix)
        assert len(view.session_id) == len(prefix) + 8

    def test_subscribes_to_state_on_init(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        store = get_store()
        assert view.id in store.subscribers

    def test_default_subscribed_actions(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.subscribed_actions == set()

    def test_auto_defer_defaults(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.auto_defer is True
        assert view.auto_defer_delay == 2.5

    def test_owner_only_defaults(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.owner_only is True


class TestMakeExitButton:
    """``make_exit_button`` returns an unattached button; ``add_exit_button`` wraps it."""

    def test_returns_unattached_button(self):
        from cascadeui.components.base import StatefulButton

        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        btn = view.make_exit_button(label="Close")
        after = len(list(view.children))

        assert isinstance(btn, StatefulButton)
        assert btn.label == "Close"
        assert after == before  # not attached

    def test_add_exit_button_still_attaches(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        view.add_exit_button()
        after = len(list(view.children))
        assert after == before + 1  # ActionRow wrapper added


class TestStatefulLayoutViewSubclass:
    """Subclass registration and kwargs auto-capture."""

    def test_subclass_registered_in_view_class_registry(self):
        class _TestLayoutPanel(StatefulLayoutView):
            pass

        key = _TestLayoutPanel._class_session_key()
        assert key in _view_class_registry
        assert _view_class_registry[key] is _TestLayoutPanel

    def test_init_kwargs_auto_captured(self):
        class _CustomLayout(StatefulLayoutView):
            def __init__(self, *args, title="default", **kwargs):
                self.title = title
                super().__init__(*args, **kwargs)

        interaction = _make_interaction()
        view = _CustomLayout(interaction=interaction, title="Dashboard")

        assert view.title == "Dashboard"
        assert view._init_kwargs == {"title": "Dashboard"}

    def test_non_reconstructible_kwargs_excluded(self):
        class _AnotherLayout(StatefulLayoutView):
            def __init__(self, *args, label="x", **kwargs):
                self.label = label
                super().__init__(*args, **kwargs)

        interaction = _make_interaction()
        view = _AnotherLayout(interaction=interaction, label="test")

        # interaction is non-reconstructible, should be excluded
        assert "interaction" not in view._init_kwargs
        assert view._init_kwargs == {"label": "test"}


class TestStatefulLayoutViewDispatch:
    """State dispatch and batch tests."""

    async def test_dispatch_forwards_to_store(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        result = await view.dispatch("VIEW_UPDATED", {"view_id": view.id})
        assert result is not None

    async def test_batch_returns_store_batch(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        batch = view.batch()
        assert batch is not None

    async def test_scoped_state_empty_without_scope(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.scoped_state == {}


class TestStatefulLayoutViewSend:
    """Send method tests."""

    async def test_send_via_interaction(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        message = await view.send()

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args
        assert call_kwargs.kwargs["view"] is view

    async def test_send_registers_view(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        await view.send()

        assert view.id in store._active_views

    async def test_send_no_content_embed_params(self):
        """V2 send() has no content/embed/embeds params."""
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        # Only ephemeral is accepted
        message = await view.send(ephemeral=False)
        assert message is not None

    async def test_send_rollback_on_failure(self):
        interaction = _make_interaction()
        interaction.response.send_message = AsyncMock(side_effect=Exception("fail"))
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        with pytest.raises(Exception, match="fail"):
            await view.send()

        assert view.id not in store._active_views

    async def test_send_requires_context_or_interaction(self):
        view = StatefulLayoutView()

        with pytest.raises(RuntimeError, match="requires either"):
            await view.send()


class TestSeedInitialState:
    """The seed_initial_state hook fires after registration, before notification."""

    async def test_default_hook_is_no_op(self):
        # The default hook does nothing -- existing views ship unchanged.
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        message = await view.send()
        assert message is not None

    async def test_hook_receives_state_dict(self):
        interaction = _make_interaction()
        captured = {}

        class SeedingView(StatefulLayoutView):
            async def seed_initial_state(self, state):
                captured["state"] = state

        view = SeedingView(interaction=interaction)
        await view.send()

        assert "state" in captured
        assert isinstance(captured["state"], dict)
        assert "views" in captured["state"]

    async def test_hook_runs_after_register_view(self):
        # When the hook fires, the view is already in the active registry
        # so the slot it seeds can reference its own view_id safely.
        interaction = _make_interaction()
        store = get_store()
        observed = {}

        class SeedingView(StatefulLayoutView):
            async def seed_initial_state(self, state):
                observed["registered"] = self.id in store._active_views

        view = SeedingView(interaction=interaction)
        await view.send()

        assert observed["registered"] is True

    async def test_hook_can_dispatch_inside_send_batch(self):
        # Dispatches from inside the seed hook collapse into the batch's
        # BATCH_COMPLETE notification rather than firing as a separate
        # subscriber pass. Verified by checking the action fires without
        # error inside the send pipeline.
        interaction = _make_interaction()
        store = get_store()

        async def _seed_reducer(action, state):
            new = {**state}
            app = {**state.get("application", {})}
            app["seeded_value"] = action["payload"].get("value")
            new["application"] = app
            return new

        store._register_reducer("SEED_TEST_ACTION", _seed_reducer)

        class SeedingView(StatefulLayoutView):
            async def seed_initial_state(self, state):
                await self.dispatch("SEED_TEST_ACTION", {"value": "seeded"})

        try:
            view = SeedingView(interaction=interaction)
            await view.send()

            assert store.state["application"].get("seeded_value") == "seeded"
        finally:
            store._unregister_reducer("SEED_TEST_ACTION")

    async def test_hook_failure_propagates(self):
        # If the override raises, the send pipeline surfaces the error. No
        # silent swallowing -- a broken seed should break the send so the
        # subclass author sees the bug immediately.
        interaction = _make_interaction()

        class BrokenSeedView(StatefulLayoutView):
            async def seed_initial_state(self, state):
                raise RuntimeError("seed broke")

        view = BrokenSeedView(interaction=interaction)
        with pytest.raises(RuntimeError, match="seed broke"):
            await view.send()


class TestStatefulLayoutViewInteraction:
    """Interaction check and owner_only tests."""

    async def test_owner_only_rejects_other_user(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)

        other_interaction = _make_interaction(user_id=999)
        result = await view.interaction_check(other_interaction)

        assert result is False

    async def test_owner_only_allows_owner(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)

        same_interaction = _make_interaction(user_id=100)
        result = await view.interaction_check(same_interaction)

        assert result is True

    async def test_owner_only_disabled(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)
        view.owner_only = False

        other_interaction = _make_interaction(user_id=999)
        result = await view.interaction_check(other_interaction)

        assert result is True


class TestStatefulLayoutViewCleanup:
    """Exit and cleanup tests."""

    async def test_exit_unregisters_view(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()
        store._register_view(view)

        assert view.id in store._active_views

        await view.exit()

        assert view.id not in store._active_views

    async def test_exit_unsubscribes(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        assert view.id in store.subscribers

        await view.exit()

        assert view.id not in store.subscribers


class TestStableCustomIds:
    """``_stabilize_custom_ids`` rewrites auto-generated ids post-build.

    discord.py assigns a random hex custom_id to any Button/Select without
    an explicit ``custom_id=``. Every ``build_ui()`` rebuild produces
    fresh UUIDs, which causes the ViewStore dispatch table to churn and
    creates a race window where pending user clicks reference evicted
    entries. Stabilization rewrites auto-generated ids to deterministic
    anchors so repeat rebuilds produce identical dispatch keys.
    """

    def _make_view_with_build(self, build_fn):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        return _V(interaction=_make_interaction())

    def test_explicit_custom_id_preserved(self):
        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire", custom_id="user_chosen_id"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()

        btn = next(view.walk_children())
        # ActionRow first, button second
        button = [c for c in view.walk_children() if isinstance(c, StatefulButton)][0]
        assert button.custom_id == "user_chosen_id"

    def test_auto_generated_id_rewritten_with_content_anchor(self):
        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire"),
                    StatefulButton(label="Close"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()

        buttons = [c for c in view.walk_children() if isinstance(c, StatefulButton)]
        # Content-unique -> content-only id, anchored to view prefix.
        prefix = view.id[:8]
        assert buttons[0].custom_id.startswith(f"{prefix}:")
        assert "Fire" in buttons[0].custom_id
        assert "Close" in buttons[1].custom_id
        # Different buttons get different ids.
        assert buttons[0].custom_id != buttons[1].custom_id

    def test_repeat_build_produces_identical_ids(self):
        """The core promise: rebuild must not churn the dispatch table."""

        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire"),
                    StatefulButton(label="Close"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()
        first = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        view.build_ui()
        second = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        assert first == second

    def test_colliding_content_uses_position_anchor(self):
        """A 3x3 grid of identical-callback cells must disambiguate by position.

        Mirrors the TicTacToe grid pattern: many buttons share one callback
        family and an identical (empty) label. Each cell's id must be
        anchored to its tree coordinates so a label change on one cell
        does not shift the ids of the others.
        """

        def build(view):
            view.clear_items()
            for _ in range(3):
                view.add_item(
                    ActionRow(
                        StatefulButton(label=""),
                        StatefulButton(label=""),
                        StatefulButton(label=""),
                    )
                )

        view = self._make_view_with_build(build)
        view.build_ui()
        ids = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        # All nine cells must have distinct ids despite identical content.
        assert len(set(ids)) == 9

    def test_label_change_does_not_shift_neighbor_ids(self):
        """The regression the hybrid algorithm exists to prevent.

        Turn 1: nine empty cells -> collide on content -> position-anchored.
        Turn 2: one cell gets "X" -> its content key becomes unique.
        The remaining eight empty cells must keep their turn 1 ids.
        """
        marks = [""] * 9

        def build(view):
            view.clear_items()
            for row in range(3):
                view.add_item(
                    ActionRow(*(StatefulButton(label=marks[row * 3 + c]) for c in range(3)))
                )

        view = self._make_view_with_build(build)
        view.build_ui()
        turn1 = {
            i: c.custom_id
            for i, c in enumerate(b for b in view.walk_children() if isinstance(b, StatefulButton))
        }

        marks[4] = "X"  # center cell played
        view.build_ui()
        turn2 = {
            i: c.custom_id
            for i, c in enumerate(b for b in view.walk_children() if isinstance(b, StatefulButton))
        }

        # The played cell's id is allowed to change (it is now disabled
        # and no click will ever route to it again).
        # Every other cell's id must be identical to turn 1.
        for i in range(9):
            if i == 4:
                continue
            assert turn1[i] == turn2[i], f"cell {i} id shifted: {turn1[i]} -> {turn2[i]}"


class TestStableCustomIdsAtRefresh:
    """``refresh()`` stabilizes custom_ids for rebuild paths that bypass ``build_ui``.

    Tab switches, paginated page flips, wizard/form step advances, and menu
    category changes all rebuild the component tree outside ``build_ui``.
    Without the refresh-time stabilization, fresh interactive items carry
    ``os.urandom(16).hex()`` ids and the ViewStore dispatch table churns on
    every message.edit -- dropping any in-flight click that arrived after
    the edit but before the client rendered the new payload.
    """

    def _make_view(self):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Initial")))

        view = _V(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.id = 12345
        return view

    async def test_refresh_stabilizes_ids_for_rebuild_outside_build_ui(self):
        view = self._make_view()

        # Simulate a tab/paginated-style rebuild: clear + add new items.
        # These bypass ``build_ui`` so the __init_subclass__ wrapper does
        # not run ``_stabilize_custom_ids``.
        view.clear_items()
        view.add_item(
            ActionRow(
                StatefulButton(label="Enable"),
                StatefulButton(label="Clear Samples"),
            )
        )

        await view.refresh()

        buttons = [c for c in view.walk_children() if isinstance(c, StatefulButton)]
        prefix = view.id[:8]
        for btn in buttons:
            assert btn.custom_id.startswith(
                f"{prefix}:"
            ), f"custom_id {btn.custom_id!r} missing stable prefix"
        assert "Enable" in buttons[0].custom_id
        assert "Clear Samples" in buttons[1].custom_id

    async def test_repeat_rebuild_outside_build_ui_produces_identical_ids(self):
        """The dispatch-table-churn regression this fix exists to prevent."""
        view = self._make_view()

        def rebuild():
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Enable"),
                    StatefulButton(label="Clear Samples"),
                )
            )

        rebuild()
        await view.refresh()
        first = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        rebuild()
        await view.refresh()
        second = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        assert first == second

    async def test_refresh_is_idempotent_on_already_stable_ids(self):
        """A second refresh must not mutate already-stabilized ids."""
        view = self._make_view()

        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Action")))
        await view.refresh()
        first = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        # No rebuild -- same items, second refresh.
        await view.refresh()
        second = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        assert first == second

    async def test_refresh_preserves_explicit_custom_ids_during_rebuild(self):
        """Items with ``_provided_custom_id = True`` must remain untouched."""
        view = self._make_view()

        view.clear_items()
        view.add_item(
            ActionRow(
                StatefulButton(label="Auto"),
                StatefulButton(label="Explicit", custom_id="user_chosen"),
            )
        )
        await view.refresh()

        buttons = [c for c in view.walk_children() if isinstance(c, StatefulButton)]
        assert buttons[1].custom_id == "user_chosen"
        # Auto button gets stable prefix, untouched button keeps explicit id.
        assert buttons[0].custom_id != buttons[1].custom_id


class TestRenderHashShortCircuit:
    """``refresh()`` skips the Discord REST edit when the component tree
    has not changed since the last successful send/refresh.

    Every Battleship shot wakes 4 subscribers but only 1 or 2 of their
    views actually change content. The short-circuit eliminates the
    redundant message.edit() calls, cutting Discord REST traffic
    proportionally and relieving per-channel rate-limit pressure.
    """

    def _make_view_with_build(self, build_fn):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        return _V(interaction=_make_interaction())

    def test_digest_is_deterministic_for_identical_tree(self):
        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire", custom_id="a"),
                    StatefulButton(label="Close", custom_id="b"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()
        d1 = view._compute_tree_digest()
        view.build_ui()
        d2 = view._compute_tree_digest()
        assert d1 == d2

    def test_digest_changes_when_label_mutates(self):
        marks = ["Fire"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=marks[0], custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        marks[0] = "Armed"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_changes_when_disabled_toggles(self):
        enabled = [True]

        def build(view):
            view.clear_items()
            btn = StatefulButton(label="Fire", custom_id="a")
            btn.disabled = not enabled[0]
            view.add_item(ActionRow(btn))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        enabled[0] = False
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_captures_textdisplay_content(self):
        text = ["Hello"]

        def build(view):
            view.clear_items()
            view.add_item(TextDisplay(text[0]))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        text[0] = "World"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_detects_in_place_content_mutation(self):
        """EmojiGrid and similar live TextDisplay subclasses are mutated
        in place and dropped back into a rebuilt tree. The digest must
        detect the content change even when the Python object identity
        is preserved across rebuilds -- content-based hashing is the
        whole point, and any shortcut that compared identity instead
        would silently miss every in-place mutation.
        """
        shared_text = TextDisplay("red")

        def build(view):
            view.clear_items()
            view.add_item(shared_text)

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        # Mutate the same object in place -- no rebind, no new instance.
        shared_text.content = "blue"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    async def test_refresh_skips_edit_when_digest_matches(self):
        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        # Simulate a prior successful send that recorded the baseline.
        view._last_tree_digest = view._compute_tree_digest()

        await view.refresh()

        view._message.edit.assert_not_called()

    async def test_refresh_edits_when_digest_differs(self):
        label = ["Fire"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=label[0], custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        # Mutate and rebuild -- tree now differs.
        label[0] = "Armed"
        view.build_ui()
        await view.refresh()

        view._message.edit.assert_awaited_once()

    async def test_refresh_with_kwargs_never_skips(self):
        """V1 views pass embed= / content= kwargs; those are outside the
        digest, so the short-circuit must not apply."""

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        await view.refresh(content="new content")

        view._message.edit.assert_awaited_once()

    async def test_first_refresh_always_runs(self):
        """A view with no baseline digest has nothing to compare against."""

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        # _last_tree_digest remains None (no send yet).
        assert view._last_tree_digest is None

        await view.refresh()

        view._message.edit.assert_awaited_once()

    async def test_refresh_updates_baseline_after_successful_edit(self):
        label = ["A"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=label[0], custom_id="x")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        label[0] = "B"
        view.build_ui()
        first_new_digest = view._compute_tree_digest()
        await view.refresh()
        assert view._last_tree_digest == first_new_digest

        # Second refresh with no changes should now skip.
        await view.refresh()
        assert view._message.edit.await_count == 1  # still 1

    async def test_skip_recorded_in_perf_sample(self):
        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        store = get_store()
        store.clear_perf()
        store.enable_perf()
        try:
            await view.refresh()
        finally:
            store.disable_perf()

        assert len(store._refresh_samples) == 1
        sample = store._refresh_samples[-1]
        assert sample["skipped"] is True

    async def test_refresh_increments_edit_counter_only_when_editing(self):
        """End-to-end: a real edit bumps the current dispatch's counter,
        a short-circuited refresh does not. ``refresh()``'s internal
        wiring feeds the store's ``_perf_edit_stack`` on every edit.
        """
        label = ["A"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=label[0], custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        store = view.state_store
        store.clear_perf()
        store.enable_perf()
        try:
            # Push a fresh counter frame (simulating being inside a dispatch).
            store._perf_edit_stack.append(0)

            # First refresh: tree unchanged, short-circuit fires, no edit.
            await view.refresh()
            assert store._perf_edit_stack[-1] == 0

            # Mutate the tree and refresh -- real edit, counter bumps.
            label[0] = "B"
            view.build_ui()
            await view.refresh()
            assert store._perf_edit_stack[-1] == 1

            # Second real edit stacks on top.
            label[0] = "C"
            view.build_ui()
            await view.refresh()
            assert store._perf_edit_stack[-1] == 2
        finally:
            store._perf_edit_stack.clear()
            store.disable_perf()


class _FakeRateLimit(discord.HTTPException):
    """Minimal 429 stand-in for throttling tests.

    ``discord.HTTPException.__init__`` requires a live ``aiohttp`` response
    object; the real path is unreachable from a unit test. Bypassing the
    parent init and setting the two attributes ``_handle_rate_limit``
    actually reads (``status`` and ``retry_after``) keeps the subclass
    check in ``refresh()`` honest.
    """

    def __init__(self, retry_after: float = 0.5):
        Exception.__init__(self, "429 rate limit")
        self.status = 429
        self.retry_after = retry_after


class TestRefreshThrottling:
    """Reactive 429 backoff (always on) + proactive cooldown (opt-in via
    ``refresh_cooldown_ms``) share the ``_refresh_not_before`` timestamp.
    Refreshes landing inside the window defer via a single scheduled task
    that re-enters ``on_state_changed`` once the window expires.
    """

    def _make_view(self, build_fn, **class_attrs):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        for name, value in class_attrs.items():
            setattr(_V, name, value)
        return _V(interaction=_make_interaction())

    def _prime(self, view):
        """Set up mocked message and initial digest so short-circuit is inactive."""
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = None  # force first edit through

    def _build_simple(self, view):
        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

    def test_cooldown_ms_is_none_by_default(self):
        """Zero-config views never touch throttle state on the hot path."""
        assert _StatefulMixin.refresh_cooldown_ms is None

    def test_cooldown_ms_zero_rejected_at_class_def(self):
        """Zero is not a meaningful cooldown; the ``_POSITIVE_INT_ATTRS``
        validator rejects it so users don't assume 0 means 'off'.
        """
        with pytest.raises(ValueError, match="refresh_cooldown_ms"):

            class _Bad(StatefulLayoutView):
                refresh_cooldown_ms = 0

    def test_cooldown_ms_negative_rejected_at_class_def(self):
        with pytest.raises(ValueError, match="refresh_cooldown_ms"):

            class _Bad(StatefulLayoutView):
                refresh_cooldown_ms = -100

    def test_cooldown_ms_none_accepted(self):
        class _OK(StatefulLayoutView):
            refresh_cooldown_ms = None

        assert _OK.refresh_cooldown_ms is None

    def test_cooldown_ms_positive_int_accepted(self):
        class _OK(StatefulLayoutView):
            refresh_cooldown_ms = 250

        assert _OK.refresh_cooldown_ms == 250

    async def test_cooldown_off_does_not_advance_throttle(self):
        """With ``refresh_cooldown_ms = None`` (default), successful edits
        leave ``_refresh_not_before`` at 0 -- the proactive path is dead.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        await view.refresh()
        assert view._refresh_not_before == 0.0

    async def test_proactive_cooldown_stamps_after_success(self):
        view = self._make_view(self._build_simple, refresh_cooldown_ms=200)
        self._prime(view)
        before = time.monotonic()
        await view.refresh()
        # Stamp should be at least now + cooldown (minus small scheduling slack).
        assert view._refresh_not_before >= before + 0.19

    async def test_refresh_in_cooldown_window_defers(self):
        """Second rapid refresh inside the window must not hit message.edit."""
        view = self._make_view(self._build_simple, refresh_cooldown_ms=500)
        self._prime(view)

        await view.refresh()  # first edit
        assert view._message.edit.await_count == 1

        # Force the digest to differ so short-circuit can't hide the skip.
        view._last_tree_digest = 0
        await view.refresh()  # should defer, not edit
        assert view._message.edit.await_count == 1
        assert view._deferred_refresh_task is not None

        # Cancel the deferred task to avoid leaking into the test runner.
        # Let the event loop pick up the task so its coroutine enters
        # the sleep before we cancel -- avoids a 'never awaited' warning.
        await asyncio.sleep(0)
        view._deferred_refresh_task.cancel()
        try:
            await view._deferred_refresh_task
        except asyncio.CancelledError:
            pass

    async def test_cooldown_drop_intermediate_produces_one_deferred_task(self):
        """N refreshes during the window schedule 1 deferred task, not N."""
        view = self._make_view(self._build_simple, refresh_cooldown_ms=500)
        self._prime(view)

        await view.refresh()  # enters cooldown
        view._last_tree_digest = 0  # force subsequent calls past short-circuit

        await view.refresh()
        first_task = view._deferred_refresh_task
        await view.refresh()
        await view.refresh()
        # Same task instance across all four deferred refreshes.
        assert view._deferred_refresh_task is first_task

        await asyncio.sleep(0)
        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

    async def test_deferred_refresh_reenters_update_from_state(self):
        """Deferred task runs ``on_state_changed`` so ``build_ui`` sees
        the *latest* store state, not kwargs captured at defer time.
        """
        view = self._make_view(self._build_simple, refresh_cooldown_ms=50)
        self._prime(view)
        # Spy on on_state_changed.
        view.on_state_changed = AsyncMock()

        # Run the deferred path directly with a tiny wait.
        await view._deferred_refresh(0.01)

        view.on_state_changed.assert_awaited_once_with(view.state_store.state)

    async def test_deferred_refresh_noop_on_finished_view(self):
        view = self._make_view(self._build_simple, refresh_cooldown_ms=50)
        self._prime(view)
        view.on_state_changed = AsyncMock()
        view.stop()  # mark view as finished

        await view._deferred_refresh(0.01)

        view.on_state_changed.assert_not_awaited()

    async def test_reactive_429_stamps_backoff_window(self):
        """429 raised by ``message.edit`` → ``_refresh_not_before`` set
        from ``retry_after``, exception swallowed.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.75))

        before = time.monotonic()
        await view.refresh()  # must not raise

        assert view._refresh_not_before >= before + 0.7

    async def test_reactive_429_defers_next_refresh(self):
        view = self._make_view(self._build_simple)
        self._prime(view)
        # First call: 429.
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.5))
        await view.refresh()
        first_count = view._message.edit.await_count

        # Second call: still inside backoff window → no edit attempted.
        view._last_tree_digest = 0
        await view.refresh()
        assert view._message.edit.await_count == first_count

        if view._deferred_refresh_task is not None:
            await asyncio.sleep(0)
            view._deferred_refresh_task.cancel()
            try:
                await view._deferred_refresh_task
            except asyncio.CancelledError:
                pass

    async def test_non_429_http_exception_is_reraised(self):
        """Only 429 is swallowed by the reactive path; other HTTP errors
        must propagate to the caller.
        """

        class _OtherError(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "500")
                self.status = 500
                self.retry_after = 0

        view = self._make_view(self._build_simple)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=_OtherError())

        with pytest.raises(_OtherError):
            await view.refresh()

    async def test_render_hash_skip_does_not_stamp_cooldown(self):
        """Short-circuited refreshes didn't ship an edit -- they shouldn't
        consume the cooldown window either.
        """
        view = self._make_view(self._build_simple, refresh_cooldown_ms=200)
        self._prime(view)
        # Prime digest so the short-circuit path fires on next refresh.
        view._last_tree_digest = view._compute_tree_digest()

        await view.refresh()

        view._message.edit.assert_not_called()
        assert view._refresh_not_before == 0.0


class TestActingViewFastPath:
    """Acting-view ``interaction.response.edit_message`` fast path.

    When the currently-handled interaction targets the acting view's
    message and the response slot is still open, ``refresh()`` routes
    through ``interaction.response.edit_message(view=self, **kwargs)``
    -- one REST round-trip instead of two (ack + channel PATCH). The
    contextvar ``_CURRENT_INTERACTION`` is bound by the stateful
    callback for the duration of the callback + dispatch sequence.
    Disqualified cases (modal interactions, cross-view mismatch,
    already-deferred response, unbound contextvar) fall through to
    the existing webhook/channel paths.
    """

    def _make_view(self, build_fn, **class_attrs):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        for name, value in class_attrs.items():
            setattr(_V, name, value)
        return _V(interaction=_make_interaction())

    def _prime(self, view, message_id=555):
        view.build_ui()
        view._message = MagicMock()
        view._message.id = message_id
        view._message.edit = AsyncMock()
        view._last_tree_digest = None

    def _build_simple(self, view):
        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

    def _make_acting_interaction(self, message_id=555, is_done=False):
        """Build a component interaction whose ``message.id`` matches
        the primed view's message so the fast path engages.
        """
        interaction = _make_interaction()
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.response.is_done.return_value = is_done
        return interaction

    async def test_fast_path_edits_via_interaction_response(self):
        """Happy path: bound interaction targets the acting view's
        message, response is open -> edit ships through
        ``interaction.response.edit_message``, channel endpoint is
        never touched.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once_with(view=view)
        view._message.edit.assert_not_called()

    async def test_cross_view_message_mismatch_falls_through(self):
        """Interaction bound but its ``message.id`` does not match the
        view's message -> the subscriber is a cross-view listener, not
        the acting view. Falls through to the channel endpoint.
        """
        view = self._make_view(self._build_simple)
        self._prime(view, message_id=555)
        interaction = self._make_acting_interaction(message_id=999)

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once()

    async def test_modal_interaction_falls_through(self):
        """Modal submissions have a message, but the response cannot
        carry a component edit -- fast path refuses and defers to the
        channel endpoint.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.type = discord.InteractionType.modal_submit

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once()

    async def test_already_deferred_response_falls_through(self):
        """Callback manually called ``respond()`` or ``defer()`` before
        refreshing -> response slot already consumed, fast path cannot
        piggyback. Channel endpoint carries the edit instead.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction(is_done=True)

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once()

    async def test_no_bound_interaction_falls_through(self):
        """Programmatic dispatch (persistence rehydrate, hook-driven
        refresh) runs outside a component callback -- the contextvar
        default ``None`` disqualifies the fast path. Channel endpoint
        owns the edit.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)

        assert _CURRENT_INTERACTION.get() is None
        await view.refresh()
        view._message.edit.assert_awaited_once()

    async def test_fast_path_429_arms_backoff_and_swallows(self):
        """429 on ``interaction.response.edit_message`` routes through
        ``_handle_rate_limit`` exactly like the channel path -- sets
        the backoff window, swallows the exception, does NOT fall
        through to the channel endpoint (no retry storm).
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.75))

        token = _CURRENT_INTERACTION.set(interaction)
        before = time.monotonic()
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        assert view._refresh_not_before >= before + 0.7
        view._message.edit.assert_not_called()

    async def test_fast_path_general_http_error_falls_through(self):
        """Non-429 HTTP errors (500, 502, network blip) on the fast
        path fall through to the channel endpoint so a transient
        failure on the interaction-response route never drops the
        edit entirely.
        """

        class _OtherError(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "500")
                self.status = 500
                self.retry_after = 0

        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_OtherError())

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once()
        view._message.edit.assert_awaited_once()

    async def test_fast_path_timeout_falls_through(self):
        """Slow ``edit_message`` response (Discord latency spike, ephemeral
        backend under load) would starve the interaction ack past the 3s
        deadline under the fast path's one-HTTP-call contract. The
        ``wait_for`` guard caps the fast path at ``max(0.5, auto_defer_delay - 1.0)``,
        cancels the in-flight edit on stall, and falls through to the
        channel endpoint so the auto-defer timer can ack independently.
        ``_refresh_not_before`` is NOT armed: a stall is not a rate-limit
        signal, so the next refresh should not be throttled.
        """

        async def _stall_forever(*args, **kwargs):
            await asyncio.sleep(60)

        # Compress auto_defer_delay so the derived fast-path timeout
        # (max(0.5, delay - 1.0)) floors at 0.5s and the test is fast.
        view = self._make_view(self._build_simple, auto_defer_delay=1.5)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall_forever)

        token = _CURRENT_INTERACTION.set(interaction)
        before = time.monotonic()
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)
        elapsed = time.monotonic() - before

        interaction.response.edit_message.assert_awaited_once()
        view._message.edit.assert_awaited_once()
        # Fell through within the derived timeout window (0.5s), not the
        # 60s sleep -- proves the wait_for guard fired.
        assert elapsed < 2.0
        # Stall is not a rate-limit signal: backoff window stays at zero.
        assert view._refresh_not_before == 0.0


class TestDisplayLayoutView:
    """``DisplayLayoutView`` renders a pre-built container without
    requiring a subclass. Used for one-shot ephemeral cards (stats,
    leaderboards, confirmations) where there's no view-local state to
    manage.
    """

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(DisplayLayoutView, StatefulLayoutView)

    def test_defaults_differ_from_stateful_layout_view(self):
        assert DisplayLayoutView.owner_only is False
        assert DisplayLayoutView.state_scope is None

    def test_container_is_rendered(self):
        interaction = _make_interaction()
        body = TextDisplay("hello")
        view = DisplayLayoutView(interaction=interaction, container=body)

        assert body in view.children

    def test_build_ui_clears_and_re_adds(self):
        interaction = _make_interaction()
        body = TextDisplay("hello")
        view = DisplayLayoutView(interaction=interaction, container=body)

        view.build_ui()

        assert list(view.children) == [body]

    def test_container_kwarg_is_required(self):
        interaction = _make_interaction()
        with pytest.raises(TypeError, match="container"):
            DisplayLayoutView(interaction=interaction)
