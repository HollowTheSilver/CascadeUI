"""Tests for FormView and FormLayoutView native ``"text"`` field support.

FormView and FormLayoutView render ``"text"`` fields through a grouped
"Edit Text Fields" button that opens a
single :class:`cascadeui.Modal`. Tests here exercise the construction-time
5-text-field ceiling, the smart singular/plural label default, the modal
round-trip (values wiring through ``form.values``), and the rebuild path
on V2.
"""

from unittest.mock import AsyncMock

import discord
import pytest
from discord.ui import ActionRow
from helpers import make_interaction as _make_interaction

from cascadeui.components.base import StatefulButton, StatefulSelect
from cascadeui.components.inputs import Modal as CascadeModal
from cascadeui.state.store import _CURRENT_INTERACTION
from cascadeui.validation import min_length, regex
from cascadeui.views.patterns.form import (
    MAX_TEXT_FIELDS,
    FormLayoutView,
    FormView,
    _build_form_modal,
    _parse_field_value,
    _resolve_modal_edit_label,
)


def _modal_text_inputs(modal):
    """Return the ``discord.ui.TextInput`` items inside a Modal.

    Walks the modal's children, unwrapping ``ui.Label`` to reach the
    inner input, and returns the TextInput instances in declaration
    order.
    """
    out = []
    for child in modal.children:
        inner = child.component if isinstance(child, discord.ui.Label) else child
        if isinstance(inner, discord.ui.TextInput):
            out.append(inner)
    return out


# // ========================================( _resolve_modal_edit_label )======================================== // #


class TestResolveTextEditLabel:
    """Text edit button label resolves from override, single-field default, or generic fallback."""

    def test_override_wins(self):
        fields = [{"id": "u", "type": "text", "label": "Username"}]
        assert _resolve_modal_edit_label("Custom", fields) == "Custom"

    def test_singular_default_uses_field_label(self):
        fields = [{"id": "u", "type": "text", "label": "Username"}]
        assert _resolve_modal_edit_label(None, fields) == "Edit Username"

    def test_singular_falls_back_to_id(self):
        fields = [{"id": "email", "type": "text"}]
        assert _resolve_modal_edit_label(None, fields) == "Edit email"

    def test_plural_default(self):
        fields = [
            {"id": "u", "type": "text", "label": "Username"},
            {"id": "e", "type": "text", "label": "Email"},
        ]
        assert _resolve_modal_edit_label(None, fields) == "Edit Text Fields"


# // ========================================( 5-text-field ceiling )======================================== // #


class TestTextFieldCeiling:
    """FormView and FormLayoutView enforce the 5-text-field modal limit."""

    def _make_text_fields(self, n):
        return [{"id": f"f{i}", "type": "text", "label": f"F{i}"} for i in range(n)]

    def test_formview_allows_five_text_fields(self):
        interaction = _make_interaction()
        view = FormView(interaction=interaction, fields=self._make_text_fields(5))
        assert len(view.fields) == 5

    def test_formview_rejects_six_text_fields(self):
        interaction = _make_interaction()
        with pytest.raises(ValueError, match="FormView"):
            FormView(interaction=interaction, fields=self._make_text_fields(6))

    def test_formlayoutview_allows_five_text_fields(self):
        interaction = _make_interaction()
        view = FormLayoutView(interaction=interaction, fields=self._make_text_fields(5))
        assert len(view.fields) == 5

    def test_formlayoutview_rejects_six_text_fields(self):
        interaction = _make_interaction()
        with pytest.raises(ValueError, match="FormLayoutView"):
            FormLayoutView(interaction=interaction, fields=self._make_text_fields(6))

    def test_error_names_count_and_limit(self):
        interaction = _make_interaction()
        with pytest.raises(ValueError, match=str(MAX_TEXT_FIELDS)):
            FormView(interaction=interaction, fields=self._make_text_fields(7))

    def test_v1_non_modal_fields_past_the_row_budget_raise(self):
        # Four selects plus two booleans need six action rows; a V1 form has
        # five. The overflow field used to vanish silently; it now raises.
        opts = [{"label": "A", "value": "a"}]
        fields = [
            {"id": f"s{i}", "type": "select", "label": f"S{i}", "options": opts} for i in range(4)
        ] + [
            {"id": "b1", "type": "boolean", "label": "B1"},
            {"id": "b2", "type": "boolean", "label": "B2"},
        ]
        with pytest.raises(ValueError, match="does not fit"):
            FormView(interaction=_make_interaction(), fields=fields)

    @pytest.mark.parametrize("form_cls", [FormView, FormLayoutView], ids=lambda c: c.__name__)
    def test_duplicate_modal_labels_rejected_at_construction(self, form_cls):
        # Modal input ids derive from the label, so two fields labelled the
        # same collided at modal-open (the Edit button's first click). The
        # clash now raises at construction, naming both fields.
        with pytest.raises(ValueError, match="collide"):
            form_cls(
                interaction=_make_interaction(),
                fields=[
                    {"id": "home", "type": "text", "label": "Address"},
                    {"id": "work", "type": "text", "label": "Address"},
                ],
            )


# // ========================================( FormView text button rendering )======================================== // #


