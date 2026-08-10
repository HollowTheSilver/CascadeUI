# // ========================================( Modules )======================================== // #


import logging
import math
from typing import Any, ClassVar, Dict, List, Optional

import discord
from discord import Interaction
from discord.ui import ActionRow

from ...components.base import StatefulButton, StatefulSelect
from ...components.inputs import Modal as CascadeModal
from ...components.inputs import TextInput as CascadeTextInput
from ...components.patterns.v2 import alert, card
from ...components.types import EmojiInput
from ...utils.hooks import await_maybe
from ..base import RenderOutcome, _StatefulMixin
from ..layout import StatefulLayoutView
from ..view import StatefulView
from .types import FormSchema, _normalize_fields

logger = logging.getLogger(__name__)

# Discord hard limit on TextInput items per Modal. Forms with more than this
# many modal-rendered fields (``"text"``, ``"integer"``, ``"float"``,
# ``"date"``) raise ``ValueError`` at construction so the failure surfaces
# at definition time rather than on first click.
MAX_TEXT_FIELDS = 5

# Discord's modal-title cap. The grouped edit label doubles as the modal
# title, so the tighter of its two caps (title 45, button label 80) is the
# one that governs it.
_MODAL_TITLE_MAX = 45

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


def _validate_modal_field_labels(cls_name: str, fields: List[Dict[str, Any]]) -> None:
    """Raise ``ValueError`` if two modal fields derive the same input id.

    A modal input's ``custom_id`` comes from its label, so two modal fields
    whose labels slugify to the same value collide when the modal is built.
    Catching it at construction names both fields, rather than raising on the
    Edit button's first click.
    """
    seen: Dict[str, Any] = {}
    for field in _collect_modal_fields(fields):
        label = field.get("label", field.get("id"))
        slug = CascadeTextInput._slug(str(label))
        if slug in seen:
            raise ValueError(
                f"{cls_name} has two modal fields whose labels collide: "
                f"{seen[slug]!r} and {field.get('id')!r} both derive input id "
                f"{slug!r}. Give them distinct labels."
            )
        seen[slug] = field.get("id")


