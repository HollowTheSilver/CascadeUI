# // ========================================( Modules )======================================== // #


"""Public typed-schema surface for form, wizard, and roles patterns.

The dataclasses declared here carry typed ``FormView`` / ``WizardView``
/ ``RolesLayoutView`` inputs and validate them at class-definition
time. The existing ``fields=[dict, ...]`` / ``steps=[dict, ...]`` dict
APIs remain valid for form and wizard; patterns normalize either shape
into the same internal dict list at construction. Roles uses the
typed ``RoleCategory`` exclusively because cardinality flags
(``exclusive``, ``required``) benefit from dataclass validation.

Public exports (also re-exported from ``cascadeui``):

    - ``FormField`` -- typed dataclass for a single form field
    - ``WizardStep`` -- typed dataclass for a single wizard step
    - ``FormSchema`` -- base class for declarative form definitions
    - ``WizardSchema`` -- base class for declarative wizard definitions
    - ``RoleCategory`` -- typed dataclass for a role-assign category
"""

import inspect
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from typing import Any, Callable, Dict, FrozenSet, List, Optional

import discord

from ...components.base import _describe_callback
from ...utils.hooks import can_accept_positional, is_async_callable

# // ========================================( FormField )======================================== // #


_FIELD_TYPES: FrozenSet[str] = frozenset(
    {"text", "integer", "float", "date", "boolean", "select", "multi_select"}
)


def _validate_field_values(label: str, field_id, field_label, field_type) -> None:
    """Check one form field's identity and type, whatever declared it.

    Shared by :class:`FormField` and the raw-dict path. An unrecognised
    type rendered no control and a missing id surfaced wherever the value
    was first keyed, so the dict form failed later and more quietly than
    the typed one for the same mistake.
    """
    if not isinstance(field_id, str) or not field_id:
        raise ValueError(f"{label}.id must be a non-empty string (got {field_id!r})")
    if not isinstance(field_label, str) or not field_label:
        raise ValueError(
            f"{label}.label must be a non-empty string "
            f"(field id={field_id!r}, got {field_label!r})"
        )
    if field_type not in _FIELD_TYPES:
        raise ValueError(
            f"{label}.type={field_type!r} is not a valid type. "
            f"Valid types: {sorted(_FIELD_TYPES)}. (field id={field_id!r})"
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

    ``secret=True`` masks the field's value in the form display (a password
    or token), so it renders as fixed dots instead of the entered text.
    Discord modals cannot mask the input itself, so this covers the display
    only.

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
    secret: bool = False

    def __post_init__(self) -> None:
        _validate_field_values("FormField", self.id, self.label, self.type)

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


def _validate_condition_arity(label: str, condition, name_note: str = "") -> None:
    """Refuse a condition the visibility check cannot call.

    ``_is_step_visible`` calls ``condition(view)`` inside a ``try`` whose
    ``except Exception`` treats a raising predicate as visible, so an arity
    mismatch is not an error the caller ever sees: the step the predicate
    meant to hide renders, and a warning is the only trace. That makes it
    the one member of this family worth refusing here, since every other
    step callable dies at its own call with the callable in the traceback.
    A zero-argument predicate is the specific shape this catches.
    """
    if condition is None or can_accept_positional(condition, 1) is not False:
        return
    raise TypeError(
        f"{label}.condition {_describe_callback(condition)} cannot be called "
        f"with (view){name_note}.\n"
        f"  Fix: accept the view -- the wizard passes itself so the predicate "
        f"can read step state."
    )


def _validate_step_callables(label: str, step: Dict[str, Any]) -> None:
    """Check the callables a raw step dict declares, if it declares them.

    Narrower than :class:`WizardStep`'s own checks on purpose. The dict form
    reads only ``builder``, ``validator`` and ``condition``, never ``name``,
    and a step with no builder is a supported shape that renders nav alone.
    What is not supported is a value under one of those keys that cannot be
    called: the builder crashed at render, the validator crashed on Next,
    and the condition was swallowed by the visibility guard and its step
    shown regardless.
    """
    for key in ("builder", "validator", "condition"):
        value = step.get(key)
        if value is None:
            continue
        if not callable(value):
            raise ValueError(
                f"{label}.{key} must be callable or absent, got {type(value).__name__}"
            )
    condition = step.get("condition")
    if condition is not None and is_async_callable(condition):
        raise TypeError(
            f"{label}.condition must be synchronous; load async data in the "
            f"view's on_load() and have the predicate read the result."
        )
    _validate_condition_arity(label, condition)


