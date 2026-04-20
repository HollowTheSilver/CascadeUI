# // ========================================( Modules )======================================== // #


import logging
from typing import Any, ClassVar, Dict, List, Optional

import discord
from discord import Interaction
from discord.ui import ActionRow

from ...components.base import StatefulButton, StatefulSelect
from ...components.inputs import Modal as CascadeModal
from ...components.inputs import TextInput as CascadeTextInput
from ...components.patterns.v2 import alert, card
from .types import FormSchema, _normalize_fields
from ..base import _StatefulMixin
from ..view import StatefulView
from ..layout import StatefulLayoutView

logger = logging.getLogger(__name__)

# Discord hard limit on TextInput items per Modal. Forms with more than this
# many modal-rendered fields (``"text"``, ``"integer"``, ``"float"``,
# ``"date"``) raise ``ValueError`` at construction so the failure surfaces
# at definition time rather than on first click.
MAX_TEXT_FIELDS = 5

# Escaped asterisk for required-field markers in TextDisplay and embed
# field names. Unescaped ``*`` in Discord markdown triggers italics when
# multiple markers appear on adjacent lines.
_REQUIRED_MARKER = " \\*"

# Field types that render through the shared form modal rather than as
# inline components. Integer / float / date ride the same TextInput tray
# as "text"; the submit callback parses each value per type before
# writing to ``form.values``.
_MODAL_TYPES = frozenset({"text", "integer", "float", "date"})


# // ========================================( Module Helpers )======================================== // #


