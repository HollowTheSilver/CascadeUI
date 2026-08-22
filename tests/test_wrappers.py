"""Tests for component wrappers: with_loading_state, with_cooldown, with_confirmation."""

import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ui import ActionRow
from helpers import make_interaction

from cascadeui.components.base import StatefulButton
from cascadeui.components.wrappers import with_confirmation, with_cooldown, with_loading_state
from cascadeui.views.base import _StatefulMixin
from cascadeui.views.layout import StatefulLayoutView

# // ========================================( Helpers )======================================== // #


def _make_button(callback=None, label="Test", emoji=None, stateful_view=False):
    """Create a minimal mock component for wrapper tests.

    ``_cascadeui_wrapped`` is seeded to a real ``set`` because a bare
    ``MagicMock`` answers every ``getattr`` with a new mock, which would let
    the wrappers' re-wrap guard read a truthy marker container that never
    stores anything: the guard would silently no-op and these tests would
    prove nothing about it.

    Args:
        stateful_view: When True, ``component.view`` is an ``AsyncMock`` spec'd
            to ``_StatefulMixin`` so ``isinstance(view, _StatefulMixin)`` checks
            inside the wrappers hit the stateful branch.
    """
    btn = MagicMock()
    btn.callback = callback or AsyncMock()
    btn.label = label
    btn.emoji = emoji
    btn.disabled = False
    btn._cascadeui_wrapped = set()
    if stateful_view:
        btn.view = AsyncMock(spec=_StatefulMixin)
        btn.view._message = MagicMock(id=555)
        btn.view.is_finished = MagicMock(return_value=False)
    else:
        # A plain (non-CascadeUI) host view. Mock it, but give the cooldown
        # store a real dict: a MagicMock would hand back a mock from
        # setdefault and the deadline comparison would raise on it.
        btn.view = MagicMock()
        btn.view.is_finished.return_value = False
        btn.view._cascadeui_cooldowns = {}
    return btn


# // ========================================( with_loading_state )======================================== // #


class TestWithLoadingState:
    """with_loading_state disables the component during callback and restores after."""

    async def test_sets_loading_appearance(self):
        """Component should be disabled with loading label while callback runs."""
        captured_states = []

        async def capture_callback(interaction):
            captured_states.append({"disabled": component.disabled, "label": component.label})

        component = _make_button(callback=capture_callback, label="Click Me")
        with_loading_state(component, loading_label="Working...")
        interaction = make_interaction()

        await component.callback(interaction)

        assert captured_states[0]["disabled"] is True
        assert captured_states[0]["label"] == "Working..."

    async def test_restores_original_state(self):
        """Component should restore original label and enabled state after callback."""
        component = _make_button(label="Original")
        with_loading_state(component, loading_label="Loading...")
        interaction = make_interaction()

        await component.callback(interaction)

        assert component.disabled is False
        assert component.label == "Original"

    async def test_restores_on_error(self):
        """Component state should be restored even if the callback raises."""

        async def failing_callback(interaction):
            raise ValueError("boom")

        component = _make_button(callback=failing_callback, label="Original")
        with_loading_state(component, loading_label="Loading...")
        interaction = make_interaction()

        with pytest.raises(ValueError, match="boom"):
            await component.callback(interaction)

        assert component.disabled is False
        assert component.label == "Original"

    async def test_uses_interaction_response_when_available(self):
        """Plain-view path: should use interaction.response.edit_message when not yet responded."""
        component = _make_button()
        with_loading_state(component)
        interaction = make_interaction(is_done=False)

        await component.callback(interaction)

        interaction.response.edit_message.assert_called_once()

    async def test_falls_back_when_already_deferred(self):
        """Plain-view path: should fall back to message.edit when interaction is already responded."""
        component = _make_button()
        with_loading_state(component)
        interaction = make_interaction(is_done=True)
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        await component.callback(interaction)

        interaction.response.edit_message.assert_not_called()
        interaction.message.edit.assert_called()

    async def test_stateful_view_routes_through_refresh(self):
        """_StatefulMixin view path: pre-edit and restore both call view.refresh()."""
        component = _make_button(stateful_view=True)
        with_loading_state(component)
        interaction = make_interaction()

        await component.callback(interaction)

        # Pre-edit + restore = 2 refresh() calls through the stateful branch.
        assert component.view.refresh.await_count == 2
        # Plain-path edit_message must NOT fire when routing through refresh.
        interaction.response.edit_message.assert_not_called()

    async def test_skips_restore_when_view_finished(self):
        """Should not edit message in finally block if the view is finished."""
        component = _make_button()
        component.view.is_finished.return_value = True
        with_loading_state(component)
        interaction = make_interaction()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        await component.callback(interaction)

        interaction.message.edit.assert_not_called()

    async def test_loading_emoji(self):
        """Loading emoji should replace the original during callback."""
        captured = []

        async def capture(interaction):
            captured.append(component.emoji)

        component = _make_button(callback=capture, emoji="\u2705")
        with_loading_state(component, loading_emoji="\u23f3")
        interaction = make_interaction()

        await component.callback(interaction)

        assert captured[0] == "\u23f3"
        assert component.emoji == "\u2705"