def _resolve_modal_edit_label(override: Optional[str], modal_fields: List[Dict[str, Any]]) -> str:
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
        singular = f"Edit {only.get('label', only.get('id'))}"
        # Doubles as the modal title, which Discord caps at 45. A long
        # field label would otherwise compose a title the caller never
        # typed and cannot see, so fall back to the generic form rather
        # than failing the modal open.
        if len(singular) <= _MODAL_TITLE_MAX:
            return singular
        return "Edit Fields"
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
        # float() accepts "nan"/"inf", which slip past min/max (nan compares
        # False to everything) and serialize to invalid JSON.
        if not math.isfinite(parsed):
            return None, f"Must be a finite number, got {raw!r}."
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

    Validators declared on individual fields run in this callback against
    the fields that parsed, and their errors merge with the parse errors, so
    one submit surfaces every mistake. A field that failed to parse shows its
    parse error and is skipped by its validators here.
    """
    modal_fields = _collect_modal_fields(form.fields)
    field_by_id: Dict[Any, Dict[str, Any]] = {f.get("id"): f for f in modal_fields}
    inputs: List[CascadeTextInput] = []
    input_to_field_id: Dict[CascadeTextInput, Any] = {}
    field_validators: Dict[Any, List] = {}

    for field in modal_fields:
        field_id = field.get("id")
        field_label = field.get("label", field_id)
        # A pending raw draft (last input failed to parse) wins, so the user
        # sees what they typed; otherwise the parsed value or the default.
        current_value = form._raw_drafts.get(
            field_id, form.values.get(field_id, field.get("default"))
        )
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
                # Hold the raw text in _raw_drafts, not values: the next modal
                # open prefills what the user typed, but values keeps the last
                # valid value rather than a string in a typed field.
                form._raw_drafts[field_id] = raw
                parse_errors[field_id] = [parse_error]
                continue
            form._raw_drafts.pop(field_id, None)
            form.values[field_id] = parsed
            if old_value != parsed:
                changes.append((field_id, old_value, parsed))

        # This modal edit recomputes every modal field's error below, so clear
        # the modal fields' prior errors and the form-level summary while
        # leaving a non-modal select or boolean error in place: the modal did
        # not touch those fields, so their messages are still accurate.
        for fid in field_by_id:
            form._field_errors.pop(fid, None)
        form._form_error = None

        for field_id, old_value, new_value in changes:
            await form._call_hook_safe(form.on_field_changed, field_id, old_value, new_value)

        # Parse errors and validator errors surface together, so one submit
        # reveals every mistake. Validators run only against fields that
        # parsed: a field that failed to parse holds its raw string, and its
        # parse error is the message it shows, so a typed validator has
        # nothing to check there.
        field_errors: Dict[Any, List[str]] = dict(parse_errors)

        to_validate = {
            fid: validators
            for fid, validators in field_validators.items()
            if fid not in parse_errors
        }
        if to_validate:
            from ...validation import validate_fields

            field_defs = [
                {
                    "id": fid,
                    "validators": fv,
                    "required": field_by_id[fid].get("required", False),
                }
                for fid, fv in to_validate.items()
            ]
            errors = await validate_fields(form.values, field_defs)
            for fid, errs in errors.items():
                field_errors.setdefault(fid, []).extend(e.message for e in errs)

        if field_errors:
            # Merge, not replace: a non-modal field's error cleared above stays
            # gone, but any that was left standing is preserved.
            form._field_errors.update(field_errors)
            await form._update_form_display()
            return

        await form._update_form_display()

    modal = CascadeModal(
        title=title,
        inputs=inputs,
        callback=on_modal_submit,
        view_id=form.id,
    )
    # Carry the form's configured backstop onto the modal instance; the value is
    # validated on the form subclass via _POSITIVE_NUMBER_ATTRS before it lands here.
    modal.auto_defer_delay = form.text_edit_modal_auto_defer_delay
    return modal


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
    text_edit_button_emoji: ClassVar[EmojiInput] = "\u270f\ufe0f"
    text_edit_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary
    # The pattern builds its text-edit modal internally, so this is the only
    # surface a subclass has to raise that modal's ack backstop for a slow
    # async field validator.
    text_edit_modal_auto_defer_delay: ClassVar[float] = 2.5
    _BUTTON_STYLE_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BUTTON_STYLE_ATTRS,
        "text_edit_button_style",
    )
    _STR_OR_NONE_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._STR_OR_NONE_ATTRS,
        "text_edit_button_label",
    )
    _EMOJI_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._EMOJI_ATTRS,
        "text_edit_button_emoji",
    )
    _POSITIVE_NUMBER_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._POSITIVE_NUMBER_ATTRS,
        "text_edit_modal_auto_defer_delay",
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
        # Seed declared defaults so a field with a ``default`` starts filled:
        # the display shows it, the required-check counts it, and select
        # defaults render. Without this, ``default`` reached only the modal
        # prefill and an otherwise-satisfied required field still blocked.
        self.values = {f["id"]: f["default"] for f in self.fields if f.get("default") is not None}

        # Raw text a modal field failed to parse, held apart from ``values``
        # so ``values`` only ever carries parsed values. The next modal open
        # prefills from here; ``values`` keeps the field's last valid value.
        self._raw_drafts: Dict[str, str] = {}

        # Inline validation error state. ``_field_errors`` maps field id
        # to list[str]; ``_form_error`` holds a form-level message used
        # for "complete all required fields" style errors. Both are
        # cleared on any field change and repopulated on submit failure.
        self._field_errors: Dict[str, List[str]] = {}
        self._form_error: Optional[str] = None

        _validate_modal_field_count(type(self).__name__, self.fields)
        _validate_modal_field_labels(type(self).__name__, self.fields)

        self._build_form()

    # // ----( Navigation state )---- // #

    def get_nav_state(self) -> dict:
        """Carry entered-but-unsubmitted values across a ``pop``.

        ``values`` is assigned in ``__init__`` and is not a constructor
        kwarg, so a reconstruction hands back an empty form: fill three
        fields, open a picker or a help screen, press Back, and the typing
        is gone with nothing to say it ever happened.

        The values and any pending raw drafts (input the user typed that
        has not parsed yet) both travel, so a field carried across a pop
        renders the same either way. Validation errors do not: they are
        the output of a submit that has not run against this data yet, and
        re-showing them beside values the user may have come back to fix
        would be stale. The next submit recomputes them.
        """
        return {"values": dict(self.values), "raw_drafts": dict(self._raw_drafts)}

    def _sync_select_defaults(self) -> None:
        """Point every select's marked option at the value the form holds.

        V1 ships an embed edit rather than rebuilding its controls, and
        Discord re-renders a select from whatever the edit payload carries.
        The options were marked once at construction, so without this the
        control reverts to its seeded selection on the very edit that
        confirms the new one in the embed: the user picks B, the summary
        reads B, and the dropdown beside it still shows A. A multi-select
        cleared its ticks entirely.

        Keyed off the ``form_{field_id}`` custom_id the controls are built
        with, so nothing extra is stored. V2 needs no equivalent because
        ``_rebuild_display`` reconstructs the controls from ``values`` on
        every update.
        """
        for item in self.children:
            if not isinstance(item, StatefulSelect):
                continue
            custom_id = getattr(item, "custom_id", "") or ""
            if not custom_id.startswith("form_"):
                continue
            item.set_selected(self.values.get(custom_id[len("form_") :]))

    def restore_nav_state(self, state: dict) -> None:
        """Restore entered values and pending drafts for fields the form
        still declares.

        Fields are filtered against the current declaration because a
        reconstruction can build a different set than it had at push time
        (a schema change, a conditional field). A value with no field to
        render it would sit in ``values`` and reach the submit payload
        without ever being shown.
        """
        known = {field.get("id") for field in self.fields}
        values = state.get("values")
        if values:
            self.values.update({k: v for k, v in values.items() if k in known})
        drafts = state.get("raw_drafts")
        if drafts:
            self._raw_drafts.update({k: v for k, v in drafts.items() if k in known})
        # The controls were built before the values arrived, so a form popped
        # back to renders its restored entries in the body over selects still
        # marked with whatever they were seeded with.
        self._sync_select_defaults()

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
        # A secret field masks its value in the display (password, token).
        # Fixed-width dots so the length is not revealed either; a blank
        # field still shows "Not set" via the paths below.
        if field.get("secret") and value is not None:
            return "\N{BULLET}" * 8
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
        # A parse-failed field holds what the user typed in _raw_drafts rather
        # than self.values, so read the draft first: a typed field with an
        # unparseable value shows the entered text like a text field does,
        # instead of reading as "Not set".
        raw = self._raw_drafts.get(fid)
        value = raw if raw is not None else self.values.get(fid)
        fvalue = self._format_field_value(field, value)
        required = _REQUIRED_MARKER if field.get("required", False) else ""
        lines = [f"{flabel}{required}: {fvalue}"]
        if fid in self._field_errors:
            errs = ", ".join(self._field_errors[fid])
            lines.append(f"\u26a0\ufe0f {errs}")
        return lines

    def _clear_errors(self) -> None:
        self._field_errors = {}
        self._form_error = None

    def _clear_field_error(self, fid) -> None:
        """Clear only the error state a change to ``fid`` invalidates.

        Another field's validator result still holds because its value did
        not change, so its message stays on screen instead of vanishing
        when the user edits an unrelated field. The form-level required
        summary can go stale when a change fills a missing field, so it
        clears and recomputes on the next submit.
        """
        self._field_errors.pop(fid, None)
        self._form_error = None

    async def set_form_error(self, message: Optional[str]) -> None:
        """Set a form-level (cross-field) error and re-render so it shows.

        Use inside an ``on_submit`` override for a check that spans fields
        and cannot be expressed as a per-field validator: set the message
        and return, and the form stays open with the error at the top. Pass
        ``None`` to clear. Re-rendering runs through ``_update_form_display``,
        the only path the error state reaches the display through.
        """
        self._form_error = message
        await self._update_form_display()

    async def set_field_error(self, field_id: str, *messages: str) -> None:
        """Set inline error(s) under one field and re-render the form.

        Call with no ``messages`` to clear that field's errors.
        """
        if messages:
            self._field_errors[field_id] = list(messages)
        else:
            self._field_errors.pop(field_id, None)
        await self._update_form_display()

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

    async def _open_text_modal(self, interaction: Interaction) -> None:
        """Open the grouped modal for every modal-rendered field on the form."""
        modal_fields = _collect_modal_fields(self.fields)
        title = _resolve_modal_edit_label(self.text_edit_button_label, modal_fields)
        modal = _build_form_modal(self, title)
        await self.open_modal(interaction, modal)

    def _is_field_empty(self, field: Dict[str, Any]) -> bool:
        """Return True when ``self.values[field_id]`` is missing or empty.

        Empty means ``None``, absent key, or (for ``multi_select``) an
        empty sequence. A value that failed to parse is held in
        ``_raw_drafts``, not ``self.values``, so it reads as empty here and
        a required field with unparsed input surfaces as required.
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
        """Validate the form, surfacing required and validator errors together.

        Returns ``(valid, field_errors, form_error)``. ``field_errors`` maps
        a field id to its validator messages; ``form_error`` is the
        form-level "complete all required fields" summary, or ``None``. A
        blank required field and a bad value elsewhere both appear at once,
        so fixing one does not hide the other until the next submit.

        A field the user entered that failed to parse holds its raw string
        in ``_raw_drafts``. Such a field is invalid, not missing, so its
        specific parse error (a typed field's ``min_value`` / ``max_value``
        range, a bad integer) is re-derived here rather than degrading to
        the generic required-field summary. This keeps the Submit path in
        step with the modal path, which reports the same specific message.
        """
        # Re-derive the specific parse error for every drafted field so the
        # Submit path matches what the modal showed for the same bad input.
        draft_errors: Dict[str, List[str]] = {}
        for field in self.fields:
            fid = field.get("id")
            raw = self._raw_drafts.get(fid)
            if raw is not None:
                _, err = _parse_field_value(field, raw)
                if err:
                    draft_errors.setdefault(fid, []).append(err)

        missing_ids = {
            field.get("id")
            for field in self.fields
            if field.get("required", False)
            and self._is_field_empty(field)
            and field.get("id") not in draft_errors
        }
        missing_labels = [
            field.get("label", field.get("id"))
            for field in self.fields
            if field.get("id") in missing_ids
        ]

        field_errors: Dict[str, List[str]] = {fid: list(errs) for fid, errs in draft_errors.items()}

        # Validators run only on fields that parsed and are not already
        # flagged missing: a missing field's error is the required-field
        # summary alone, and a drafted field's is its parse message.
        to_check = [
            field
            for field in self.fields
            if field.get("id") not in missing_ids
            and field.get("id") not in self._raw_drafts
            and field.get("validators")
        ]
        if to_check:
            from ...validation import validate_fields

            errors = await validate_fields(self.values, to_check)
            for fid, errs in errors.items():
                field_errors.setdefault(fid, []).extend(e.message for e in errs)

        form_error = None
        if missing_labels:
            form_error = f"Please complete all required fields: {', '.join(missing_labels)}"

        valid = not missing_labels and not field_errors
        return valid, field_errors, form_error

    async def on_state_changed(self, state):
        """Update form display when state changes."""
        await self._update_form_display()


