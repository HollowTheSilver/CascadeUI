"""Tests for modal input wrappers and Modal construction contracts.

Covers the canonical validator-attachment API: validators live on
input wrapper instances (TextInput, Checkbox, CheckboxGroup, RadioGroup,
FileUpload) and are auto-collected by ``Modal`` at construction time.
"""

import asyncio

import discord
import pytest
from helpers import make_interaction

from cascadeui.components.inputs import (
    Checkbox,
    CheckboxGroup,
    FileUpload,
    Modal,
    RadioGroup,
    TextInput,
    _assign_submitted_value,
)
from cascadeui.state.singleton import get_store
from cascadeui.testing import stub_interaction
from cascadeui.validation import min_length, regex

# // ========================================( TextInput._slug )======================================== // #


class TestTextInputSlug:
    """TextInput._slug converts labels to deterministic custom_id fragments."""

    def test_basic_label(self):
        assert TextInput._slug("Username") == "input_username"

    def test_label_with_spaces(self):
        assert TextInput._slug("Full Name") == "input_full_name"

    def test_label_mixed_case(self):
        assert TextInput._slug("Email Address") == "input_email_address"

    def test_slug_matches_construction(self):
        field = TextInput(label="Username")
        assert field.custom_id == TextInput._slug("Username")

    def test_slug_is_classmethod(self):
        # Reachable both on the class and on an instance; no instance state required
        assert TextInput._slug("Foo") == "input_foo"
        assert TextInput(label="Bar")._slug("Foo") == "input_foo"

    def test_slug_delegates_to_slugify(self):
        # Punctuation collapses to a single underscore (the shared slugify
        # rule) instead of surviving verbatim, keeping modal input ids safe.
        from cascadeui import slugify

        assert TextInput._slug("A/B Test!") == "input_a_b_test"
        assert TextInput._slug("A/B Test!") == f"input_{slugify('A/B Test!')}"


# // ========================================( TextInput.validators )======================================== // #


class TestTextInputValidators:
    """TextInput stores and exposes validators from the kwarg."""

    def test_default_is_empty_list(self):
        field = TextInput(label="Username")
        assert field.validators == []

    def test_validators_kwarg_stored(self):
        v1 = min_length(3)
        v2 = regex(r"^[a-z]+$", "lowercase only")
        field = TextInput(label="Username", validators=[v1, v2])
        assert field.validators == [v1, v2]

    def test_validators_defensive_copy(self):
        # Mutating the source list after construction must not leak into the instance
        source = [min_length(3)]
        field = TextInput(label="Username", validators=source)
        source.append(regex(r"^[a-z]+$", "lowercase only"))
        assert len(field.validators) == 1

    def test_none_validators_becomes_empty_list(self):
        field = TextInput(label="Username", validators=None)
        assert field.validators == []


# // ========================================( Modal auto-collection )======================================== // #


class TestModalValidatorAutoCollect:
    """Modal auto-collects validators from all five input wrapper types."""

    def test_no_validators_produces_empty_dict(self):
        modal = Modal(
            title="Test",
            inputs=[TextInput(label="Username"), TextInput(label="Email")],
        )
        assert modal.validators == {}

    def test_single_field_validators_collected(self):
        v = min_length(3)
        modal = Modal(
            title="Test",
            inputs=[TextInput(label="Username", validators=[v])],
        )
        assert modal.validators == {"input_username": [v]}

    def test_multiple_fields_collected_under_custom_ids(self):
        v1 = min_length(3)
        v2 = regex(r"@", "must contain @")
        modal = Modal(
            title="Test",
            inputs=[
                TextInput(label="Username", validators=[v1]),
                TextInput(label="Email", validators=[v2]),
                TextInput(label="Bio"),  # no validators -- not in dict
            ],
        )
        assert modal.validators == {
            "input_username": [v1],
            "input_email": [v2],
        }
        assert "input_bio" not in modal.validators

    def test_modal_collects_defensive_copy(self):
        # Mutating TextInput.validators after Modal construction must not leak
        v1 = min_length(3)
        field = TextInput(label="Username", validators=[v1])
        modal = Modal(title="Test", inputs=[field])
        field.validators.append(regex(r"^[a-z]+$", "lowercase only"))
        assert len(modal.validators["input_username"]) == 1

    def test_raw_discord_textinput_escape_hatch(self):
        # Raw discord.ui.TextInput items still work but carry no validators
        raw = discord.ui.TextInput(label="Raw", custom_id="raw_field")
        modal = Modal(
            title="Test",
            inputs=[TextInput(label="Username", validators=[min_length(3)]), raw],
        )
        assert "input_username" in modal.validators
        assert "raw_field" not in modal.validators
        assert "raw_field" in modal.inputs


# // ========================================( Modal view_id dispatch wiring )======================================== // #


class TestModalViewIdWiring:
    """Modal stores view_id for callback routing back to the originating view."""

    def test_view_id_stored_from_kwargs(self):
        modal = Modal(title="Test", inputs=[TextInput(label="X")], view_id="view_abc")
        assert modal.view_id == "view_abc"

    def test_view_id_defaults_to_none(self):
        modal = Modal(title="Test", inputs=[TextInput(label="X")])
        assert modal.view_id is None

    def test_an_unrecognized_keyword_is_refused(self):
        """``on_submit`` is the method discord.py subclasses override.

        Reaching for it as the constructor keyword left the handler unset,
        and the modal then acknowledged every submission, ran nothing, and
        logged nothing.
        """
        with pytest.raises(TypeError, match="on_submit"):
            Modal(title="Test", inputs=[TextInput(label="X")], on_submit=lambda i, v: None)

    def test_custom_id_reaches_discord_py(self):
        """A real ``discord.ui.Modal`` parameter was discarded with the rest."""
        modal = Modal(title="Test", inputs=[TextInput(label="X")], custom_id="settings_modal")
        assert modal.custom_id == "settings_modal"

    def test_a_one_parameter_callback_is_refused(self):
        """``on_submit`` calls the handler with the collected values, always.

        A one-parameter callback could only ever raise a bare arity error at
        submit time, from inside the library.
        """

        async def handler(interaction):
            pass

        with pytest.raises(TypeError, match=r"cannot be called with \(interaction, values\)"):
            Modal(title="Test", inputs=[TextInput(label="X")], callback=handler)

    def test_a_two_parameter_callback_is_accepted(self):
        async def handler(interaction, values):
            pass

        modal = Modal(title="Test", inputs=[TextInput(label="X")], callback=handler)

        assert modal.user_callback is handler


# // ========================================( TextInput.value + Modal.values_by_input )======================================== // #