class TestFormViewTextButton:
    """V1 FormView text edit button presence and absence based on field types."""

    def _find_text_button(self, view):
        for item in view.children:
            if (
                isinstance(item, StatefulButton)
                and getattr(item, "custom_id", None) == "form_edit_text"
            ):
                return item
        return None

    def test_no_text_fields_no_button(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "x", "type": "boolean", "label": "X"}],
        )
        assert self._find_text_button(view) is None

    def test_text_field_emits_grouped_button(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        button = self._find_text_button(view)
        assert button is not None
        # Smart default: exactly one text field -> "Edit <label>"
        assert button.label == "Edit Username"

    def test_plural_label_for_multiple_text_fields(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "u", "type": "text", "label": "Username"},
                {"id": "e", "type": "text", "label": "Email"},
            ],
        )
        button = self._find_text_button(view)
        assert button.label == "Edit Text Fields"

    def test_class_attribute_override_wins(self):
        class Custom(FormView):
            text_edit_button_label = "Fill Form"
            text_edit_button_style = discord.ButtonStyle.primary

        view = Custom(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        button = self._find_text_button(view)
        assert button.label == "Fill Form"
        assert button.style == discord.ButtonStyle.primary

    def test_submit_button_still_present(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        custom_ids = [getattr(c, "custom_id", None) for c in view.children]
        assert "form_submit" in custom_ids
        assert "form_edit_text" in custom_ids


# // ========================================( FormLayoutView text button rendering )======================================== // #


class TestFormLayoutViewTextButton:
    """V2 FormLayoutView text edit button presence and absence based on field types."""

    def _find_text_button(self, view):
        for item in view.walk_children():
            if (
                isinstance(item, StatefulButton)
                and getattr(item, "custom_id", None) == "form_edit_text"
            ):
                return item
        return None

    def test_text_field_emits_grouped_button_in_action_row(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        button = self._find_text_button(view)
        assert button is not None
        # Button must live inside an ActionRow (V2 constraint)
        parents = [c for c in view.children if isinstance(c, ActionRow)]
        assert any(button in row.children for row in parents)

    def test_no_text_fields_no_button(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[{"id": "x", "type": "boolean", "label": "X"}],
        )
        assert self._find_text_button(view) is None


# // ========================================( _build_form_modal round-trip )======================================== // #


class TestBuildTextModal:
    """_build_form_modal generates a modal with one TextInput per text field."""

    def test_modal_contains_one_input_per_text_field(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "u", "type": "text", "label": "Username"},
                {"id": "e", "type": "text", "label": "Email"},
                {"id": "keep", "type": "boolean", "label": "Keep"},
            ],
        )
        modal = _build_form_modal(view, "Edit Text Fields")
        assert isinstance(modal, CascadeModal)
        # One discord TextInput per declared "text" field
        text_inputs = _modal_text_inputs(modal)
        assert len(text_inputs) == 2

    def test_modal_view_id_wired_to_form(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        modal = _build_form_modal(view, "Edit")
        assert modal.view_id == view.id

    def test_modal_preserves_current_values_as_defaults(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        view.values["u"] = "existing_user"
        modal = _build_form_modal(view, "Edit")
        text_inputs = _modal_text_inputs(modal)
        assert text_inputs[0].default == "existing_user"

    def test_modal_input_shows_the_field_default(self):
        """A field ``default`` is seeded into ``form.values`` at construction,
        and the modal renders it as the input's default.
        """
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "u",
                    "type": "text",
                    "label": "Username",
                    "default": "seeded_user",
                }
            ],
        )
        assert view.values["u"] == "seeded_user"  # seeded at construction
        modal = _build_form_modal(view, "Edit")
        text_inputs = _modal_text_inputs(modal)
        assert text_inputs[0].default == "seeded_user"

    def test_modal_default_is_none_when_neither_value_nor_default_set(self):
        """Both ``form.values[field_id]`` and ``field["default"]`` absent →
        the modal input's ``default`` is ``None`` (not the string ``"None"``).
        """
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        modal = _build_form_modal(view, "Edit")
        text_inputs = _modal_text_inputs(modal)
        assert text_inputs[0].default is None

    def test_modal_does_not_carry_validators(self):
        """Validators are handled by the callback, not the Modal layer.

        Submitted values are written to form.values before validation
        runs -- if the Modal rejected on failure, the callback would
        never fire and values would be lost.
        """
        v = min_length(3)
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "u",
                    "type": "text",
                    "label": "Username",
                    "validators": [v],
                }
            ],
        )
        modal = _build_form_modal(view, "Edit")
        # Modal.validators should be empty — validation lives in the callback.
        assert modal.validators == {}

    async def test_on_modal_submit_writes_values_back(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        modal = _build_form_modal(view, "Edit")

        # Stub the refresh path — unit test, no real discord message.
        async def _noop():
            pass

        view._update_form_display = _noop

        # The form callback now reads values directly off the TextInput
        # instances, which would normally be populated by Modal.on_submit.
        # Stamp the expected value onto the wrapped instance, then invoke.
        wrapped_input = next(iter(modal.inputs.values()))
        wrapped_input.value = "new_user"

        interaction = _make_interaction()
        await modal.user_callback(interaction, {})
        assert view.values["u"] == "new_user"

    async def test_values_preserved_on_validation_failure(self):
        """When a text field validator rejects, submitted values stay in form.values.

        This is the core regression test for the "text fields cleared on
        validation failure" bug. Previously, validators lived on the Modal
        layer -- failure caused an early return before the callback fired,
        so form.values was never updated and the next modal open reset all
        inputs. The inline-error flow surfaces the rejection
        on ``_field_errors`` instead of an ephemeral message.
        """
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "u",
                    "type": "text",
                    "label": "Username",
                    "validators": [min_length(10)],  # will fail for "abc"
                }
            ],
        )
        modal = _build_form_modal(view, "Edit")

        view._update_form_display = AsyncMock()

        # Simulate the user typing "abc" (too short for min_length(10))
        wrapped_input = next(iter(modal.inputs.values()))
        wrapped_input.value = "abc"

        interaction = _make_interaction()
        await modal.user_callback(interaction, {})

        # Value should be written to form.values even though validation failed
        assert view.values["u"] == "abc"
        # Validation errors live on the form, not on an ephemeral response.
        assert "u" in view._field_errors
        assert any("10" in msg or "length" in msg.lower() for msg in view._field_errors["u"])
        interaction.response.send_message.assert_not_called()

    @pytest.mark.parametrize("form_cls", [FormView, FormLayoutView], ids=lambda c: c.__name__)
    async def test_clearing_an_optional_select_does_not_crash(self, form_cls):
        """An optional select (min_values=0) delivers an empty list when
        cleared, and the callback indexed it, so clearing a choice raised
        IndexError and the user saw 'This interaction failed'."""
        view = form_cls(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "color",
                    "type": "select",
                    "label": "Color",
                    "required": False,
                    "options": [{"label": "Red", "value": "red"}],
                }
            ],
        )
        view._update_form_display = AsyncMock()
        select = next(c for c in view.walk_children() if isinstance(c, StatefulSelect))

        select._values = ["red"]
        await select.callback(_make_interaction())
        select._values = []  # the deselect-all gesture Discord allows
        await select.callback(_make_interaction())

        assert view.values["color"] is None

    async def test_parse_and_validator_errors_surface_together(self):
        """One submit reveals every mistake, across parse and validator errors.

        A parse error on one field used to short-circuit the whole submit, so
        validator errors on other fields stayed hidden until the parse error
        was fixed and the form resubmitted. The user met their mistakes one
        round at a time.
        """
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "age", "type": "integer", "label": "Age", "min_value": 18},
                {
                    "id": "email",
                    "type": "text",
                    "label": "Email",
                    "validators": [regex(r"^[^@]+@[^@]+\.[^@]+$", "Must be a valid email")],
                },
                {
                    "id": "password",
                    "type": "text",
                    "label": "Password",
                    "validators": [min_length(8, "At least 8 characters")],
                },
            ],
        )
        modal = _build_form_modal(view, "Register")
        view._update_form_display = AsyncMock()

        typed = {"Age": "5", "Email": "nope", "Password": "x"}  # all three wrong
        for wrapped in modal.inputs.values():
            wrapped.value = typed[wrapped.label]

        await modal.user_callback(_make_interaction(), {})

        # The integer range error and both validator errors are all present.
        assert set(view._field_errors) == {"age", "email", "password"}

    async def test_validators_skip_a_field_that_failed_to_parse(self):
        """A field that failed to parse holds its raw string, so its own
        validators must not run against it and mask the parse error."""
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "age",
                    "type": "integer",
                    "label": "Age",
                    "min_value": 18,
                    "validators": [min_length(2)],  # would see the raw string if it ran
                },
            ],
        )
        modal = _build_form_modal(view, "Register")
        view._update_form_display = AsyncMock()

        next(iter(modal.inputs.values())).value = "notanumber"
        await modal.user_callback(_make_interaction(), {})

        # Exactly one error, and it is the parse error. If the min_length(2)
        # validator had run against the raw string, a second error would join
        # it, or worse the raw string would slip past validation.
        assert len(view._field_errors["age"]) == 1
        assert "whole number" in view._field_errors["age"][0]

    async def test_modal_submit_preserves_a_non_modal_field_error(self):
        """A modal edit clears only the modal fields' errors. A select or
        boolean error is on a field the modal never touched, so it stays on
        screen instead of being wiped when a text field is fixed."""
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "country",
                    "type": "select",
                    "label": "Country",
                    "options": [{"label": "US", "value": "us"}],
                },
                {"id": "age", "type": "integer", "label": "Age", "min_value": 18},
                {
                    "id": "email",
                    "type": "text",
                    "label": "Email",
                    "validators": [regex(r"^[^@]+@[^@]+\.[^@]+$", "Must be a valid email")],
                },
            ],
        )
        view._update_form_display = AsyncMock()
        # A prior submit left an error on the non-modal select and on a text field.
        view._field_errors = {
            "country": ["Pick a valid country"],
            "email": ["Must be a valid email"],
        }

        modal = _build_form_modal(view, "Register")
        typed = {"Age": "5", "Email": "valid@example.com"}  # age still bad, email now valid
        for wrapped in modal.inputs.values():
            wrapped.value = typed[wrapped.label]
        await modal.user_callback(_make_interaction(), {})

        assert "country" in view._field_errors  # non-modal error preserved
        assert "email" not in view._field_errors  # fixed via the modal
        assert "age" in view._field_errors  # still out of range


