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


def min_value(n: Union[int, float], msg: Optional[str] = None):
    """Validate that a numeric value is at least ``n``."""

    def validator(value, field, all_values):
        try:
            if float(value) < n:
                return ValidationResult(False, msg or f"Must be at least {n}")
        except (TypeError, ValueError):
            return ValidationResult(False, msg or f"Must be a number >= {n}")
        return ValidationResult(True)

    return validator


def max_value(n: Union[int, float], msg: Optional[str] = None):
    """Validate that a numeric value is at most ``n``."""

    def validator(value, field, all_values):
        try:
            if float(value) > n:
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

    for validator in validators:
        if inspect.iscoroutinefunction(validator):
            result = await validator(value, field_def, all_values)
        else:
            result = validator(value, field_def, all_values)
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
