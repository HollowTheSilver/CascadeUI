# // ========================================( Modules )======================================== // #


import asyncio
import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Union

# // ========================================( Result )======================================== // #


@dataclass
class ValidationResult:
    """Outcome of a single validation check."""

    valid: bool
    message: str = ""


# // ========================================( Built-in Validators )======================================== // #


def min_length(n: int, msg: Optional[str] = None):
    """Validate that a string value has at least ``n`` characters."""

    def validator(value, field, all_values):
        if value is None or len(str(value)) < n:
            return ValidationResult(False, msg or f"Must be at least {n} characters")
        return ValidationResult(True)

    return validator


def max_length(n: int, msg: Optional[str] = None):
    """Validate that a string value has at most ``n`` characters."""

    def validator(value, field, all_values):
        if value is not None and len(str(value)) > n:
            return ValidationResult(False, msg or f"Must be at most {n} characters")
        return ValidationResult(True)

    return validator


def regex(pattern: str, msg: Optional[str] = None):
    """Validate that a string value matches a regex pattern."""
    compiled = re.compile(pattern)

    def validator(value, field, all_values):
        if value is None or not compiled.match(str(value)):
            return ValidationResult(False, msg or f"Does not match required format")
        return ValidationResult(True)

    return validator


def choices(allowed: List[Any], msg: Optional[str] = None):
    """Validate that the value is one of the allowed choices."""

    def validator(value, field, all_values):
        if value not in allowed:
            return ValidationResult(
                False, msg or f"Must be one of: {', '.join(str(a) for a in allowed)}"
            )
        return ValidationResult(True)

    return validator


def emoji(msg: Optional[str] = None):
    """Validate that a value is a single emoji or a custom Discord token.

    Backed by :func:`cascadeui.utils.is_emoji`. Intended for a ``Modal``
    ``TextInput`` collecting an arbitrary emoji -- Discord has no native emoji
    input field. An empty value passes; ``required=True`` on the input rejects
    blank, and a non-empty value that is not an emoji fails.
    """
    from .utils.strings import is_emoji

    def validator(value, field, all_values):
        text = str(value or "").strip()
        if not text or is_emoji(text):
            return ValidationResult(True)
        return ValidationResult(False, msg or "Enter a single emoji or a custom server emoji.")

    return validator


def min_value(n: Union[int, float], msg: Optional[str] = None):
    """Validate that a numeric value is at least ``n``."""

    def validator(value, field, all_values):
        try:
            # Phrased as ``not (>=)`` rather than ``<`` so NaN, which is
            # neither, fails the bound instead of passing it.
            if not (float(value) >= n):
                return ValidationResult(False, msg or f"Must be at least {n}")
        except (TypeError, ValueError):
            return ValidationResult(False, msg or f"Must be a number >= {n}")
        return ValidationResult(True)

    return validator


def max_value(n: Union[int, float], msg: Optional[str] = None):
    """Validate that a numeric value is at most ``n``."""

    def validator(value, field, all_values):
        try:
            if not (float(value) <= n):
                return ValidationResult(False, msg or f"Must be at most {n}")
        except (TypeError, ValueError):
            return ValidationResult(False, msg or f"Must be a number <= {n}")
        return ValidationResult(True)

    return validator


# // ========================================( Runner )======================================== // #


async def validate_field(
    value: Any,
    field_def: Dict[str, Any],
    all_values: Dict[str, Any],
) -> List[ValidationResult]:
    """Run all validators for a single field.

    Args:
        value: The field's current value.
        field_def: The field definition dict (must have "validators" key).
        all_values: All field values (for cross-field validation).

    Returns:
        List of failed ValidationResult instances (empty if all pass).
    """
    validators = field_def.get("validators", [])
    errors = []

    # An empty value on an optional field passes without running validators.
    # Most validators reject blank (min_length, regex, choices, min/max_value),
    # so running them would make every validated optional field secretly
    # required; the required-check owns the blank-required case separately.
    # 0 and False are values, not blanks, so only None and blank strings skip.
    is_empty = value is None or (isinstance(value, str) and not value.strip())
    if is_empty and not field_def.get("required", False):
        return errors

    for validator in validators:
        result = validator(value, field_def, all_values)
        # Await after the call so a plain coroutine function, an async
        # ``__call__`` object (the documented awaitable shape), and a
        # ``functools.partial`` wrapping either all resolve.
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ValidationResult):
            name = getattr(validator, "__qualname__", None) or repr(validator)
            raise TypeError(
                f"Validator {name} for field {field_def.get('id')!r} must return "
                f"a ValidationResult (or an awaitable of one), got "
                f"{type(result).__name__}."
            )
        if not result.valid:
            errors.append(result)

    return errors


async def validate_fields(
    values: Dict[str, Any],
    field_defs: List[Dict[str, Any]],
) -> Dict[str, List[ValidationResult]]:
    """Run validators for all fields.

    Args:
        values: Mapping of field_id -> value.
        field_defs: List of field definitions.

    Returns:
        Dict mapping field_id -> list of failed ValidationResult.
        Only fields with errors are included.
    """
    errors: Dict[str, List[ValidationResult]] = {}

    for field_def in field_defs:
        field_id = field_def.get("id")
        if field_id is None:
            continue

        value = values.get(field_id)
        field_errors = await validate_field(value, field_def, values)
        if field_errors:
            errors[field_id] = field_errors

    return errors