# // ========================================( _open_text_modal wiring )======================================== // #


class TestOpenTextModal:
    """_open_text_modal routes through open_modal for both V1 and V2 forms."""

    async def test_formview_open_text_modal_calls_send_modal(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        interaction = _make_interaction()
        await view._open_text_modal(interaction)
        interaction.response.send_modal.assert_awaited_once()
        sent = interaction.response.send_modal.await_args.args[0]
        assert isinstance(sent, CascadeModal)

    async def test_formlayoutview_open_text_modal_calls_send_modal(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        interaction = _make_interaction()
        await view._open_text_modal(interaction)
        interaction.response.send_modal.assert_awaited_once()


# // ========================================( on_field_changed hook )======================================== // #


class TestOnFieldChangedHook:
    """on_field_changed fires on value transitions across every field type."""

    async def _find_select_callback(self, view, field_id):
        for child in view.walk_children() if hasattr(view, "walk_children") else view.children:
            if getattr(child, "custom_id", None) == f"form_{field_id}":
                return child.callback
        raise AssertionError(f"no select callback for {field_id}")

    async def _find_bool_callback(self, view, label):
        source = view.walk_children() if hasattr(view, "walk_children") else view.children
        for child in source:
            if isinstance(child, StatefulButton) and child.label == label:
                return child.callback
        raise AssertionError(f"no bool button with label {label}")

    async def test_select_change_fires_hook_on_v2(self):
        changes = []

        class TrackedForm(FormLayoutView):
            async def on_field_changed(self, field_name, old, new):
                changes.append((field_name, old, new))

        view = TrackedForm(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "color",
                    "type": "select",
                    "label": "Color",
                    "options": [
                        {"label": "Red", "value": "red"},
                        {"label": "Blue", "value": "blue"},
                    ],
                }
            ],
        )
        view._update_form_display = AsyncMock()

        # Locate the select and simulate a user picking "blue"
        select = next(
            c for c in view.walk_children() if getattr(c, "custom_id", None) == "form_color"
        )
        select._values = ["blue"]
        await select.callback(_make_interaction())

        assert changes == [("color", None, "blue")]

    async def test_select_no_change_does_not_fire(self):
        """Repeat selection of the current value short-circuits the hook."""
        changes = []

        class TrackedForm(FormLayoutView):
            async def on_field_changed(self, field_name, old, new):
                changes.append((field_name, old, new))

        view = TrackedForm(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "color",
                    "type": "select",
                    "label": "Color",
                    "options": [{"label": "Red", "value": "red"}],
                }
            ],
        )
        view._update_form_display = AsyncMock()
        view.values["color"] = "red"

        select = next(
            c for c in view.walk_children() if getattr(c, "custom_id", None) == "form_color"
        )
        select._values = ["red"]
        await select.callback(_make_interaction())

        assert changes == []

    async def test_boolean_change_fires_hook(self):
        changes = []

        class TrackedForm(FormLayoutView):
            async def on_field_changed(self, field_name, old, new):
                changes.append((field_name, old, new))

        view = TrackedForm(
            interaction=_make_interaction(),
            fields=[{"id": "subscribe", "type": "boolean", "label": "Subscribe?"}],
        )
        view._update_form_display = AsyncMock()

        yes_btn = next(
            c
            for c in view.walk_children()
            if isinstance(c, StatefulButton) and c.custom_id == "form_subscribe_yes"
        )
        await yes_btn.callback(_make_interaction())

        assert changes == [("subscribe", None, True)]

    async def test_modal_text_submit_fires_hook_per_changed_field(self):
        """Modal submit collects changes and fires the hook once per changed field."""
        changes = []

        class TrackedForm(FormView):
            async def on_field_changed(self, field_name, old, new):
                changes.append((field_name, old, new))

        view = TrackedForm(
            interaction=_make_interaction(),
            fields=[
                {"id": "u", "type": "text", "label": "Username"},
                {"id": "e", "type": "text", "label": "Email"},
            ],
        )
        view._update_form_display = AsyncMock()
        view.values["u"] = "old_user"  # prime existing value

        modal = _build_form_modal(view, "Edit Text Fields")

        # Stamp the text inputs with user submissions. ``_parse_field_value``
        # collapses empty / whitespace-only input to ``None`` so an untouched
        # field does not flip from ``None`` to ``""``.
        wrapped = list(modal.inputs.values())
        wrapped[0].value = "new_user"  # changed
        wrapped[1].value = ""  # stays None -- not a change

        interaction = _make_interaction()
        await modal.user_callback(interaction, {})

        assert changes == [("u", "old_user", "new_user")]

    async def test_modal_text_submit_skips_unchanged_field(self):
        """Fields whose modal value equals the current value do not fire the hook."""
        changes = []

        class TrackedForm(FormView):
            async def on_field_changed(self, field_name, old, new):
                changes.append((field_name, old, new))

        view = TrackedForm(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        view._update_form_display = AsyncMock()
        view.values["u"] = "stable"

        modal = _build_form_modal(view, "Edit")
        wrapped = next(iter(modal.inputs.values()))
        wrapped.value = "stable"  # identical

        await modal.user_callback(_make_interaction(), {})

        assert changes == []


# // ========================================( Inline validation errors )======================================== // #


class TestInlineValidationErrors:
    """Validator failure surfaces on the form, not an ephemeral.

    The V1 and V2 submit callbacks both set ``_field_errors`` /
    ``_form_error`` from ``_validate_form`` and re-render, so the user sees
    the error inline on the form body. Required and validator errors surface
    together. Clearing happens on any field-change gesture so the UI stays in
    sync with the latest input.
    """

    async def _find_submit_callback(self, view):
        source = view.walk_children() if hasattr(view, "walk_children") else view.children
        for child in source:
            if (
                isinstance(child, StatefulButton)
                and getattr(child, "custom_id", None) == "form_submit"
            ):
                return child.callback
        raise AssertionError("no submit button found")

    async def test_v1_submit_missing_required_populates_form_error(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "name", "type": "text", "label": "Name", "required": True}],
        )
        view._update_form_display = AsyncMock()
        submit_cb = await self._find_submit_callback(view)

        interaction = _make_interaction()
        await submit_cb(interaction)

        assert view._form_error is not None
        assert "Name" in view._form_error
        assert view._field_errors == {}
        # No ephemeral fallback; error state lives on the form.
        interaction.response.send_message.assert_not_called()
        view._update_form_display.assert_awaited()

    async def test_missing_required_and_bad_value_surface_together(self):
        """A blank required field and a validator failure elsewhere both show
        on one submit. The required-check used to return early, hiding the
        validator error until the missing field was filled and resubmitted.
        """
        from cascadeui.validation import regex

        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "age", "type": "integer", "label": "Age", "required": True},
                {
                    "id": "email",
                    "type": "text",
                    "label": "Email",
                    "required": True,
                    "validators": [regex(r"^[^@]+@[^@]+\.[^@]+$", "Must be a valid email")],
                },
            ],
        )
        view._update_form_display = AsyncMock()
        view.values = {"email": "notanemail"}  # age missing, email bad
        submit_cb = await self._find_submit_callback(view)

        await submit_cb(_make_interaction())

        # The required summary names the missing field...
        assert "Age" in view._form_error
        # ...and the validator error on the other field is present too.
        assert "email" in view._field_errors

    async def test_drafted_field_shows_specific_parse_error_not_missing(self):
        """A field the user entered that failed to parse surfaces its specific
        parse message on submit, matching the modal path. A typed integer's
        range lives in the parse layer, not a validators list, so before the
        draft was consulted the Submit path reported the field only as a
        generic missing-required summary, inconsistent with the modal.
        """
        from cascadeui.validation import regex

        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "age",
                    "type": "integer",
                    "label": "Age",
                    "required": True,
                    "min_value": 13,
                },
                {
                    "id": "email",
                    "type": "text",
                    "label": "Email",
                    "required": True,
                    "validators": [regex(r"^[^@]+@[^@]+\.[^@]+$", "Must be a valid email")],
                },
            ],
        )
        view._update_form_display = AsyncMock()
        # Age entered out of range: the modal drafts the raw string because
        # _parse_field_value rejects it to None. Email holds a bad value.
        view.values = {"email": "notanemail"}
        view._raw_drafts = {"age": "5"}
        submit_cb = await self._find_submit_callback(view)

        await submit_cb(_make_interaction())

        # Age surfaces as a field error with its specific range message, not
        # as the generic required-field summary.
        assert "age" in view._field_errors
        assert any("at least 13" in msg for msg in view._field_errors["age"])
        assert view._form_error is None
        # The other field's error still surfaces in the same submit.
        assert "email" in view._field_errors

    async def test_v1_submit_validator_failure_populates_field_errors(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "name",
                    "type": "text",
                    "label": "Name",
                    "required": True,
                    "validators": [min_length(10)],
                }
            ],
        )
        view.values["name"] = "abc"
        view._update_form_display = AsyncMock()
        submit_cb = await self._find_submit_callback(view)

        await submit_cb(_make_interaction())

        assert "name" in view._field_errors
        assert view._form_error is None

    async def test_v2_submit_validator_failure_populates_field_errors(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "name",
                    "type": "text",
                    "label": "Name",
                    "required": True,
                    "validators": [min_length(10)],
                }
            ],
        )
        view.values["name"] = "abc"
        view._update_form_display = AsyncMock()
        submit_cb = await self._find_submit_callback(view)

        await submit_cb(_make_interaction())

        assert "name" in view._field_errors
        assert view._form_error is None

    async def test_select_change_clears_field_errors(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "color",
                    "type": "select",
                    "label": "Color",
                    "options": [{"label": "Red", "value": "red"}],
                }
            ],
        )
        view._field_errors = {"color": ["stale"]}
        view._form_error = "stale form error"
        view._update_form_display = AsyncMock()

        select = next(
            c for c in view.walk_children() if getattr(c, "custom_id", None) == "form_color"
        )
        select._values = ["red"]
        await select.callback(_make_interaction())

        assert view._field_errors == {}
        assert view._form_error is None

    async def test_field_change_preserves_unrelated_field_errors(self):
        """Changing one field clears only its own error. Selecting a country
        used to wipe the still-accurate age and email messages; now an
        unrelated field's error stays on screen until that field changes or
        the next submit re-checks it."""
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "country",
                    "type": "select",
                    "label": "Country",
                    "options": [{"label": "US", "value": "us"}],
                },
                {"id": "age", "type": "integer", "label": "Age"},
                {"id": "email", "type": "text", "label": "Email"},
            ],
        )
        view._field_errors = {
            "age": ["Must be at least 13."],
            "email": ["Must be a valid email address"],
        }
        view._update_form_display = AsyncMock()

        select = next(
            c for c in view.walk_children() if getattr(c, "custom_id", None) == "form_country"
        )
        select._values = ["us"]
        await select.callback(_make_interaction())

        assert "age" in view._field_errors  # unrelated errors survive the country change
        assert "email" in view._field_errors
        assert "country" not in view._field_errors

    async def test_boolean_change_clears_field_errors(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "flag", "type": "boolean", "label": "Flag"}],
        )
        view._field_errors = {"flag": ["stale"]}
        view._update_form_display = AsyncMock()

        yes_btn = next(
            c
            for c in view.children
            if isinstance(c, StatefulButton) and c.custom_id == "form_flag_yes"
        )
        await yes_btn.callback(_make_interaction())

        assert view._field_errors == {}

    async def test_modal_submit_clears_prior_errors_on_change(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        view._field_errors = {"u": ["stale"]}
        view._form_error = "stale"
        view._update_form_display = AsyncMock()
        modal = _build_form_modal(view, "Edit")

        wrapped = next(iter(modal.inputs.values()))
        wrapped.value = "fresh"

        await modal.user_callback(_make_interaction(), {})

        assert view._field_errors == {}
        assert view._form_error is None

    def test_clear_errors_resets_both_fields(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        view._field_errors = {"u": ["something"]}
        view._form_error = "also something"
        view._clear_errors()
        assert view._field_errors == {}
        assert view._form_error is None


# // ========================================( Field groups )======================================== // #


class TestFieldGroups:
    """The ``group`` field key collects consecutive runs, no merging.

    ``_iter_field_groups`` yields ``(group_name_or_None, [fields])``
    in declaration order. Reordering fields reorders groups; interleaved
    same-name groups render as separate runs so declaration order is the
    UI contract.
    """

    def test_no_groups_yields_single_none_run(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "a", "type": "boolean", "label": "A"},
                {"id": "b", "type": "boolean", "label": "B"},
            ],
        )
        runs = view._iter_field_groups()
        assert len(runs) == 1
        assert runs[0][0] is None
        assert [f["id"] for f in runs[0][1]] == ["a", "b"]
        assert view._has_field_groups() is False

    def test_consecutive_same_group_collects_into_one_run(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "a", "type": "boolean", "label": "A", "group": "Contact"},
                {"id": "b", "type": "boolean", "label": "B", "group": "Contact"},
            ],
        )
        runs = view._iter_field_groups()
        assert runs == [("Contact", [view.fields[0], view.fields[1]])]
        assert view._has_field_groups() is True

    def test_interleaved_groups_do_not_merge(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "a", "type": "boolean", "label": "A", "group": "X"},
                {"id": "b", "type": "boolean", "label": "B", "group": "Y"},
                {"id": "c", "type": "boolean", "label": "C", "group": "X"},
            ],
        )
        runs = view._iter_field_groups()
        names = [name for name, _ in runs]
        assert names == ["X", "Y", "X"]

    def test_ungrouped_fields_mix_with_grouped(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "a", "type": "boolean", "label": "A"},
                {"id": "b", "type": "boolean", "label": "B", "group": "Prefs"},
                {"id": "c", "type": "boolean", "label": "C", "group": "Prefs"},
                {"id": "d", "type": "boolean", "label": "D"},
            ],
        )
        runs = view._iter_field_groups()
        names = [name for name, _ in runs]
        assert names == [None, "Prefs", None]

    def test_v2_rebuild_emits_group_card_per_run(self):
        from discord.ui import Container

        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[
                {"id": "a", "type": "boolean", "label": "A", "group": "Account"},
                {"id": "b", "type": "boolean", "label": "B", "group": "Account"},
                {"id": "c", "type": "boolean", "label": "C", "group": "Notifications"},
            ],
        )
        # One title card + one card per group run = 3 Containers before the
        # ActionRow-wrapped controls.
        containers = [c for c in view.children if isinstance(c, Container)]
        assert len(containers) == 3

    async def test_v1_display_renders_group_headings(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "a", "type": "boolean", "label": "A", "group": "Account"},
                {"id": "b", "type": "boolean", "label": "B", "group": "Account"},
            ],
        )
        captured = {}

        async def fake_refresh(**kwargs):
            captured["embed"] = kwargs.get("embed")

        view.refresh = fake_refresh
        await view._update_form_display()

        embed = captured["embed"]
        assert embed is not None
        # The group name surfaces as a bold field heading.
        headings = [f.name for f in embed.fields]
        assert any("Account" in h for h in headings)


