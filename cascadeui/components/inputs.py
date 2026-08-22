# // ========================================( Modules )======================================== // #


import asyncio
import contextvars
import logging
from typing import Any, Callable, Dict, List, Optional, Union

import discord
from discord import CheckboxGroupOption, Interaction, RadioGroupOption, TextStyle
from discord.ui.select import BaseSelect

from ..utils.hooks import await_maybe
from ..utils.responses import ack_backstop, respond_safe, trailing_ack
from ..utils.strings import slugify
from ..validation import validate_fields
from .base import StatefulComponent, require_value_callback

logger = logging.getLogger(__name__)


# // ========================================( Helpers )======================================== // #


def _validate_range(owner: str, name: str, value: Optional[int], lo: int, hi: int) -> None:
    """Reject an out-of-range numeric bound at construction time.

    Discord accepts these bounds only within ``[lo, hi]`` and 400s the modal
    open otherwise; raising here surfaces the mistake where the value is set.
    ``None`` (Discord's server default) is always allowed.
    """
    # The type check precedes the range check because the comparison below is
    # what fails otherwise, and it fails as a bare operand TypeError naming
    # neither the field nor the bound. A bool passes ``isinstance(int)`` and
    # serializes as true/false, and a float serializes with a decimal point;
    # Discord rejects both, so neither reaches the range test.
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(
            f"{owner} {name} must be an int or None, got {type(value).__name__}: {value!r}"
        )
    if value is not None and not lo <= value <= hi:
        raise ValueError(f"{owner} {name}={value} is out of range; Discord accepts {lo}-{hi}.")


# Discord caps a modal title and each input label at 45 characters, and
# rejects an empty one. discord.py stores both unchecked, so a label built
# from a schema field or a record name fails only when the modal opens.
_MODAL_TEXT_MAX = 45


def _validate_text(owner: str, name: str, value: str) -> None:
    """Reject a modal title or label Discord will not accept."""
    # Checked before the emptiness and length tests, which are the operations
    # that would otherwise fail: a value with no ``__len__`` raises a bare
    # TypeError from inside the guard, and one that has a length (a list, a
    # bytestring) passes both tests and reaches Discord as a label it cannot
    # render.
    if not isinstance(value, str):
        raise TypeError(f"{owner} {name} must be a str, got {type(value).__name__}: {value!r}")
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
        """Convert dicts to option instances, rejecting anything else.

        The pass-through branch used to accept whatever it was handed, so a
        list of plain strings became a list of plain strings and reached
        Discord as options with no label and no value. A single option dict
        passed without its list is worse: iterating a mapping yields its
        keys, so ``{"label": "A", "value": "a"}`` produced two options named
        ``"label"`` and ``"value"``, which the count check then counted and
        approved. Both failed at modal-open with an attribute error from
        inside discord.py, and the user saw "This interaction failed".
        """
        if hasattr(raw, "items"):
            raise TypeError(
                f"{option_cls.__name__} options must be a list of option dicts, "
                f"not a single mapping. Iterating a mapping yields its keys.\n"
                f"  Fix: wrap it in a list, e.g. options=[{{'label': ..., 'value': ...}}]."
            )
        processed = []
        for index, opt in enumerate(raw):
            if isinstance(opt, dict):
                processed.append(
                    option_cls(
                        label=opt.get("label", "Option"),
                        value=opt.get("value", opt.get("label", "Option")),
                        description=opt.get("description"),
                        default=opt.get("default", False),
                    )
                )
            elif isinstance(opt, option_cls):
                processed.append(opt)
            else:
                raise TypeError(
                    f"{option_cls.__name__} options[{index}] must be a dict or a "
                    f"{option_cls.__name__}, got {type(opt).__name__}: {opt!r}\n"
                    f"  Fix: pass {{'label': ..., 'value': ...}} per option."
                )
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


_SUBMIT_VERDICT: contextvars.ContextVar = contextvars.ContextVar(
    "cascadeui_submit_verdict", default=None
)
"""What ``Modal.on_submit`` decided on this task: True accepted, False
rejected by a validator, None never ran.

Task-scoped rather than held on the modal, because a modal instance is
shared by every submission it receives. A context is copied into a task
at creation, so discord.py dispatching a real submission cannot overwrite
the verdict an offline drive is waiting to read, while a write inside a
coroutine awaited on the same task stays visible to its awaiter.
"""