class TestSubmittedValuePropagation:
    """After submit, each wrapped TextInput instance owns its submitted value."""

    def test_textinput_value_defaults_to_none(self):
        field = TextInput(label="Username")
        assert field.value is None

    def test_modal_values_by_input_empty_before_submit(self):
        modal = Modal(title="T", inputs=[TextInput(label="X")])
        assert modal.values_by_input == {}

    async def test_rejected_value_is_not_written_to_the_wrapper(self):
        """The wrapper is documented as populated only after validation passes.

        The write-back used to run before the validation gate, so a caller
        reading ``.value`` after a failed submit saw the rejected input.
        """
        from unittest.mock import AsyncMock, MagicMock

        from cascadeui.validation import ValidationResult

        def reject(value, field, all_values):
            return ValidationResult(False, "no")

        field = TextInput(label="Email", required=True, validators=[reject])
        modal = Modal(title="T", inputs=[field])
        for wrapped, discord_input in modal._wrapped_pairs:
            discord_input._value = "rejected"

        interaction = MagicMock()
        interaction.user.id = 1
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        await modal.on_submit(interaction)

        assert field.value is None  # not populated on failure
        assert modal.values_by_input == {}

    def test_wrapped_pairs_track_input_instances(self):
        a = TextInput(label="Name")
        b = TextInput(label="Age")
        modal = Modal(title="T", inputs=[a, b])
        assert len(modal._wrapped_pairs) == 2
        assert modal._wrapped_pairs[0][0] is a
        assert modal._wrapped_pairs[1][0] is b

    async def test_on_submit_stamps_value_onto_instances(self):
        # discord.ui.TextInput.value is a real property over discord.py
        # internals and cannot be set directly. Replace the paired children
        # with lightweight shims exposing only the attributes on_submit reads.
        a = TextInput(label="Name")
        b = TextInput(label="Age")

        captured = {}

        async def callback(interaction, values):
            captured["a"] = a.value
            captured["b"] = b.value
            captured["by_input_a"] = interaction._modal.values_by_input[a]

        modal = Modal(title="T", inputs=[a, b], callback=callback)

        class _Shim:
            def __init__(self, custom_id, value):
                self.custom_id = custom_id
                self.value = value

        shim_a = _Shim("input_name", "Kael")
        shim_b = _Shim("input_age", "42")
        modal._wrapped_pairs = [(a, shim_a), (b, shim_b)]
        # ``self.children`` is a read-only property on discord.ui.Modal; patch
        # it on the type for the duration of this call so the legacy
        # values-dict collection loop sees the shims instead of the real
        # discord.ui.TextInput children (whose ``.value`` is unwritable).
        from unittest.mock import patch

        class _Response:
            def __init__(self):
                self._done = False

            def is_done(self):
                return self._done

            async def defer(self):
                self._done = True

        class _Interaction:
            def __init__(self, modal):
                self.response = _Response()
                self._modal = modal
                self.user = type("U", (), {"id": 1})()

        interaction = _Interaction(modal)
        with patch.object(type(modal).__mro__[1], "children", new=[shim_a, shim_b], create=True):
            await modal.on_submit(interaction)

        assert a.value == "Kael"
        assert b.value == "42"
        assert modal.values_by_input == {a: "Kael", b: "42"}
        assert captured == {"a": "Kael", "b": "42", "by_input_a": "Kael"}


class TestModalAckBackstop:
    """discord.py's modal dispatch has no auto-defer timer, so ``on_submit``
    arms one before the (potentially slow, I/O-bound) validators run on the 3s
    interaction clock.
    """

    async def test_timer_fires_during_slow_validator(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from cascadeui.validation import ValidationResult

        deferred_during = {}

        async def slow_reject(value, field, all_values):
            # Simulate a DB-backed validator on the 3s clock.
            await asyncio.sleep(0.15)
            deferred_during["value"] = interaction.response.defer.called
            return ValidationResult(False, "no")

        field = TextInput(label="Email", required=True, validators=[slow_reject])
        modal = Modal(title="T", inputs=[field])
        modal.auto_defer_delay = 0.05  # fires before the 0.15s validator ends
        for wrapped, discord_input in modal._wrapped_pairs:
            discord_input._value = "x"

        interaction = MagicMock()
        interaction.user.id = 1
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await modal.on_submit(interaction)

        assert deferred_during["value"] is True

    async def test_scheduled_task_covers_slow_interaction_check(self):
        """A slow interaction_check override is acked before the 3s wall.

        discord.py's dispatcher runs interaction_check before on_submit, so
        on_submit's own timer cannot cover it; the Modal._scheduled_task
        override arms a backstop for the whole dispatch.
        """
        import asyncio

        from helpers import make_interaction

        acked_during_check = {}

        class SlowCheckModal(Modal):
            auto_defer_delay = 0.05

            async def interaction_check(self, interaction):
                await asyncio.sleep(0.15)
                acked_during_check["value"] = interaction.response.defer.await_count >= 1
                return True

            async def on_submit(self, interaction):
                pass

        modal = SlowCheckModal(title="T", inputs=[])
        interaction = make_interaction()

        await modal._scheduled_task(interaction, [], {})

        assert acked_during_check["value"] is True

    async def test_scheduled_task_acks_on_raise(self):
        """A fast-raising on_submit does not leave the interaction unacked.

        discord.py's Modal.on_error only logs, so without the override's
        post-dispatch defer a raising validator or callback would strand the
        interaction as "This interaction failed".
        """
        from helpers import make_interaction

        class RaiseModal(Modal):
            async def interaction_check(self, interaction):
                return True

            async def on_submit(self, interaction):
                raise RuntimeError("fast failure")

        modal = RaiseModal(title="T", inputs=[])
        interaction = make_interaction()

        await modal._scheduled_task(interaction, [], {})

        assert interaction.response.is_done()

    def test_negative_auto_defer_delay_rejected_at_definition(self):
        """A non-positive auto_defer_delay fails when the Modal subclass is defined."""
        with pytest.raises(ValueError, match="auto_defer_delay must be a positive number"):

            class BadModal(Modal):
                auto_defer_delay = -1


class TestModalPostSubmitDeferHardening:
    """The trailing post-submit ack mirrors the view's ``_scheduled_task``
    defer: a dead (10062) or already-acked interaction must not turn a
    successful submission into an unhandled error routed to ``on_error``."""

    async def test_post_submit_defer_swallows_dead_interaction(self):
        from unittest.mock import AsyncMock, MagicMock

        modal = Modal(title="T", inputs=[])

        interaction = MagicMock()
        interaction.user = MagicMock(id=1)
        interaction.response = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.defer = AsyncMock(side_effect=discord.NotFound(MagicMock(), ""))

        await modal.on_submit(interaction)  # must not raise

    async def test_post_submit_defer_swallows_already_acked_race(self):
        from unittest.mock import AsyncMock, MagicMock

        modal = Modal(title="T", inputs=[])

        interaction = MagicMock()
        interaction.user = MagicMock(id=1)
        interaction.response = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.defer = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=400), {"code": 40060})
        )

        await modal.on_submit(interaction)  # must not raise


# // ========================================( Checkbox )======================================== // #