# // ========================================( with_cooldown )======================================== // #


class TestWithCooldown:
    """with_cooldown blocks rapid re-invocations within the cooldown window."""

    async def test_allows_first_call(self):
        """First call should always pass through to the original callback."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5)
        interaction = make_interaction()

        await component.callback(interaction)

        original.assert_called_once()

    async def test_rejects_during_cooldown(self):
        """Second call within cooldown period should send rejection message."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5)
        interaction = make_interaction()

        await component.callback(interaction)

        interaction2 = make_interaction()
        await component.callback(interaction2)

        assert original.call_count == 1
        assert interaction2.response.send_message.called or interaction2.followup.send.called

    async def test_allows_after_cooldown_expires(self):
        """Calls should succeed after the monotonic clock advances past the deadline."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=1)

        interaction = make_interaction()
        await component.callback(interaction)

        base = time.monotonic()
        with patch("cascadeui.components.wrappers.time.monotonic", return_value=base + 2.0):
            interaction2 = make_interaction()
            await component.callback(interaction2)

        assert original.call_count == 2

    async def test_user_scope_isolates_users(self):
        """Per-user cooldown should not affect other users."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, scope="user")

        await component.callback(make_interaction(user_id=1))
        await component.callback(make_interaction(user_id=2))

        assert original.call_count == 2

    async def test_guild_scope(self):
        """Per-guild cooldown should block all users in the same guild."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, scope="guild")

        await component.callback(make_interaction(user_id=1, guild_id=300))
        await component.callback(make_interaction(user_id=2, guild_id=300))

        assert original.call_count == 1

    async def test_user_guild_scope(self):
        """Per-user-per-guild scope: same user in different guilds gets independent cooldowns."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, scope="user_guild")

        # Same user, different guilds -- both should pass.
        await component.callback(make_interaction(user_id=1, guild_id=300))
        await component.callback(make_interaction(user_id=1, guild_id=400))
        assert original.call_count == 2

        # Same user, same guild again -- blocked.
        await component.callback(make_interaction(user_id=1, guild_id=300))
        assert original.call_count == 2

    async def test_global_scope(self):
        """Global cooldown should block everyone."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, scope="global")

        await component.callback(make_interaction(user_id=1, guild_id=300))
        await component.callback(make_interaction(user_id=2, guild_id=400))

        assert original.call_count == 1

    async def test_invalid_scope_raises_at_decoration_time(self):
        """Typo'd scope value should raise ValueError before any click arrives."""
        component = _make_button()
        with pytest.raises(ValueError, match="not a valid cooldown scope"):
            with_cooldown(component, seconds=5, scope="guld")

    async def test_custom_message(self):
        """Custom cooldown message with {remaining} placeholder."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5, message="Wait {remaining}s!")
        interaction = make_interaction()

        await component.callback(interaction)

        interaction2 = make_interaction()
        await component.callback(interaction2)

        if interaction2.response.send_message.called:
            msg = interaction2.response.send_message.call_args[0][0]
        else:
            msg = interaction2.followup.send.call_args[0][0]
        assert msg.startswith("Wait ")
        assert msg.endswith("s!")

    async def test_falls_back_to_followup_when_deferred(self):
        """Plain-view path: should use followup.send when interaction is already responded."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_cooldown(component, seconds=5)

        await component.callback(make_interaction())

        interaction2 = make_interaction(is_done=True)
        await component.callback(interaction2)

        interaction2.response.send_message.assert_not_called()
        interaction2.followup.send.assert_called_once()

    async def test_stateful_view_rejects_via_respond(self):
        """_StatefulMixin view path: rejection routes through view.respond()."""
        original = AsyncMock()
        component = _make_button(callback=original, stateful_view=True)
        with_cooldown(component, seconds=5)

        await component.callback(make_interaction())
        interaction2 = make_interaction()
        await component.callback(interaction2)

        component.view.respond.assert_awaited_once()
        # Raw interaction.response/followup must NOT be touched on stateful path.
        interaction2.response.send_message.assert_not_called()


