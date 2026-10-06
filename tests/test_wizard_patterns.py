"""Tests for WizardView / WizardLayoutView customization and parity."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from discord.ui import ActionRow, Container, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.components.base import StatefulButton
from cascadeui.views.patterns import WizardLayoutView, WizardView
from cascadeui.views.patterns.types import WizardStep

# // ========================================( Button style validation )======================================== // #


class TestButtonStyleValidation:
    """Invalid wizard button styles raise at class definition time."""

    def test_invalid_wizard_style_raises_at_definition(self):
        with pytest.raises(ValueError, match="must be a discord.ButtonStyle"):

            class BadWizard(WizardLayoutView):
                back_button_style = "secondary"  # str, not enum

    def test_valid_wizard_style_accepted(self):
        class GoodWizard(WizardLayoutView):
            back_button_style = discord.ButtonStyle.danger
            next_button_style = discord.ButtonStyle.success
            finish_button_style = discord.ButtonStyle.primary

        assert GoodWizard.back_button_style is discord.ButtonStyle.danger


# // ========================================( Customization triples round-trip )======================================== // #


class TestWizardLayoutViewCustomization:
    """Custom labels and styles apply to generated wizard navigation buttons."""

    async def test_back_button_label_override(self):
        async def builder():
            return [Container(TextDisplay("s"))]

        class CustomWizard(WizardLayoutView):
            back_button_label = "Previous"
            next_button_label = "Continue"
            finish_button_label = "Create"

        view = CustomWizard(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder},
            ],
        )

        assert view._back_btn.label == "Previous"
        assert view._next_btn.label == "Continue"

    async def test_finish_label_on_single_step(self):
        async def builder():
            return [Container(TextDisplay("s"))]

        class CustomWizard(WizardLayoutView):
            finish_button_label = "Create Character"

        view = CustomWizard(
            interaction=_make_interaction(),
            steps=[{"name": "Only", "builder": builder}],
        )

        assert view._next_btn.label == "Create Character"
        assert view._next_btn.style is discord.ButtonStyle.success  # default finish_button_style

    async def test_step_indicator_label_callable(self):
        async def builder():
            return [Container(TextDisplay("s"))]

        class CustomWizard(WizardLayoutView):
            step_indicator_label = staticmethod(lambda current, total: f"{current} of {total}")

        view = CustomWizard(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder},
                {"name": "C", "builder": builder},
            ],
        )

        assert view._step_indicator.label == "1 of 3"


# // ========================================( on_finish method hook grammar )======================================== // #


class TestOnFinishMethodHook:
    """on_finish method override fires when reaching the last step."""

    async def test_on_finish_method_override_fires(self):
        async def builder():
            return [Container(TextDisplay("s"))]

        calls = []

        class FinishWizard(WizardLayoutView):
            async def on_finish(self, interaction):
                calls.append(interaction)
                if not interaction.response.is_done():
                    await interaction.response.defer()

        view = FinishWizard(
            interaction=_make_interaction(),
            steps=[{"name": "Only", "builder": builder}],
        )

        nav = _make_interaction()
        await view._go_next(nav)

        assert len(calls) == 1
        assert calls[0] is nav

    async def test_on_finish_kwarg_no_longer_accepted(self):
        """``on_finish=`` is not a supported WizardView kwarg; overriding ``on_finish()`` is the extension path."""

        async def builder():
            return [Container(TextDisplay("s"))]

        with pytest.raises(TypeError):
            WizardLayoutView(
                interaction=_make_interaction(),
                steps=[{"name": "Only", "builder": builder}],
                on_finish=lambda i: None,
            )


# // ========================================( V2 button-mutation parity )======================================== // #


class TestWizardBackButtonDerivation:
    """The back button's disabled state derives from _current_step in the
    builder, so a rebuild off a non-first step leaves Back enabled instead of
    reverting to the first-step default.
    """

    async def test_back_derives_disabled_from_current_step(self):
        async def _b():
            return discord.ui.TextDisplay("step")

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": _b},
                {"name": "B", "builder": _b},
                {"name": "C", "builder": _b},
            ],
        )
        assert view._back_btn.disabled is True  # step 0: nowhere to go back

        view._current_step = 1
        view._build_nav_buttons()
        assert view._back_btn.disabled is False

    def test_v1_back_derives_disabled_from_current_step(self):
        """V1 WizardView's builder derives the back button's disabled state from
        _current_step, mirroring the V2 fix on its own independent builder.
        """
        builder = lambda v: None
        view = WizardView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder},
                {"name": "C", "builder": builder},
            ],
        )
        assert view._back_btn.disabled is True  # step 0: nowhere to go back

        view._current_step = 1
        view.clear_items()
        view._build_nav_buttons()
        assert view._back_btn.disabled is False


class TestWizardLayoutViewButtonIdentity:
    """V2 variant must mutate nav buttons in place, not rebuild them."""

    async def test_button_identity_stable_across_refresh(self):
        async def builder():
            return [Container(TextDisplay("content"))]

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder},
                {"name": "C", "builder": builder},
            ],
        )

        back_id = id(view._back_btn)
        next_id = id(view._next_btn)
        indicator_id = id(view._step_indicator)
        nav_row_id = id(view._nav_row)

        # Drive a refresh; edit is a no-op when _message is unset but the
        # button-mutation path runs unconditionally before refresh().
        view._message = None

        try:
            await view._refresh_wizard()
        except AttributeError:
            # refresh() short-circuits when _message is None; that's fine.
            pass

        assert id(view._back_btn) == back_id
        assert id(view._next_btn) == next_id
        assert id(view._step_indicator) == indicator_id
        assert id(view._nav_row) == nav_row_id


class TestWizardNavClicksAreBoundToTheirStep:
    """A Back or Next click acts only on the step it was drawn for.

    A client sends the custom_id it rendered, and a second click queued
    behind the first reached the same button after the first had moved the
    wizard on, so a double-click on Next skipped a step and its validator.
    """

    def _wizard(self, cls=WizardLayoutView, validator=None):
        async def builder():
            return [Container(TextDisplay("s"))]

        steps = [{"name": f"S{i}", "builder": builder} for i in range(3)]
        if validator is not None:
            steps[0]["validator"] = validator
        view = cls(interaction=_make_interaction(), steps=steps)

        async def render(**kwargs):
            view._sync_wizard_nav()

        view._refresh_wizard = render
        return view

    def _click(self, button):
        interaction = _make_interaction()
        interaction.data = {"custom_id": button.custom_id}
        return interaction

    @pytest.mark.parametrize("cls", [WizardLayoutView, WizardView])
    async def test_a_second_next_queued_behind_the_first_is_ignored(self, cls):
        view = self._wizard(cls)
        first, second = self._click(view._next_btn), self._click(view._next_btn)

        await view._next_btn.original_callback(first)
        await view._next_btn.original_callback(second)

        assert view.current_step == 1
        await view._next_btn.original_callback(self._click(view._next_btn))
        assert view.current_step == 2

    async def test_a_second_back_queued_behind_the_first_is_ignored(self):
        view = self._wizard()
        view._current_step = 2
        view._sync_wizard_nav()
        first, second = self._click(view._back_btn), self._click(view._back_btn)

        await view._back_btn.original_callback(first)
        await view._back_btn.original_callback(second)

        assert view.current_step == 1

    async def test_the_validator_runs_once_for_a_double_click(self):
        calls = []

        def validator():
            calls.append(1)
            return True

        view = self._wizard(validator=validator)
        first, second = self._click(view._next_btn), self._click(view._next_btn)

        await view._next_btn.original_callback(first)
        await view._next_btn.original_callback(second)

        assert calls == [1]

    async def test_go_next_called_from_another_button_still_advances(self):
        view = self._wizard()
        interaction = _make_interaction()
        interaction.data = {"custom_id": "continue"}

        await view._go_next(interaction)

        assert view.current_step == 1


# // ========================================( Navigation hooks )======================================== // #


class TestWizardDoubleFinish:
    """A second Finish from one double-click does not run ``on_finish`` again.

    The step-stamped ids cannot catch it: both clicks come from the last
    step, so they carry the same id. An ``on_finish`` that leaves the view
    open (a refusal, or a result shown in place) ran once per click.
    """

    def _wizard(self, cls, *, serialize=True, validator=None):
        calls = []

        class _Wizard(cls):
            serialize_interactions = serialize

            async def on_finish(self, interaction):
                calls.append(interaction)
                # Stays open, and yields so a second click can arrive while
                # this one is still being handled.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        async def builder():
            return [Container(TextDisplay("s"))]

        step = {"name": "Only", "builder": builder}
        if validator is not None:
            step["validator"] = validator
        view = _Wizard(interaction=_make_interaction(), steps=[step])

        async def render(**kwargs):
            view._sync_wizard_nav()

        view._refresh_wizard = render
        return view, calls

    @pytest.mark.parametrize("cls", [WizardLayoutView, WizardView])
    async def test_a_double_click_runs_on_finish_once(self, cls, caplog):
        caplog.set_level(logging.DEBUG, logger="cascadeui")
        view, calls = self._wizard(cls)
        finish = view._next_btn

        await asyncio.gather(
            view._scheduled_task(finish, _make_interaction()),
            view._scheduled_task(finish, _make_interaction()),
        )

        assert len(calls) == 1
        assert any(
            "Dropped a click" in r.getMessage() and "a second Finish" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )

    async def test_the_dropped_finish_is_answered_without_auto_defer(self):
        # No callback of the user's ran for it, so nothing else could answer it.
        view, calls = self._wizard(WizardLayoutView)
        view.auto_defer = False
        first, second = _make_interaction(), _make_interaction()

        await asyncio.gather(
            view._scheduled_task(view._next_btn, first),
            view._scheduled_task(view._next_btn, second),
        )

        assert len(calls) == 1
        second.response.defer.assert_awaited()

    async def test_a_click_sent_after_the_result_runs_again(self):
        view, calls = self._wizard(WizardLayoutView)
        finish = view._next_btn
        await view._scheduled_task(finish, _make_interaction())

        await view._scheduled_task(finish, _make_interaction())

        assert len(calls) == 2

    async def test_a_finish_called_from_code_after_a_click_still_runs(self):
        """The click's number ends with the click, so later code is not taken
        for part of it."""
        view, calls = self._wizard(WizardLayoutView)
        await view._scheduled_task(view._next_btn, _make_interaction())

        await view._go_next(_make_interaction())

        assert len(calls) == 2

    async def test_a_next_that_finishes_runs_on_finish_once_for_a_double_click(self):
        """A Next whose validator hid every later step finished the wizard
        without the Finish hold, so the double-click's second click, now a
        Finish, ran on_finish again."""
        skipped = {}
        calls = []

        async def choose_express():
            await asyncio.sleep(0)
            skipped["details"] = True
            return True, None

        class _Wizard(WizardLayoutView):
            async def on_finish(self, interaction):
                calls.append(interaction)
                await asyncio.sleep(0)

        async def builder():
            return [Container(TextDisplay("s"))]

        view = _Wizard(
            interaction=_make_interaction(),
            steps=[
                {"name": "Plan", "builder": builder, "validator": choose_express},
                {"name": "Details", "builder": builder, "condition": lambda v: not skipped},
            ],
        )

        async def render(**kwargs):
            view._sync_wizard_nav()

        view._refresh_wizard = render
        next_button = view._next_btn

        await asyncio.gather(
            view._scheduled_task(next_button, _make_interaction()),
            view._scheduled_task(next_button, _make_interaction()),
        )

        assert len(calls) == 1

    async def test_unserialized_clicks_run_on_finish_once(self):
        """Without the lock the two run side by side instead of queueing."""
        view, calls = self._wizard(WizardLayoutView, serialize=False)
        finish = view._next_btn

        await asyncio.gather(
            view._scheduled_task(finish, _make_interaction()),
            view._scheduled_task(finish, _make_interaction()),
        )

        assert len(calls) == 1

    async def test_unserialized_clicks_with_an_async_validator_run_on_finish_once(self):
        """The mark was taken after the validator's await, so a second
        Finish arriving while it ran passed the guard."""

        async def slow_valid():
            await asyncio.sleep(0)
            return True, None

        view, calls = self._wizard(WizardLayoutView, serialize=False, validator=slow_valid)
        finish = view._next_btn

        await asyncio.gather(
            view._scheduled_task(finish, _make_interaction()),
            view._scheduled_task(finish, _make_interaction()),
        )

        assert len(calls) == 1

    async def test_a_finish_queued_after_a_fixing_click_runs(self):
        """A refused Finish is not a result the next one repeats: a class was
        picked while the first check ran, and the Finish after it was dropped
        as a double-click."""
        chosen = {}

        async def needs_a_class():
            await asyncio.sleep(0)
            return (True, None) if chosen else (False, "Pick a class.")

        view, calls = self._wizard(WizardLayoutView, validator=needs_a_class)

        async def pick(interaction):
            chosen["class"] = "Warrior"

        picker = StatefulButton(label="Warrior", callback=pick)
        view.add_item(ActionRow(picker))
        finish = view._next_btn

        await asyncio.gather(
            view._scheduled_task(finish, _make_interaction()),
            view._scheduled_task(picker, _make_interaction()),
            view._scheduled_task(finish, _make_interaction()),
        )

        assert len(calls) == 1

    async def test_a_finish_after_a_failed_validation_runs(self):
        answers = [(False, "Pick a class."), (True, None)]

        async def validator():
            return answers.pop(0)

        view, calls = self._wizard(WizardLayoutView, validator=validator)
        finish = view._next_btn
        await view._scheduled_task(finish, _make_interaction())

        await view._scheduled_task(finish, _make_interaction())

        assert len(calls) == 1

    async def test_a_double_click_of_a_refused_finish_reports_the_error_once(self):
        async def needs_a_class():
            await asyncio.sleep(0)
            return False, "Pick a class."

        view, _ = self._wizard(WizardLayoutView, validator=needs_a_class)
        reported = []

        async def on_validation_failed(step_index, error, interaction=None):
            reported.append(error)

        view.on_validation_failed = on_validation_failed
        finish = view._next_btn

        await asyncio.gather(
            view._scheduled_task(finish, _make_interaction()),
            view._scheduled_task(finish, _make_interaction()),
        )

        assert reported == ["Pick a class."]

    async def test_a_refused_call_from_code_beside_a_click_leaves_finish_working(self):
        """Each put back the mark it found, and the call from code found the
        click's hold: every Finish after that was dropped for good."""
        chosen = {}

        async def needs_a_class():
            await asyncio.sleep(0.01)
            return (True, None) if chosen else (False, "Pick a class.")

        view, calls = self._wizard(WizardLayoutView, validator=needs_a_class)
        view.on_validation_failed = AsyncMock()
        clicked = asyncio.create_task(view._scheduled_task(view._next_btn, _make_interaction()))
        await asyncio.sleep(0)
        from_code = asyncio.create_task(view._go_next(_make_interaction()))
        await asyncio.gather(clicked, from_code)
        chosen["class"] = "Warrior"

        await view._scheduled_task(view._next_btn, _make_interaction())

        assert len(calls) == 1


