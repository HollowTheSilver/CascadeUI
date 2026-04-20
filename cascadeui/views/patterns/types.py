# // ========================================( Modules )======================================== // #


"""Public typed-schema surface for form and wizard patterns.

The dataclasses declared here carry typed ``FormView`` / ``WizardView``
inputs and validate them at class-definition time. The existing
``fields=[dict, ...]`` / ``steps=[dict, ...]`` dict APIs remain valid;
patterns normalize either shape into the same internal dict list at
construction.

Public exports (also re-exported from ``cascadeui``):

    - ``FormField`` -- typed dataclass for a single form field
    - ``WizardStep`` -- typed dataclass for a single wizard step
    - ``FormSchema`` -- base class for declarative form definitions
    - ``WizardSchema`` -- base class for declarative wizard definitions
"""

from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any, Callable, Dict, FrozenSet, List, Optional

import discord


# // ========================================( FormField )======================================== // #


_FIELD_TYPES: FrozenSet[str] = frozenset(
    {"text", "integer", "float", "date", "boolean", "select", "multi_select"}
)


@dataclass
class FormField:
    """Typed declaration of a single form field.

    Accepted by ``FormView`` / ``FormLayoutView`` in place of a raw dict::

        fields = [
            FormField(id="name", label="Your name", required=True),
            FormField(id="age", label="Your age", type="integer", min_value=0),
        ]
        FormView(fields=fields)

    The dataclass lowers to the same dict the pattern has always consumed
    via :meth:`to_dict`, so every existing helper (``_collect_modal_fields``,
    ``_parse_field_value``, ``_format_field_value``) keeps working without
    modification.

    Validation runs in ``__post_init__``: unknown ``type`` values raise
    ``ValueError`` at construction time rather than at first click.

    Subclassing note: ``FormField`` follows standard dataclass inheritance
    rules. Adding a new *required* field in a subclass triggers Python's
    "non-default argument follows default argument" ``TypeError`` because
    the parent class's optional fields all carry defaults. To extend
    ``FormField``, either add only optional fields (with defaults) or
    redeclare the full field order in the subclass using
    ``@dataclass(kw_only=True)``.
    """

    id: str
    label: str
    type: str = "text"
    required: bool = False
    default: Any = None
    placeholder: Optional[str] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    options: Optional[List[Any]] = None
    max_values: Optional[int] = None
    validators: Optional[List[Callable]] = None
    style: Optional[discord.TextStyle] = None
    group: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError(
                f"FormField.id must be a non-empty string (got {self.id!r})"
            )
        if not isinstance(self.label, str) or not self.label:
            raise ValueError(
                f"FormField.label must be a non-empty string "
                f"(field id={self.id!r}, got {self.label!r})"
            )
        if self.type not in _FIELD_TYPES:
            raise ValueError(
                f"FormField.type={self.type!r} is not a valid type. "
                f"Valid types: {sorted(_FIELD_TYPES)}. (field id={self.id!r})"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return the dict shape the form pattern consumes internally.

        Keys whose value is ``None`` are stripped so the dict matches the
        hand-written shape the pattern has always accepted. ``validators``
        and ``options`` are kept as ``None`` only when explicitly absent.
        """
        out: Dict[str, Any] = {}
        for f in dataclass_fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            out[f.name] = value
        return out


# // ========================================( WizardStep )======================================== // #


@dataclass
class WizardStep:
    """Typed declaration of a single wizard step.

    Accepted by ``WizardView`` / ``WizardLayoutView`` in place of a raw dict::

        steps = [
            WizardStep(name="Welcome", builder=self.build_welcome),
            WizardStep(name="Config", builder=self.build_config, validator=self.check_config),
            WizardStep(name="Confirm", builder=self.build_confirm),
        ]
        WizardView(steps=steps)

    The dataclass lowers to the same dict the pattern has always consumed
    via :meth:`to_dict`. ``builder`` is required; ``validator`` and
    ``condition`` are optional and match the dict-API semantics exactly.
    """

    name: str
    builder: Callable
    validator: Optional[Callable] = None
    condition: Optional[Callable] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(
                f"WizardStep.name must be a non-empty string (got {self.name!r})"
            )
        if not callable(self.builder):
            raise ValueError(
                f"WizardStep.builder must be callable "
                f"(step name={self.name!r}, got {type(self.builder).__name__})"
            )
        if self.validator is not None and not callable(self.validator):
            raise ValueError(
                f"WizardStep.validator must be callable or None "
                f"(step name={self.name!r}, got {type(self.validator).__name__})"
            )
        if self.condition is not None and not callable(self.condition):
            raise ValueError(
                f"WizardStep.condition must be callable or None "
                f"(step name={self.name!r}, got {type(self.condition).__name__})"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return the dict shape the wizard pattern consumes internally."""
        out: Dict[str, Any] = {"name": self.name, "builder": self.builder}
        if self.validator is not None:
            out["validator"] = self.validator
        if self.condition is not None:
            out["condition"] = self.condition
        return out


# // ========================================( FormSchema / WizardSchema )======================================== // #


class FormSchema:
    """Base class for declarative form definitions.

    Subclasses override :meth:`get_fields` to supply the list of
    :class:`FormField` instances. A schema instance is passed to
    ``FormView`` / ``FormLayoutView`` via the ``schema=`` kwarg::

        class ProfileSchema(FormSchema):
            def get_fields(self):
                return [
                    FormField(id="name", label="Your name", required=True),
                    FormField(id="bio", label="Bio", type="text"),
                ]

        FormLayoutView(schema=ProfileSchema())

    The schema object is stateless aside from what the subclass chooses to
    store; ``FormView`` calls ``get_fields()`` once at construction and
    converts the result through ``FormField.to_dict()``.
    """

    def get_fields(self) -> List[FormField]:
        """Subclasses return the list of ``FormField`` instances."""
        raise NotImplementedError(
            f"{type(self).__name__} must override get_fields() "
            f"to return a list[FormField]."
        )


class WizardSchema:
    """Base class for declarative wizard definitions.

    Subclasses override :meth:`get_steps` to supply the list of
    :class:`WizardStep` instances. A schema instance is passed to
    ``WizardView`` / ``WizardLayoutView`` via the ``schema=`` kwarg::

        class SetupSchema(WizardSchema):
            def __init__(self, view):
                self.view = view

            def get_steps(self):
                return [
                    WizardStep(name="Welcome", builder=self.view.build_welcome),
                    WizardStep(name="Config", builder=self.view.build_config,
                               validator=self.view.validate_config),
                    WizardStep(name="Confirm", builder=self.view.build_confirm),
                ]

        WizardLayoutView(schema=SetupSchema(self))

    The schema holds no wizard state -- it is a recipe for step
    construction. Per-step values live on the view as they always have.
    """

    def get_steps(self) -> List[WizardStep]:
        """Subclasses return the list of ``WizardStep`` instances."""
        raise NotImplementedError(
            f"{type(self).__name__} must override get_steps() "
            f"to return a list[WizardStep]."
        )


# // ========================================( Module Helpers )======================================== // #


def _normalize_fields(
    fields: Optional[List[Any]],
    schema: Optional[FormSchema],
    cls_name: str,
) -> List[Dict[str, Any]]:
    """Resolve ``fields`` / ``schema`` into the internal dict list.

    Raises ``ValueError`` when both ``fields`` and ``schema`` are supplied.
    Returns an empty list when both are ``None`` -- a zero-field form is a
    valid zero-config state, not an error. Typed ``FormField`` items lower
    to dicts via ``to_dict()``; raw dicts pass through unchanged so
    hand-written fields stay valid.
    """
    if schema is not None and fields is not None:
        raise ValueError(
            f"{cls_name} accepts either 'fields=' or 'schema=', not both."
        )
    if schema is not None:
        if not isinstance(schema, FormSchema):
            raise TypeError(
                f"{cls_name}(schema=...) expects a FormSchema instance "
                f"(got {type(schema).__name__})."
            )
        fields = schema.get_fields()
    if not fields:
        return []
    out: List[Dict[str, Any]] = []
    for item in fields:
        if isinstance(item, FormField):
            out.append(item.to_dict())
        elif isinstance(item, dict):
            out.append(item)
        else:
            raise TypeError(
                f"{cls_name} field entries must be FormField or dict "
                f"(got {type(item).__name__})."
            )
    return out


def _normalize_steps(
    steps: Optional[List[Any]],
    schema: Optional[WizardSchema],
    cls_name: str,
) -> List[Dict[str, Any]]:
    """Resolve ``steps`` / ``schema`` into the internal dict list.

    Raises ``ValueError`` when both ``steps`` and ``schema`` are supplied.
    Returns an empty list when both are ``None`` -- a zero-step wizard is a
    valid zero-config state, not an error. Typed ``WizardStep`` items lower
    to dicts via ``to_dict()``; raw dicts pass through unchanged so
    hand-written steps stay valid.
    """
    if schema is not None and steps is not None:
        raise ValueError(
            f"{cls_name} accepts either 'steps=' or 'schema=', not both."
        )
    if schema is not None:
        if not isinstance(schema, WizardSchema):
            raise TypeError(
                f"{cls_name}(schema=...) expects a WizardSchema instance "
                f"(got {type(schema).__name__})."
            )
        steps = schema.get_steps()
    if not steps:
        return []
    out: List[Dict[str, Any]] = []
    for item in steps:
        if isinstance(item, WizardStep):
            out.append(item.to_dict())
        elif isinstance(item, dict):
            out.append(item)
        else:
            raise TypeError(
                f"{cls_name} step entries must be WizardStep or dict "
                f"(got {type(item).__name__})."
            )
    return out