# // ========================================( _parse_field_value per type )======================================== // #


class TestParseFieldValue:
    """Typed-field parsing: text passthrough, int/float parsing, range clamps, date ISO round-trip."""

    def test_text_passthrough(self):
        parsed, err = _parse_field_value({"id": "x", "type": "text"}, "hello")
        assert parsed == "hello"
        assert err is None

    @pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
    def test_float_rejects_non_finite(self, raw):
        # float() accepts these, and a NaN slips past min/max bounds and
        # serializes to invalid JSON, so the parse rejects them outright.
        parsed, err = _parse_field_value({"id": "x", "type": "float"}, raw)
        assert parsed is None
        assert err is not None

    def test_none_input_returns_none(self):
        parsed, err = _parse_field_value({"id": "x", "type": "integer"}, None)
        assert parsed is None
        assert err is None

    def test_empty_string_collapses_to_none(self):
        parsed, err = _parse_field_value({"id": "x", "type": "text"}, "   ")
        assert parsed is None
        assert err is None

    def test_integer_parses_digits(self):
        parsed, err = _parse_field_value({"id": "x", "type": "integer"}, "42")
        assert parsed == 42
        assert err is None

    def test_integer_rejects_non_numeric(self):
        parsed, err = _parse_field_value({"id": "x", "type": "integer"}, "abc")
        assert parsed is None
        assert err is not None
        assert "whole number" in err

    def test_integer_rejects_float_string(self):
        parsed, err = _parse_field_value({"id": "x", "type": "integer"}, "3.14")
        assert parsed is None
        assert err is not None

    def test_integer_min_value_clamps(self):
        field = {"id": "x", "type": "integer", "min_value": 10}
        parsed, err = _parse_field_value(field, "5")
        assert parsed is None
        assert "at least 10" in err

    def test_integer_max_value_clamps(self):
        field = {"id": "x", "type": "integer", "max_value": 100}
        parsed, err = _parse_field_value(field, "150")
        assert parsed is None
        assert "at most 100" in err

    def test_integer_within_range_passes(self):
        field = {"id": "x", "type": "integer", "min_value": 0, "max_value": 100}
        parsed, err = _parse_field_value(field, "50")
        assert parsed == 50
        assert err is None

    def test_float_parses_decimal(self):
        parsed, err = _parse_field_value({"id": "x", "type": "float"}, "3.14")
        assert parsed == pytest.approx(3.14)
        assert err is None

    def test_float_rejects_non_numeric(self):
        parsed, err = _parse_field_value({"id": "x", "type": "float"}, "nope")
        assert parsed is None
        assert err is not None

    def test_float_range_clamps(self):
        field = {"id": "x", "type": "float", "min_value": 0.0, "max_value": 1.0}
        parsed, err = _parse_field_value(field, "1.5")
        assert parsed is None
        assert "at most 1.0" in err

    def test_date_parses_iso_format(self):
        parsed, err = _parse_field_value({"id": "x", "type": "date"}, "2026-04-18")
        assert parsed == "2026-04-18"
        assert err is None

    def test_date_rejects_non_iso(self):
        parsed, err = _parse_field_value({"id": "x", "type": "date"}, "04/18/2026")
        assert parsed is None
        assert err is not None
        assert "YYYY-MM-DD" in err

    def test_date_rejects_invalid_calendar_date(self):
        parsed, err = _parse_field_value({"id": "x", "type": "date"}, "2026-02-30")
        assert parsed is None
        assert err is not None