# // ========================================( with_confirmation )======================================== // #


class TestWithConfirmation:
    """with_confirmation sends a confirm/cancel prompt before running the callback."""

    async def test_sends_ephemeral_prompt(self):
        """Plain-view path: should send an ephemeral embed with confirm/cancel buttons."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_confirmation(component, title="Delete?", message="This is permanent.")
        interaction = make_interaction()

        await component.callback(interaction)

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        assert call_kwargs["ephemeral"] is True
        assert call_kwargs["embed"].title == "Delete?"

    async def test_does_not_call_original_before_confirm(self):
        """Original callback should not fire until confirm button is clicked."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)

        original.assert_not_called()

    async def test_custom_labels(self):
        """Custom confirm and cancel labels should appear on the buttons."""
        component = _make_button()
        with_confirmation(
            component,
            confirm_label="Destroy",
            cancel_label="Keep",
            confirm_style=MagicMock(),
            cancel_style=MagicMock(),
        )
        interaction = make_interaction()

        await component.callback(interaction)

        call_kwargs = interaction.response.send_message.call_args[1]
        view = call_kwargs["view"]
        labels = [child.label for child in view.children]
        assert "Destroy" in labels
        assert "Keep" in labels

    async def test_uses_interaction_response(self):
        """Confirmation wrapper should consume the interaction response slot."""
        component = _make_button()
        with_confirmation(component)
        interaction = make_interaction(is_done=False)

        await component.callback(interaction)

        interaction.response.send_message.assert_called_once()

    async def test_stateful_view_prompt_uses_respond(self):
        """_StatefulMixin view path: prompt routes through view.respond() not raw send."""
        component = _make_button(stateful_view=True)
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)

        component.view.respond.assert_awaited_once()
        interaction.response.send_message.assert_not_called()

    async def test_confirm_stops_inner_view(self):
        """Confirm branch must call stop() so the timeout task doesn't leak."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        with patch.object(inner_view, "stop") as mock_stop:
            confirm_btn = next(c for c in inner_view.children if c.label == "Yes")
            confirm_interaction = make_interaction()
            await confirm_btn.callback(confirm_interaction)
            mock_stop.assert_called_once()

    async def test_cancel_stops_inner_view(self):
        """Cancel branch must call stop() so the timeout task doesn't leak."""
        component = _make_button()
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        with patch.object(inner_view, "stop") as mock_stop:
            cancel_btn = next(c for c in inner_view.children if c.label == "No")
            cancel_interaction = make_interaction()
            await cancel_btn.callback(cancel_interaction)
            mock_stop.assert_called_once()

    async def test_cancel_stops_even_on_callback_error(self):
        """stop() fires in finally so a failing on_cancel still cleans up the View."""

        async def boom(_):
            raise RuntimeError("cancel handler failed")

        component = _make_button()
        with_confirmation(component, on_cancel=boom)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        with patch.object(inner_view, "stop") as mock_stop:
            cancel_btn = next(c for c in inner_view.children if c.label == "No")
            with pytest.raises(RuntimeError, match="cancel handler failed"):
                await cancel_btn.callback(make_interaction())
            mock_stop.assert_called_once()

    async def test_confirm_runs_action_even_when_prompt_edit_fails(self):
        """A failed prompt edit (deleted message, dead ack, transient 5xx) must
        not skip the confirmed action -- the contract is 'on confirm, run the
        callback'. The cosmetic edit failure is contained; the action still runs."""
        original = AsyncMock()
        component = _make_button(callback=original)
        with_confirmation(component)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        confirm_btn = next(c for c in inner_view.children if c.label == "Yes")

        confirm_interaction = make_interaction()
        confirm_interaction.response.edit_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(), "")
        )

        await confirm_btn.callback(confirm_interaction)  # must not raise
        original.assert_awaited_once()

    async def test_cancel_runs_hook_even_when_prompt_edit_fails(self):
        """Symmetric to the confirm path: a failed prompt edit must not skip the
        on_cancel hook."""
        on_cancel = AsyncMock()
        component = _make_button()
        with_confirmation(component, on_cancel=on_cancel)
        interaction = make_interaction()

        await component.callback(interaction)
        inner_view = interaction.response.send_message.call_args[1]["view"]
        cancel_btn = next(c for c in inner_view.children if c.label == "No")

        cancel_interaction = make_interaction()
        cancel_interaction.response.edit_message = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "boom")
        )

        await cancel_btn.callback(cancel_interaction)  # must not raise
        on_cancel.assert_awaited_once()