class TestWizardNavigationHooks:
    """on_step_entered / on_step_exited / on_validation_failed fire at the expected points."""

    def _make_steps(self, count=3):
        async def builder():
            return [Container(TextDisplay("s"))]

        return [{"name": f"S{i}", "builder": builder} for i in range(count)]

    async def test_on_step_entered_fires_on_forward_nav(self):
        entered = []

        class TrackedWizard(WizardLayoutView):
            async def on_step_entered(self, step_index):
                entered.append(step_index)

        view = TrackedWizard(interaction=_make_interaction(), steps=self._make_steps(3))
        view._refresh_wizard = AsyncMock()  # stub out message.edit path

        await view._go_next(_make_interaction())

        assert entered == [1]
        assert view._current_step == 1

    async def test_on_step_entered_fires_on_back_nav(self):
        entered = []

        class TrackedWizard(WizardLayoutView):
            async def on_step_entered(self, step_index):
                entered.append(step_index)

        view = TrackedWizard(interaction=_make_interaction(), steps=self._make_steps(3))
        view._refresh_wizard = AsyncMock()
        view._current_step = 2  # start advanced so Back has somewhere to go

        await view._go_back(_make_interaction())

        assert entered == [1]
        assert view._current_step == 1

    async def test_on_step_exited_reports_old_index_on_forward(self):
        """on_step_exited fires with the step being LEFT (pre-increment)."""
        exited = []

        class TrackedWizard(WizardLayoutView):
            async def on_step_exited(self, step_index):
                exited.append(step_index)

        view = TrackedWizard(interaction=_make_interaction(), steps=self._make_steps(3))
        view._refresh_wizard = AsyncMock()

        await view._go_next(_make_interaction())

        assert exited == [0]

    async def test_on_step_exited_reports_old_index_on_back(self):
        exited = []

        class TrackedWizard(WizardLayoutView):
            async def on_step_exited(self, step_index):
                exited.append(step_index)

        view = TrackedWizard(interaction=_make_interaction(), steps=self._make_steps(3))
        view._refresh_wizard = AsyncMock()
        view._current_step = 2

        await view._go_back(_make_interaction())

        assert exited == [2]

    async def test_exit_before_enter_ordering(self):
        """on_step_exited(old) must fire before on_step_entered(new)."""
        order = []

        class TrackedWizard(WizardLayoutView):
            async def on_step_exited(self, step_index):
                order.append(("exit", step_index))

            async def on_step_entered(self, step_index):
                order.append(("enter", step_index))

        view = TrackedWizard(interaction=_make_interaction(), steps=self._make_steps(3))
        view._refresh_wizard = AsyncMock()

        await view._go_next(_make_interaction())

        assert order == [("exit", 0), ("enter", 1)]

    async def test_on_validation_failed_fires_when_validator_rejects(self):
        async def builder():
            return [Container(TextDisplay("s"))]

        failed = []

        class ValidatedWizard(WizardLayoutView):
            async def on_validation_failed(self, step_index, error, interaction=None):
                failed.append((step_index, error))

        async def bad_validator():
            return (False, "nope")

        view = ValidatedWizard(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder, "validator": bad_validator},
                {"name": "B", "builder": builder},
            ],
        )
        view._refresh_wizard = AsyncMock()

        await view._go_next(_make_interaction())

        # Failed at step 0 -- view should NOT advance
        assert failed == [(0, "nope")]
        assert view._current_step == 0

    async def test_on_validation_failed_does_not_fire_on_pass(self):
        async def builder():
            return [Container(TextDisplay("s"))]

        failed = []

        class ValidatedWizard(WizardLayoutView):
            async def on_validation_failed(self, step_index, error, interaction=None):
                failed.append((step_index, error))

        async def good_validator():
            return (True, "")

        view = ValidatedWizard(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder, "validator": good_validator},
                {"name": "B", "builder": builder},
            ],
        )
        view._refresh_wizard = AsyncMock()

        await view._go_next(_make_interaction())

        assert failed == []
        assert view._current_step == 1


