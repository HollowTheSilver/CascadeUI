# API: Validation

## `ValidationResult`

Dataclass representing the outcome of a single validation check.

```python
from cascadeui import ValidationResult

result = ValidationResult(valid=True)
result = ValidationResult(valid=False, message="Must be at least 3 characters")
```

- `valid` (bool): Whether the check passed
- `message` (str): Error message (empty string if valid)

---

## Validator Factories

Each returns a callable with signature `(value, field, all_values) -> ValidationResult`.

### `min_length(n, msg=None)`

Value must have at least `n` characters.

### `max_length(n, msg=None)`

Value must have at most `n` characters.

### `regex(pattern, msg=None)`

Value must match the regex pattern.

### `choices(allowed, msg=None)`

Value must be in the `allowed` list.

### `min_value(n, msg=None)`

Numeric value must be at least `n`. Non-numeric values fail with a type error message.

### `max_value(n, msg=None)`

Numeric value must be at most `n`. Non-numeric values fail with a type error message.

---

## Runner Functions

### `validate_field(value, field_def, all_values)`

Runs all validators for a single field definition.

- `value`: The field's current value
- `field_def` (dict): Must have a `"validators"` key with a list of callables
- `all_values` (dict): All field values (for cross-field validation)

**Returns:** `List[ValidationResult]` of failed checks (empty if all pass).

### `validate_fields(values, field_defs)`

Runs validators for all fields.

- `values` (dict): Mapping of field_id -> value
- `field_defs` (list[dict]): List of field definitions, each with `"id"` and optional `"validators"`

**Returns:** `Dict[str, List[ValidationResult]]` mapping field_id -> list of failures. Only fields with errors are included.

---

## Custom Validators

Any callable with the signature `(value, field, all_values) -> ValidationResult`:

```python
def no_spaces(value, field, all_values):
    if value and " " in str(value):
        return ValidationResult(False, "Must not contain spaces")
    return ValidationResult(True)
```

Async validators are also supported:

```python
async def unique_name(value, field, all_values):
    exists = await db.check(value)
    return ValidationResult(not exists, "Already taken" if exists else "")
```