# // ========================================( Idempotence )======================================== // #


class TestWrapperIdempotence:
    """Re-wrapping a component must not stack another layer.

    Every wrapper installs itself by reassigning ``component.callback``
    around the previous one, so a build method that wraps a component
    outliving the rebuild adds a layer per render. The nesting is unbounded
    and grows with session length.

    These use real ``StatefulButton`` instances rather than the module's
    ``_make_button`` mock: ``getattr`` on a ``MagicMock`` auto-creates the
    marker attribute, so the guard silently no-ops and a mock-backed test
    proves nothing.
    """

    def _depth(self, button, wrapper_name):
        """Count nested wrapper frames on the button's callback chain."""
        depth, cb = 0, button.callback
        while getattr(cb, "__closure__", None):
            nxt = None
            for cell in cb.__closure__:
                inner = cell.cell_contents
                if callable(inner) and getattr(inner, "__name__", "") == wrapper_name:
                    nxt = inner
            if nxt is None:
                break
            depth += 1
            cb = nxt
        return depth + 1

    async def test_with_cooldown_re_wrap_does_not_nest(self):
        button = StatefulButton(label="Fire", callback=AsyncMock())
        for _ in range(5):
            with_cooldown(button, seconds=5)
        assert self._depth(button, "cooldown_callback") == 1

    async def test_with_loading_state_re_wrap_does_not_nest(self):
        button = StatefulButton(label="Fire", callback=AsyncMock())
        for _ in range(5):
            with_loading_state(button)
        assert self._depth(button, "loading_callback") == 1

    async def test_with_confirmation_re_wrap_demands_one_confirmation(self):
        """Three renders used to demand three clicks of "Yes" for one action."""
        ran = []

        async def action(interaction):
            ran.append(1)

        button = StatefulButton(label="Delete", callback=action)
        button._view = None
        for _ in range(3):
            with_confirmation(button, title="Sure?")

        interaction = make_interaction()
        interaction.response.is_done.return_value = False
        interaction.original_response = AsyncMock(return_value=MagicMock())
        await button.callback(interaction)

        assert interaction.response.send_message.await_count == 1
        prompt_view = interaction.response.send_message.call_args[1]["view"]
        confirm = next(c for c in prompt_view.children if c.label == "Yes")

        confirm_interaction = make_interaction()
        confirm_interaction.response.edit_message = AsyncMock()
        await confirm.callback(confirm_interaction)

        assert ran == [1]  # one confirmation, one action

    async def test_distinct_wrappers_still_compose(self):
        """The guard is per-wrapper, not per-component: stacking a cooldown
        and a confirmation on one button stays legal.
        """
        button = StatefulButton(label="Fire", callback=AsyncMock())
        with_cooldown(button, seconds=5)
        with_confirmation(button)
        assert button._cascadeui_wrapped == {"with_cooldown", "with_confirmation"}

    async def test_fresh_instances_wrap_independently(self):
        """A rebuild that constructs a NEW component must still wrap it:
        the marker lives on the instance, not the class.
        """
        first = StatefulButton(label="Fire", callback=AsyncMock())
        second = StatefulButton(label="Fire", callback=AsyncMock())
        with_cooldown(first, seconds=5)
        with_cooldown(second, seconds=5)
        assert "with_cooldown" in first._cascadeui_wrapped
        assert "with_cooldown" in second._cascadeui_wrapped


