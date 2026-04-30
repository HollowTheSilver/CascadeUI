# // ========================================( Modules )======================================== // #


from typing import Any, Callable, Dict, List, Optional, Union

import discord
from discord import CheckboxGroupOption, Interaction, RadioGroupOption, TextStyle

from ..validation import validate_fields
from .base import StatefulComponent

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

        Single source of truth for the slug rule. Used both during
        :class:`TextInput` construction and by form patterns when
        round-tripping ``"text"`` field values through a modal.
        """
        return f"input_{label.lower().replace(' ', '_')}"

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
        self.description = description
        self.options = self._process_options(options, CheckboxGroupOption)
        self.required = required
        self.min_values = min_values
        self.max_values = max_values
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
        self.description = description
        self.options = CheckboxGroup._process_options(options, RadioGroupOption)
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
        self.description = description
        self.required = required
        self.min_values = min_values
        self.max_values = max_values
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

    def __init__(
        self,
        title: str,
        inputs: list,
        callback: Optional[Callable] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ):
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
                self.inputs[inner.custom_id] = input_item

        self.user_callback = callback

    async def on_submit(self, interaction: Interaction):
        """Handle modal submission with optional validation."""
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

        # Write submitted values back onto the original CascadeUI wrapper
        # instances so callers can read ``.value`` / ``.values`` directly
        # instead of reaching into the slug-keyed ``values`` dict.
        self.values_by_input = {}
        for wrapped, discord_input in self._wrapped_pairs:
            if isinstance(wrapped, (CheckboxGroup, FileUpload)):
                wrapped.values = discord_input.values
                self.values_by_input[wrapped] = discord_input.values
            else:
                wrapped.value = discord_input.value
                self.values_by_input[wrapped] = discord_input.value

        # Run validation if validators were provided
        if self.validators:
            field_defs = [
                {"id": field_id, "validators": field_validators}
                for field_id, field_validators in self.validators.items()
            ]
            errors = await validate_fields(values, field_defs)
            if errors:
                lines = []
                for field_id, field_errors in errors.items():
                    for err in field_errors:
                        lines.append(f"**{field_id}**: {err.message}")
                await interaction.response.send_message("\n".join(lines), ephemeral=True)
                return

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
            # Safety net: defer if the callback forgot to respond
            if not interaction.response.is_done():
                await interaction.response.defer()
        else:
            await interaction.response.defer()