class TestCheckbox:
    """Checkbox input derives custom_id from label and stores boolean value."""

    def test_custom_id_from_label(self):
        cb = Checkbox(label="Accept Terms")
        assert cb.custom_id == "input_accept_terms"

    def test_default_value(self):
        cb = Checkbox(label="Opt In")
        assert cb.value is None
        assert cb.default is False

    def test_default_true(self):
        cb = Checkbox(label="Opt In", default=True)
        assert cb.default is True

    def test_validators_stored(self):
        v = min_length(1)  # not meaningful for bool, but tests the plumbing
        cb = Checkbox(label="Check", validators=[v])
        assert cb.validators == [v]

    def test_create_discord_component(self):
        cb = Checkbox(label="Check", default=True)
        comp = cb.create_discord_component()
        assert isinstance(comp, discord.ui.Label)
        assert comp.text == "Check"
        inner = comp.component
        assert isinstance(inner, discord.ui.Checkbox)
        assert inner.custom_id == "input_check"
        assert inner.default is True


# // ========================================( CheckboxGroup )======================================== // #


class TestCheckboxGroup:
    """CheckboxGroup input derives custom_id from label and stores multi-values."""

    def test_custom_id_from_label(self):
        cg = CheckboxGroup(label="Toppings", options=[{"label": "Cheese", "value": "cheese"}])
        assert cg.custom_id == "input_toppings"

    def test_values_default_none(self):
        cg = CheckboxGroup(label="X", options=[{"label": "A", "value": "a"}])
        assert cg.values is None

    def test_empty_options_rejected_at_construction(self):
        """Discord accepts 1-10 checkbox options; the bound is enforced at
        construction so the mistake surfaces where the options are built,
        not as an HTTP 400 when the modal opens.
        """
        with pytest.raises(ValueError, match="accepts 1-10"):
            CheckboxGroup(label="Empty", options=[])

    def test_eleven_options_rejected_at_construction(self):
        options = [{"label": f"O{i}", "value": str(i)} for i in range(11)]
        with pytest.raises(ValueError, match="accepts 1-10"):
            CheckboxGroup(label="Overfull", options=options)

    def test_ten_options_accepted(self):
        options = [{"label": f"O{i}", "value": str(i)} for i in range(10)]
        cg = CheckboxGroup(label="Full", options=options)
        assert len(cg.options) == 10

    def test_dict_options_converted(self):
        cg = CheckboxGroup(
            label="Toppings",
            options=[
                {"label": "Cheese", "value": "cheese"},
                {"label": "Bacon"},
            ],
        )
        assert len(cg.options) == 2
        assert isinstance(cg.options[0], discord.CheckboxGroupOption)
        assert cg.options[1].value == "Bacon"  # defaults to label

    def test_native_options_passthrough(self):
        opt = discord.CheckboxGroupOption(label="A", value="a")
        cg = CheckboxGroup(label="X", options=[opt])
        assert cg.options[0] is opt

    def test_create_discord_component(self):
        cg = CheckboxGroup(
            label="Sizes",
            options=[discord.CheckboxGroupOption(label="S", value="s")],
            min_values=1,
            max_values=3,
        )
        comp = cg.create_discord_component()
        assert isinstance(comp, discord.ui.Label)
        assert comp.text == "Sizes"
        inner = comp.component
        assert isinstance(inner, discord.ui.CheckboxGroup)
        assert inner.custom_id == "input_sizes"
        assert inner.min_values == 1
        assert inner.max_values == 3

    def test_validators_collected_by_modal(self):
        v = min_length(1)
        cg = CheckboxGroup(
            label="Picks",
            options=[discord.CheckboxGroupOption(label="A", value="a")],
            validators=[v],
        )
        modal = Modal(title="T", inputs=[cg])
        assert modal.validators == {"input_picks": [v]}

    def test_wrapped_pairs_include_checkbox_group(self):
        cg = CheckboxGroup(
            label="X",
            options=[discord.CheckboxGroupOption(label="A", value="a")],
        )
        modal = Modal(title="T", inputs=[cg])
        assert len(modal._wrapped_pairs) == 1
        assert modal._wrapped_pairs[0][0] is cg


# // ========================================( RadioGroup )======================================== // #


class TestConstructionBoundValidation:
    """Documented numeric bounds raise a directed ValueError at construction,
    instead of a Discord HTTP 400 when the modal opens."""

    def test_textinput_min_length_out_of_range(self):
        with pytest.raises(ValueError, match="min_length=5000"):
            TextInput(label="X", min_length=5000)

    def test_textinput_max_length_out_of_range(self):
        with pytest.raises(ValueError, match="max_length=5000"):
            TextInput(label="X", max_length=5000)

    def test_textinput_valid_bounds_ok(self):
        ti = TextInput(label="X", min_length=0, max_length=100)
        assert ti.min_length == 0 and ti.max_length == 100

    def test_textinput_none_bounds_ok(self):
        ti = TextInput(label="X")
        assert ti.min_length is None and ti.max_length is None

    def test_checkboxgroup_max_values_out_of_range(self):
        with pytest.raises(ValueError, match="max_values=11"):
            CheckboxGroup(label="X", options=[{"label": "A", "value": "a"}], max_values=11)

    def test_fileupload_max_values_out_of_range(self):
        with pytest.raises(ValueError, match="max_values=11"):
            FileUpload(label="X", max_values=11)

    def test_fileupload_valid_bounds_ok(self):
        fu = FileUpload(label="X", min_values=0, max_values=5)
        assert fu.max_values == 5

    def test_statefulselect_option_cap_raises(self):
        from cascadeui.components.base import StatefulSelect

        options = [discord.SelectOption(label=f"o{i}", value=str(i)) for i in range(26)]
        with pytest.raises(ValueError, match="at most 25"):
            StatefulSelect(options=options)

    def test_statefulselect_at_cap_ok(self):
        from cascadeui.components.base import StatefulSelect

        options = [discord.SelectOption(label=f"o{i}", value=str(i)) for i in range(25)]
        sel = StatefulSelect(options=options)
        assert len(sel.options) == 25