# // ========================================( Modal parse-error inline surfacing )======================================== // #


class TestModalParseErrors:
    """Parse failures surface as inline field errors + preserve raw user input."""

    async def test_integer_parse_error_populates_field_errors(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "age", "type": "integer", "label": "Age"}],
        )
        view._update_form_display = AsyncMock()

        modal = _build_form_modal(view, "Edit")
        wrapped = next(iter(modal.inputs.values()))
        wrapped.value = "not a number"

        await modal.user_callback(_make_interaction(), {})

        assert "age" in view._field_errors
        assert any("whole number" in e for e in view._field_errors["age"])

    async def test_parse_error_drafts_the_raw_string(self):
        """On parse failure the raw text is held in ``_raw_drafts`` so the next
        modal open prefills what the user typed, while ``values`` keeps only
        parsed values rather than a string in a typed field."""
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "age", "type": "integer", "label": "Age"}],
        )
        view._update_form_display = AsyncMock()

        modal = _build_form_modal(view, "Edit")
        next(iter(modal.inputs.values())).value = "abc"

        await modal.user_callback(_make_interaction(), {})

        # The raw text is not laundered into values.
        assert "age" not in view.values
        assert view._raw_drafts["age"] == "abc"
        # The next modal open still shows what the user typed.
        modal2 = _build_form_modal(view, "Edit")
        assert next(iter(modal2.inputs.values())).default == "abc"

    async def test_parse_error_skips_field_validators(self):
        """Parse errors must not trigger field validators, which would raise
        TypeError against the raw string. The submit pipeline short-circuits
        after the parse-error branch."""
        call_count = {"n": 0}

        def tracking_validator(value):
            call_count["n"] += 1
            return None

        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "age",
                    "type": "integer",
                    "validators": [tracking_validator],
                }
            ],
        )
        view._update_form_display = AsyncMock()

        modal = _build_form_modal(view, "Edit")
        wrapped = next(iter(modal.inputs.values()))
        wrapped.value = "not-int"

        await modal.user_callback(_make_interaction(), {})

        assert call_count["n"] == 0
        assert "age" in view._field_errors

    async def test_successful_parse_writes_parsed_value(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "age", "type": "integer", "label": "Age"}],
        )
        view._update_form_display = AsyncMock()

        modal = _build_form_modal(view, "Edit")
        wrapped = next(iter(modal.inputs.values()))
        wrapped.value = "25"

        await modal.user_callback(_make_interaction(), {})

        assert view.values["age"] == 25
        assert view._field_errors == {}

    async def test_date_parse_writes_iso_string(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "d", "type": "date", "label": "Day"}],
        )
        view._update_form_display = AsyncMock()

        modal = _build_form_modal(view, "Edit")
        wrapped = next(iter(modal.inputs.values()))
        wrapped.value = "2026-04-18"

        await modal.user_callback(_make_interaction(), {})

        assert view.values["d"] == "2026-04-18"

    async def test_modal_placeholder_for_date_field(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "d", "type": "date", "label": "Day"}],
        )
        modal = _build_form_modal(view, "Edit")
        text_input = _modal_text_inputs(modal)[0]
        assert text_input.placeholder == "YYYY-MM-DD"

    async def test_modal_placeholder_for_integer_field(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "n", "type": "integer", "label": "Count"}],
        )
        modal = _build_form_modal(view, "Edit")
        text_input = _modal_text_inputs(modal)[0]
        assert text_input.placeholder == "0"


# // ========================================( Typed fields ride the grouped modal )======================================== // #