# // ========================================( Conditional steps )======================================== // #


class TestConditionAritySteps:
    """A condition the visibility check cannot call is refused at declaration.

    ``_is_step_visible`` runs ``condition(view)`` inside a ``try`` whose
    ``except Exception`` treats a raising predicate as visible, so an arity
    mismatch does not surface as an error at all: the step the predicate
    meant to hide renders, with a warning as the only trace. Both
    declaration forms are checked, because a raw dict never passes through
    ``WizardStep`` and neither form is the lenient one.
    """

    @staticmethod
    def _builder():
        async def builder():
            return [Container(TextDisplay("s"))]

        return builder

    def test_zero_arg_condition_refused_in_a_raw_dict(self):
        with pytest.raises(TypeError, match=r"cannot be called with \(view\)"):
            WizardLayoutView(
                steps=[{"name": "A", "builder": self._builder(), "condition": lambda: False}],
                user_id=1,
                guild_id=2,
            )

    def test_zero_arg_condition_refused_in_a_wizard_step(self):
        with pytest.raises(TypeError, match=r"cannot be called with \(view\)"):
            WizardStep(name="A", builder=self._builder(), condition=lambda: False)

    def test_the_refusal_names_the_step_and_the_fix(self):
        with pytest.raises(TypeError) as caught:
            WizardStep(name="Payment", builder=self._builder(), condition=lambda: False)
        message = str(caught.value)
        assert "Payment" in message
        assert "condition" in message
        assert "Fix:" in message

    def test_a_condition_taking_the_view_is_accepted(self):
        """The guard must not cost the shape the contract asks for."""
        view = WizardLayoutView(
            steps=[{"name": "A", "builder": self._builder(), "condition": lambda v: False}],
            user_id=1,
            guild_id=2,
        )
        assert view._visible_step_indices() == []
        view.stop()

    def test_a_defaulted_second_parameter_is_accepted(self):
        """One declared positional is the requirement, not exactly one."""

        def condition(view, extra=None):
            return True

        WizardStep(name="A", builder=self._builder(), condition=condition)