def _collect_modal_fields(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return fields whose ``type`` renders through the shared form modal."""
    return [f for f in fields if f.get("type") in _MODAL_TYPES]


def _validate_modal_field_count(cls_name: str, fields: List[Dict[str, Any]]) -> None:
    """Raise ``ValueError`` if more than ``MAX_TEXT_FIELDS`` modal fields exist."""
    count = len(_collect_modal_fields(fields))
    if count > MAX_TEXT_FIELDS:
        raise ValueError(
            f"{cls_name} defines {count} modal fields (text/integer/float/date), "
            f"but Discord modals allow at most {MAX_TEXT_FIELDS} text inputs per modal."
        )


def _resolve_modal_edit_label(
    override: Optional[str], modal_fields: List[Dict[str, Any]]
) -> str:
    """Resolve the grouped modal-edit button label.

    Precedence: explicit override -> singular ``"Edit {label}"`` when
    exactly one modal field exists -> ``"Edit Text Fields"`` when every
    modal field is ``type="text"`` -> ``"Edit Fields"`` for mixed or
    purely typed forms.
    """
    if override is not None:
        return override
    if len(modal_fields) == 1:
        only = modal_fields[0]
        return f"Edit {only.get('label', only.get('id'))}"
    if all(f.get("type") == "text" for f in modal_fields):
        return "Edit Text Fields"
    return "Edit Fields"


def _parse_field_value(field: Dict[str, Any], raw: Optional[str]):
    """Parse a modal input string per field type.

    Returns ``(parsed_value, error_message_or_None)``. Empty / missing
    inputs yield ``(None, None)`` -- required enforcement lives in
    ``_validate_form`` so the two concerns stay separated.

    For ``type="text"`` the raw string passes through unchanged. Integer
    and float parse via ``int()`` / ``float()``; ``min_value`` and
    ``max_value`` field keys clamp the parsed result. ``date`` uses
    :meth:`datetime.date.fromisoformat` (YYYY-MM-DD) and stores the
    canonical ISO string back so values round-trip through persistence
    without a datetime serializer.
    """
    ftype = field.get("type", "text")
    if raw is None:
        return None, None
    raw = raw.strip()
    if raw == "":
        return None, None

    if ftype == "text":
        return raw, None

    if ftype == "integer":
        try:
            parsed = int(raw)
        except ValueError:
            return None, f"Must be a whole number, got {raw!r}."
        min_v = field.get("min_value")
        max_v = field.get("max_value")
        if min_v is not None and parsed < min_v:
            return None, f"Must be at least {min_v}."
        if max_v is not None and parsed > max_v:
            return None, f"Must be at most {max_v}."
        return parsed, None

    if ftype == "float":
        try:
            parsed = float(raw)
        except ValueError:
            return None, f"Must be a number, got {raw!r}."
        min_v = field.get("min_value")
        max_v = field.get("max_value")
        if min_v is not None and parsed < min_v:
            return None, f"Must be at least {min_v}."
        if max_v is not None and parsed > max_v:
            return None, f"Must be at most {max_v}."
        return parsed, None

    if ftype == "date":
        from datetime import date as _date

        try:
            parsed = _date.fromisoformat(raw)
        except ValueError:
            return None, f"Must be YYYY-MM-DD, got {raw!r}."
        return parsed.isoformat(), None

    # Any other modal-registered type passes through as a raw string.
    return raw, None


def _build_form_modal(form, title: str) -> CascadeModal:
    """Build a grouped modal containing one ``TextInput`` per modal field.

    On submit, each input's raw string is parsed per its declared type.
    Successful parses write the parsed value into ``form.values``; parse
    failures write the raw string instead so the next modal open shows
    what the user typed, then repopulate ``_field_errors`` so the error
    surfaces inline on the form body.

    The modal dispatches ``MODAL_SUBMITTED`` with ``view_id=form.id`` so
    devtools and user-registered hooks see the event; the undo middleware
    already skips ``MODAL_SUBMITTED``, so form-edit hops do not pollute
    the undo stack.

    Validators declared on individual fields run in the submit callback
    after parsing succeeds -- they never fire against unparsed raw
    strings.
    """
    modal_fields = _collect_modal_fields(form.fields)
    field_by_id: Dict[Any, Dict[str, Any]] = {f.get("id"): f for f in modal_fields}
    inputs: List[CascadeTextInput] = []
    input_to_field_id: Dict[CascadeTextInput, Any] = {}
    field_validators: Dict[Any, List] = {}

    for field in modal_fields:
        field_id = field.get("id")
        field_label = field.get("label", field_id)
        current_value = form.values.get(field_id, field.get("default"))
        placeholder = field.get("placeholder")
        if placeholder is None:
            ftype = field.get("type", "text")
            if ftype == "date":
                placeholder = "YYYY-MM-DD"
            elif ftype in ("integer", "float"):
                placeholder = "0"
        text_input = CascadeTextInput(
            label=field_label,
            placeholder=placeholder,
            default=str(current_value) if current_value is not None else None,
            required=field.get("required", False),
            min_length=field.get("min_length"),
            max_length=field.get("max_length"),
            style=field.get("style", discord.TextStyle.short),
        )
        input_to_field_id[text_input] = field_id
        inputs.append(text_input)
        validators = field.get("validators")
        if validators:
            field_validators[field_id] = validators

    async def on_modal_submit(interaction: Interaction, values: Dict[str, Any]) -> None:
        changes: List[tuple] = []
        parse_errors: Dict[Any, List[str]] = {}

        for text_input, field_id in input_to_field_id.items():
            field = field_by_id[field_id]
            old_value = form.values.get(field_id)
            raw = text_input.value
            parsed, parse_error = _parse_field_value(field, raw)
            if parse_error is not None:
                # Preserve the raw string so the next modal open shows
                # what the user typed -- the error surfaces via
                # _field_errors instead.
                form.values[field_id] = raw
                parse_errors[field_id] = [parse_error]
                continue
            form.values[field_id] = parsed
            if old_value != parsed:
                changes.append((field_id, old_value, parsed))

        if changes:
            form._clear_errors()

        for field_id, old_value, new_value in changes:
            await form._call_hook_safe(
                form.on_field_changed, field_id, old_value, new_value
            )

        if parse_errors:
            # Parse errors take precedence over validator errors -- running
            # validators against raw strings would TypeError.
            form._field_errors = dict(parse_errors)
            form._form_error = None
            await form._update_form_display()
            return

        # Run field validators that were originally declared on the fields.
        if field_validators:
            from ...validation import validate_fields

            field_defs = [
                {"id": fid, "validators": fv} for fid, fv in field_validators.items()
            ]
            errors = await validate_fields(form.values, field_defs)
            if errors:
                form._set_validation_errors(errors)
                await form._update_form_display()
                return

        await form._update_form_display()

    return CascadeModal(
        title=title,
        inputs=inputs,
        callback=on_modal_submit,
        view_id=form.id,
    )


# // ========================================( Shared Mixin )======================================== // #


class _BaseFormMixin:
    """Version-agnostic form logic shared by ``FormView`` and ``FormLayoutView``.

    Holds the ``__init__`` body, the ``on_submit`` default hook, the
    text-edit button triple, the grouped-modal opener, the validation
    pipeline, and the state-driven refresh entry point. V1 and V2
    subclasses supply only the control-construction and display paths
    that genuinely differ between component systems.

    Internal. Not exported. The public hierarchy
    (``FormView`` / ``FormLayoutView``) is unchanged.
    """

    text_edit_button_label: ClassVar[Optional[str]] = None
    text_edit_button_emoji: ClassVar[Optional[str]] = "\u270f\ufe0f"
    text_edit_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary
    _BUTTON_STYLE_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BUTTON_STYLE_ATTRS,
        "text_edit_button_style",
    )

    def __init__(
        self,
        *args,
        title="Form",
        fields=None,
        schema: Optional[FormSchema] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.title = title
        self.fields = _normalize_fields(fields, schema, type(self).__name__)
        self.values = {}

        # Inline validation error state. ``_field_errors`` maps field id
        # to list[str]; ``_form_error`` holds a form-level message used
        # for "complete all required fields" style errors. Both are
        # cleared on any field change and repopulated on submit failure.
        self._field_errors: Dict[str, List[str]] = {}
        self._form_error: Optional[str] = None

        _validate_modal_field_count(type(self).__name__, self.fields)

        self._build_form()

    # // ----( Group + error helpers )---- // #

    def _iter_field_groups(self):
        """Yield ``(group_name_or_None, [fields])`` in declaration order.

        Consecutive fields sharing the same ``"group"`` key form a single
        run; interleaved groups render as separate runs (no merging).
        Fields without a ``group`` key land in a ``None`` run.
        """
        runs = []
        current_name = None
        current_fields = []
        started = False
        for field in self.fields:
            group = field.get("group")
            if not started or group != current_name:
                if started:
                    runs.append((current_name, current_fields))
                current_name = group
                current_fields = [field]
                started = True
            else:
                current_fields.append(field)
        if started:
            runs.append((current_name, current_fields))
        return runs

    def _has_field_groups(self) -> bool:
        return any(field.get("group") is not None for field in self.fields)

    def _format_field_value(self, field: Dict[str, Any], value: Any) -> str:
        """Render a single field's value as display text."""
        ftype = field.get("type")
        if ftype == "boolean":
            if value is True:
                return "Yes"
            if value is False:
                return "No"
            return "Not set"
        if ftype == "multi_select":
            if not value:
                return "Not set"
            if isinstance(value, (list, tuple, set)):
                return ", ".join(str(v) for v in value)
            return str(value)
        return "Not set" if value is None else str(value)

    def _format_field_lines(self, field: Dict[str, Any]) -> List[str]:
        """Return one or two display lines for a field -- value plus inline error."""
        fid = field.get("id")
        flabel = field.get("label", fid)
        fvalue = self._format_field_value(field, self.values.get(fid))
        required = _REQUIRED_MARKER if field.get("required", False) else ""
        lines = [f"{flabel}{required}: {fvalue}"]
        if fid in self._field_errors:
            errs = ", ".join(self._field_errors[fid])
            lines.append(f"\u26a0\ufe0f {errs}")
        return lines

    def _set_validation_errors(self, errors: Any) -> None:
        """Populate ``_field_errors`` / ``_form_error`` from ``_validate_form()``."""
        if isinstance(errors, dict):
            self._field_errors = {
                fid: [e.message for e in errs] for fid, errs in errors.items()
            }
            self._form_error = None
        else:
            self._field_errors = {}
            self._form_error = errors

    def _clear_errors(self) -> None:
        self._field_errors = {}
        self._form_error = None

    def _build_form(self):
        """Construct the initial form display.

        V1 calls ``_create_form_controls`` directly (display lives on
        the embed sent later). V2 calls ``_rebuild_display`` which
        constructs the TextDisplay container and then the controls.
        Subclasses override to point at the right entry point.
        """
        raise NotImplementedError

    async def on_submit(self, interaction: Interaction, values: Dict[str, Any]) -> None:
        """Called when the user clicks Submit and every validator passes.

        Default implementation posts a generic confirmation message back
        to the user. Override to persist the form, send a receipt, or
        transition to another view. ``on_*`` is reserved for method hooks,
        so form examples should subclass and override this rather than
        passing a callable in.
        """
        await self.respond(
            interaction,
            f"Form submitted with values: {values}",
            ephemeral=True,
        )

    async def on_field_changed(self, field_name: str, old: Any, new: Any) -> None:
        """Called when a field's value changes in response to user input.

        Fires after ``self.values[field_name]`` is updated by a select,
        boolean, or text-modal callback. ``old`` is the previous value
        (or ``None`` if unset); ``new`` is the value just written.
        Only fires when ``old != new`` so repeated identical submissions
        do not trigger redundant work.

        Fire-and-forget: exceptions raised by an override are logged and
        swallowed by the pattern, so a buggy hook never blocks the form
        rebuild or the user's next interaction.

        Default is a no-op. Override to persist live deltas, dispatch
        an analytics action, trigger inter-field recomputation, or
        surface warnings before the user submits.
        """

    async def _call_hook_safe(self, hook, *args) -> None:
        """Run a fire-and-forget hook, logging any exception.

        Matches the wrapping pattern at ``base.py:_enforce_instance_limit``
        for ``on_replaced`` -- override errors must never block the
        surrounding form rebuild or interaction-response flow.
        """
        try:
            await hook(*args)
        except Exception as exc:
            logger.warning(
                f"{hook.__name__} raised in {type(self).__name__}: {exc}"
            )

    async def _open_text_modal(self, interaction: Interaction) -> None:
        """Open the grouped modal for every modal-rendered field on the form."""
        modal_fields = _collect_modal_fields(self.fields)
        title = _resolve_modal_edit_label(self.text_edit_button_label, modal_fields)
        modal = _build_form_modal(self, title)
        await self.open_modal(interaction, modal)

    def _is_field_empty(self, field: Dict[str, Any]) -> bool:
        """Return True when ``self.values[field_id]`` is missing or empty.

        Empty means ``None``, absent key, or (for ``multi_select``) an
        empty sequence. Parse errors leave raw strings in ``self.values``
        which still count as "present" for required-field purposes --
        the parse error is what blocks submission in that case.
        """
        field_id = field.get("id")
        if field_id not in self.values:
            return True
        value = self.values[field_id]
        if value is None:
            return True
        if field.get("type") == "multi_select" and isinstance(value, (list, tuple, set)):
            return len(value) == 0
        return False

    async def _validate_form(self):
        """Validate form data using both required-field checks and field validators."""
        missing_fields = []
        for field in self.fields:
            if field.get("required", False) and self._is_field_empty(field):
                missing_fields.append(field.get("label", field.get("id")))

        if missing_fields:
            return False, f"Please complete all required fields: {', '.join(missing_fields)}"

        has_validators = any(field.get("validators") for field in self.fields)
        if has_validators:
            from ...validation import validate_fields

            errors = await validate_fields(self.values, self.fields)
            if errors:
                return False, errors

        return True, ""

    async def on_state_changed(self, state):
        """Update form display when state changes."""
        await self._update_form_display()


# // ========================================( V1: FormView )======================================== // #


class FormView(_BaseFormMixin, StatefulView):
    """A view for collecting form data from users.

    Supports field types: ``"select"``, ``"boolean"``, ``"text"``.

    Text fields cannot render inline (Discord restricts ``TextInput`` to
    modals), so a grouped "Edit Text Fields" button opens a single
    :class:`cascadeui.Modal` containing one input per declared text
    field. The 5-input modal cap is enforced at construction time.

    Fields can include a ``validators`` list of callables for per-field
    validation. See ``cascadeui.validation`` for built-in validators.
    """

    def _build_form(self):
        self._create_form_controls()

    def _create_form_controls(self):
        """Create form controls based on field definitions."""
        current_row = 0

        for field in self.fields:
            if current_row > 4:
                break  # Discord max 5 rows (0-4)

            field_type = field.get("type", "string")
            field_id = field.get("id")
            field_label = field.get("label", field_id)
            field_required = field.get("required", False)

            if field_type == "select":
                current = self.values.get(field_id)
                options = [
                    discord.SelectOption(
                        label=opt.get("label"),
                        value=opt.get("value"),
                        description=opt.get("description"),
                        default=(current is not None and opt.get("value") == current),
                    )
                    for opt in field.get("options", [])
                ]

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=1,
                    custom_id=f"form_{field_id}",
                    row=current_row,
                )

                # Capture field_id and select per-iteration via default args
                def make_select_callback(fid, sel):
                    async def callback(interaction):
                        old_value = self.values.get(fid)
                        new_value = sel.values[0]
                        self.values[fid] = new_value
                        if old_value != new_value:
                            self._clear_errors()
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, new_value
                            )
                        await self._update_form_display()

                    return callback

                select.callback = make_select_callback(field_id, select)
                self.add_item(select)
                current_row += 1  # Select takes a full row

            elif field_type == "multi_select":
                options_src = field.get("options", [])
                current_values = self.values.get(field_id) or []
                if not isinstance(current_values, (list, tuple, set)):
                    current_values = [current_values]
                current_set = set(current_values)
                options = [
                    discord.SelectOption(
                        label=opt.get("label"),
                        value=opt.get("value"),
                        description=opt.get("description"),
                        default=(opt.get("value") in current_set),
                    )
                    for opt in options_src
                ]

                max_values = field.get("max_values")
                if max_values is None:
                    max_values = max(1, len(options))

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=max_values,
                    custom_id=f"form_{field_id}",
                    row=current_row,
                )

                def make_multi_select_callback(fid, sel):
                    async def callback(interaction):
                        old_value = self.values.get(fid)
                        new_value = list(sel.values)
                        self.values[fid] = new_value
                        if list(old_value or []) != new_value:
                            self._clear_errors()
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, new_value
                            )
                        await self._update_form_display()

                    return callback

                select.callback = make_multi_select_callback(field_id, select)
                self.add_item(select)
                current_row += 1

            elif field_type == "boolean":
                yes_button = StatefulButton(
                    label=f"{field_label}: Yes",
                    style=discord.ButtonStyle.success,
                    custom_id=f"form_{field_id}_yes",
                    row=current_row,
                )

                no_button = StatefulButton(
                    label=f"{field_label}: No",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"form_{field_id}_no",
                    row=current_row,
                )

                # Capture field_id per-iteration via default arg
                def make_bool_callback(fid, value):
                    async def callback(interaction):
                        old_value = self.values.get(fid)
                        self.values[fid] = value
                        if old_value != value:
                            self._clear_errors()
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, value
                            )
                        await self._update_form_display()

                    return callback

                yes_button.callback = make_bool_callback(field_id, True)
                no_button.callback = make_bool_callback(field_id, False)

                self.add_item(yes_button)
                self.add_item(no_button)
                current_row += 1  # Boolean pair takes one row

        # Grouped modal-edit button -- one modal covers every text /
        # integer / float / date field on the form.
        modal_fields = _collect_modal_fields(self.fields)
        if modal_fields:
            text_button_row = min(current_row, 4)
            text_button = StatefulButton(
                label=_resolve_modal_edit_label(self.text_edit_button_label, modal_fields),
                emoji=self.text_edit_button_emoji,
                style=self.text_edit_button_style,
                custom_id="form_edit_text",
                row=text_button_row,
            )
            text_button.callback = self._open_text_modal
            self.add_item(text_button)
            current_row = text_button_row + 1

        # Add submit button on the next available row (or last row if full)
        submit_row = min(current_row, 4)
        submit_button = StatefulButton(
            label="Submit",
            style=discord.ButtonStyle.primary,
            custom_id="form_submit",
            row=submit_row,
        )

        async def submit_callback(interaction):
            # Parse errors from the modal leave raw strings in self.values;
            # submitting would fire validators against unparsed strings.
            # Keep the user on the existing error view until they reopen
            # the modal and fix the input.
            if self._field_errors or self._form_error:
                await self._update_form_display()
                return

            valid, errors = await self._validate_form()

            if valid:
                await self.on_submit(interaction, self.values)
                # ``on_submit`` overrides that bypass ``interaction.response``
                # leave the interaction unacked; the post-callback defer in
                # ``_scheduled_task`` handles that case.
                if not self.is_finished():
                    await self.exit()
            else:
                self._set_validation_errors(errors)
                await self._update_form_display()

        submit_button.callback = submit_callback
        self.add_item(submit_button)

    async def _update_form_display(self):
        """Update the form display with current values.

        Renders fields grouped by consecutive ``"group"`` runs when any
        field declares a group, falling back to a flat field list when
        none do. Inline field errors surface as an italic warning line
        under the offending field value; the form-level error becomes
        the embed's red-tinted description.
        """
        has_form_error = self._form_error is not None
        colour = discord.Color.red() if has_form_error or self._field_errors else None
        embed = discord.Embed(title=self.title, color=colour)

        if has_form_error:
            embed.description = f"\u26a0\ufe0f {self._form_error}"

        if self._has_field_groups():
            for group_name, group_fields in self._iter_field_groups():
                group_lines = []
                for field in group_fields:
                    group_lines.extend(self._format_field_lines(field))
                heading = f"**{group_name}**" if group_name else "\u200b"
                embed.add_field(
                    name=heading,
                    value="\n".join(group_lines),
                    inline=False,
                )
        else:
            for field in self.fields:
                lines = self._format_field_lines(field)
                required = _REQUIRED_MARKER if field.get("required", False) else ""
                field_label = field.get("label", field.get("id"))
                # First line already embeds label+value; use the bare value
                # portion for the embed-field body so the label rendered by
                # Discord's bold field name is not duplicated.
                value_line = lines[0].split(":", 1)[-1].strip()
                body = value_line if len(lines) == 1 else value_line + "\n" + lines[1]
                embed.add_field(
                    name=f"{field_label}{required}",
                    value=body,
                    inline=False,
                )

        await self.refresh(embed=embed)