class TestCooldownMessageTemplate:
    """``message=`` is a ``str.format`` template, checked where it is given.

    The refusal branch is the only place it renders, and that branch runs
    on a second click inside the cooldown window -- so an unrenderable
    template survives construction, survives the first click, and then
    raises from inside the wrapper naming neither this parameter nor the
    placeholder it accepts.
    """

    @staticmethod
    def _button():
        async def cb(interaction):
            pass

        return StatefulButton(label="B", custom_id="b", callback=cb)

    def test_a_typo_in_the_placeholder_is_refused(self):
        with pytest.raises(ValueError, match="cannot be rendered"):
            with_cooldown(self._button(), seconds=60, message="Wait {remaning}s")

    def test_a_format_spec_on_the_value_is_refused(self):
        """The value arrives pre-formatted, so a spec is a type error.

        The docstring advertises one-decimal rendering, which makes
        ``{remaining:.0f}`` a natural thing to reach for, and it raised a
        ValueError about a str at click time.
        """
        with pytest.raises(ValueError, match="cannot be rendered"):
            with_cooldown(self._button(), seconds=60, message="Wait {remaining:.0f}s")

    def test_the_refusal_names_the_parameter_and_the_placeholder(self):
        with pytest.raises(ValueError) as caught:
            with_cooldown(self._button(), seconds=60, message="{nope}")
        message = str(caught.value)
        assert "with_cooldown(message=" in message
        assert "{remaining}" in message

    @pytest.mark.parametrize(
        "template",
        [
            "Wait {remaining}s",
            "on cooldown",
            "{remaining} then {remaining}",
            None,
        ],
    )
    def test_renderable_templates_are_accepted(self, template):
        """The guard must not cost a template that would have worked."""
        with_cooldown(self._button(), seconds=60, message=template)