class TestTypedModalAggregation:
    """integer/float/date fields share one modal with text fields, bounded by MAX_TEXT_FIELDS."""

    def test_mixed_typed_fields_in_one_modal(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "n", "type": "integer", "label": "Age"},
                {"id": "p", "type": "float", "label": "Price"},
                {"id": "d", "type": "date", "label": "Day"},
                {"id": "t", "type": "text", "label": "Name"},
            ],
        )
        modal = _build_form_modal(view, "Edit")
        text_inputs = _modal_text_inputs(modal)
        assert len(text_inputs) == 4

    def test_six_typed_fields_still_rejected(self):
        """Mixed typed fields count against MAX_TEXT_FIELDS the same as text."""
        fields = [
            {"id": "a", "type": "text"},
            {"id": "b", "type": "integer"},
            {"id": "c", "type": "float"},
            {"id": "d", "type": "date"},
            {"id": "e", "type": "text"},
            {"id": "f", "type": "integer"},
        ]
        with pytest.raises(ValueError, match="6"):
            FormView(interaction=_make_interaction(), fields=fields)

    def test_label_defaults_to_edit_fields_for_mixed_types(self):
        """When modal fields include non-text types, the plural label becomes
        ``"Edit Fields"`` rather than ``"Edit Text Fields"``."""
        fields = [
            {"id": "n", "type": "integer", "label": "Age"},
            {"id": "d", "type": "date", "label": "Day"},
        ]
        assert _resolve_modal_edit_label(None, fields) == "Edit Fields"

    def test_label_remains_edit_text_fields_for_all_text(self):
        fields = [
            {"id": "u", "type": "text", "label": "User"},
            {"id": "e", "type": "text", "label": "Email"},
        ]
        assert _resolve_modal_edit_label(None, fields) == "Edit Text Fields"


# // ========================================( Submit short-circuits on stale errors )======================================== // #


class TestSubmitAlwaysRevalidates:
    """Submit always re-validates, so every current error surfaces at once.

    An earlier short-circuit skipped validation whenever an error was
    already on screen, which could hide a non-modal error (a missing
    required select) until the visible one was fixed. Submit now recomputes
    the full error set every time; unparsed input lives in _raw_drafts, so
    validators never run against a raw string.
    """

    async def test_v1_submit_revalidates_when_field_errors_already_set(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "age", "type": "integer", "required": True}],
        )
        view._update_form_display = AsyncMock()
        view._field_errors = {"age": ["Must be a whole number, got 'x'."]}
        view._raw_drafts["age"] = "x"  # unparsed input lives in _raw_drafts, not values

        validate_spy = AsyncMock(
            return_value=(False, {"age": ["Must be a whole number, got 'x'."]}, None)
        )
        view._validate_form = validate_spy

        submit_btn = next(
            c
            for c in view.children
            if isinstance(c, StatefulButton) and c.custom_id == "form_submit"
        )

        await submit_btn.callback(_make_interaction())
        validate_spy.assert_called_once()  # re-validated instead of short-circuiting
        view._update_form_display.assert_called_once()

    async def test_v1_submit_runs_when_no_errors_set(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "name", "type": "text", "required": True}],
        )
        view._update_form_display = AsyncMock()
        view.values["name"] = "Alice"

        validate_spy = AsyncMock(return_value=(True, {}, None))
        view._validate_form = validate_spy
        view.on_submit = AsyncMock()
        view.exit = AsyncMock()

        submit_btn = next(
            c
            for c in view.children
            if isinstance(c, StatefulButton) and c.custom_id == "form_submit"
        )

        await submit_btn.callback(_make_interaction())
        validate_spy.assert_called_once()


# // ========================================( multi_select field type )======================================== // #


class TestMultiSelect:
    """multi_select renders a StatefulSelect with min=0 / max=len(options), callback writes a list."""

    def _find_select(self, view, custom_id):
        for item in view.walk_children() if hasattr(view, "walk_children") else view.children:
            if isinstance(item, StatefulSelect) and item.custom_id == custom_id:
                return item
        return None

    def test_v1_renders_multi_select(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "label": "Tags",
                    "options": [
                        {"label": "Red", "value": "r"},
                        {"label": "Blue", "value": "b"},
                        {"label": "Green", "value": "g"},
                    ],
                }
            ],
        )
        select = next(
            (
                c
                for c in view.children
                if isinstance(c, StatefulSelect) and c.custom_id == "form_tags"
            ),
            None,
        )
        assert select is not None
        assert select.max_values == 3

    def test_v2_renders_multi_select_in_action_row(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "label": "Tags",
                    "options": [
                        {"label": "Red", "value": "r"},
                        {"label": "Blue", "value": "b"},
                    ],
                }
            ],
        )
        select = next(
            (
                item
                for item in view.walk_children()
                if isinstance(item, StatefulSelect) and item.custom_id == "form_tags"
            ),
            None,
        )
        assert select is not None

    def test_required_multi_select_has_min_one(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "required": True,
                    "options": [
                        {"label": "A", "value": "a"},
                        {"label": "B", "value": "b"},
                    ],
                }
            ],
        )
        select = next(
            c for c in view.children if isinstance(c, StatefulSelect) and c.custom_id == "form_tags"
        )
        assert select.min_values == 1

    def test_optional_multi_select_has_min_zero(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "options": [{"label": "A", "value": "a"}],
                }
            ],
        )
        select = next(
            c for c in view.children if isinstance(c, StatefulSelect) and c.custom_id == "form_tags"
        )
        assert select.min_values == 0

    def test_max_values_override_honored(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "max_values": 2,
                    "options": [
                        {"label": "A", "value": "a"},
                        {"label": "B", "value": "b"},
                        {"label": "C", "value": "c"},
                    ],
                }
            ],
        )
        select = next(
            c for c in view.children if isinstance(c, StatefulSelect) and c.custom_id == "form_tags"
        )
        assert select.max_values == 2

    def test_default_preservation_marks_selected_options(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "options": [
                        {"label": "A", "value": "a"},
                        {"label": "B", "value": "b"},
                        {"label": "C", "value": "c"},
                    ],
                }
            ],
        )
        view.values["tags"] = ["a", "c"]
        # Rebuild to pick up the new selection
        view.clear_items()
        view._create_form_controls()

        select = next(
            c for c in view.children if isinstance(c, StatefulSelect) and c.custom_id == "form_tags"
        )
        defaulted = {opt.value for opt in select.options if opt.default}
        assert defaulted == {"a", "c"}

    async def test_multi_select_callback_writes_list(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "options": [
                        {"label": "A", "value": "a"},
                        {"label": "B", "value": "b"},
                    ],
                }
            ],
        )
        view._update_form_display = AsyncMock()

        select = next(
            c for c in view.children if isinstance(c, StatefulSelect) and c.custom_id == "form_tags"
        )
        # Stamp the select with fake user selection
        select._values = ["a", "b"]
        # discord.ui.Select.values reads from _selected_values in newer versions;
        # fall back to monkey-patching the property:
        type(select).values = property(lambda self: ["a", "b"])

        await select.callback(_make_interaction())
        assert view.values["tags"] == ["a", "b"]

    def test_format_multi_select_display(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "options": [{"label": "A", "value": "a"}],
                }
            ],
        )
        rendered = view._format_field_value(view.fields[0], ["a", "b"])
        assert rendered == "a, b"

    def test_format_multi_select_empty(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "options": [{"label": "A", "value": "a"}],
                }
            ],
        )
        assert view._format_field_value(view.fields[0], []) == "Not set"

    def test_secret_field_masks_its_value(self):
        """A secret field renders masked in the display, at a fixed length so
        the value and its length are both hidden; a blank one is still 'Not
        set'."""
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "pw", "type": "text", "label": "Password", "secret": True}],
        )
        short = view._format_field_value(view.fields[0], "ab")
        long = view._format_field_value(view.fields[0], "a-much-longer-password")
        assert "ab" not in short and "password" not in long
        assert short == long  # fixed length: value length not revealed
        assert view._format_field_value(view.fields[0], None) == "Not set"

    def test_display_shows_raw_draft_for_parse_failed_field(self):
        """A field the user entered that failed to parse shows the typed text
        in the display, not "Not set", matching how a text field renders its
        value. The draft lives in _raw_drafts, not self.values, so the display
        reads the draft first; a field with neither still reads "Not set"."""
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "age", "type": "integer", "label": "Age", "min_value": 13},
                {"id": "email", "type": "text", "label": "Email"},
                {"id": "bio", "type": "text", "label": "Bio"},
            ],
        )
        view.values["email"] = "notanemail"  # text parses, lands in values
        view._raw_drafts["age"] = "5"  # out-of-range int, parsed to None, drafted

        age_line = view._format_field_lines(next(f for f in view.fields if f["id"] == "age"))[0]
        email_line = view._format_field_lines(next(f for f in view.fields if f["id"] == "email"))[0]
        bio_line = view._format_field_lines(next(f for f in view.fields if f["id"] == "bio"))[0]

        assert "5" in age_line and "Not set" not in age_line
        assert "notanemail" in email_line
        assert "Not set" in bio_line  # genuinely empty, no value and no draft

    def test_formfield_carries_secret_flag(self):
        from cascadeui import FormField

        view = FormView(
            interaction=_make_interaction(),
            fields=[FormField(id="pw", label="Password", type="text", secret=True)],
        )
        assert view.fields[0]["secret"] is True

    def test_required_multi_select_empty_list_blocks_submit(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {
                    "id": "tags",
                    "type": "multi_select",
                    "required": True,
                    "options": [{"label": "A", "value": "a"}],
                }
            ],
        )
        view.values["tags"] = []
        assert view._is_field_empty(view.fields[0]) is True