# // ========================================( V1: FormView )======================================== // #


class FormView(_BaseFormMixin, StatefulView):
    """A view for collecting form data from users.

    Supports field types: ``"text"``, ``"integer"``, ``"float"``, ``"date"``, ``"boolean"``, ``"select"``, and ``"multi_select"``.

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
                # Discord allows five action rows (0-4), and each select or
                # boolean field takes one. Past the budget the field used to
                # vanish with no error, leaving a required field unfillable;
                # name the overflow instead.
                raise ValueError(
                    f"{type(self).__name__} field {field.get('id')!r} does not "
                    f"fit: a V1 form has five component rows, and its select and "
                    f"boolean fields have filled them. Use fewer non-text fields, "
                    f"or a V2 FormLayoutView, which holds more."
                )

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

                # Capture field_id per-iteration; the chosen values arrive as the
                # callback's second parameter.
                def make_select_callback(fid):
                    async def callback(interaction, values):
                        old_value = self.values.get(fid)
                        # An optional select (min_values=0) delivers an empty
                        # list when the user clears it; None is the "not set"
                        # value the display and required-check already expect.
                        new_value = values[0] if values else None
                        self.values[fid] = new_value
                        if old_value != new_value:
                            self._clear_field_error(fid)
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, new_value
                            )
                        await self._update_form_display()

                    return callback

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=1,
                    custom_id=f"form_{field_id}",
                    row=current_row,
                    callback=make_select_callback(field_id),
                )
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

                def make_multi_select_callback(fid):
                    async def callback(interaction, values):
                        old_value = self.values.get(fid)
                        new_value = list(values)
                        self.values[fid] = new_value
                        if list(old_value or []) != new_value:
                            self._clear_field_error(fid)
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, new_value
                            )
                        await self._update_form_display()

                    return callback

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=max_values,
                    custom_id=f"form_{field_id}",
                    row=current_row,
                    callback=make_multi_select_callback(field_id),
                )
                self.add_item(select)
                current_row += 1

            elif field_type == "boolean":
                # Capture field_id per-iteration
                def make_bool_callback(fid, value):
                    async def callback(interaction):
                        old_value = self.values.get(fid)
                        self.values[fid] = value
                        if old_value != value:
                            self._clear_field_error(fid)
                            await self._call_hook_safe(self.on_field_changed, fid, old_value, value)
                        await self._update_form_display()

                    return callback

                yes_button = StatefulButton(
                    label=f"{field_label}: Yes",
                    style=discord.ButtonStyle.success,
                    custom_id=f"form_{field_id}_yes",
                    row=current_row,
                    callback=make_bool_callback(field_id, True),
                )

                no_button = StatefulButton(
                    label=f"{field_label}: No",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"form_{field_id}_no",
                    row=current_row,
                    callback=make_bool_callback(field_id, False),
                )

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
                callback=self._open_text_modal,
            )
            self.add_item(text_button)
            current_row = text_button_row + 1

        # Add submit button on the next available row (or last row if full)
        submit_row = min(current_row, 4)

        async def submit_callback(interaction):
            # Always re-validate on submit: _validate_form derives every
            # field's current error (drafts included), so a submit surfaces
            # all outstanding problems at once rather than re-showing a stale
            # subset. Unparsed input lives in _raw_drafts, never self.values,
            # so validators never run against a raw string.
            valid, field_errors, form_error = await self._validate_form()

            if valid:
                # Validation passed, so clear any stale error state: an error
                # present after on_submit is one on_submit set itself.
                self._field_errors = {}
                self._form_error = None
                await await_maybe(self.on_submit(interaction, self.values))
                # on_submit may reject a cross-field rule via set_form_error /
                # set_field_error; if it did, keep the form open (the setter
                # already re-rendered) rather than exiting. Otherwise an
                # override that bypasses interaction.response leaves the ack to
                # the post-callback defer; skip cleanup if it already
                # exited/pushed/replaced.
                if self._form_error or self._field_errors:
                    return
                if not self.is_finished():
                    await self.exit()
            else:
                self._field_errors = field_errors
                self._form_error = form_error
                await self._update_form_display()

        submit_button = StatefulButton(
            label="Submit",
            style=discord.ButtonStyle.primary,
            custom_id="form_submit",
            row=submit_row,
            callback=submit_callback,
        )
        self.add_item(submit_button)

    def _build_form_embed(self) -> discord.Embed:
        """Render the current values as the form's embed.

        Fields group by consecutive ``"group"`` runs when any field declares
        a group, falling back to a flat field list when none do. Inline field
        errors surface as an italic warning line under the offending field
        value; the form-level error becomes the embed's red-tinted
        description.

        Kept separate from shipping it: a navigation edit needs the embed
        without the refresh, and a field change needs both.
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

        return embed

    async def _reload_render(self) -> Optional[RenderOutcome]:
        return await self._update_form_display()

    async def _update_form_display(self) -> Optional[RenderOutcome]:
        """Rebuild the form embed and ship it."""
        self._sync_select_defaults()
        return await self.refresh(**await self._nav_edit_kwargs())

    async def send(
        self,
        content: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
        **kwargs,
    ):
        """Send the view, using the form's own embed when none is given.

        V1 form content lives in the embed, so the first message would
        otherwise ship the field controls over an empty body. This is the
        same render ``_nav_edit_kwargs`` supplies on a ``pop``. An explicit
        ``embed`` or ``content`` wins.
        """
        if embed is None and content is None:
            embed = (await self._nav_edit_kwargs()).get("embed")
        return await super().send(
            content=content,
            embed=embed,
            **kwargs,
        )

    nav_rebuild = staticmethod(lambda v: v._nav_edit_kwargs())

    async def _nav_edit_kwargs(self) -> dict:
        """The embed a navigation edit needs to show the current values.

        ``pop()`` passes no rebuild of its own, so without this the edit
        would restore the controls and leave whatever the child view put on
        the message. V1 form content lives in the embed, so the embed is the
        render.
        """
        return {"embed": self._build_form_embed()}