# // ========================================( V2: FormLayoutView )======================================== // #


class FormLayoutView(_BaseFormMixin, StatefulLayoutView):
    """A V2 layout view for collecting form data from users.

    The V2 equivalent of ``FormView``. Uses ``TextDisplay`` inside a
    ``Container`` instead of embeds for field display.

    Supports field types: ``"select"``, ``"boolean"``, ``"text"``. Text
    fields render via a grouped "Edit Text Fields" button that opens a
    single :class:`cascadeui.Modal`; the 5-input modal cap is enforced
    at construction time.
    """

    def _build_form(self):
        self._rebuild_display()

    def _create_form_controls(self):
        """Create form controls based on field definitions.

        Selects mark the option matching ``self.values[field_id]`` as
        ``default=True`` so the current selection is visually preserved
        across rebuilds.
        """
        for field in self.fields:
            field_type = field.get("type", "string")
            field_id = field.get("id")
            field_label = field.get("label", field_id)
            field_required = field.get("required", False)

            if field_type == "select":
                current = self.values.get(field_id)
                options = [
                    discord.SelectOption(
                        label=opt.get("label"),
                        value=opt.get("value"),
                        description=opt.get("description"),
                        default=(current is not None and opt.get("value") == current),
                    )
                    for opt in field.get("options", [])
                ]

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=1,
                    custom_id=f"form_{field_id}",
                )

                def make_select_callback(fid, sel):
                    async def callback(interaction):
                        old_value = self.values.get(fid)
                        new_value = sel.values[0]
                        self.values[fid] = new_value
                        if old_value != new_value:
                            self._clear_errors()
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, new_value
                            )
                        await self._update_form_display()

                    return callback

                select.callback = make_select_callback(field_id, select)
                self.add_item(ActionRow(select))

            elif field_type == "multi_select":
                options_src = field.get("options", [])
                current_values = self.values.get(field_id) or []
                if not isinstance(current_values, (list, tuple, set)):
                    current_values = [current_values]
                current_set = set(current_values)
                options = [
                    discord.SelectOption(
                        label=opt.get("label"),
                        value=opt.get("value"),
                        description=opt.get("description"),
                        default=(opt.get("value") in current_set),
                    )
                    for opt in options_src
                ]

                max_values = field.get("max_values")
                if max_values is None:
                    max_values = max(1, len(options))

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=max_values,
                    custom_id=f"form_{field_id}",
                )

                def make_multi_select_callback(fid, sel):
                    async def callback(interaction):
                        old_value = self.values.get(fid)
                        new_value = list(sel.values)
                        self.values[fid] = new_value
                        if list(old_value or []) != new_value:
                            self._clear_errors()
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, new_value
                            )
                        await self._update_form_display()

                    return callback

                select.callback = make_multi_select_callback(field_id, select)
                self.add_item(ActionRow(select))

            elif field_type == "boolean":
                yes_button = StatefulButton(
                    label=f"{field_label}: Yes",
                    style=discord.ButtonStyle.success,
                    custom_id=f"form_{field_id}_yes",
                )

                no_button = StatefulButton(
                    label=f"{field_label}: No",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"form_{field_id}_no",
                )

                def make_bool_callback(fid, value):
                    async def callback(interaction):
                        old_value = self.values.get(fid)
                        self.values[fid] = value
                        if old_value != value:
                            self._clear_errors()
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, value
                            )
                        await self._update_form_display()

                    return callback

                yes_button.callback = make_bool_callback(field_id, True)
                no_button.callback = make_bool_callback(field_id, False)

                self.add_item(ActionRow(yes_button, no_button))

        # Grouped modal-edit button -- one modal covers every text /
        # integer / float / date field on the form.
        modal_fields = _collect_modal_fields(self.fields)
        if modal_fields:
            text_button = StatefulButton(
                label=_resolve_modal_edit_label(self.text_edit_button_label, modal_fields),
                emoji=self.text_edit_button_emoji,
                style=self.text_edit_button_style,
                custom_id="form_edit_text",
            )
            text_button.callback = self._open_text_modal
            self.add_item(ActionRow(text_button))

        # Submit button
        submit_button = StatefulButton(
            label="Submit",
            style=discord.ButtonStyle.primary,
            custom_id="form_submit",
        )

        async def submit_callback(interaction):
            # Parse errors from the modal leave raw strings in self.values;
            # submitting would fire validators against unparsed strings.
            # Keep the user on the existing error view until they reopen
            # the modal and fix the input.
            if self._field_errors or self._form_error:
                await self._update_form_display()
                return

            valid, errors = await self._validate_form()

            if valid:
                await self.on_submit(interaction, self.values)
                # ``on_submit`` overrides that bypass ``interaction.response``
                # leave the interaction unacked; the post-callback defer in
                # ``_scheduled_task`` handles that case. Skip cleanup if
                # on_submit already called exit/push/replace.
                if not self.is_finished():
                    await self.exit()
            else:
                self._set_validation_errors(errors)
                await self._update_form_display()

        submit_button.callback = submit_callback
        self.add_item(ActionRow(submit_button))

    def _rebuild_display(self):
        """Rebuild the full view tree from current form state.

        V2 ``LayoutView`` merges display and controls into a single flat
        component tree, so any display change forces a full rebuild.
        Construction is cheap and callbacks close over ``self.values``,
        so rebuilding controls each update is the canonical immediate-mode
        pattern. Interaction routing is stable across rebuilds via
        deterministic ``custom_id`` values, and select selections are
        visually preserved via ``SelectOption(default=...)`` inside
        :meth:`_create_form_controls`.

        The entire rebuild runs inside the view's theme context so
        ``alert()`` and ``card()`` inherit the view's accent colour
        without explicit ``color=`` arguments.
        """
        from ...theming.context import _current_theme, set_current_theme

        token = set_current_theme(self.get_theme())
        try:
            self.clear_items()
            title_line = f"**{self.title}**"

            if self._has_field_groups():
                self.add_item(card(title_line))
                if self._form_error is not None:
                    self.add_item(alert(self._form_error, level="error"))
                for group_name, group_fields in self._iter_field_groups():
                    section_lines: List[str] = []
                    if group_name:
                        section_lines.append(f"**{group_name}**")
                    for field in group_fields:
                        section_lines.extend(self._format_field_lines(field))
                    self.add_item(card("\n".join(section_lines)))
            else:
                if self._form_error is not None:
                    self.add_item(alert(self._form_error, level="error"))
                lines = [title_line, ""]
                for field in self.fields:
                    lines.extend(self._format_field_lines(field))
                self.add_item(card("\n".join(lines)))

            self._create_form_controls()
        finally:
            _current_theme.reset(token)

    async def _update_form_display(self):
        """Update the form display with current values."""
        self._rebuild_display()
        await self.refresh()