class TestCooldownSurvivesRebuild:
    """Deadlines live on the owning view, so a rebuilt component keeps them.

    A reactive view constructs a fresh component on every render and wraps it
    again. Holding deadlines in the wrap call discarded the one recorded on
    click N before click N+1 landed. Because an accepted click is what
    triggers the rebuild, the cooldown never fired at all. It failed silently:
    no error, no warning, simply no throttling.
    """

    def _make_panel(self, seconds=999, **wrap_kwargs):
        ran = []

        class _Panel(StatefulLayoutView):
            async def _act(self, interaction):
                ran.append(1)

            def build_ui(self):
                self.clear_items()
                button = StatefulButton(label="Drift", callback=self._act, custom_id="d")
                with_cooldown(button, seconds=seconds, **wrap_kwargs)
                self.add_item(ActionRow(button))

        view = _Panel(interaction=make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        return view, ran

    def _button(self, view):
        return view.children[0].children[0]

    def _click(self, user_id=42):
        interaction = make_interaction()
        interaction.user.id = user_id
        interaction.guild_id = 7
        interaction.response.is_done.return_value = False
        return interaction

    async def test_cooldown_holds_across_a_rebuild(self):
        view, ran = self._make_panel()
        view.build_ui()
        first = self._button(view)
        await first.callback(self._click())

        view.build_ui()  # the accepted click triggers the reload that rebuilds
        second = self._button(view)
        assert first is not second

        interaction = self._click()
        await second.callback(interaction)

        assert ran == [1]  # the second click was rejected
        assert interaction.response.send_message.await_count == 1

    async def test_cooldown_is_still_per_user_across_a_rebuild(self):
        view, ran = self._make_panel(scope="user")
        view.build_ui()
        await self._button(view).callback(self._click(user_id=42))
        view.build_ui()
        await self._button(view).callback(self._click(user_id=99))

        assert ran == [1, 1]  # a different clicker is unaffected

    async def test_a_new_view_instance_starts_clean(self):
        """Deadlines last for the view instance, not forever."""
        first_view, first_ran = self._make_panel()
        first_view.build_ui()
        await self._button(first_view).callback(self._click())

        second_view, second_ran = self._make_panel()
        second_view.build_ui()
        await self._button(second_view).callback(self._click())

        assert second_ran == [1]

    async def test_two_buttons_with_distinct_callbacks_do_not_share_a_cooldown(self):
        """The default key must tell two ordinary buttons apart.

        A StatefulButton's ``callback`` is the stateful wrapper, and every
        stateful component in the library shares that wrapper's qualname,
        so keying on it gave a whole view one cooldown, and clicking Approve
        silently throttled Deny. The key has to read through to the caller's
        own function.
        """
        fired = []

        class _Panel(StatefulLayoutView):
            async def _approve(self, interaction):
                fired.append("approve")

            async def _deny(self, interaction):
                fired.append("deny")

            def build_ui(self):
                self.clear_items()
                approve = StatefulButton(label="Approve", callback=self._approve, custom_id="a")
                deny = StatefulButton(label="Deny", callback=self._deny, custom_id="d")
                with_cooldown(approve, seconds=999, scope="user")
                with_cooldown(deny, seconds=999, scope="user")
                self.add_item(ActionRow(approve, deny))

        view = _Panel(interaction=make_interaction())
        view._message = MagicMock()
        view.build_ui()
        approve, deny = view.children[0].children

        await approve.callback(self._click())
        rejection = self._click()
        await deny.callback(rejection)

        assert fired == ["approve", "deny"]  # the second button is unaffected
        assert rejection.response.send_message.await_count == 0

    @pytest.mark.parametrize(
        "first_wrapper", [with_confirmation, with_loading_state], ids=lambda w: w.__name__
    )
    async def test_composed_wrappers_keep_two_buttons_apart(self, first_wrapper):
        """A wrapper installed first must not become the cooldown's identity.

        Each wrapper replaces ``component.callback`` with its own closure, so
        the callback a later ``with_cooldown`` finds is shared by every
        component that wrapper touched. Keying on it gave the whole view one
        deadline, and clicking Approve threw a cooldown at Deny.
        """
        fired = []

        async def _approve(interaction):
            fired.append("approve")

        async def _deny(interaction):
            fired.append("deny")

        approve = discord.ui.Button(label="Approve")
        approve.callback = _approve
        deny = discord.ui.Button(label="Deny")
        deny.callback = _deny

        view = MagicMock()
        view.is_finished.return_value = False
        view._cascadeui_cooldowns = {}

        for button in (approve, deny):
            first_wrapper(button)
            with_cooldown(button, seconds=999, scope="user")
            button._view = view

        await approve.callback(self._click())
        rejection = self._click()
        await deny.callback(rejection)

        rejected = [c.args[0] for c in rejection.response.send_message.call_args_list if c.args]
        assert not any("cooldown" in str(text).lower() for text in rejected)

    async def test_custom_id_separates_controls_minted_by_one_factory(self):
        """A callback minted per item carries the factory's qualname, not its own.

        Ten buttons built in a loop are ten distinct callbacks, and every one
        of them answers to ``make_cb.<locals>.callback``, so keying on the
        qualname alone gave the whole loop one deadline. An explicit
        ``custom_id`` is the caller's own name for the control.
        """
        ran = []

        def make_cb(name):
            async def callback(interaction):
                ran.append(name)

            return callback

        class _Panel(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                row = []
                for name in ("alice", "bob"):
                    button = StatefulButton(
                        label=f"Kick {name}",
                        callback=make_cb(name),
                        custom_id=f"kick_{name}",
                    )
                    with_cooldown(button, seconds=999, scope="user")
                    row.append(button)
                self.add_item(ActionRow(*row))

        view = _Panel(interaction=make_interaction())
        view._message = MagicMock()
        view.build_ui()
        alice, bob = view.children[0].children

        await alice.callback(self._click())
        rejection = self._click()
        await bob.callback(rejection)

        assert ran == ["alice", "bob"]
        assert rejection.response.send_message.await_count == 0

    async def test_custom_id_key_survives_a_rebuild(self):
        """A custom_id key outlives the component the render replaced.

        The key names the control, not the object, so the deadline has to
        find its way back to the fresh button an accepted click rebuilt.
        """
        ran = []

        def make_cb(name):
            async def callback(interaction):
                ran.append(name)

            return callback

        class _Panel(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                button = StatefulButton(
                    label="Kick alice", callback=make_cb("alice"), custom_id="kick_alice"
                )
                with_cooldown(button, seconds=999, scope="user")
                self.add_item(ActionRow(button))

        view = _Panel(interaction=make_interaction())
        view._message = MagicMock()
        view.build_ui()

        await view.children[0].children[0].callback(self._click())

        # The accepted click is what triggers the rebuild.
        view.build_ui()
        rejection = self._click()
        await view.children[0].children[0].callback(rejection)

        assert ran == ["alice"]  # the rebuilt button is still throttled
        assert rejection.response.send_message.await_count == 1

    async def _click_two(self, view):
        first, second = view.children[0].children
        await first.callback(self._click())
        await second.callback(self._click())

    async def test_shared_default_key_warns_once(self, caplog):
        """A loop's controls answer to one name, and the log says so."""
        from cascadeui.components.wrappers import _shared_cooldown_warned

        def make_kick(name):
            async def callback(interaction):
                pass

            return callback

        class _KickPanel(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                row = []
                for name in ("alice", "bob"):
                    button = StatefulButton(label=f"Kick {name}", callback=make_kick(name))
                    with_cooldown(button, seconds=999, scope="user")
                    row.append(button)
                self.add_item(ActionRow(*row))

        _shared_cooldown_warned.clear()
        view = _KickPanel(interaction=make_interaction())
        view._message = MagicMock()
        view.build_ui()

        with caplog.at_level(logging.WARNING, logger="cascadeui.components.wrappers"):
            await self._click_two(view)

        hits = [r for r in caplog.records if "share the cooldown name" in r.getMessage()]
        assert len(hits) == 1
        assert "custom_id=" in hits[0].getMessage()

    async def test_controls_deliberately_sharing_one_callback_stay_silent(self, caplog):
        """Several controls on one callback is the shape ``key=`` documents.

        A bound method is a fresh object on every attribute read, so a
        detector comparing by identity reports this as a collision.
        """
        from cascadeui.components.wrappers import _shared_cooldown_warned

        class _Panel(StatefulLayoutView):
            async def _pick(self, interaction):
                pass

            def build_ui(self):
                self.clear_items()
                row = []
                for name in ("a", "b"):
                    button = StatefulButton(label=name, callback=self._pick)
                    with_cooldown(button, seconds=999, scope="user")
                    row.append(button)
                self.add_item(ActionRow(*row))

        _shared_cooldown_warned.clear()
        view = _Panel(interaction=make_interaction())
        view._message = MagicMock()
        view.build_ui()

        with caplog.at_level(logging.WARNING, logger="cascadeui.components.wrappers"):
            await self._click_two(view)

        assert not [r for r in caplog.records if "share the cooldown name" in r.getMessage()]

    async def test_explicit_key_separates_components_sharing_a_callback(self):
        """Several components wired to one callback share the default key
        (it derives from the callback), so each needs its own ``key`` to
        avoid throttling the others.
        """
        ran = []

        class _Panel(StatefulLayoutView):
            async def _pick(self, interaction):
                ran.append(1)

            def build_ui(self):
                self.clear_items()
                row = []
                for option in ("a", "b"):
                    button = StatefulButton(label=option, callback=self._pick, custom_id=option)
                    with_cooldown(button, seconds=999, key=f"pick_{option}")
                    row.append(button)
                self.add_item(ActionRow(*row))

        view = _Panel(interaction=make_interaction())
        view._message = MagicMock()
        view.build_ui()

        interaction = make_interaction()
        interaction.user.id = 42
        interaction.response.is_done.return_value = False
        await view.children[0].children[0].callback(interaction)

        other = make_interaction()
        other.user.id = 42
        other.response.is_done.return_value = False
        await view.children[0].children[1].callback(other)

        assert ran == [1, 1]  # distinct keys -> independent cooldowns


class TestRateLimitedIsASibling:
    """``RateLimited`` does not inherit from ``HTTPException``.

    ``except discord.HTTPException`` misses it entirely, and it reaches
    user code whenever the client was built with ``max_ratelimit_timeout``
    -- a supported upstream option. Each escape below cost more than a log
    line, so each is driven with a real ``RateLimited`` rather than
    asserted from the shape of the catch clause.
    """

    @staticmethod
    def _prompt_buttons(interaction):
        """The confirm/cancel pair, off the view the prompt was sent with."""
        return interaction.response.send_message.call_args[1]["view"].children

    def test_ratelimited_is_not_an_httpexception(self):
        """The premise every catch below depends on. If upstream ever makes
        it a subclass, those catches become redundant rather than wrong, and
        this test is what says so first."""
        assert not issubclass(discord.RateLimited, discord.HTTPException)

    async def test_confirmed_action_survives_a_rate_limited_prompt_edit(self):
        """``with_confirmation``'s own comment promises the confirmed action
        runs even when the cosmetic prompt edit fails."""
        ran = []

        async def action(interaction):
            ran.append(True)

        component = _make_button(callback=action)
        with_confirmation(component)
        opening = make_interaction()
        await component.callback(opening)

        confirm, _cancel = self._prompt_buttons(opening)
        prompt = make_interaction()
        prompt.response.edit_message = AsyncMock(side_effect=discord.RateLimited(5.0))
        await confirm.callback(prompt)

        assert ran == [True]

    async def test_cancel_hook_survives_a_rate_limited_prompt_edit(self):
        cancelled = []

        async def on_cancel(interaction):
            cancelled.append(True)

        component = _make_button(callback=AsyncMock())
        with_confirmation(component, on_cancel=on_cancel)
        opening = make_interaction()
        await component.callback(opening)

        _confirm, cancel = self._prompt_buttons(opening)
        prompt = make_interaction()
        prompt.response.edit_message = AsyncMock(side_effect=discord.RateLimited(5.0))
        await cancel.callback(prompt)

        assert cancelled == [True]

    async def test_loading_state_runs_the_action_after_a_rate_limited_pre_edit(self):
        """The loading state is cosmetic; failing to show it must not cancel
        the click that asked for the action."""
        ran = []

        async def action(interaction):
            ran.append(True)

        component = _make_button(callback=action)
        with_loading_state(component)

        interaction = make_interaction(is_done=False)
        interaction.response.edit_message = AsyncMock(side_effect=discord.RateLimited(5.0))
        await component.callback(interaction)

        assert ran == [True]


class TestDecoratorsAcceptEitherShape:
    """``with_error_boundary``, ``with_retry`` and ``cascade_component``
    take a plain ``def`` as well as an ``async def``.

    All three are public, root-exported, and awaited whatever they were
    given, so a synchronous function ran, returned, and then failed on the
    library awaiting its return value. None had a functional test of any
    kind before this.
    """

    async def test_error_boundary_runs_a_sync_function(self):
        from cascadeui import with_error_boundary

        @with_error_boundary("sync_work")
        def work(n):
            return n * 2

        assert await work(21) == 42

    async def test_error_boundary_still_reraises_from_a_sync_function(self):
        from cascadeui import with_error_boundary

        @with_error_boundary("sync_boom")
        def work():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await work()

    async def test_error_boundary_runs_an_async_function(self):
        from cascadeui import with_error_boundary

        @with_error_boundary("async_work")
        async def work(n):
            return n * 2

        assert await work(21) == 42

    async def test_retry_runs_a_sync_function(self):
        from cascadeui import RetryConfig, with_retry

        @with_retry(RetryConfig(max_retries=2, backoff_factor=0.0))
        def work():
            return "done"

        assert await work() == "done"

    async def test_retry_retries_a_failing_sync_function(self):
        from cascadeui import RetryConfig, with_retry

        attempts = []

        @with_retry(RetryConfig(max_retries=3, backoff_factor=0.0))
        def work():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("not yet")
            return "done"

        assert await work() == "done"
        assert len(attempts) == 3

    async def test_cascade_component_runs_a_sync_handler(self):
        from cascadeui.utils.decorators import cascade_component

        seen = []

        class Host:
            id = "view-1"

            @cascade_component("btn")
            def handler(self, interaction):
                seen.append(interaction.user.id)

            async def dispatch(self, action_type, payload):
                return None

        await Host().handler(make_interaction(user_id=7))

        assert seen == [7]
