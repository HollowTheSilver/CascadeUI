# // ========================================( Modules )======================================== // #


import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Union

import discord
from discord import CheckboxGroupOption, Interaction, RadioGroupOption, TextStyle

from ..utils.responses import ack_backstop, respond_safe, trailing_ack
from ..utils.strings import slugify
from ..validation import validate_fields
from .base import StatefulComponent

logger = logging.getLogger(__name__)


# // ========================================( Helpers )======================================== // #


def _validate_range(owner: str, name: str, value: Optional[int], lo: int, hi: int) -> None:
    """Reject an out-of-range numeric bound at construction time.

    Discord accepts these bounds only within ``[lo, hi]`` and 400s the modal
    open otherwise; raising here surfaces the mistake where the value is set.
    ``None`` (Discord's server default) is always allowed.
    """
    if value is not None and not lo <= value <= hi:
        raise ValueError(f"{owner} {name}={value} is out of range; Discord accepts {lo}-{hi}.")


# Discord caps a modal title and each input label at 45 characters, and
# rejects an empty one. discord.py stores both unchecked, so a label built
# from a schema field or a record name fails only when the modal opens.
_MODAL_TEXT_MAX = 45


def _validate_text(owner: str, name: str, value: str) -> None:
    """Reject a modal title or label Discord will not accept."""
    if not value:
        raise ValueError(f"{owner} {name} must not be empty; Discord rejects a blank {name}.")
    if len(value) > _MODAL_TEXT_MAX:
        raise ValueError(
            f"{owner} {name} is {len(value)} characters, over Discord's "
            f"{_MODAL_TEXT_MAX}-character cap. Shorten it, or move the detail "
            f"into the placeholder or description."
        )


# // ========================================( Classes )======================================== // #


class TextInput(StatefulComponent):
    """A modal text input with state management and optional validators.

    Renders as a ``discord.ui.Label`` wrapping a ``discord.ui.TextInput``
    inside a :class:`Modal`. The label string moves to ``ui.Label.text``;
    the optional ``description`` populates ``ui.Label.description`` for
    the secondary helper line beneath the title.

    Parameters
    ----------
    label:
        Field label shown above the input (rendered as ``ui.Label.text``).
    description:
        Optional secondary helper text beneath the label. Renders as
        ``ui.Label.description``.
    placeholder, default, required, min_length, max_length, style:
        Standard ``discord.ui.TextInput`` passthroughs.
    validators:
        Optional list of validator callables attached to this field. Each
        callable receives ``(value, field_def, all_values)`` and returns a
        :class:`~cascadeui.ValidationResult` (or an awaitable that resolves
        to one). :class:`Modal` auto-collects these at construction time --
        a ``Modal`` built from ``TextInput`` instances needs no further
        wiring. Reused by :class:`~cascadeui.FormView` and
        :class:`~cascadeui.FormLayoutView` when rendering ``"text"`` fields
        through the grouped text-edit modal.
    """

    def __init__(
        self,
        label: str,
        placeholder: Optional[str] = None,
        default: Optional[str] = None,
        required: bool = True,
        min_length: Optional[int] = None,
        max_length: Optional[int] = None,
        style: TextStyle = TextStyle.short,
        validators: Optional[List[Callable]] = None,
        description: Optional[str] = None,
    ):
        self.label = label
        self.description = description
        self.placeholder = placeholder
        self.default = default
        self.required = required
        self.min_length = min_length
        self.max_length = max_length
        _validate_text(f"TextInput {label!r}", "label", label)
        _validate_range(f"TextInput {label!r}", "min_length", min_length, 0, 4000)
        _validate_range(f"TextInput {label!r}", "max_length", max_length, 1, 4000)
        self.style = style
        self.validators: List[Callable] = list(validators) if validators else []
        self.custom_id = self._slug(label)
        # Populated by Modal.on_submit after validation passes. ``None`` until
        # the user submits a modal containing this input; stable across
        # re-submissions so callers can read ``text_input.value`` directly
        # instead of round-tripping through a slug-keyed dict.
        self.value: Optional[str] = None

    @classmethod
    def _slug(cls, label: str) -> str:
        """Derive a stable ``custom_id`` slug from a field label.

        Single source of truth for the modal-input slug rule. Prefixes
        ``input_`` and delegates to :func:`slugify`, so modal inputs share
        the one safe lowercase-alphanumeric derivation the rest of the
        library uses.
        """
        return f"input_{slugify(label)}"

    def create_discord_component(self):
        """Build a ``ui.Label`` wrapping the inner ``ui.TextInput``."""
        inner = discord.ui.TextInput(
            placeholder=self.placeholder,
            default=self.default,
            required=self.required,
            min_length=self.min_length,
            max_length=self.max_length,
            style=self.style,
            custom_id=self.custom_id,
        )
        return discord.ui.Label(text=self.label, component=inner, description=self.description)