def _label_text(item):
    """Return the display name a modal input can be addressed by.

    A ``ui.Label`` carries it as ``text``. A CascadeUI wrapper carries it
    as a plain ``label`` attribute on the instance, which is read out of
    the instance dictionary rather than through ``getattr``: a raw
    discord.py component exposes ``label`` as a deprecated class property
    whose getter forces a ``DeprecationWarning`` past any filter a caller
    sets, so an ordinary attribute read would put a warning in every
    consumer's test output for an attribute they never touched. Going
    through ``__dict__`` cannot invoke a property at all, which makes the
    distinction structural rather than a list of types to keep current.

    A raw component with no ``ui.Label`` therefore has no alias, which
    costs nothing: its custom_id is its identity and always resolves.
    """
    if isinstance(item, discord.ui.Label):
        return getattr(item, "text", None)
    return getattr(item, "__dict__", {}).get("label")


def _assign_submitted_value(inner, key: str, value) -> None:
    """Write a submitted value onto a discord.py modal component.

    Discord delivers these from the gateway payload, so a modal component
    exposes its value as a read-only property over a private attribute of
    the same name. Writing that attribute is what ``_refresh_state`` does
    upstream, and there is no public setter to prefer over it.

    Which property carries the value is read off the component rather than
    matched against a list of known types: a multi-select and a file
    upload expose ``values`` where a text input exposes ``value``, and any
    modal component discord.py adds later answers for itself.

    The storage attribute is confirmed present before the write. Absent,
    it means discord.py moved it, and a plain assignment would create a
    fresh attribute nothing reads while the submission ran against
    defaults and reported success. Reading the value back instead would
    not answer this: a property that coerces its input (a text input
    returns ``''`` for ``None``) reports no change for a write that
    landed, and one holding a default reports no change for a write that
    did not.
    """
    prop = "values" if hasattr(type(inner), "values") else "value"
    storage = f"_{prop}"
    if not hasattr(inner, storage):
        raise RuntimeError(
            f"Modal.submit could not set {key!r}: {type(inner).__name__} has no "
            f"{storage!r} behind its {prop!r} property.\n"
            f"  This means discord.py moved the attribute. The submission would "
            f"otherwise run against default values and report success."
        )
    setattr(inner, storage, value)


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
    custom_id:
        Forwarded to ``discord.ui.Modal``. Omit it to let discord.py
        generate one.

    The signature is closed: an unrecognized keyword raises ``TypeError``
    naming it. ``on_submit`` is the method discord.py subclasses override,
    so it is the natural wrong guess for ``callback``, and a discarded
    handler would leave the modal acknowledging every submission while
    running nothing.

    Raises:
        TypeError: An unrecognized keyword, a non-``str`` title, ``inputs``
            given as a single wrapper rather than a list, a ``callback``
            that cannot receive ``(interaction, values)``, or an input
            carrying no ``custom_id`` and therefore no value to collect.
        ValueError: An empty title, or two inputs deriving the same
            ``custom_id``. That id comes from the label, so two inputs
            sharing a label collide on one key and the second would
            silently replace the first.
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
        view_id: Optional[str] = None,
        custom_id: Optional[str] = None,
    ):
        _validate_text("Modal", "title", title)
        # discord.py generates a custom_id when none is supplied, and its
        # sentinel default is not None, so the caller's value is forwarded
        # only when there is one.
        if custom_id is None:
            super().__init__(title=title, timeout=timeout)
        else:
            super().__init__(title=title, timeout=timeout, custom_id=custom_id)

        self.view_id = view_id
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

        # A single wrapper passed without its list is not iterable, so the
        # loop below fails on the argument rather than naming it.
        if isinstance(inputs, _WRAPPED_INPUT_TYPES):
            raise TypeError(
                f"Modal inputs must be a list, got a single "
                f"{type(inputs).__name__}.\n"
                f"  Fix: wrap it in a list, e.g. "
                f"inputs=[{type(inputs).__name__}(...)]."
            )
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
                if not hasattr(inner, "custom_id"):
                    raise TypeError(
                        f"Modal: {type(inner).__name__} carries no custom_id, so "
                        f"its value cannot be collected on submission.\n"
                        f"  Fix: pass an input component -- a CascadeUI wrapper, "
                        f"or a raw discord.ui input, optionally inside a ui.Label."
                    )
                self._reject_duplicate_input(inner.custom_id)
                self.inputs[inner.custom_id] = input_item

        # on_submit calls this with the collected values on every submit,
        # so a one-parameter callback is broken from the first one.
        require_value_callback(callback, "Modal", "callback", "values")
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
                elif isinstance(inner, BaseSelect):
                    # Reached only through the raw-item escape hatch, since
                    # no wrapper builds one -- but discord.py fills a modal
                    # select on submission like any other child, and without
                    # this branch the chosen option was dropped before the
                    # callback, values_by_input, and the state dispatch, on
                    # real submissions as well as offline drives.
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
                    _SUBMIT_VERDICT.set(False)
                    return

            # Write submitted values back onto the original CascadeUI wrapper
            # instances so callers can read ``.value`` / ``.values`` directly.
            # This runs after validation so a rejected value never appears on the
            # wrapper, matching the documented "populated after validation passes"
            # contract.
            _SUBMIT_VERDICT.set(True)
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
                await await_maybe(self.user_callback(interaction, values))

            # Ack the submission if nothing above responded. The validation-error
            # path already responded via respond() and returned; a fast callback
            # here may have edited via the channel endpoint without acking.
            await self._safe_post_submit_defer(interaction)
        finally:
            if not defer_task.done():
                defer_task.cancel()

    async def submit(self, interaction: Interaction, values: Dict[str, Any]) -> bool:
        """Drive a submission offline, through the pipeline a real one takes.

        The offline-testing surface for modals, alongside ``on_load()``,
        ``validate()``, and ``cascadeui.testing.stub_client()``. Reaching
        past it to call the stored callback directly skips everything
        :meth:`on_submit` does first, and the validator pass is the one
        that matters: a test written that way succeeds against input the
        validators would have rejected, so its coverage reads wider than
        the seam it crossed.

        This assigns the supplied values onto the underlying components
        and then calls :meth:`on_submit` itself, so there is no second
        implementation to drift: whatever a real submission runs, this
        runs.

        Args:
            interaction: The interaction to hand the pipeline. A test
                double is expected here; nothing about this method
                requires a live one.
            values: Field values, keyed by either the input's ``label``
                ("Emoji") or its derived ``custom_id`` ("input_emoji").
                A custom_id is the identity and always resolves to its own
                input; a label is an alias, and takes only a name no
                custom_id has claimed. A field left out keeps whatever
                value it holds, so a test supplies only the fields it
                cares about.

        Returns:
            ``True`` when the submission was accepted and the callback
            ran, ``False`` when a validator rejected it. A rejection is
            not an error: it is the outcome under test.

            The verdict is what :meth:`on_submit` records as it runs, so
            a subclass overriding that method must call up to it. One that
            does not leaves nothing recorded, and is refused rather than
            reported as a rejection.

            The verdict is scoped to the calling task, so a submission
            arriving from Discord while this one is in flight cannot be
            read as its answer. The submitted values are not: they live on
            the components, as they do for a real submission, so drive one
            modal one submission at a time.

        Raises:
            TypeError: ``values`` is not a mapping.
            ValueError: A key matches no input on this modal, or two keys
                name the same input (its label and its custom_id both).
                Raised before any value is written, so a mapping carrying
                one bad key leaves the modal untouched.
            RuntimeError: The component has no storage behind its value
                property, meaning discord.py moved the attribute; or the
                submit pipeline did not run, leaving no verdict to return.
                Both are raised rather than reported, since either would
                otherwise pass as an ordinary result.
        """
        if not hasattr(values, "items"):
            raise TypeError(
                f"Modal.submit expects a mapping of field to value, got "
                f"{type(values).__name__}.\n"
                f"  Fix: pass {{'Field Label': value}} -- keys are an input's "
                f"label or its custom_id."
            )
        targets = self._resolve_submit_keys()

        # Resolved in full before anything is written, so a bad key leaves
        # the modal untouched rather than half-populated up to whichever
        # key the mapping happened to reach first.
        resolved = []
        claimed = {}
        for key, value in values.items():
            inner = targets.get(key)
            if inner is None:
                raise ValueError(
                    f"Modal.submit: no input named {key!r} on {self.title!r}.\n"
                    f"  Fix: key by an input's label or custom_id -- "
                    f"{sorted(targets)}"
                )
            if id(inner) in claimed:
                # An input answers to its label and its custom_id, so one
                # mapping can name the same field twice. Assigning both
                # would keep whichever came last and lose the other with
                # nothing said, which is what the constructor already
                # refuses two inputs for.
                raise ValueError(
                    f"Modal.submit: {claimed[id(inner)]!r} and {key!r} both name "
                    f"the same input on {self.title!r}, so one value would be "
                    f"lost.\n"
                    f"  Fix: pass that field once."
                )
            claimed[id(inner)] = key
            resolved.append((inner, key, value))
        for inner, key, value in resolved:
            _assign_submitted_value(inner, key, value)

        # on_submit records its decision at both points where it decides,
        # into a channel scoped to this task, so this call's recording is
        # the only one it can read. Reading the outcome this way keeps the
        # production path unaware that anything is driving it offline.
        token = _SUBMIT_VERDICT.set(None)
        try:
            await self.on_submit(interaction)
            verdict = _SUBMIT_VERDICT.get()
        finally:
            _SUBMIT_VERDICT.reset(token)
        if verdict is None:
            # Nothing recorded a decision, so the submit pipeline did not
            # run. No comparison of classes can detect that up front: the
            # call dispatches through the instance, and a subclass that
            # calls up records its verdict exactly as this class does.
            raise RuntimeError(
                f"Modal.submit got no verdict from {type(self).__name__}: "
                f"the submit pipeline did not run, so the validators never "
                f"ran and nothing recorded whether the input was accepted.\n"
                f"  Fix: an on_submit override must await "
                f"super().on_submit(interaction), which runs the validators "
                f"and the callback, before doing its own work."
            )
        return verdict

    def _resolve_submit_keys(self):
        """Map every name :meth:`submit` accepts to the component it sets.

        ``self.inputs`` is the modal's identity map: keyed by custom_id,
        carrying raw escape-hatch items alongside wrapped ones, and the
        same key space as the ``values`` mapping the callback receives.
        Building from it is what lets ``submit`` reach a raw input at all.

        Identities register first, aliases second. A raw item carries
        whatever custom_id its author chose, so a label can name an input
        already spoken for; the identity wins and the alias is simply not
        registered. Refusing the name instead would leave the input
        holding it reachable by nothing, since that name is the only one
        it has.

        Returns:
            Every accepted name mapped to the component it writes.
        """
        # For a wrapped input, self.inputs holds the CascadeUI wrapper,
        # whose .value is a plain attribute written back after a submit.
        # The component that CARRIES the submitted value is the discord.py
        # one underneath, which _wrapped_pairs pairs it with.
        inner_for = {wrapped: inner for wrapped, inner in self._wrapped_pairs}

        targets = {}
        for custom_id, item in self.inputs.items():
            targets[custom_id] = inner_for.get(item) or _unwrap_label(item)

        # Aliases in a second pass, so every identity is claimed before any
        # label competes for a name. A label two inputs would claim is not
        # a free name: registering the first silently sends a value to
        # whichever happened to be listed earlier, and both inputs still
        # answer to their own custom_id.
        claimed_labels = {}
        for custom_id, item in self.inputs.items():
            label = _label_text(item)
            if not label or label in targets:
                continue
            if label in claimed_labels:
                claimed_labels[label] = None
            else:
                claimed_labels[label] = inner_for.get(item) or _unwrap_label(item)
        for label, inner in claimed_labels.items():
            if inner is not None:
                targets[label] = inner
        return targets

    async def _safe_post_submit_defer(self, interaction: Interaction) -> None:
        """Acknowledge the modal submission if the callback left it unanswered.

        The submission's state dispatch has already landed, so a dead or
        already-acked interaction on the trailing ack must not turn a
        successful submit into an unhandled error routed to ``on_error``.
        """
        await trailing_ack(interaction, owner=self.__class__.__name__, log=logger)