class TestConditionalSteps:
    """Steps with a ``condition`` callable are skipped when the predicate is False."""

    def _make_builder(self):
        async def builder():
            return [Container(TextDisplay("s"))]

        return builder

    async def test_invisible_step_skipped_on_next(self):
        """Next from step 0 jumps over an invisible step 1 to step 2."""
        builder = self._make_builder()
        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder, "condition": lambda view: False},
                {"name": "C", "builder": builder},
            ],
        )
        view._refresh_wizard = AsyncMock()

        await view._go_next(_make_interaction())

        assert view._current_step == 2  # skipped step 1

    async def test_invisible_step_skipped_on_back(self):
        """Back from step 2 jumps over an invisible step 1 to step 0."""
        builder = self._make_builder()
        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder, "condition": lambda view: False},
                {"name": "C", "builder": builder},
            ],
        )
        view._refresh_wizard = AsyncMock()
        view._current_step = 2

        await view._go_back(_make_interaction())

        assert view._current_step == 0

    async def test_no_visible_steps_ahead_triggers_finish(self):
        """Next fires on_finish when every remaining step is invisible."""
        builder = self._make_builder()
        finished = []

        class FinishWizard(WizardLayoutView):
            async def on_finish(self, interaction):
                finished.append(interaction)

        view = FinishWizard(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder, "condition": lambda view: False},
                {"name": "C", "builder": builder, "condition": lambda view: False},
            ],
        )
        view._refresh_wizard = AsyncMock()

        await view._go_next(_make_interaction())

        assert len(finished) == 1
        assert view._current_step == 0  # did not advance -- no visible step ahead

    async def test_step_indicator_counts_visible_only(self):
        """Indicator shows '1/2' when 3 steps exist but only 2 are visible."""
        builder = self._make_builder()
        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder, "condition": lambda view: False},
                {"name": "C", "builder": builder},
            ],
        )

        assert view._step_indicator.label == "Step 1/2"

    async def test_condition_exception_treats_as_visible(self):
        """A condition that raises is treated as visible (safe fallback)."""
        builder = self._make_builder()

        def boom(view):
            raise RuntimeError("bad predicate")

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder, "condition": boom},
                {"name": "C", "builder": builder},
            ],
        )
        view._refresh_wizard = AsyncMock()

        await view._go_next(_make_interaction())

        # Raised -> visible, so step 1 is entered, not skipped.
        assert view._current_step == 1