class Checkbox(StatefulComponent):
    """A modal checkbox with state management and optional validators.

    Renders as a ``discord.ui.Label`` wrapping a single
    ``discord.ui.Checkbox`` inside a :class:`Modal`. The submitted
    ``.value`` is ``True`` or ``False``.

    Parameters
    ----------
    label:
        Field label rendered as ``ui.Label.text`` and used to derive the
        ``custom_id`` slug.
    description:
        Optional secondary helper text beneath the label.
    default:
        Whether the checkbox is pre-selected. Defaults to ``False``.
    validators:
        Optional list of validator callables. Each receives
        ``(value, field_def, all_values)`` and returns a
        :class:`~cascadeui.ValidationResult`.
    """

    def __init__(
        self,
        label: str,
        default: bool = False,
        validators: Optional[List[Callable]] = None,
        description: Optional[str] = None,
    ):
        self.label = label
        _validate_text(f"Checkbox {label!r}", "label", label)
        self.description = description
        self.default = default
        self.validators: List[Callable] = list(validators) if validators else []
        self.custom_id = TextInput._slug(label)
        self.value: Optional[bool] = None

    def create_discord_component(self):
        """Build a ``ui.Label`` wrapping the inner ``ui.Checkbox``."""
        inner = discord.ui.Checkbox(
            default=self.default,
            custom_id=self.custom_id,
        )
        return discord.ui.Label(text=self.label, component=inner, description=self.description)


class CheckboxGroup(StatefulComponent):
    """A modal checkbox group with state management and optional validators.

    Renders as a ``discord.ui.Label`` wrapping a
    ``discord.ui.CheckboxGroup`` inside a :class:`Modal`. The submitted
    ``.values`` is a list of selected option value strings.

    Options can be passed as :class:`discord.CheckboxGroupOption` instances
    or as plain dicts with ``label``, ``value``, ``description``, and
    ``default`` keys (matching :class:`~cascadeui.Dropdown`'s dict
    shorthand).

    Parameters
    ----------
    label:
        Field label rendered as ``ui.Label.text`` and used to derive the
        ``custom_id`` slug.
    description:
        Optional secondary helper text beneath the label.
    options:
        List of :class:`discord.CheckboxGroupOption` or dicts.
    required:
        Whether at least one option must be selected. Defaults to ``True``.
    min_values:
        Minimum selections required (0-10).
    max_values:
        Maximum selections allowed (1-10).
    validators:
        Optional list of validator callables.
    """

    def __init__(
        self,
        label: str,
        options: List[Union[CheckboxGroupOption, Dict[str, Any]]],
        required: bool = True,
        min_values: Optional[int] = None,
        max_values: Optional[int] = None,
        validators: Optional[List[Callable]] = None,
        description: Optional[str] = None,
    ):
        self.label = label
        _validate_text(f"CheckboxGroup {label!r}", "label", label)
        self.description = description
        self.options = self._process_options(options, CheckboxGroupOption)
        # Discord rejects an out-of-range option count with HTTP 400 when
        # the modal opens; raising here surfaces the mistake at its source.
        if not 1 <= len(self.options) <= 10:
            raise ValueError(
                f"CheckboxGroup {label!r} has {len(self.options)} options; Discord "
                f"accepts 1-10. Trim the list or split the choices across inputs."
            )
        self.required = required
        self.min_values = min_values
        self.max_values = max_values
        _validate_range(f"CheckboxGroup {label!r}", "min_values", min_values, 0, 10)
        _validate_range(f"CheckboxGroup {label!r}", "max_values", max_values, 1, 10)
        self.validators: List[Callable] = list(validators) if validators else []
        self.custom_id = TextInput._slug(label)
        self.values: Optional[List[str]] = None

    @staticmethod
    def _process_options(raw, option_cls):
        """Convert dicts to option instances, pass through existing ones."""
        processed = []
        for opt in raw:
            if isinstance(opt, dict):
                processed.append(
                    option_cls(
                        label=opt.get("label", "Option"),
                        value=opt.get("value", opt.get("label", "Option")),
                        description=opt.get("description"),
                        default=opt.get("default", False),
                    )
                )
            else:
                processed.append(opt)
        return processed

    def create_discord_component(self):
        """Build a ``ui.Label`` wrapping the inner ``ui.CheckboxGroup``."""
        inner = discord.ui.CheckboxGroup(
            options=self.options,
            required=self.required,
            min_values=self.min_values,
            max_values=self.max_values,
            custom_id=self.custom_id,
        )
        return discord.ui.Label(text=self.label, component=inner, description=self.description)


