# Form Validation

CascadeUI provides a validation system for checking user input against rules before processing it. Validators work with `FormView`, `Modal`, or standalone.

## Built-in Validators

Each validator is a factory function that returns a validation callable:

```python
from cascadeui import min_length, max_length, regex, choices, min_value, max_value, emoji
```

| Validator | What it checks | Example |
|-----------|---------------|---------|
| `min_length(n)` | String has at least `n` characters | `min_length(3)` |
| `max_length(n)` | String has at most `n` characters | `max_length(20)` |
| `regex(pattern, msg)` | String matches the regex | `regex(r"^[a-z]+$", "Lowercase only")` |
| `choices(allowed)` | Value is in the allowed list | `choices(["a", "b", "c"])` |
| `min_value(n)` | Number is at least `n` | `min_value(13)` |
| `max_value(n)` | Number is at most `n` | `max_value(120)` |
| `emoji()` | A unicode emoji or custom `<:name:id>` token | `emoji()` |

All validators accept an optional `msg` parameter to customize the error message.

## Recipe: an emoji input

Discord has no native emoji input, but the pieces to build one already ship: a `choice_row` of your emoji options plus a `Custom` option that opens a `Modal` gated by the `emoji()` validator. There is no separate "emoji picker" widget -- it is composition.

```python
from cascadeui import Choice, Modal, TextInput, choice_row, emoji

# A row of emoji buttons for the options your UI uses, plus a "type your
# own" escape (choice_row switches to a dropdown past five options):
choice_row(
    [Choice("Trophy", "🏆", emoji="🏆"),
     Choice("Crown", "👑", emoji="👑"),
     Choice("Fire", "🔥", emoji="🔥"),
     Choice("Custom...", "__custom__", emoji="✏️")],
    selected=self.icon,
    on_select=self._pick_icon,
)

async def _pick_icon(self, interaction, value):
    if value != "__custom__":
        self.icon = value
        self.build_ui()
        await self.refresh()
        return

    field = TextInput(label="Emoji", placeholder="🏆", required=False,
                      validators=[emoji()])

    async def on_submitted(modal_interaction, values):
        # emoji() rejected anything that is not an emoji before this runs.
        if field.value:
            self.icon = field.value
            self.build_ui()
            await self.refresh()

    await self.open_modal(
        interaction,
        Modal(title="Custom emoji", inputs=[field], callback=on_submitted),
    )
```

The curated emoji list is yours (it is your app's vocabulary); the library supplies the control (`choice_row` renders the options as buttons, or a dropdown past five), the modal, and the `emoji()` gate. `is_emoji()` is also exported from `cascadeui.utils` if you need the bare predicate.

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

!!! note "Blank values on optional fields skip validators"
    When a field is not `required` and its value is blank (`None` or an
    empty or whitespace string), the runner returns no errors without
    running the field's validators. Most built-in validators (`min_length`,
    `regex`, `choices`, `min_value`, `max_value`) reject blank input, which
    would make every validated optional field secretly required. `0` and
    `False` count as values, not blanks, so a numeric field defaulting to
    `0` still runs its validators. The required-check, not the validators,
    is what blocks a blank *required* field.

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

The runner works the same for a plain coroutine function, an object whose
`__call__` is async, or a `functools.partial` wrapping either: anything
`inspect.isawaitable` recognizes once called. A validator that returns
something other than a `ValidationResult` (or an awaitable resolving to one)
raises `TypeError` naming the validator and the field.

### Cross-field validation

The `all_values` parameter gives access to every field's value, enabling cross-field checks:

```python
def passwords_match(value, field, all_values):
    if value != all_values.get("password"):
        return ValidationResult(False, "Passwords do not match")
    return ValidationResult(True)
```

!!! note "`all_values` keys differ between forms and standalone modals"
    In a `FormView`, `all_values` is keyed by your field `id` (`"password"`).
    In a standalone `Modal`, fields have no separate id, so `all_values` is
    keyed by the input's `custom_id`, which is derived from the label
    (a field labelled "Password" keys as `"input_password"`). A cross-field
    validator written for one reads a different key in the other. Each context
    is internally consistent; the difference matters only when moving a
    validator between the two.

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

view = FormView(context=ctx, fields=fields)
```

If validation fails, the user sees an ephemeral embed with per-field error messages. Required field checks run first, then validators.

## Integration with Modals

Attach validators directly to each `TextInput`. `Modal` collects them automatically at construction time, so there is no separate modal-level validators argument:

```python
from cascadeui import Modal, TextInput, min_length, regex

modal = Modal(
    title="Set Username",
    inputs=[
        TextInput(
            label="Username",
            placeholder="alphanumeric only",
            validators=[
                min_length(3),
                regex(r"^[a-zA-Z0-9_]+$", "Alphanumeric only"),
            ],
        ),
    ],
    callback=handle_username,
)
await self.open_modal(interaction, modal)
```

Inside a CascadeUI view, use `self.open_modal()` instead of raw `send_modal()`.

The same `validators=` attachment works on every wrapped input type, not
just `TextInput`. Each validator receives whatever its input submits:

| Input | Value passed to validators |
|-------|------------------------------|
| `TextInput` | `str` |
| `Checkbox` | `bool` |
| `CheckboxGroup` | `list[str]` |
| `RadioGroup` | `str` |
| `FileUpload` | `list[discord.Attachment]` |

See [Components -- Modal Inputs](components.md#modal-inputs) for the
construction reference and a composed multi-type example.

If any validator fails, the user sees an ephemeral message listing the errors and the callback is not called. If all validators pass, the callback runs normally.

You can also call `validate_fields()` manually in a custom `on_submit` if you need more control over error presentation.

!!! note "Discord's own validation"
    Discord's `TextInput` has built-in `min_length` and `max_length` constraints that are enforced client-side (the modal won't submit if they fail). CascadeUI validators run server-side after submission, so they can check patterns, ranges, and cross-field logic that Discord can't.