# // ========================================( Progress header (V2) )======================================== // #


class TestProgressHeader:
    """V2 WizardLayoutView renders a progress bar above step content by default."""

    async def test_progress_header_rendered_by_default(self):
        async def builder():
            return [Container(TextDisplay("s"))]

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder},
                {"name": "C", "builder": builder},
            ],
        )
        await view._rebuild_step_content()

        # First top-level child should be the progress Container.
        top_level = list(view.children)
        assert any(isinstance(c, Container) for c in top_level)

    async def test_progress_header_hidden_when_one_visible_step(self):
        """Header auto-hides when only a single step is visible."""

        async def builder():
            return [Container(TextDisplay("only"))]

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[{"name": "Only", "builder": builder}],
        )
        await view._rebuild_step_content()

        # Exactly one Container (from the step builder) -- no progress header.
        containers = [c for c in view.children if isinstance(c, Container)]
        assert len(containers) == 1
        assert "only" in containers[0].children[0].content

    async def test_progress_header_disabled_by_show_progress_bar(self):
        async def builder():
            return [Container(TextDisplay("s"))]

        class PlainWizard(WizardLayoutView):
            show_progress_bar = False

        view = PlainWizard(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder},
            ],
        )
        await view._rebuild_step_content()

        # Only the builder's Container should be present, no header.
        containers = [c for c in view.children if isinstance(c, Container)]
        assert len(containers) == 1

    async def test_progress_header_override_returns_none(self):
        """Returning None from _build_progress_header suppresses the header."""

        async def builder():
            return [Container(TextDisplay("s"))]

        class NoHeaderWizard(WizardLayoutView):
            def _build_progress_header(self, visible_indices):
                return None

        view = NoHeaderWizard(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder},
            ],
        )
        await view._rebuild_step_content()

        containers = [c for c in view.children if isinstance(c, Container)]
        assert len(containers) == 1  # builder's container only

    async def test_progress_header_reflects_current_position(self):
        """Header text updates to reflect current step position after nav."""

        async def builder():
            return [Container(TextDisplay("s"))]

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": builder},
                {"name": "B", "builder": builder},
                {"name": "C", "builder": builder},
            ],
        )
        await view._rebuild_step_content()

        first_header = next(c for c in view.children if isinstance(c, Container))
        first_bar_text = first_header.children[0].content

        view._current_step = 2
        await view._rebuild_step_content()

        second_header = next(c for c in view.children if isinstance(c, Container))
        second_bar_text = second_header.children[0].content

        # Percent advances from ~33% to 100% between step 1/3 and step 3/3.
        assert first_bar_text != second_bar_text
        assert "100%" in second_bar_text


# // ========================================( Theme context propagation )======================================== // #