# // ========================================( FormField typed schema )======================================== // #


class TestFormFieldDataclass:
    """FormField construction validates required fields and type values."""

    def test_minimal_construction(self):
        from cascadeui import FormField

        f = FormField(id="name", label="Your name")
        assert f.id == "name"
        assert f.label == "Your name"
        assert f.type == "text"
        assert f.required is False

    def test_empty_id_raises(self):
        from cascadeui import FormField

        with pytest.raises(ValueError, match="id must be a non-empty string"):
            FormField(id="", label="Label")

    def test_empty_label_raises(self):
        from cascadeui import FormField

        with pytest.raises(ValueError, match="label must be a non-empty string"):
            FormField(id="x", label="")

    def test_unknown_type_raises(self):
        from cascadeui import FormField

        with pytest.raises(ValueError, match="is not a valid type"):
            FormField(id="x", label="X", type="interger")

    def test_to_dict_strips_none(self):
        from cascadeui import FormField

        f = FormField(id="x", label="X")
        assert "min_value" not in f.to_dict()
        assert "validators" not in f.to_dict()

    def test_to_dict_keeps_explicit_false(self):
        from cascadeui import FormField

        f = FormField(id="x", label="X", required=False)
        assert f.to_dict()["required"] is False


class TestFormFieldPatternIntegration:
    """FormField instances pass through FormView / FormLayoutView cleanly."""

    def test_v1_accepts_formfield_list(self):
        from cascadeui import FormField

        view = FormView(
            interaction=_make_interaction(),
            fields=[
                FormField(id="name", label="Your name", required=True),
                FormField(id="bio", label="Bio"),
            ],
        )
        assert len(view.fields) == 2
        assert view.fields[0]["id"] == "name"
        assert view.fields[0]["required"] is True

    def test_v2_accepts_formfield_list(self):
        from cascadeui import FormField

        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[
                FormField(id="email", label="Email", type="text"),
            ],
        )
        assert view.fields[0]["id"] == "email"

    async def test_declared_default_seeds_values(self):
        """A field ``default`` used to reach only the modal prefill, so a
        required field with a default still displayed 'Not set' and blocked
        submission. Defaults now seed ``values`` at construction."""
        view = FormView(
            interaction=_make_interaction(),
            fields=[
                {"id": "age", "type": "integer", "label": "Age", "default": 18, "required": True},
                {"id": "bio", "type": "text", "label": "Bio"},  # no default
            ],
        )
        assert view.values["age"] == 18
        assert "bio" not in view.values  # absent, not seeded to None
        result = await view._validate_form()
        valid = result[0] if isinstance(result, tuple) else result
        assert valid  # the required field is satisfied by its default

    def test_dict_without_type_gets_a_text_control(self):
        """A raw dict with no ``type`` used to render no control at all, so a
        required field left the form permanently unsubmittable. It now fills
        to text, matching the FormField default."""
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "bio", "label": "Bio", "required": True}],
        )
        assert view.fields[0]["type"] == "text"
        # An edit control now exists for the field, not just the submit button.
        custom_ids = [c.custom_id for c in view.walk_children() if getattr(c, "custom_id", None)]
        assert any("edit" in cid.lower() for cid in custom_ids)

    def test_mixed_dict_and_formfield(self):
        from cascadeui import FormField

        view = FormView(
            interaction=_make_interaction(),
            fields=[
                FormField(id="a", label="A"),
                {"id": "b", "label": "B", "type": "text"},
            ],
        )
        assert [f["id"] for f in view.fields] == ["a", "b"]

    def test_invalid_entry_type_raises(self):
        with pytest.raises(TypeError, match="field entries must be FormField or dict"):
            FormView(interaction=_make_interaction(), fields=["not a field"])


class TestFormSchema:
    """FormSchema subclass hooks up via schema= kwarg."""

    def test_schema_subclass_feeds_fields(self):
        from cascadeui import FormField, FormSchema

        class ProfileSchema(FormSchema):
            def get_fields(self):
                return [
                    FormField(id="name", label="Your name", required=True),
                    FormField(id="age", label="Your age", type="integer", min_value=0),
                ]

        view = FormLayoutView(interaction=_make_interaction(), schema=ProfileSchema())
        assert [f["id"] for f in view.fields] == ["name", "age"]
        assert view.fields[1]["type"] == "integer"
        assert view.fields[1]["min_value"] == 0

    def test_base_class_get_fields_raises(self):
        from cascadeui import FormSchema

        with pytest.raises(NotImplementedError, match="must override get_fields"):
            FormSchema().get_fields()

    def test_schema_and_fields_together_raises(self):
        from cascadeui import FormField, FormSchema

        class S(FormSchema):
            def get_fields(self):
                return [FormField(id="a", label="A")]

        with pytest.raises(ValueError, match="either 'fields=' or 'schema='"):
            FormView(
                interaction=_make_interaction(),
                fields=[{"id": "b", "label": "B"}],
                schema=S(),
            )

    def test_non_formschema_raises(self):
        with pytest.raises(TypeError, match="expects a FormSchema instance"):
            FormView(interaction=_make_interaction(), schema="not a schema")

    def test_no_fields_and_no_schema_returns_empty(self):
        """Zero-config form (no fields, no schema) is a valid state, not an error.

        Locks in the documented zero-config contract so a future refactor
        that adds a guard for "neither supplied" fails loudly against this
        test rather than silently flipping the polarity.
        """
        view = FormLayoutView(interaction=_make_interaction())
        assert view.fields == []


class TestFormViewInitialRender:
    """V1 form content reaches the first message, not just the pop edit."""

    async def test_send_ships_the_form_embed(self):
        """The form embed is the V1 render; the controls alone are not.

        _build_form_embed is private, so before send() supplied it a V1 form
        had no public way to put its first frame on the message.
        """
        interaction = _make_interaction()
        view = FormView(
            interaction=interaction,
            title="Signup",
            fields=[{"id": "name", "label": "Name", "type": "text"}],
        )

        await view.send()

        embed = interaction.response.send_message.call_args.kwargs.get("embed")
        assert embed is not None
        assert "Signup" in (embed.title or "")

    async def test_explicit_embed_wins(self):
        """A caller-supplied embed is not replaced by the form's own."""
        interaction = _make_interaction()
        view = FormView(
            interaction=interaction,
            title="Signup",
            fields=[{"id": "name", "label": "Name", "type": "text"}],
        )

        await view.send(embed=discord.Embed(title="Caller"))

        embed = interaction.response.send_message.call_args.kwargs.get("embed")
        assert embed.title == "Caller"


