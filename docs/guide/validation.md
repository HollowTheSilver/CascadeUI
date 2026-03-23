# Form Validation

CascadeUI provides a validation system for checking user input against rules before processing it. Validators work with `FormView`, `Modal`, or standalone.

## Built-in Validators

Each validator is a factory function that returns a validation callable:

```python
from cascadeui import min_length, max_length, regex, choices, min_value, max_value
```

| Validator | What it checks | Example |
|-----------|---------------|---------|
| `min_length(n)` | String has at least `n` characters | `min_length(3)` |
| `max_length(n)` | String has at most `n` characters | `max_length(20)` |
| `regex(pattern, msg)` | String matches the regex | `regex(r"^[a-z]+$", "Lowercase only")` |
| `choices(allowed)` | Value is in the allowed list | `choices(["a", "b", "c"])` |
| `min_value(n)` | Number is at least `n` | `min_value(13)` |
| `max_value(n)` | Number is at most `n` | `max_value(120)` |

All validators accept an optional `msg` parameter to customize the error message.

## Field Definitions

Validators are attached to field definitions as a list:

```python
field_defs = [
    {
        "id": "username",
        "label": "Username",
        "validators": [
            min_length(3),
            max_length(20),
            regex(r"^[a-zA-Z0-9_]+$", "Alphanumeric and underscores only"),
        ],
    },
    {
        "id": "age",
        "label": "Age",
        "validators": [min_value(13), max_value(120)],
    },
]
```

Each field definition is a dict with at minimum an `"id"` key. The `"validators"` key holds a list of validator callables.

## Running Validation

### Single field

```python
from cascadeui import validate_field

errors = await validate_field(value="ab", field_def=field_defs[0], all_values={})
# errors: [ValidationResult(valid=False, message="Must be at least 3 characters")]
```

### All fields at once

```python
from cascadeui import validate_fields

values = {"username": "ab", "age": "10"}
errors = await validate_fields(values, field_defs)
# errors: {
#     "username": [ValidationResult(valid=False, message="Must be at least 3 characters")],
#     "age": [ValidationResult(valid=False, message="Must be at least 13")],
# }
```

`validate_fields` returns a dict mapping field ID to a list of failed `ValidationResult` objects. Fields that pass all validators are not included.

## Custom Validators

A validator is any callable with the signature `(value, field, all_values) -> ValidationResult`:

```python
from cascadeui import ValidationResult

def no_spaces(value, field, all_values):
    if value and " " in str(value):
        return ValidationResult(False, "Must not contain spaces")
    return ValidationResult(True)
```

### Async validators

For validators that need I/O (e.g., checking a database for uniqueness):

```python
async def unique_name(value, field, all_values):
    exists = await db.check_username(value)
    if exists:
        return ValidationResult(False, "Username already taken")
    return ValidationResult(True)
```

### Cross-field validation

The `all_values` parameter gives access to every field's value, enabling cross-field checks:

```python
def passwords_match(value, field, all_values):
    if value != all_values.get("password"):
        return ValidationResult(False, "Passwords do not match")
    return ValidationResult(True)
```

## Integration with FormView

Add `validators` to your field definitions and `FormView` will run them on submit:

```python
from cascadeui import FormView, choices

fields = [
    {
        "id": "role", "type": "select", "label": "Role",
        "required": True,
        "options": [
            {"label": "Developer", "value": "dev"},
            {"label": "Designer", "value": "design"},
        ],
        "validators": [choices(["dev", "design"])],
    },
]

view = FormView(context=ctx, fields=fields, on_submit=my_handler)
```

If validation fails, the user sees an ephemeral embed with per-field error messages. Required field checks run first, then validators.

## Integration with Modals

Pass a `validators` dict to `Modal` to validate input on submission. Keys are the field's `custom_id`, values are lists of validators:

```python
from cascadeui import Modal, TextInput, min_length, regex

modal = Modal(
    title="Set Username",
    inputs=[
        TextInput(label="Username", placeholder="alphanumeric only"),
    ],
    callback=handle_username,
    validators={
        "input_username": [
            min_length(3),
            regex(r"^[a-zA-Z0-9_]+$", "Alphanumeric only"),
        ],
    },
)
await interaction.response.send_modal(modal)
```

If any validator fails, the user sees an ephemeral message listing the errors and the callback is not called. If all validators pass, the callback runs normally.

You can also call `validate_fields()` manually in a custom `on_submit` if you need more control over error presentation.

!!! note "Discord's own validation"
    Discord's `TextInput` has built-in `min_length` and `max_length` constraints that are enforced client-side (the modal won't submit if they fail). CascadeUI validators run server-side after submission, so they can check patterns, ranges, and cross-field logic that Discord can't.