class TestStepBuilderThemeContext:
    """Step builders run inside the view's theme context so ``card()`` inherits accent."""

    async def test_builder_sees_view_theme(self):
        from cascadeui.theming.context import get_current_theme
        from cascadeui.theming.core import Theme

        themed = Theme(name="test_wizard_theme", styles={"accent_colour": 0xABCDEF})
        seen = []

        async def builder():
            seen.append(get_current_theme())
            return [Container(TextDisplay("s"))]

        class ThemedWizard(WizardLayoutView):
            theme = themed

        view = ThemedWizard(
            interaction=_make_interaction(),
            steps=[{"name": "A", "builder": builder}, {"name": "B", "builder": builder}],
        )
        await view._rebuild_step_content()

        assert seen and seen[0] is themed

    async def test_theme_resets_after_rebuild(self):
        from cascadeui.theming.context import get_current_theme

        async def builder():
            return [Container(TextDisplay("s"))]

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[{"name": "A", "builder": builder}, {"name": "B", "builder": builder}],
        )
        await view._rebuild_step_content()

        # After the rebuild returns, the context is reset.
        assert get_current_theme() is None


# // ========================================( WizardStep typed schema )======================================== // #


class TestWizardStepDataclass:
    """WizardStep construction validates name and callable fields."""

    def test_minimal_construction(self):
        from cascadeui import WizardStep

        builder = lambda v: None
        s = WizardStep(name="Welcome", builder=builder)
        assert s.name == "Welcome"
        assert s.builder is builder
        assert s.validator is None
        assert s.condition is None

    def test_empty_name_raises(self):
        from cascadeui import WizardStep

        with pytest.raises(ValueError, match="name must be a non-empty string"):
            WizardStep(name="", builder=lambda v: None)

    def test_non_callable_builder_raises(self):
        from cascadeui import WizardStep

        with pytest.raises(ValueError, match="builder must be callable"):
            WizardStep(name="X", builder="not a callable")

    def test_non_callable_validator_raises(self):
        from cascadeui import WizardStep

        with pytest.raises(ValueError, match="validator must be callable"):
            WizardStep(name="X", builder=lambda v: None, validator="nope")

    def test_to_dict_strips_missing_optionals(self):
        from cascadeui import WizardStep

        builder = lambda v: None
        s = WizardStep(name="Step", builder=builder)
        d = s.to_dict()
        assert d == {"name": "Step", "builder": builder}
        assert "validator" not in d
        assert "condition" not in d

    def test_to_dict_keeps_optionals_when_set(self):
        from cascadeui import WizardStep

        builder = lambda: None
        validator = lambda: (True, None)
        condition = lambda view: True
        s = WizardStep(name="Step", builder=builder, validator=validator, condition=condition)
        assert s.to_dict()["validator"] is validator
        assert s.to_dict()["condition"] is condition


class TestWizardStepPatternIntegration:
    """WizardStep instances pass through WizardView / WizardLayoutView cleanly."""

    def test_v1_accepts_wizardstep_list(self):
        from cascadeui import WizardStep

        builder = lambda v: None
        view = WizardView(
            interaction=_make_interaction(),
            steps=[
                WizardStep(name="A", builder=builder),
                WizardStep(name="B", builder=builder),
            ],
        )
        assert len(view._steps) == 2
        assert view._steps[0]["name"] == "A"

    def test_v2_accepts_wizardstep_list(self):
        from cascadeui import WizardStep

        async def builder():
            return []

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[WizardStep(name="Only", builder=builder)],
        )
        assert view._steps[0]["name"] == "Only"

    def test_mixed_dict_and_wizardstep(self):
        from cascadeui import WizardStep

        builder = lambda v: None
        view = WizardView(
            interaction=_make_interaction(),
            steps=[
                WizardStep(name="A", builder=builder),
                {"name": "B", "builder": builder},
            ],
        )
        assert [s["name"] for s in view._steps] == ["A", "B"]

    def test_invalid_entry_type_raises(self):
        with pytest.raises(TypeError, match="step entries must be WizardStep or dict"):
            WizardView(interaction=_make_interaction(), steps=["not a step"])


class TestWizardSchema:
    """WizardSchema subclass hooks up via schema= kwarg."""

    def test_schema_subclass_feeds_steps(self):
        from cascadeui import WizardSchema, WizardStep

        builder = lambda v: None

        class SetupSchema(WizardSchema):
            def get_steps(self):
                return [
                    WizardStep(name="Welcome", builder=builder),
                    WizardStep(name="Confirm", builder=builder),
                ]

        view = WizardView(interaction=_make_interaction(), schema=SetupSchema())
        assert [s["name"] for s in view._steps] == ["Welcome", "Confirm"]

    def test_base_class_get_steps_raises(self):
        from cascadeui import WizardSchema

        with pytest.raises(NotImplementedError, match="must override get_steps"):
            WizardSchema().get_steps()

    def test_schema_and_steps_together_raises(self):
        from cascadeui import WizardSchema, WizardStep

        builder = lambda v: None

        class S(WizardSchema):
            def get_steps(self):
                return [WizardStep(name="A", builder=builder)]

        with pytest.raises(ValueError, match="either 'steps=' or 'schema='"):
            WizardView(
                interaction=_make_interaction(),
                steps=[{"name": "B", "builder": builder}],
                schema=S(),
            )

    def test_non_wizardschema_raises(self):
        with pytest.raises(TypeError, match="expects a WizardSchema instance"):
            WizardView(interaction=_make_interaction(), schema="not a schema")

    def test_no_steps_and_no_schema_returns_empty(self):
        """Zero-config wizard (no steps, no schema) is a valid state, not an error.

        Locks in the documented zero-config contract so a future refactor
        that adds a guard for "neither supplied" fails loudly against this
        test rather than silently flipping the polarity.
        """
        view = WizardView(interaction=_make_interaction())
        assert view._steps == []