# // ========================================( Text-edit modal ack backstop )======================================== // #


class TestTextEditModalBackstop:
    """The internal text-edit modal takes its ack backstop from the form class."""

    def test_override_reaches_modal_v1(self):
        class SlowForm(FormView):
            text_edit_modal_auto_defer_delay = 5.0

        view = SlowForm(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        modal = _build_form_modal(view, "Edit")
        assert modal.auto_defer_delay == 5.0

    def test_override_reaches_modal_v2(self):
        class SlowLayoutForm(FormLayoutView):
            text_edit_modal_auto_defer_delay = 4.0

        view = SlowLayoutForm(
            interaction=_make_interaction(),
            fields=[{"id": "u", "type": "text", "label": "Username"}],
        )
        modal = _build_form_modal(view, "Edit")
        assert modal.auto_defer_delay == 4.0

    def test_non_positive_backstop_rejected_at_definition(self):
        with pytest.raises(ValueError, match="text_edit_modal_auto_defer_delay"):

            class BadForm(FormView):
                text_edit_modal_auto_defer_delay = 0


# // ========================================( Inline error setters )======================================== // #


class TestFormErrorSetters:
    """set_form_error / set_field_error write the error state AND re-render it."""

    def _tree_text(self, view):
        from discord.ui import TextDisplay

        return " ".join(
            getattr(t, "content", "") for t in view.walk_children() if isinstance(t, TextDisplay)
        )

    async def test_set_form_error_renders_on_v2(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[{"id": "n", "type": "text", "label": "Name"}],
        )
        view.refresh = AsyncMock()
        await view.set_form_error("start must precede end")
        assert view._form_error == "start must precede end"
        assert "start must precede end" in self._tree_text(view)
        view.refresh.assert_awaited()

    async def test_set_field_error_renders_on_v2(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[{"id": "n", "type": "text", "label": "Name"}],
        )
        view.refresh = AsyncMock()
        await view.set_field_error("n", "too short")
        assert view._field_errors["n"] == ["too short"]
        assert "too short" in self._tree_text(view)

    async def test_set_form_error_renders_on_v1(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "n", "type": "text", "label": "Name"}],
        )
        view.refresh = AsyncMock()
        await view.set_form_error("start must precede end")
        assert view._form_error == "start must precede end"
        embed = view.refresh.call_args.kwargs.get("embed")
        assert embed is not None
        assert "start must precede end" in (embed.description or "")

    async def test_set_form_error_none_clears_v2(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[{"id": "n", "type": "text", "label": "Name"}],
        )
        view.refresh = AsyncMock()
        await view.set_form_error("boom")
        await view.set_form_error(None)
        assert view._form_error is None
        assert "boom" not in self._tree_text(view)


class TestOnSubmitCrossFieldReject:
    """on_submit rejects a cross-field rule via set_form_error / set_field_error
    and keeps the form open. The submit flow no longer auto-exits after on_submit
    when on_submit set an error -- the setter's documented contract.
    """

    @staticmethod
    def _submit_button(view):
        walker = getattr(view, "walk_children", None)
        items = list(walker()) if callable(walker) else list(view.children)
        for item in items:
            if isinstance(item, StatefulButton) and getattr(item, "custom_id", "") == "form_submit":
                return item
        raise AssertionError("no submit button found")

    @pytest.mark.parametrize("view_cls", [FormView, FormLayoutView])
    async def test_set_field_error_in_on_submit_keeps_form_open(self, view_cls):
        class _RejectingForm(view_cls):
            async def on_submit(self, interaction, values):
                await self.set_field_error("age", "too young")

        view = _RejectingForm(
            interaction=_make_interaction(),
            fields=[{"id": "age", "type": "integer", "label": "Age", "default": 5}],
        )
        view.refresh = AsyncMock()
        view.exit = AsyncMock()

        await self._submit_button(view).original_callback(_make_interaction())

        view.exit.assert_not_called()  # rejection keeps the form open
        assert view._field_errors.get("age") == ["too young"]

    @pytest.mark.parametrize("view_cls", [FormView, FormLayoutView])
    async def test_clean_on_submit_still_auto_exits(self, view_cls):
        class _CleanForm(view_cls):
            async def on_submit(self, interaction, values):
                pass  # no error, no explicit exit -> the flow auto-exits

        view = _CleanForm(
            interaction=_make_interaction(),
            fields=[{"id": "age", "type": "integer", "label": "Age", "default": 5}],
        )
        view.refresh = AsyncMock()
        view.exit = AsyncMock()

        await self._submit_button(view).original_callback(_make_interaction())

        view.exit.assert_awaited_once()  # unchanged: a clean submit still exits


class TestFormReload:
    """reload() re-renders the V1 form embed instead of shipping an empty edit."""

    async def test_v1_reload_ships_the_embed(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "n", "type": "text", "label": "Name"}],
        )
        view.refresh = AsyncMock()
        await view.reload()
        embed = view.refresh.call_args.kwargs.get("embed")
        assert embed is not None  # was the empty-kwargs no-op before the fix


class TestStatefulCallbackWiring:
    """Form controls route through the stateful callback wrapper.

    Assigning ``.callback`` after construction overwrites the wrapper that
    ``StatefulButton`` / ``StatefulSelect`` install, which drops the
    COMPONENT_INTERACTION dispatch and leaves ``_CURRENT_INTERACTION`` unbound,
    so every form refresh misses the acting-view fast path.
    """

    @staticmethod
    def _find(view, custom_id):
        walker = getattr(view, "walk_children", None)
        items = list(walker()) if walker else list(view.children)
        for item in items:
            if getattr(item, "custom_id", None) == custom_id:
                return item
        raise AssertionError(f"no component with custom_id {custom_id!r}")

    @staticmethod
    def _probe(view_cls, fields):
        seen = {}

        class _ProbeForm(view_cls):
            async def on_field_changed(self, field_id, old_value, new_value):
                seen["interaction"] = _CURRENT_INTERACTION.get()

        view = _ProbeForm(interaction=_make_interaction(), fields=fields)
        view.refresh = AsyncMock()
        return view, seen

    @pytest.mark.parametrize("view_cls", [FormView, FormLayoutView])
    async def test_boolean_button_binds_the_interaction(self, view_cls):
        view, seen = self._probe(view_cls, [{"id": "ok", "type": "boolean", "label": "Ready"}])
        interaction = _make_interaction()

        await self._find(view, "form_ok_yes").callback(interaction)

        assert seen["interaction"] is interaction

    @pytest.mark.parametrize("view_cls", [FormView, FormLayoutView])
    async def test_select_receives_values_through_the_wrapper(self, view_cls, monkeypatch):
        view, _ = self._probe(
            view_cls,
            [
                {
                    "id": "color",
                    "type": "select",
                    "label": "Color",
                    "options": [{"label": "Red", "value": "red"}],
                }
            ],
        )
        select = self._find(view, "form_color")
        monkeypatch.setattr(type(select), "values", property(lambda s: ["red"]))

        # The inner callback takes (interaction, values); only the wrapper
        # supplies the second argument.
        await select.callback(_make_interaction())

        assert view.values["color"] == "red"
