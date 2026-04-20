"""Tests for modal input wrappers and Modal construction contracts.

Covers the v3.0.0 canonical validator-attachment API: validators live on
input wrapper instances (TextInput, Checkbox, CheckboxGroup, RadioGroup,
FileUpload) and are auto-collected by ``Modal`` at construction time.
"""

import discord
import pytest

from cascadeui.components.inputs import (
    Checkbox,
    CheckboxGroup,
    FileUpload,
    Modal,
    RadioGroup,
    TextInput,
)
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
                TextInput(label="Bio"),  # no validators — not in dict
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


# // ========================================( TextInput.value + Modal.values_by_input )======================================== // #


class TestSubmittedValuePropagation:
    """After submit, each wrapped TextInput instance owns its submitted value."""

    def test_textinput_value_defaults_to_none(self):
        field = TextInput(label="Username")
        assert field.value is None

    def test_modal_values_by_input_empty_before_submit(self):
        modal = Modal(title="T", inputs=[TextInput(label="X")])
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
        assert isinstance(comp, discord.ui.Checkbox)
        assert comp.custom_id == "input_check"
        assert comp.default is True


# // ========================================( CheckboxGroup )======================================== // #


class TestCheckboxGroup:
    """CheckboxGroup input derives custom_id from label and stores multi-values."""
    def test_custom_id_from_label(self):
        cg = CheckboxGroup(label="Toppings", options=[])
        assert cg.custom_id == "input_toppings"

    def test_values_default_none(self):
        cg = CheckboxGroup(label="X", options=[])
        assert cg.values is None

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
        assert isinstance(comp, discord.ui.CheckboxGroup)
        assert comp.custom_id == "input_sizes"
        assert comp.min_values == 1
        assert comp.max_values == 3

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


class TestRadioGroup:
    """RadioGroup input derives custom_id from label and stores single value."""
    def test_custom_id_from_label(self):
        rg = RadioGroup(label="Difficulty", options=[])
        assert rg.custom_id == "input_difficulty"

    def test_value_default_none(self):
        rg = RadioGroup(label="X", options=[])
        assert rg.value is None

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
        assert isinstance(comp, discord.ui.RadioGroup)
        assert comp.custom_id == "input_mode"

    def test_validators_collected_by_modal(self):
        v = regex(r"^easy$", "must be easy")
        rg = RadioGroup(
            label="Mode",
            options=[discord.RadioGroupOption(label="Easy", value="easy")],
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
        assert isinstance(comp, discord.ui.FileUpload)
        assert comp.custom_id == "input_docs"
        assert comp.min_values == 1
        assert comp.max_values == 5

    def test_wrapped_pairs_include_file_upload(self):
        fu = FileUpload(label="Upload")
        modal = Modal(title="T", inputs=[fu])
        assert len(modal._wrapped_pairs) == 1
        assert modal._wrapped_pairs[0][0] is fu


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
                options=[discord.RadioGroupOption(label="S", value="s")],
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
                options=[discord.RadioGroupOption(label="A", value="a")],
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
            options=[discord.RadioGroupOption(label="A", value="a")],
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