class TestWizardViewInitialRender:
    """V1 step content reaches the first message, not just the pop edit."""

    async def test_send_ships_the_current_step_embed(self):
        """Step builders are async, so only send() can put them on the message."""

        async def step_one():
            return discord.Embed(title="Step One")

        interaction = _make_interaction()
        view = WizardView(
            interaction=interaction,
            steps=[{"id": "one", "title": "One", "builder": step_one}],
        )

        await view.send()

        embed = interaction.response.send_message.call_args.kwargs.get("embed")
        assert embed is not None
        assert embed.title == "Step One"

    async def test_send_ships_nav_alone_for_a_builderless_step(self):
        """A step with no builder contributes no embed and must not raise."""
        interaction = _make_interaction()
        view = WizardView(
            interaction=interaction,
            steps=[{"id": "one", "title": "One"}],
        )

        await view.send()

        assert interaction.response.send_message.call_args.kwargs.get("embed") is None

    async def test_refresh_ships_the_edit_for_a_builderless_step(self):
        """The nav row still has to reach Discord when a step has no builder.

        _sync_wizard_nav relabels the buttons in memory either way; skipping
        the edit when no embed comes back leaves the cursor advanced and the
        display on the previous step.
        """
        interaction = _make_interaction()
        view = WizardView(
            interaction=interaction,
            steps=[{"id": "one", "title": "One"}, {"id": "two", "title": "Two"}],
        )
        view._message = AsyncMock()

        await view._refresh_wizard()

        assert view._message.edit.await_count == 1


class TestWizardRefreshContent:
    """refresh_content() re-renders the current step (V1's carries the embed)."""

    async def test_v1_rerenders(self):
        async def embed_builder():
            return discord.Embed(title="step")

        view = WizardView(
            interaction=_make_interaction(),
            steps=[
                {"name": "A", "builder": embed_builder},
                {"name": "B", "builder": embed_builder},
            ],
        )
        view.refresh = AsyncMock()
        await view.refresh_content()
        embed = view.refresh.call_args.kwargs.get("embed")
        assert embed is not None  # not the empty-kwargs no-op reload() shipped

    async def test_v1_reload_ships_the_embed(self):
        async def sb():
            return discord.Embed(title="step")

        view = WizardView(
            interaction=_make_interaction(),
            steps=[{"name": "A", "builder": sb}, {"name": "B", "builder": sb}],
        )
        view.refresh = AsyncMock()
        await view.reload()
        embed = view.refresh.call_args.kwargs.get("embed")
        assert embed is not None

    async def test_a_step_render_waits_for_a_reload_already_rebuilding(self):
        """Both clear and refill the tree around an awaited step builder, so
        running side by side they shipped the nav row twice."""
        builds = {"n": 0}

        async def step_one():
            builds["n"] += 1
            await asyncio.sleep(0.05 if builds["n"] == 2 else 0.01)
            return TextDisplay(f"one {builds['n']}")

        async def step_two():
            return TextDisplay("two")

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[{"name": "One", "builder": step_one}, {"name": "Two", "builder": step_two}],
        )
        await view.on_load()
        message = MagicMock(id=1)
        message.edit = AsyncMock(return_value=message)
        view._message = message

        reloading = asyncio.create_task(view.reload())
        await asyncio.sleep(0)
        rendering = asyncio.create_task(view.refresh_content())
        await asyncio.wait_for(asyncio.gather(reloading, rendering), timeout=3)

        ids = [c.custom_id for c in view.walk_children() if getattr(c, "custom_id", None)]
        assert len(ids) == len(set(ids))
        assert sum(isinstance(c, TextDisplay) for c in view.children) == 1

    async def test_a_step_render_leaves_an_armed_view_on_its_refresh_button(self):
        """A background update that re-rendered the current step rebuilt the
        tree over the refresh button, and an armed view drops every
        notification that could put it back."""

        async def step():
            return TextDisplay("content")

        class Wizard(WizardLayoutView):
            auto_refresh_ephemeral = True

        view = Wizard(
            interaction=_make_interaction(),
            steps=[{"name": "One", "builder": step}, {"name": "Two", "builder": step}],
            timeout=None,
        )
        await view.send(ephemeral=True)
        await view._arm_refresh_button()
        armed_tree = [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]

        await view.refresh_content()

        assert [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)] == (
            armed_tree
        )

    async def test_explicit_embeds_are_sent_in_place_of_the_current_step(self):
        """The current step's embed was added beside them, and discord.py
        refuses ``embed`` and ``embeds`` together."""

        async def step_one():
            return discord.Embed(title="step")

        interaction = _make_interaction()
        view = WizardView(interaction=interaction, steps=[{"name": "One", "builder": step_one}])
        mine = [discord.Embed(title="mine")]

        await view.send(embeds=mine)

        sent = interaction.response.send_message.call_args.kwargs
        assert sent["embeds"] is mine
        assert "embed" not in sent

    @pytest.mark.parametrize(
        "cls, content",
        [
            (WizardView, lambda text: discord.Embed(title=text)),
            (WizardLayoutView, TextDisplay),
        ],
        ids=["v1", "v2"],
    )
    async def test_step_renders_that_outlive_an_exit_leave_the_frozen_panel_alone(
        self, cls, content
    ):
        """A render whose builder was running when exit() froze the panel, and
        one queued behind it, both shipped live controls onto it afterwards."""
        gate = asyncio.Event()
        builds = {"n": 0}

        async def step_one():
            builds["n"] += 1
            if builds["n"] > 1:
                await gate.wait()
            return content(f"one {builds['n']}")

        async def step_two():
            return content("two")

        view = cls(
            interaction=_make_interaction(),
            steps=[{"name": "One", "builder": step_one}, {"name": "Two", "builder": step_two}],
        )
        await view.send()
        message = view._message
        in_flight = asyncio.create_task(view.refresh_content())
        await asyncio.sleep(0)
        queued = asyncio.create_task(view.refresh_content())
        await asyncio.sleep(0)
        await view.exit()
        edits = message.edit.await_count

        gate.set()
        await asyncio.wait_for(asyncio.gather(in_flight, queued), timeout=2)

        assert message.edit.await_count == edits
        assert builds["n"] == 2  # the queued render never built on the dead view