# // ========================================( V2: FormLayoutView )======================================== // #


class FormLayoutView(_BaseFormMixin, StatefulLayoutView):
    """A V2 layout view for collecting form data from users.

    The V2 equivalent of ``FormView``. Uses ``TextDisplay`` inside a
    ``Container`` instead of embeds for field display.

    Supports field types: ``"text"``, ``"integer"``, ``"float"``, ``"date"``, ``"boolean"``, ``"select"``, and ``"multi_select"``. Text
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

                # Capture field_id per-iteration; the chosen values arrive as the
                # callback's second parameter.
                def make_select_callback(fid):
                    async def callback(interaction, values):
                        old_value = self.values.get(fid)
                        # An optional select (min_values=0) delivers an empty
                        # list when the user clears it; None is the "not set"
                        # value the display and required-check already expect.
                        new_value = values[0] if values else None
                        self.values[fid] = new_value
                        if old_value != new_value:
                            self._clear_field_error(fid)
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, new_value
                            )
                        await self._update_form_display()

                    return callback

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=1,
                    custom_id=f"form_{field_id}",
                    callback=make_select_callback(field_id),
                )
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

                def make_multi_select_callback(fid):
                    async def callback(interaction, values):
                        old_value = self.values.get(fid)
                        new_value = list(values)
                        self.values[fid] = new_value
                        if list(old_value or []) != new_value:
                            self._clear_field_error(fid)
                            await self._call_hook_safe(
                                self.on_field_changed, fid, old_value, new_value
                            )
                        await self._update_form_display()

                    return callback

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=max_values,
                    custom_id=f"form_{field_id}",
                    callback=make_multi_select_callback(field_id),
                )
                self.add_item(ActionRow(select))

            elif field_type == "boolean":
                # Capture field_id per-iteration
                def make_bool_callback(fid, value):
                    async def callback(interaction):
                        old_value = self.values.get(fid)
                        self.values[fid] = value
                        if old_value != value:
                            self._clear_field_error(fid)
                            await self._call_hook_safe(self.on_field_changed, fid, old_value, value)
                        await self._update_form_display()

                    return callback

                yes_button = StatefulButton(
                    label=f"{field_label}: Yes",
                    style=discord.ButtonStyle.success,
                    custom_id=f"form_{field_id}_yes",
                    callback=make_bool_callback(field_id, True),
                )

                no_button = StatefulButton(
                    label=f"{field_label}: No",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"form_{field_id}_no",
                    callback=make_bool_callback(field_id, False),
                )

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
                callback=self._open_text_modal,
            )
            self.add_item(ActionRow(text_button))

        # Submit button
        async def submit_callback(interaction):
            # Always re-validate on submit: _validate_form derives every
            # field's current error (drafts included), so a submit surfaces
            # all outstanding problems at once rather than re-showing a stale
            # subset. Unparsed input lives in _raw_drafts, never self.values,
            # so validators never run against a raw string.
            valid, field_errors, form_error = await self._validate_form()

            if valid:
                # Validation passed, so clear any stale error state: an error
                # present after on_submit is one on_submit set itself.
                self._field_errors = {}
                self._form_error = None
                await await_maybe(self.on_submit(interaction, self.values))
                # on_submit may reject a cross-field rule via set_form_error /
                # set_field_error; if it did, keep the form open (the setter
                # already re-rendered) rather than exiting. Otherwise an
                # override that bypasses interaction.response leaves the ack to
                # the post-callback defer; skip cleanup if it already
                # exited/pushed/replaced.
                if self._form_error or self._field_errors:
                    return
                if not self.is_finished():
                    await self.exit()
            else:
                self._field_errors = field_errors
                self._form_error = form_error
                await self._update_form_display()

        submit_button = StatefulButton(
            label="Submit",
            style=discord.ButtonStyle.primary,
            custom_id="form_submit",
            callback=submit_callback,
        )
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
        from ...theming.context import theme_context

        with theme_context(self.get_theme()):
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
            # Restore the navigation back button if push() added one.
            self._restore_navigation_artifacts()

    def restore_nav_state(self, state: dict) -> None:
        """Restore entered values and rebuild the tree to show them.

        The base restores the values; a V2 view is its components, so the
        tree has to be recomposed or the message would render the empty
        fields ``__init__`` built while the restored values sat unseen in
        ``values`` and reached the next submit anyway.
        """
        super().restore_nav_state(state)
        self._rebuild_display()

    async def _update_form_display(self):
        """Update the form display with current values."""
        self._rebuild_display()
        await self.refresh()