class RadioGroup(StatefulComponent):
    """A modal radio group with state management and optional validators.

    Renders as a ``discord.ui.Label`` wrapping a ``discord.ui.RadioGroup``
    inside a :class:`Modal`. The submitted ``.value`` is the selected
    option's value string, or ``None`` if nothing was selected.

    Options can be passed as :class:`discord.RadioGroupOption` instances
    or as plain dicts (same shorthand as :class:`CheckboxGroup`).

    Parameters
    ----------
    label:
        Field label rendered as ``ui.Label.text`` and used to derive the
        ``custom_id`` slug.
    description:
        Optional secondary helper text beneath the label.
    options:
        List of :class:`discord.RadioGroupOption` or dicts.
    required:
        Whether a selection is required. Defaults to ``True``.
    validators:
        Optional list of validator callables.
    """

    def __init__(
        self,
        label: str,
        options: List[Union[RadioGroupOption, Dict[str, Any]]],
        required: bool = True,
        validators: Optional[List[Callable]] = None,
        description: Optional[str] = None,
    ):
        self.label = label
        _validate_text(f"RadioGroup {label!r}", "label", label)
        self.description = description
        self.options = CheckboxGroup._process_options(options, RadioGroupOption)
        # Discord rejects an out-of-range option count with HTTP 400 when
        # the modal opens; raising here surfaces the mistake at its source.
        if not 2 <= len(self.options) <= 10:
            raise ValueError(
                f"RadioGroup {label!r} has {len(self.options)} options; Discord "
                f"requires 2-10. Use a Checkbox for a single yes/no choice, or "
                f"trim the list."
            )
        self.required = required
        self.validators: List[Callable] = list(validators) if validators else []
        self.custom_id = TextInput._slug(label)
        self.value: Optional[str] = None

    def create_discord_component(self):
        """Build a ``ui.Label`` wrapping the inner ``ui.RadioGroup``."""
        inner = discord.ui.RadioGroup(
            options=self.options,
            required=self.required,
            custom_id=self.custom_id,
        )
        return discord.ui.Label(text=self.label, component=inner, description=self.description)


class FileUpload(StatefulComponent):
    """A modal file upload with state management and optional validators.

    Renders as a ``discord.ui.Label`` wrapping a ``discord.ui.FileUpload``
    inside a :class:`Modal`. The submitted ``.values`` is a list of
    :class:`discord.Attachment` objects.

    .. warning::

        Attachment objects are ephemeral -- they contain CDN URLs that
        expire and cannot be serialized to JSON. Read attachment data
        in the modal callback; do not store attachments in the state
        store or expect them to persist.

    Parameters
    ----------
    label:
        Field label rendered as ``ui.Label.text`` and used to derive the
        ``custom_id`` slug.
    description:
        Optional secondary helper text beneath the label.
    required:
        Whether at least one file must be uploaded. Defaults to ``True``.
    min_values:
        Minimum uploads required (0-10).
    max_values:
        Maximum uploads allowed (1-10).
    validators:
        Optional list of validator callables.
    """

    def __init__(
        self,
        label: str,
        required: bool = True,
        min_values: Optional[int] = None,
        max_values: Optional[int] = None,
        validators: Optional[List[Callable]] = None,
        description: Optional[str] = None,
    ):
        self.label = label
        _validate_text(f"FileUpload {label!r}", "label", label)
        self.description = description
        self.required = required
        self.min_values = min_values
        self.max_values = max_values
        _validate_range(f"FileUpload {label!r}", "min_values", min_values, 0, 10)
        _validate_range(f"FileUpload {label!r}", "max_values", max_values, 1, 10)
        self.validators: List[Callable] = list(validators) if validators else []
        self.custom_id = TextInput._slug(label)
        self.values: Optional[List] = None

    def create_discord_component(self):
        """Build a ``ui.Label`` wrapping the inner ``ui.FileUpload``."""
        inner = discord.ui.FileUpload(
            required=self.required,
            min_values=self.min_values,
            max_values=self.max_values,
            custom_id=self.custom_id,
        )
        return discord.ui.Label(text=self.label, component=inner, description=self.description)