class TestStepCursorRewindsWhenTheEditNeverLanded:
    """The step advances before the repaint, so a dropped edit desyncs them.

    ``_refresh_wizard``'s own docstring names the hazard the unconditional
    edit exists to prevent: the cursor advancing while the display keeps the
    previous step. A swallowed transport failure reintroduces it, so the
    cursor goes back to where the screen still is.
    """

    @staticmethod
    def _wire(view):
        view.interaction = _make_interaction()
        view.user_id = 1
        view.guild_id = 2
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock()
        view._message = message
        return message

    @pytest.mark.parametrize(
        "cls,steps",
        [
            (
                WizardLayoutView,
                [
                    {"name": "a", "builder": lambda: [TextDisplay("a")]},
                    {"name": "b", "builder": lambda: [TextDisplay("b")]},
                    {"name": "c", "builder": lambda: [TextDisplay("c")]},
                ],
            ),
            (
                WizardView,
                [
                    {"name": "a", "builder": lambda: discord.Embed(title="a")},
                    {"name": "b", "builder": lambda: discord.Embed(title="b")},
                ],
            ),
        ],
        ids=["v2", "v1"],
    )
    async def test_a_dropped_next_does_not_skip_a_step(self, cls, steps):
        view = cls(steps=steps)
        message = self._wire(view)

        message.edit = AsyncMock(side_effect=aiohttp.ClientOSError(104, "reset"))
        await view._go_next(_make_interaction())
        assert view._current_step == 0, "the cursor must stay on the step still shown"

        message.edit = AsyncMock()
        await view._go_next(_make_interaction())
        assert view._current_step == 1, "the recovery press advances one step, not two"

    @pytest.mark.parametrize(
        "cls,steps",
        [
            (
                WizardLayoutView,
                [
                    {"name": "a", "builder": lambda: [TextDisplay("a")]},
                    {"name": "b", "builder": lambda: [TextDisplay("b")]},
                    {"name": "c", "builder": lambda: [TextDisplay("c")]},
                ],
            ),
            (
                WizardView,
                [
                    {"name": "a", "builder": lambda: discord.Embed(title="a")},
                    {"name": "b", "builder": lambda: discord.Embed(title="b")},
                ],
            ),
        ],
        ids=["v2", "v1"],
    )
    async def test_a_refused_next_does_not_skip_a_step(self, cls, steps):
        view = cls(steps=steps)
        message = self._wire(view)

        message.edit = AsyncMock(
            side_effect=discord.HTTPException(
                MagicMock(status=400, reason="Bad Request"), "Invalid Form Body"
            )
        )
        with pytest.raises(discord.HTTPException):
            await view._go_next(_make_interaction())
        assert view._current_step == 0, "the cursor must stay on the step still shown"
        assert view._back_btn.disabled, "the nav row must describe the first step again"

        message.edit = AsyncMock()
        await view._go_next(_make_interaction())
        assert view._current_step == 1, "the recovery press advances one step, not two"

    async def test_a_step_whose_builder_raises_leaves_the_cursor(self):
        def broken():
            raise RuntimeError("database unavailable")

        view = WizardLayoutView(
            steps=[
                {"name": "a", "builder": lambda: [TextDisplay("a")]},
                {"name": "b", "builder": broken},
            ]
        )
        message = self._wire(view)

        with pytest.raises(RuntimeError, match="database unavailable"):
            await view._go_next(_make_interaction())

        assert view._current_step == 0
        message.edit.assert_not_awaited()
        shown = [c.content for c in view.walk_children() if isinstance(c, TextDisplay)]
        assert "a" in shown


# // ========================================( Indicator Label Shape )======================================== // #


class TestStepIndicatorLabelMustBeSynchronous:
    """The indicator label resolves inside the synchronous button build.

    discord.py stringifies whatever it is handed, so an async callable
    renders "<coroutine object ...>" on the button and nothing raises --
    the worst shape a wrong override can take, and the reason this one is
    refused at definition rather than backstopped at render.
    """

    def test_async_label_is_refused_at_definition(self):
        async def _labeller(current, total):
            return f"{current}/{total}"

        with pytest.raises(TypeError, match="step_indicator_label must be synchronous"):

            class _Bad(WizardLayoutView):
                step_indicator_label = _labeller

    def test_sync_label_still_defines(self):
        class _Good(WizardLayoutView):
            step_indicator_label = staticmethod(lambda current, total: f"{current} of {total}")

        assert _Good.step_indicator_label(2, 5) == "2 of 5"
