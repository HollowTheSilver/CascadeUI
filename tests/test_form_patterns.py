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
from cascadeui.validation import min_length
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

    def test_modal_falls_back_to_field_default_when_value_absent(self):
        """When ``form.values`` has no entry for a field, the modal seeds
        the input from ``field["default"]``. Exercises the fallback branch
        in ``_build_form_modal`` (``form.values.get(field_id, field["default"])``).
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
        assert "u" not in view.values  # precondition: nothing written yet
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
        inputs. The inline-error flow (Commit 3) surfaces the rejection
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
    """Commit 3: validator failure surfaces on the form, not an ephemeral.

    The V1 and V2 submit callbacks both route rejection through
    ``_set_validation_errors`` + ``_update_form_display`` so the user
    sees the error inline on the form body. Clearing happens on any
    field-change gesture so the UI stays in sync with the latest input.
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
    """Commit 3: ``group`` field key collects consecutive runs, no merging.

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

    async def test_parse_error_preserves_raw_string(self):
        """On parse failure, the raw string is written to form.values so the
        next modal open shows what the user typed rather than clearing input."""
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "age", "type": "integer", "label": "Age"}],
        )
        view._update_form_display = AsyncMock()

        modal = _build_form_modal(view, "Edit")
        wrapped = next(iter(modal.inputs.values()))
        wrapped.value = "abc"

        await modal.user_callback(_make_interaction(), {})

        assert view.values["age"] == "abc"

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


class TestSubmitShortCircuit:
    """Submit while _field_errors is populated refreshes display instead of validating."""

    async def test_v1_submit_skips_validate_when_field_errors_set(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "age", "type": "integer", "required": True}],
        )
        view._update_form_display = AsyncMock()
        view._field_errors = {"age": ["Must be a whole number, got 'x'."]}
        view.values["age"] = "x"  # raw string from parse failure

        validate_spy = AsyncMock()
        view._validate_form = validate_spy

        submit_btn = next(
            c
            for c in view.children
            if isinstance(c, StatefulButton) and c.custom_id == "form_submit"
        )

        await submit_btn.callback(_make_interaction())
        validate_spy.assert_not_called()
        view._update_form_display.assert_called_once()

    async def test_v1_submit_runs_when_errors_cleared(self):
        view = FormView(
            interaction=_make_interaction(),
            fields=[{"id": "name", "type": "text", "required": True}],
        )
        view._update_form_display = AsyncMock()
        view.values["name"] = "Alice"
        # No errors set -> validate_form runs normally

        validate_spy = AsyncMock(return_value=(True, ""))
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