class TestRadioGroup:
    """RadioGroup input derives custom_id from label and stores single value."""

    def test_custom_id_from_label(self):
        rg = RadioGroup(
            label="Difficulty",
            options=[{"label": "Easy", "value": "e"}, {"label": "Hard", "value": "h"}],
        )
        assert rg.custom_id == "input_difficulty"

    def test_value_default_none(self):
        rg = RadioGroup(
            label="X",
            options=[{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
        )
        assert rg.value is None

    def test_single_option_rejected_at_construction(self):
        """Discord requires 2-10 radio options; the bound is enforced at
        construction so the mistake surfaces where the options are built.
        """
        with pytest.raises(ValueError, match="requires 2-10"):
            RadioGroup(label="Lonely", options=[{"label": "A", "value": "a"}])

    def test_eleven_options_rejected_at_construction(self):
        options = [{"label": f"O{i}", "value": str(i)} for i in range(11)]
        with pytest.raises(ValueError, match="requires 2-10"):
            RadioGroup(label="Overfull", options=options)

    def test_dict_options_converted(self):
        rg = RadioGroup(
            label="Size",
            options=[
                {"label": "Small", "value": "sm"},
                {"label": "Large"},
            ],
        )
        assert len(rg.options) == 2
        assert isinstance(rg.options[0], discord.RadioGroupOption)

    def test_create_discord_component(self):
        rg = RadioGroup(
            label="Mode",
            options=[
                discord.RadioGroupOption(label="Easy", value="easy"),
                discord.RadioGroupOption(label="Hard", value="hard"),
            ],
        )
        comp = rg.create_discord_component()
        assert isinstance(comp, discord.ui.Label)
        assert comp.text == "Mode"
        inner = comp.component
        assert isinstance(inner, discord.ui.RadioGroup)
        assert inner.custom_id == "input_mode"

    def test_validators_collected_by_modal(self):
        v = regex(r"^easy$", "must be easy")
        rg = RadioGroup(
            label="Mode",
            options=[
                discord.RadioGroupOption(label="Easy", value="easy"),
                discord.RadioGroupOption(label="Hard", value="hard"),
            ],
            validators=[v],
        )
        modal = Modal(title="T", inputs=[rg])
        assert "input_mode" in modal.validators


# // ========================================( FileUpload )======================================== // #


class TestFileUpload:
    """FileUpload input derives custom_id from label and stores file values."""

    def test_custom_id_from_label(self):
        fu = FileUpload(label="Avatar")
        assert fu.custom_id == "input_avatar"

    def test_values_default_none(self):
        fu = FileUpload(label="X")
        assert fu.values is None

    def test_create_discord_component(self):
        fu = FileUpload(label="Docs", min_values=1, max_values=5)
        comp = fu.create_discord_component()
        assert isinstance(comp, discord.ui.Label)
        assert comp.text == "Docs"
        inner = comp.component
        assert isinstance(inner, discord.ui.FileUpload)
        assert inner.custom_id == "input_docs"
        assert inner.min_values == 1
        assert inner.max_values == 5

    def test_wrapped_pairs_include_file_upload(self):
        fu = FileUpload(label="Upload")
        modal = Modal(title="T", inputs=[fu])
        assert len(modal._wrapped_pairs) == 1
        assert modal._wrapped_pairs[0][0] is fu


# // ========================================( ui.Label description )======================================== // #


class TestLabelDescription:
    """The ``description=`` kwarg lands on ``ui.Label.description`` for every wrapper."""

    def test_text_input_description(self):
        ti = TextInput(label="Name", description="Your full name")
        comp = ti.create_discord_component()
        assert isinstance(comp, discord.ui.Label)
        assert comp.description == "Your full name"

    def test_checkbox_description(self):
        cb = Checkbox(label="Agree", description="To the terms")
        comp = cb.create_discord_component()
        assert comp.description == "To the terms"

    def test_checkbox_group_description(self):
        cg = CheckboxGroup(
            label="Toppings",
            description="Pick any combination",
            options=[discord.CheckboxGroupOption(label="A", value="a")],
        )
        comp = cg.create_discord_component()
        assert comp.description == "Pick any combination"

    def test_radio_group_description(self):
        rg = RadioGroup(
            label="Mode",
            description="Choose one",
            options=[
                discord.RadioGroupOption(label="A", value="a"),
                discord.RadioGroupOption(label="B", value="b"),
            ],
        )
        comp = rg.create_discord_component()
        assert comp.description == "Choose one"

    def test_file_upload_description(self):
        fu = FileUpload(label="Docs", description="PDF or DOCX only")
        comp = fu.create_discord_component()
        assert comp.description == "PDF or DOCX only"

    def test_description_default_is_none(self):
        ti = TextInput(label="Name")
        comp = ti.create_discord_component()
        assert comp.description is None


# // ========================================( Modal child structure )======================================== // #


class TestModalChildStructure:
    """Modal carries ``ui.Label`` children for wrapped inputs; raw items pass through."""

    def test_wrapped_input_renders_as_label_child(self):
        ti = TextInput(label="Name")
        modal = Modal(title="T", inputs=[ti])
        # The modal's first child is a Label wrapping the TextInput
        children = list(modal.children)
        assert len(children) == 1
        assert isinstance(children[0], discord.ui.Label)
        assert isinstance(children[0].component, discord.ui.TextInput)

    def test_raw_label_passthrough(self):
        # User constructs ui.Label themselves -- escape hatch path
        raw_label = discord.ui.Label(
            text="Custom",
            component=discord.ui.TextInput(custom_id="raw_field"),
        )
        modal = Modal(title="T", inputs=[raw_label])
        children = list(modal.children)
        assert children[0] is raw_label
        # Modal indexes by inner component's custom_id
        assert "raw_field" in modal.inputs


# // ========================================( Modal mixed inputs )======================================== // #


class TestModalMixedInputs:
    """Modal correctly handles a mix of all five wrapper types."""

    def test_all_wrapper_types_accepted(self):
        inputs = [
            TextInput(label="Name"),
            Checkbox(label="Agree"),
            CheckboxGroup(
                label="Colors",
                options=[discord.CheckboxGroupOption(label="Red", value="r")],
            ),
            RadioGroup(
                label="Size",
                options=[
                    discord.RadioGroupOption(label="S", value="s"),
                    discord.RadioGroupOption(label="L", value="l"),
                ],
            ),
            FileUpload(label="File"),
        ]
        modal = Modal(title="Mixed", inputs=inputs)
        assert len(modal._wrapped_pairs) == 5
        assert set(modal.inputs.keys()) == {
            "input_name",
            "input_agree",
            "input_colors",
            "input_size",
            "input_file",
        }

    def test_validators_collected_across_types(self):
        v1 = min_length(1)
        v2 = regex(r".", "non-empty")
        inputs = [
            TextInput(label="Name", validators=[v1]),
            RadioGroup(
                label="Mode",
                options=[
                    discord.RadioGroupOption(label="A", value="a"),
                    discord.RadioGroupOption(label="B", value="b"),
                ],
                validators=[v2],
            ),
            Checkbox(label="Ok"),  # no validators
        ]
        modal = Modal(title="T", inputs=inputs)
        assert len(modal.validators) == 2
        assert "input_name" in modal.validators
        assert "input_mode" in modal.validators
        assert "input_ok" not in modal.validators

    async def test_write_back_uses_value_for_single_values(self):
        """Checkbox and RadioGroup write back to .value (singular)."""
        cb = Checkbox(label="Agree")
        rg = RadioGroup(
            label="Pick",
            options=[
                discord.RadioGroupOption(label="A", value="a"),
                discord.RadioGroupOption(label="B", value="b"),
            ],
        )
        modal = Modal(title="T", inputs=[cb, rg])

        class _ValShim:
            def __init__(self, custom_id, value):
                self.custom_id = custom_id
                self.value = value

        shim_cb = _ValShim("input_agree", True)
        shim_rg = _ValShim("input_pick", "a")
        modal._wrapped_pairs = [(cb, shim_cb), (rg, shim_rg)]

        class _Response:
            _done = False

            def is_done(self):
                return self._done

            async def defer(self):
                self._done = True

        class _Interaction:
            response = _Response()
            user = type("U", (), {"id": 1})()

        from unittest.mock import patch

        interaction = _Interaction()
        with patch.object(type(modal).__mro__[1], "children", new=[shim_cb, shim_rg], create=True):
            await modal.on_submit(interaction)

        assert cb.value is True
        assert rg.value == "a"

    async def test_write_back_uses_values_for_multi_values(self):
        """CheckboxGroup and FileUpload write back to .values (plural)."""
        cg = CheckboxGroup(
            label="Picks",
            options=[discord.CheckboxGroupOption(label="A", value="a")],
        )
        fu = FileUpload(label="Docs")
        modal = Modal(title="T", inputs=[cg, fu])

        class _ValsShim:
            def __init__(self, custom_id, values):
                self.custom_id = custom_id
                self.values = values

        shim_cg = _ValsShim("input_picks", ["a", "b"])
        shim_fu = _ValsShim("input_docs", ["attachment_obj"])
        modal._wrapped_pairs = [(cg, shim_cg), (fu, shim_fu)]

        class _Response:
            _done = False

            def is_done(self):
                return self._done

            async def defer(self):
                self._done = True

        class _Interaction:
            response = _Response()
            user = type("U", (), {"id": 1})()

        from unittest.mock import patch

        interaction = _Interaction()
        with patch.object(type(modal).__mro__[1], "children", new=[shim_cg, shim_fu], create=True):
            await modal.on_submit(interaction)

        assert cg.values == ["a", "b"]
        assert fu.values == ["attachment_obj"]
        assert modal.values_by_input[cg] == ["a", "b"]
        assert modal.values_by_input[fu] == ["attachment_obj"]


# // ========================================( Modal validation error label )======================================== // #


class TestModalValidationErrorLabel:
    """A failed validator surfaces the field label, not the custom_id slug."""

    async def test_error_message_uses_label_not_slug(self):
        from cascadeui.validation import ValidationResult

        def _always_fail(value, field, all_values):
            return ValidationResult(False, "Enter a single emoji.")

        # Required so the validator fires on the empty submitted value; an
        # optional blank field skips validation.
        field = TextInput(label="Emoji", required=True, validators=[_always_fail])
        assert field.custom_id == "input_emoji"  # the derived slug
        modal = Modal(title="T", inputs=[field])

        sent = {}

        class _Response:
            def is_done(self):
                return False

            async def send_message(self, content, **kwargs):
                sent["content"] = content

        class _Interaction:
            response = _Response()
            user = type("U", (), {"id": 1})()

        await modal.on_submit(_Interaction())

        assert "**Emoji**" in sent["content"]
        assert "input_emoji" not in sent["content"]


class TestStubInteraction:
    """``submit()`` asks for a double; ``cascadeui.testing`` supplies it.

    Left to build one, a consumer both over-builds and under-builds: the
    surface actually read is three attributes, while the part that cannot
    be reached by measurement is which half answers a rejection, since
    that depends on whether an ack backstop had already fired.
    """

    @staticmethod
    def _modal():
        async def on_submitted(interaction, values):
            pass

        return Modal(
            title="V",
            inputs=[TextInput(label="Name", validators=[min_length(5)])],
            callback=on_submitted,
        )

    async def test_a_rejection_is_assertable_without_a_connection(self):
        interaction = stub_interaction()

        assert await self._modal().submit(interaction, {"Name": "ab"}) is False
        assert any("Name" in reply for reply in interaction.replies)
        assert interaction.answered is True

    async def test_an_acceptance_acknowledges_and_says_nothing(self):
        interaction = stub_interaction()

        assert await self._modal().submit(interaction, {"Name": "abcdef"}) is True
        assert interaction.replies == []
        assert interaction.deferred is True

    async def test_a_second_answer_is_refused_as_the_real_slot_refuses_it(self):
        """The double must not be more permissive than what it stands for.

        Permitting a double-response offline would let a test pass over a
        seam that raises in production, which is the failure the whole
        offline surface exists to remove.
        """
        interaction = stub_interaction()
        await interaction.response.send_message("first")

        with pytest.raises(discord.InteractionResponded):
            await interaction.response.send_message("second")

    async def test_opening_a_modal_counts_as_answered(self):
        """It spends the response slot, so the seam has answered."""
        interaction = stub_interaction()
        await interaction.response.send_modal(object())

        assert interaction.response.is_done() is True
        assert interaction.answered is True

    async def test_the_response_slot_reports_itself_spent(self):
        """A library path that checks before answering sees a real slot."""
        interaction = stub_interaction()
        assert interaction.response.is_done() is False

        await self._modal().submit(interaction, {"Name": "ab"})

        assert interaction.response.is_done() is True


class TestModalSubmit:
    """``submit()`` drives the pipeline a real submission takes.

    The reason it exists is the validator pass. Reaching past it to the
    stored callback runs neither the validators nor the state dispatch,
    so a test written that way succeeds against input the modal would
    have rejected and reports coverage of a seam it never crossed.
    """

    @staticmethod
    def _modal(seen):
        name = TextInput(label="Name", validators=[min_length(3)], required=True)

        async def on_submitted(interaction, values):
            seen.append(values)

        return Modal(title="Profile", inputs=[name], callback=on_submitted)

    async def test_a_rejected_value_reports_false_and_skips_the_callback(self):
        """The case the direct-callback route could not see."""
        seen = []
        modal = self._modal(seen)

        accepted = await modal.submit(make_interaction(), {"Name": "ab"})

        assert accepted is False
        assert seen == []

    async def test_an_accepted_value_runs_the_whole_pipeline(self):
        seen = []
        modal = self._modal(seen)

        accepted = await modal.submit(make_interaction(), {"Name": "Ada"})

        assert accepted is True
        # The callback ran, and with the values a real submit assembles.
        assert seen == [{"input_name": "Ada"}]
        # The wrapper write-back and the by-input mapping both landed.
        assert modal.inputs["input_name"].value == "Ada"
        assert len(modal.values_by_input) == 1

    async def test_values_key_by_label_or_custom_id(self):
        """A caller writes labels; the internal mapping uses custom_ids."""
        by_label, by_id = [], []
        await self._modal(by_label).submit(make_interaction(), {"Name": "Ada"})
        await self._modal(by_id).submit(make_interaction(), {"input_name": "Ada"})

        assert by_label == by_id == [{"input_name": "Ada"}]

    async def test_an_unknown_key_is_refused_naming_the_valid_ones(self):
        modal = self._modal([])

        with pytest.raises(ValueError, match="no input named"):
            await modal.submit(make_interaction(), {"Nmae": "typo"})

    async def test_a_field_left_out_keeps_its_value(self):
        """A test supplies only the fields it cares about."""
        seen = []
        first = TextInput(label="First", required=False)
        second = TextInput(label="Second", required=False, default="kept")

        async def on_submitted(interaction, values):
            seen.append(values)

        modal = Modal(title="Two", inputs=[first, second], callback=on_submitted)
        await modal.submit(make_interaction(), {"First": "set"})

        assert seen == [{"input_first": "set", "input_second": "kept"}]

    async def test_the_state_dispatch_the_direct_route_skipped(self):
        """``MODAL_SUBMITTED`` reaches the store, as it does on a real submit."""
        store = get_store()
        fired = []
        store.on("MODAL_SUBMITTED", lambda action, state: fired.append(action))

        modal = self._modal([])
        modal.view_id = "view-under-test"
        await modal.submit(make_interaction(), {"Name": "Ada"})

        assert len(fired) == 1
        assert fired[0]["payload"]["values"] == {"input_name": "Ada"}

    async def test_a_raw_escape_hatch_input_is_reachable(self):
        """Raw items live in ``inputs`` but never in ``_wrapped_pairs``.

        Resolving from the pair list alone leaves a raw input unsettable
        and silent about it, so the submission runs against its default
        while the call reports success.
        """
        seen = []

        async def on_submitted(interaction, values):
            seen.append(values)

        raw = discord.ui.TextInput(custom_id="raw_field")
        modal = Modal(
            title="Mixed",
            inputs=[discord.ui.Label(text="Raw Field", component=raw)],
            callback=on_submitted,
        )

        assert await modal.submit(make_interaction(), {"raw_field": "R"}) is True
        assert raw.value == "R"

    async def test_a_raw_input_resolves_by_its_label_text(self):
        """A raw item's name is the text on the ``ui.Label`` wrapping it."""
        raw = discord.ui.TextInput(custom_id="rf2")

        async def on_submitted(interaction, values):
            pass

        modal = Modal(
            title="Labelled",
            inputs=[discord.ui.Label(text="Nice Name", component=raw)],
            callback=on_submitted,
        )
        await modal.submit(make_interaction(), {"Nice Name": "via label"})

        assert raw.value == "via label"

    async def test_a_custom_id_outranks_a_label_claiming_the_same_name(self):
        """A raw item carries any custom_id, so it can equal a label.

        The custom_id is that input's identity and its only name. The
        label is an alias, and one the wrapped input does not need, since
        its own custom_id resolves. So the identity wins and the alias is
        not registered: refusing the name instead would leave the raw
        input reachable by nothing at all.
        """
        seen = []

        async def on_submitted(interaction, values):
            seen.append(values)

        raw = discord.ui.TextInput(custom_id="Name")
        wrapped = TextInput(label="Name")
        modal = Modal(
            title="Clash",
            inputs=[
                discord.ui.Label(text="Raw", component=raw),
                wrapped,
            ],
            callback=on_submitted,
        )

        assert await modal.submit(make_interaction(), {"Name": "to the raw"}) is True
        assert raw.value == "to the raw"

        # And the input whose label lost the name is still reachable.
        assert await modal.submit(make_interaction(), {"input_name": "to the wrapper"}) is True
        assert wrapped.value == "to the wrapper"

    async def test_a_raising_validator_propagates_as_itself(self):
        """The validator's own exception reaches the caller, unconverted.

        A validator raising is a programmer error, not a rejection, so it
        must not arrive as the guard's "no verdict" RuntimeError, and
        ``values_by_input`` must be left the dict it is from construction
        onward rather than a ``None`` the class never otherwise exposes.

        The validator takes the three arguments ``validate_field`` calls
        it with. An earlier version took one, so the exception under test
        never fired: the arity mismatch raised first, and a broad
        ``pytest.raises`` could not tell the two apart.
        """

        def explode(value, field_def, all_values):
            raise RuntimeError("validator exploded")

        async def on_submitted(interaction, values):
            pass

        modal = Modal(
            title="Boom",
            inputs=[TextInput(label="Name", validators=[explode])],
            callback=on_submitted,
        )

        with pytest.raises(RuntimeError, match="validator exploded"):
            await modal.submit(make_interaction(), {"Name": "Ada"})

        assert modal.values_by_input == {}

    @staticmethod
    def _validated(cls=Modal, seen=None):
        async def on_submitted(interaction, values):
            if seen is not None:
                seen.append(values)

        return cls(
            title="V",
            inputs=[TextInput(label="Name", validators=[min_length(5)])],
            callback=on_submitted,
        )

    async def test_a_replacing_override_is_refused_rather_than_reported(self):
        """No verdict is not a rejection, and must not be returned as one.

        The verdict is what ``Modal.on_submit`` records as it runs. A
        subclass that replaces the method without calling up records
        nothing, so reporting ``False`` would call every submission
        rejected, silently, on a documented seam.
        """

        class Replaced(Modal):
            async def on_submit(self, interaction):
                pass

        with pytest.raises(RuntimeError, match="got no verdict"):
            await self._validated(Replaced).submit(make_interaction(), {"Name": "abcdef"})

    async def test_an_instance_assigned_handler_is_refused_too(self):
        """The call dispatches through the instance, so the check must.

        Comparing classes misses this shape entirely: nothing about the
        class changed, yet the pipeline did not run.
        """
        modal = self._validated()

        async def replacement(interaction):
            pass

        modal.on_submit = replacement

        with pytest.raises(RuntimeError, match="got no verdict"):
            await modal.submit(make_interaction(), {"Name": "abcdef"})

    async def test_an_override_that_calls_up_reports_both_outcomes(self):
        """The half the previous test never asked about.

        A cooperative override was verified only on the accepting path,
        so a rejection routed through one could raise unnoticed: the
        validation-error branch returns before the acceptance is recorded,
        and inferring the verdict from that made a correct override look
        like a broken one.
        """
        seen = []

        class Cooperative(Modal):
            async def on_submit(self, interaction):
                await super().on_submit(interaction)

        assert (
            await self._validated(Cooperative, seen).submit(make_interaction(), {"Name": "abcdef"})
            is True
        )
        assert seen == [{"input_name": "abcdef"}]

        seen.clear()
        assert (
            await self._validated(Cooperative, seen).submit(make_interaction(), {"Name": "ab"})
            is False
        )
        assert seen == []

    async def test_a_subclass_of_a_cooperative_override_reports_too(self):
        """Depth is irrelevant once no class comparison is involved."""

        class Cooperative(Modal):
            async def on_submit(self, interaction):
                await super().on_submit(interaction)

        class Deeper(Cooperative):
            pass

        assert await self._validated(Deeper).submit(make_interaction(), {"Name": "ab"}) is False

    async def test_a_dispatch_on_another_task_cannot_overwrite_the_verdict(self):
        """The verdict belongs to the task that asked for it.

        A modal instance is shared by every submission it receives, so a
        real one arriving from Discord while an offline drive is parked in
        its callback would, as instance state, overwrite what that drive
        was waiting to read. It would then report a rejection for a
        submission it had watched succeed.
        """
        parked, released = asyncio.Event(), asyncio.Event()

        async def slow_callback(interaction, values):
            parked.set()
            await released.wait()

        modal = Modal(
            title="T",
            inputs=[TextInput(label="Name", validators=[min_length(5)])],
            callback=slow_callback,
        )

        offline = asyncio.create_task(modal.submit(make_interaction(), {"Name": "abcdef"}))
        await parked.wait()

        # What discord.py does: fill the components, await on_submit, in
        # its own task. This submission is rejected by the validators.
        async def gateway():
            modal._resolve_submit_keys()["Name"]._value = "ab"
            await modal.on_submit(make_interaction())

        await asyncio.create_task(gateway())
        released.set()

        assert await offline is True

    @staticmethod
    def _with_select():
        sel = discord.ui.Select(
            custom_id="pick",
            options=[
                discord.SelectOption(label="A", value="a"),
                discord.SelectOption(label="B", value="b"),
            ],
        )
        seen = []

        async def on_submitted(interaction, values):
            seen.append(values)

        modal = Modal(
            title="Pick",
            inputs=[
                TextInput(label="Name"),
                discord.ui.Label(text="Pick one", component=sel),
            ],
            callback=on_submitted,
        )
        return modal, sel, seen

    async def test_a_modal_select_reaches_the_callback_offline(self):
        """A select is writable and listed, so it must also be delivered.

        Accepting the key, writing it, and returning True while dropping
        the value before the callback is a silent value loss certified as
        success, which is what this method exists to remove.
        """
        modal, sel, seen = self._with_select()

        assert await modal.submit(make_interaction(), {"Name": "Ada", "pick": ["b"]}) is True

        assert sel.values == ["b"]
        assert seen == [{"input_name": "Ada", "pick": ["b"]}]

    async def test_a_modal_select_reaches_the_callback_on_a_real_submission(self):
        """The same gap on the path a user actually takes.

        discord.py fills a modal select's storage on submission like any
        other child; the collection loop is what decides whether the
        callback ever sees it.
        """
        modal, sel, seen = self._with_select()
        sel._values = ["b"]

        await modal.on_submit(make_interaction())

        assert seen == [{"input_name": "", "pick": ["b"]}]

    def test_a_modal_child_with_no_custom_id_is_refused_by_name(self):
        """A display item carries no value, so it cannot be an input."""

        async def on_submitted(interaction, values):
            pass

        with pytest.raises(TypeError, match="carries no custom_id"):
            Modal(
                title="T",
                inputs=[discord.ui.TextDisplay("Section header")],
                callback=on_submitted,
            )

    async def test_a_label_two_inputs_claim_resolves_to_neither(self):
        """A name two inputs answer to is not a free name.

        Registering the first sends a value to whichever happened to be
        listed earlier, silently. Both keep their own custom_id, which is
        their identity and always resolves.
        """
        first = discord.ui.TextInput(custom_id="one")
        second = discord.ui.TextInput(custom_id="two")

        async def on_submitted(interaction, values):
            pass

        modal = Modal(
            title="Dup",
            inputs=[
                discord.ui.Label(text="Amount", component=first),
                discord.ui.Label(text="Amount", component=second),
            ],
            callback=on_submitted,
        )
        targets = modal._resolve_submit_keys()

        assert "Amount" not in targets
        assert targets["one"] is first
        assert targets["two"] is second

    async def test_an_uncontested_label_still_resolves(self):
        """The refusal must not cost the ordinary single-label case."""
        only = discord.ui.TextInput(custom_id="solo")

        async def on_submitted(interaction, values):
            pass

        modal = Modal(
            title="Solo",
            inputs=[discord.ui.Label(text="Amount", component=only)],
            callback=on_submitted,
        )

        assert modal._resolve_submit_keys()["Amount"] is only

    async def test_naming_one_input_twice_is_refused(self):
        """An input answers to two names, so one mapping can address it twice.

        Assigning both keeps whichever came last and loses the other with
        nothing said, which is the same silent overwrite the constructor
        already refuses two inputs for.
        """
        modal = self._validated()

        with pytest.raises(ValueError, match="both name the same input"):
            await modal.submit(make_interaction(), {"Name": "alpha", "input_name": "beta"})

    async def test_naming_two_different_inputs_is_fine(self):
        """The refusal must not fire on an ordinary two-field mapping."""
        seen = []

        async def on_submitted(interaction, values):
            seen.append(values)

        modal = Modal(
            title="Two",
            inputs=[TextInput(label="First"), TextInput(label="Second")],
            callback=on_submitted,
        )

        assert await modal.submit(make_interaction(), {"First": "a", "input_second": "b"}) is True
        assert seen == [{"input_first": "a", "input_second": "b"}]

    async def test_a_bad_key_writes_nothing_before_refusing(self):
        """Rejection precedes state mutation, as everywhere else here.

        Assigning as keys resolve leaves the modal half-populated up to
        whichever key the mapping happened to reach first, so the residue
        depended on dict order.
        """
        modal = self._validated()
        inner = modal._resolve_submit_keys()["Name"]

        with pytest.raises(ValueError, match="no input named"):
            await modal.submit(make_interaction(), {"Name": "abcdef", "Typo": 1})

        assert inner.value == ""

    def test_a_moved_upstream_attribute_raises_instead_of_testing_nothing(self):
        """A vanished storage attribute is refused, not written past.

        A component's value is a read-only property over a private
        attribute, which is what a real submission writes. If discord.py
        moves that attribute, a plain setattr would create a fresh one
        nothing reads, and the submission would run against defaults while
        reporting success. The attribute's presence is checked instead of
        the written value being read back, because a property that
        coerces its input answers a readback wrongly in both directions.
        """

        class Moved:
            """Exposes the property with nothing behind the old name."""

            @property
            def value(self):
                return "frozen"

        with pytest.raises(RuntimeError, match="has no '_value'"):
            _assign_submitted_value(Moved(), "Name", "Ada")

    def test_a_coercing_property_is_not_mistaken_for_a_moved_attribute(self):
        """The write lands even when the property reports something else.

        A text input returns ``''`` for a stored ``None``, so reading the
        value back would report no change for a write that landed and
        blame discord.py for the caller's own input.
        """
        inner = discord.ui.TextInput()

        _assign_submitted_value(inner, "Name", None)

        assert inner._value is None
        assert inner.value == ""

    def test_a_component_exposing_values_is_written_through_that_name(self):
        """The property name is read off the component, not matched.

        A select exposes ``values`` where a text input exposes ``value``,
        and it is a legal modal component discord.py already ships, so a
        fixed list of known types answers for it wrongly.
        """
        sel = discord.ui.Select(
            custom_id="pick",
            options=[discord.SelectOption(label="a", value="a")],
        )

        _assign_submitted_value(sel, "pick", ["a"])

        assert sel.values == ["a"]

    def test_writing_the_value_already_present_is_not_an_error(self):
        """The readback guard must not fire when nothing needed to change."""
        inner = discord.ui.TextInput(default="same")

        _assign_submitted_value(inner, "Name", "same")

        assert inner.value == "same"

    async def test_every_input_type_is_settable_not_just_text(self):
        """CheckboxGroup and FileUpload expose ``values``; the rest ``value``.

        The plural branch of the write helper targets ``_values``, and no
        other test reaches it. A discord.py rename on either private
        attribute surfaces here as the readback RuntimeError instead of
        in a consumer's suite.
        """
        seen = []

        async def on_submitted(interaction, values):
            seen.append(values)

        modal = Modal(
            title="All Types",
            inputs=[
                TextInput(label="T"),
                Checkbox(label="C"),
                CheckboxGroup(
                    label="G",
                    options=[{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
                ),
                RadioGroup(
                    label="R",
                    options=[{"label": "X", "value": "x"}, {"label": "Y", "value": "y"}],
                ),
                FileUpload(label="F"),
            ],
            callback=on_submitted,
        )

        accepted = await modal.submit(
            make_interaction(),
            {"T": "hi", "C": True, "G": ["b"], "R": "x", "F": ["upload.png"]},
        )

        assert accepted is True
        assert seen == [
            {
                "input_t": "hi",
                "input_c": True,
                "input_g": ["b"],
                "input_r": "x",
                "input_f": ["upload.png"],
            }
        ]
        # The plural write-back lands on the wrapper, as ``value`` does.
        assert modal.inputs["input_g"].values == ["b"]

    async def test_a_rejected_submit_leaves_values_by_input_a_dict(self):
        """The accept-signal sentinel must not leak to callers on rejection."""
        modal = self._modal([])

        await modal.submit(make_interaction(), {"Name": "ab"})

        assert modal.values_by_input == {}

    async def test_a_modal_with_no_callback_still_accepts(self):
        """``callback=`` is optional; ``submit`` reports the validator verdict."""
        modal = Modal(title="Quiet", inputs=[TextInput(label="A")])

        assert await modal.submit(make_interaction(), {"A": "x"}) is True
        assert modal.inputs["input_a"].value == "x"

    async def test_an_empty_mapping_submits_the_current_values(self):
        """``submit({})`` is a submission of whatever the fields hold.

        The required Name field still holds its default, which fails
        min_length(3) exactly as an untouched real submission would.
        """
        modal = self._modal([])

        assert await modal.submit(make_interaction(), {}) is False

    async def test_a_raising_validator_propagates_instead_of_reporting_false(self):
        """A validator that raises is a programmer error, not a rejection."""

        def boom(value, field_def, all_values):
            raise RuntimeError("validator exploded")

        modal = Modal(
            title="Boom",
            inputs=[TextInput(label="A", validators=[boom], required=True)],
        )

        with pytest.raises(RuntimeError, match="validator exploded"):
            await modal.submit(make_interaction(), {"A": "abc"})


class TestModalDuplicateInputs:
    """Modal rejects two inputs that derive the same custom_id."""

    def test_same_label_raises(self):
        with pytest.raises(ValueError, match="Duplicate modal input custom_id"):
            Modal(title="T", inputs=[TextInput(label="Name"), TextInput(label="Name")])

    def test_error_names_the_slug(self):
        with pytest.raises(ValueError, match="input_name"):
            Modal(title="T", inputs=[TextInput(label="Name"), TextInput(label="Name")])

    def test_cross_type_same_label_raises(self):
        # All five wrappers share the input_{label} namespace.
        with pytest.raises(ValueError, match="Duplicate modal input custom_id"):
            Modal(title="T", inputs=[TextInput(label="Opt"), Checkbox(label="Opt")])

    def test_distinct_labels_pass(self):
        modal = Modal(title="T", inputs=[TextInput(label="Name"), TextInput(label="Email")])
        assert len(modal.inputs) == 2


class TestOpenModalEmptyGuard:
    """open_modal rejects a zero-component modal before send (Discord 400)."""

    async def test_empty_modal_raises(self):
        from helpers import make_interaction

        from cascadeui import StatefulLayoutView

        view = StatefulLayoutView(interaction=make_interaction())
        with pytest.raises(ValueError, match="no components"):
            await view.open_modal(make_interaction(), Modal(title="Empty", inputs=[]))


class TestModalRespond:
    """``Modal.respond()`` is is_done-aware for on_submit-override replies."""

    async def test_open_slot_uses_send_message(self):
        from helpers import make_interaction

        modal = Modal(title="T", inputs=[TextInput(label="X")])
        interaction = make_interaction(is_done=False)
        await modal.respond(interaction, "hi", ephemeral=True)
        interaction.response.send_message.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()

    async def test_acked_slot_uses_followup(self):
        from helpers import make_interaction

        modal = Modal(title="T", inputs=[TextInput(label="X")])
        interaction = make_interaction(is_done=True)
        await modal.respond(interaction, "hi", ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()


class TestModalTextCaps:
    """Discord caps a modal title and each input label at 45 characters.

    discord.py stores both unchecked, so a label built from a schema field
    or a record name fails only when the modal opens, far from where it
    was set, and only for the data that happens to be long.
    """

    def test_oversized_modal_title_rejected(self):
        with pytest.raises(ValueError, match="over Discord's 45-character cap"):
            Modal(title="T" * 46, inputs=[])

    def test_empty_modal_title_rejected(self):
        with pytest.raises(ValueError, match="must not be empty"):
            Modal(title="", inputs=[])

    def test_boundary_title_accepted(self):
        assert Modal(title="T" * 45, inputs=[]).title == "T" * 45

    def test_oversized_input_label_rejected(self):
        with pytest.raises(ValueError, match="over Discord's 45-character cap"):
            TextInput("L" * 46)

    def test_empty_input_label_rejected(self):
        with pytest.raises(ValueError, match="must not be empty"):
            TextInput("")


class TestFormEditLabelFitsTheModalTitle:
    """The grouped edit label doubles as the modal title.

    A long field label would compose a title the caller never typed and
    cannot see, so the singular form degrades to the generic one rather
    than failing the modal open.
    """

    def test_short_label_uses_the_singular_form(self):
        from cascadeui.views.patterns.form import _resolve_modal_edit_label

        fields = [{"id": "n", "label": "Name", "type": "text"}]
        assert _resolve_modal_edit_label(None, fields) == "Edit Name"

    def test_long_label_falls_back_to_the_generic_form(self):
        from cascadeui.views.patterns.form import _resolve_modal_edit_label

        fields = [{"id": "n", "label": "A" * 60, "type": "text"}]
        result = _resolve_modal_edit_label(None, fields)
        assert result == "Edit Fields"
        assert len(result) <= 45

    def test_an_explicit_override_is_left_alone(self):
        from cascadeui.views.patterns.form import _resolve_modal_edit_label

        fields = [{"id": "n", "label": "A" * 60, "type": "text"}]
        assert _resolve_modal_edit_label("Custom", fields) == "Custom"