def _validate_step_values(label: str, name, builder, validator, condition) -> None:
    """Check one wizard step's callables and name, whatever declared it.

    Shared by :class:`WizardStep` and the raw-dict path so both answer the
    same way. The dict form is a documented alternative, not a lesser one,
    and left unchecked it failed later and mostly in silence: a step with
    no builder rendered as an empty page, a non-callable condition was
    swallowed by the visibility guard and shown anyway, and only a
    non-callable builder raised at all.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"{label}.name must be a non-empty string (got {name!r})")
    if not callable(builder):
        raise ValueError(
            f"{label}.builder must be callable (step name={name!r}, "
            f"got {type(builder).__name__})"
        )
    for field_name, value in (("validator", validator), ("condition", condition)):
        if value is not None and not callable(value):
            raise ValueError(
                f"{label}.{field_name} must be callable or None "
                f"(step name={name!r}, got {type(value).__name__})"
            )
    # The visibility check reads the predicate's answer synchronously, so an
    # async one returns a coroutine, and a coroutine is truthy: the step
    # renders whatever the predicate would have said.
    if condition is not None and is_async_callable(condition):
        raise TypeError(
            f"{label}.condition must be synchronous (step name={name!r}); "
            f"load async data in the view's on_load() and have the predicate "
            f"read the result."
        )
    _validate_condition_arity(label, condition, f" (step name={name!r})")


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
        _validate_step_values("WizardStep", self.name, self.builder, self.validator, self.condition)

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
            f"{type(self).__name__} must override get_fields() " f"to return a list[FormField]."
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
            f"{type(self).__name__} must override get_steps() " f"to return a list[WizardStep]."
        )


# // ========================================( RoleCategory )======================================== // #


@dataclass
class RoleCategory:
    """Typed declaration of a single role-assign category.

    Accepted by ``RolesLayoutView`` / ``PersistentRolesLayoutView`` via
    the class-level ``categories`` attribute::

        class MyRoles(PersistentRolesLayoutView):
            categories = [
                RoleCategory(
                    name="Colors",
                    roles={"Red": 111, "Blue": 222, "Green": 333},
                    exclusive=True,
                    color=discord.Color.red(),
                ),
            ]

    Cardinality is controlled by two orthogonal flags:

    - ``exclusive``: at most one role in the category may be active.
      Selecting another removes the previously-active one (swap).
    - ``required``: at least one role in the category must stay active.
      Removing the last role is rejected.

    The four combinations (free / radio-optional / required-checkbox /
    radio-required) all produce valid cardinality behavior; the pattern
    enforces whichever constraints are set.

    Validation at class-definition time: ``name`` and ``roles`` are
    required and non-empty; ``roles`` values must be integers (role
    IDs); ``exclusive`` and ``required`` must be booleans.
    """

    name: str
    roles: Dict[str, int]
    exclusive: bool = False
    required: bool = False
    color: Optional[discord.Color] = None
    button_style: Optional[discord.ButtonStyle] = None
    icon: Optional[str] = None
    description: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"RoleCategory.name must be a non-empty string (got {self.name!r})")
        if not isinstance(self.roles, dict) or not self.roles:
            raise ValueError(
                f"RoleCategory.roles must be a non-empty dict "
                f"(category name={self.name!r}, got {type(self.roles).__name__})"
            )
        for role_name, role_id in self.roles.items():
            if not isinstance(role_name, str) or not role_name:
                raise ValueError(
                    f"RoleCategory.roles keys must be non-empty strings "
                    f"(category name={self.name!r}, got key {role_name!r})"
                )
            if not isinstance(role_id, int) or isinstance(role_id, bool):
                raise ValueError(
                    f"RoleCategory.roles values must be integer role IDs "
                    f"(category name={self.name!r}, role label={role_name!r}, "
                    f"got {type(role_id).__name__}: {role_id!r})"
                )
        if not isinstance(self.exclusive, bool):
            raise ValueError(
                f"RoleCategory.exclusive must be a bool "
                f"(category name={self.name!r}, got {type(self.exclusive).__name__})"
            )
        if not isinstance(self.required, bool):
            raise ValueError(
                f"RoleCategory.required must be a bool "
                f"(category name={self.name!r}, got {type(self.required).__name__})"
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
    to dicts via ``to_dict()``; raw dicts pass through, with a missing
    ``type`` filled to ``"text"`` so a hand-written dict matches the
    ``FormField`` default and renders a control.
    """
    if schema is not None and fields is not None:
        raise ValueError(f"{cls_name} accepts either 'fields=' or 'schema=', not both.")
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
            # A dict with no "type" renders no control at all; FormField
            # defaults type="text", so fill it to match rather than mutate
            # the caller's dict.
            resolved = item if "type" in item else {**item, "type": "text"}
            # An unrecognised type renders no control at all, so the field
            # vanishes from the form with nothing raised and nothing logged.
            # ``FormField`` has always rejected it; the dict form now does too.
            if resolved.get("type") not in _FIELD_TYPES:
                raise ValueError(
                    f"{cls_name} fields[{len(out)}].type={resolved.get('type')!r} "
                    f"is not a valid type. Valid types: {sorted(_FIELD_TYPES)}."
                )
            out.append(resolved)
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
    to dicts via ``to_dict()``; raw dicts keep their own keys but face the
    same checks, so neither declaration form is the lenient one.
    """
    if schema is not None and steps is not None:
        raise ValueError(f"{cls_name} accepts either 'steps=' or 'schema=', not both.")
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
            _validate_step_callables(f"{cls_name} steps[{len(out)}]", item)
            out.append(item)
        else:
            raise TypeError(
                f"{cls_name} step entries must be WizardStep or dict "
                f"(got {type(item).__name__})."
            )
    return out
