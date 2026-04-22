"""Tests for WizardView / WizardLayoutView customization and parity."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import Container, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.views.patterns import WizardLayoutView, WizardView

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


# // ========================================( Navigation hooks )======================================== // #


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

        builder = lambda v: None
        validator = lambda v: (True, None)
        condition = lambda: True
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