# // ========================================( Wrapper Base )======================================== // #

# All CascadeUI modal input wrappers (TextInput, Checkbox, CheckboxGroup,
# RadioGroup, FileUpload) share the same contract:
#   - ``.custom_id`` derived from label via ``TextInput._slug()``
#   - ``.validators`` list auto-collected by ``Modal.__init__``
#   - ``.create_discord_component()`` produces a ``ui.Label`` wrapping
#     the inner discord.py item
#   - ``.value`` or ``.values`` populated by ``Modal.on_submit`` write-back
#
# The tuple below is used by ``Modal.__init__`` to recognize any wrapped
# input type without hardcoding isinstance checks for each class.
_WRAPPED_INPUT_TYPES = (TextInput, Checkbox, CheckboxGroup, RadioGroup, FileUpload)


def _unwrap_label(item):
    """Return the inner component of a ``ui.Label`` or pass through.

    Modal child traversal needs to look inside ``ui.Label`` wrappers to
    reach the actual input component carrying the submitted value. Raw
    discord.py items added directly (the escape hatch path) pass through
    unchanged.
    """
    if isinstance(item, discord.ui.Label):
        return item.component
    return item


class Modal(discord.ui.Modal, StatefulComponent):
    """A modal dialog with stateful inputs and auto-collected validation.

    Validators are attached per-field on each wrapped input (TextInput,
    Checkbox, CheckboxGroup, RadioGroup, FileUpload) and auto-collected
    at construction time -- building a ``Modal`` from these instances
    requires no further validator wiring. Raw ``discord.ui.TextInput``
    items are still accepted as an escape hatch for features the library
    does not yet wrap, but they cannot carry validators; use
    :class:`TextInput` with ``validators=[...]`` for any field that needs
    validation.

    Each wrapped input renders as a ``discord.ui.Label`` containing the
    inner component, so the modal's component tree carries
    ``ui.Label`` children for wrapped inputs and raw items for the
    escape hatch.

    Parameters
    ----------
    title:
        The modal title shown to the user.
    inputs:
        List of wrapped CascadeUI inputs (``TextInput``, ``Checkbox``, ...)
        or raw ``discord.ui.TextInput`` / ``discord.ui.Label`` items.
    callback:
        Async function called with ``(interaction, values)`` after validation passes.
        If omitted, the interaction is deferred automatically.
    timeout:
        Modal timeout in seconds (``None`` for no timeout).
    view_id:
        If provided, a ``MODAL_SUBMITTED`` action is dispatched to the store
        with ``source_id`` set to the same view id, so custom reducers and
        subscribers can distinguish per-view submissions.
    """

    # Ack backstop for discord.py's modal dispatch, which has no auto-defer
    # timer. A slow async validator or the MODAL_SUBMITTED fan-out runs on the
    # 3s interaction clock; this timer acks before the wall.
    auto_defer_delay: float = 2.5

    def __init_subclass__(cls, **kwargs):
        """Reject a non-positive ``auto_defer_delay`` at subclass-definition time.

        A value at or below zero (or the wrong type) silently defeats the ack
        backstop, so it fails when the subclass is defined rather than as a
        dropped interaction at runtime. Mirrors the view side's
        ``_POSITIVE_NUMBER_ATTRS`` check.
        """
        super().__init_subclass__(**kwargs)
        delay = cls.auto_defer_delay
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay <= 0:
            raise ValueError(
                f"{cls.__name__}.auto_defer_delay must be a positive number, got {delay!r}"
            )

    def __init__(
        self,
        title: str,
        inputs: list,
        callback: Optional[Callable] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ):
        _validate_text("Modal", "title", title)
        super().__init__(title=title, timeout=timeout)

        self.view_id = kwargs.get("view_id")
        self.inputs: Dict[str, Any] = {}
        self.validators: Dict[str, List[Callable]] = {}
        # Pairs each wrapped CascadeUI input (TextInput, Checkbox, etc.)
        # with the inner discord.py component (extracted from the
        # ``ui.Label`` wrapper) so ``on_submit`` can write back to the
        # original instances without traversing the Label every time.
        self._wrapped_pairs: List[tuple] = []
        # Populated during ``on_submit``: {wrapper instance: submitted value}.
        # Preferred over the slug-keyed ``values`` dict passed to callbacks.
        self.values_by_input: Dict[Any, Any] = {}

        for input_item in inputs:
            if isinstance(input_item, _WRAPPED_INPUT_TYPES):
                label_wrapper = input_item.create_discord_component()
                inner = label_wrapper.component
                self._reject_duplicate_input(input_item.custom_id)
                self.add_item(label_wrapper)
                self.inputs[input_item.custom_id] = input_item
                self._wrapped_pairs.append((input_item, inner))
                if input_item.validators:
                    self.validators[input_item.custom_id] = list(input_item.validators)
            else:
                # Raw items: ui.Label, ui.TextInput, or any other discord.py
                # modal-compatible component the user constructed directly.
                self.add_item(input_item)
                inner = _unwrap_label(input_item)
                self._reject_duplicate_input(inner.custom_id)
                self.inputs[inner.custom_id] = input_item

        self.user_callback = callback

    def _reject_duplicate_input(self, custom_id: str) -> None:
        """Raise before a duplicate input custom_id silently overwrites.

        Modal input ids derive from the label (``input_{label}``), so two
        inputs with the same label collide on one key. Without this guard
        the second silently overwrites the first in ``self.inputs`` and
        its submitted value is lost at ``on_submit``.
        """
        if custom_id in self.inputs:
            raise ValueError(
                f"Duplicate modal input custom_id: {custom_id!r}. Modal input ids derive "
                f"from the label ('input_{{label}}'), so two inputs with the same label "
                f"collide on one key: one would silently overwrite the other and its "
                f"submitted value would be lost.\n"
                f"  Fix: Give the colliding inputs distinct labels."
            )

    async def _auto_defer_timer(self, interaction: Interaction) -> None:
        """Defer the submission if ``on_submit`` has not responded in time.

        discord.py's modal dispatch has no auto-defer; a slow async validator
        or the ``MODAL_SUBMITTED`` fan-out runs on the 3s interaction clock.
        Without this timer a slow validator drops the submit (10062).
        """
        await ack_backstop(
            interaction, self.auto_defer_delay, owner=self.__class__.__name__, log=logger
        )

    async def _scheduled_task(self, interaction, components, resolved):
        """Arm the ack backstop before discord.py's dispatcher runs ``interaction_check``.

        discord.py's ``Modal._scheduled_task`` awaits ``interaction_check``
        before ``on_submit`` and its default ``on_error`` only logs, so
        ``on_submit``'s own timer cannot cover a slow ``interaction_check``
        override or a fast raise from a validator or callback. This override
        arms a backstop for the whole dispatch and, in the ``finally``, acks any
        interaction the dispatch left unanswered (the raise path). ``on_submit``
        keeps its own timer as a second layer for the submission body.
        """
        defer_task = None
        if not interaction.response.is_done():
            defer_task = asyncio.create_task(self._auto_defer_timer(interaction))
        try:
            await super()._scheduled_task(interaction, components, resolved)
        finally:
            if defer_task is not None and not defer_task.done():
                defer_task.cancel()
            # Ack anything the dispatch left unanswered (the raise path: discord.py's
            # Modal.on_error only logs). Reuses the is_done-aware post-submit helper.
            await self._safe_post_submit_defer(interaction)

    async def respond(
        self, interaction: Interaction, content=None, *, ephemeral: bool = False, **kwargs
    ) -> None:
        """Send an interaction response, falling back to followup if already acked.

        ``on_submit`` arms an auto-defer timer before the validators and the
        user callback run, so a direct ``interaction.response.send_message``
        raises ``InteractionResponded`` once that timer has acked. This checks
        ``interaction.response.is_done()`` and routes to
        ``interaction.followup.send`` when the slot is already consumed. A
        ``Modal`` subclass overriding ``on_submit`` calls ``self.respond(...)``
        for replies; mirrors ``_StatefulMixin.respond``.
        """
        await respond_safe(interaction, content, ephemeral=ephemeral, **kwargs)

    async def on_submit(self, interaction: Interaction):
        """Handle modal submission with optional validation."""
        # discord.py's modal dispatch has no auto-defer timer, so arm one
        # before the validators and state dispatch (both potentially slow and
        # I/O-bound) run on the 3s interaction clock.
        defer_task = asyncio.create_task(self._auto_defer_timer(interaction))
        try:
            # Collect values from the underlying discord.py components,
            # unwrapping ``ui.Label`` to reach the actual input carrying
            # the submitted value.
            values = {}
            for child in self.children:
                inner = _unwrap_label(child)
                if isinstance(inner, (discord.ui.TextInput, discord.ui.RadioGroup)):
                    values[inner.custom_id] = inner.value
                elif isinstance(inner, (discord.ui.CheckboxGroup, discord.ui.FileUpload)):
                    values[inner.custom_id] = inner.values
                elif isinstance(inner, discord.ui.Checkbox):
                    values[inner.custom_id] = inner.value

            # Run validation if validators were provided
            if self.validators:
                field_defs = [
                    {
                        "id": field_id,
                        "validators": field_validators,
                        "required": getattr(self.inputs.get(field_id), "required", False),
                    }
                    for field_id, field_validators in self.validators.items()
                ]
                errors = await validate_fields(values, field_defs)
                if errors:
                    lines = []
                    for field_id, field_errors in errors.items():
                        # Field label ("Emoji"), not the derived custom_id slug ("input_emoji").
                        field = self.inputs.get(field_id)
                        name = getattr(field, "label", field_id)
                        for err in field_errors:
                            lines.append(f"**{name}**: {err.message}")
                    # is_done-aware: the auto-defer timer may have acked during a
                    # slow validator, so a bare send_message would raise
                    # InteractionResponded.
                    await self.respond(interaction, "\n".join(lines), ephemeral=True)
                    return

            # Write submitted values back onto the original CascadeUI wrapper
            # instances so callers can read ``.value`` / ``.values`` directly.
            # This runs after validation so a rejected value never appears on the
            # wrapper, matching the documented "populated after validation passes"
            # contract.
            self.values_by_input = {}
            for wrapped, discord_input in self._wrapped_pairs:
                if isinstance(wrapped, (CheckboxGroup, FileUpload)):
                    wrapped.values = discord_input.values
                    self.values_by_input[wrapped] = discord_input.values
                else:
                    wrapped.value = discord_input.value
                    self.values_by_input[wrapped] = discord_input.value

            # Dispatch state update
            if self.view_id:
                from ..state.singleton import get_store

                store = get_store()

                payload = {
                    "view_id": self.view_id,
                    "values": values,
                    "user_id": interaction.user.id,
                }

                await store.dispatch("MODAL_SUBMITTED", payload, source_id=self.view_id)

            # Call user callback if provided
            if self.user_callback:
                await self.user_callback(interaction, values)

            # Ack the submission if nothing above responded. The validation-error
            # path already responded via respond() and returned; a fast callback
            # here may have edited via the channel endpoint without acking.
            await self._safe_post_submit_defer(interaction)
        finally:
            if not defer_task.done():
                defer_task.cancel()

    async def _safe_post_submit_defer(self, interaction: Interaction) -> None:
        """Acknowledge the modal submission if the callback left it unanswered.

        The submission's state dispatch has already landed, so a dead or
        already-acked interaction on the trailing ack must not turn a
        successful submit into an unhandled error routed to ``on_error``.
        """
        await trailing_ack(interaction, owner=self.__class__.__name__, log=logger)
