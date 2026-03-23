# Components

CascadeUI components extend discord.py's built-in UI components with automatic state dispatching and composability.

## Stateful Components

### StatefulButton

Extends `discord.ui.Button` with automatic `COMPONENT_INTERACTION` dispatching:

```python
from cascadeui import StatefulButton

button = StatefulButton(
    label="Click Me",
    style=discord.ButtonStyle.primary,
    callback=my_handler,
)
self.add_item(button)
```

Every click dispatches a `COMPONENT_INTERACTION` action to the state store, tracking the interaction for debugging and state history.

### StatefulSelect

Extends `discord.ui.Select` with the same state integration:

```python
from cascadeui import StatefulSelect

select = StatefulSelect(
    placeholder="Pick one...",
    options=[
        discord.SelectOption(label="Option A", value="a"),
        discord.SelectOption(label="Option B", value="b"),
    ],
    callback=my_handler,
)
self.add_item(select)
```

## Modals

CascadeUI's `Modal` wraps `discord.ui.Modal` with state integration and optional validation.

### Opening a Modal

Open a modal from any button callback using `interaction.response.send_modal()`:

```python
from cascadeui import Modal, TextInput

async def open_feedback(interaction):
    modal = Modal(
        title="Send Feedback",
        inputs=[
            TextInput(label="Subject", placeholder="Brief summary"),
            TextInput(label="Details", style=discord.TextStyle.long),
        ],
        callback=handle_feedback,
    )
    await interaction.response.send_modal(modal)

async def handle_feedback(interaction, values):
    subject = values.get("input_subject")
    details = values.get("input_details")
    await interaction.response.send_message(
        f"Thanks! Received: {subject}", ephemeral=True
    )
```

`TextInput` generates a `custom_id` from the label automatically (e.g. `"Subject"` becomes `"input_subject"`). You can also pass raw `discord.ui.TextInput` items if you need full control over the `custom_id`.

If no `callback` is provided, the modal defers the interaction automatically.

### Validation

Pass a `validators` dict mapping `custom_id` to a list of validator functions. If any field fails validation, the user sees an ephemeral error message and the callback is not called:

```python
from cascadeui import Modal, TextInput, min_length, max_length, regex

modal = Modal(
    title="Create Tag",
    inputs=[
        TextInput(label="Name", placeholder="tag-name"),
        TextInput(label="Content", style=discord.TextStyle.long),
    ],
    callback=save_tag,
    validators={
        "input_name": [
            min_length(2, "Tag name must be at least 2 characters"),
            max_length(32, "Tag name must be at most 32 characters"),
            regex(r"^[a-z0-9-]+$", "Only lowercase letters, numbers, and hyphens"),
        ],
        "input_content": [
            min_length(1, "Content cannot be empty"),
        ],
    },
)
```

All validators from the [validation system](validation.md) work here: `min_length`, `max_length`, `regex`, `choices`, `min_value`, `max_value`, and custom async validators.

### State Integration

If you pass `view_id` to the modal, a `MODAL_SUBMITTED` action is dispatched to the state store before the callback runs:

```python
modal = Modal(
    title="Edit Name",
    inputs=[TextInput(label="Name")],
    callback=handle_edit,
    view_id=self.id,  # Links this modal to the current view's state
)
```

This lets reducers and middleware observe modal submissions alongside other actions.

## Composite Components

Composite components group related items and add them to a view as a unit.

### ConfirmationButtons

A confirm/cancel button pair:

```python
from cascadeui import ConfirmationButtons

confirmation = ConfirmationButtons(
    on_confirm=handle_confirm,
    on_cancel=handle_cancel,
)
confirmation.add_to_view(my_view)
```

### PaginationControls

Previous/Next buttons for paginated content:

```python
from cascadeui import PaginationControls

pagination = PaginationControls(
    page_count=5,
    on_page_change=handle_page,
)
pagination.add_to_view(my_view)
```

### FormLayout

Builds form controls from a field definition list:

```python
from cascadeui import FormLayout

layout = FormLayout(fields=[
    {"id": "color", "type": "select", "label": "Color",
     "options": ["Red", "Blue", "Green"]},
    {"id": "enabled", "type": "boolean", "label": "Enabled"},
])
layout.add_to_view(my_view)
```

!!! note
    String fields are not supported inline (Discord requires Modals for text input). FormLayout will emit a warning if you include a `"string"` type field.

### ToggleGroup

Radio-button-like selection where only one option is active at a time:

```python
from cascadeui import ToggleGroup

group = ToggleGroup(
    options=["Easy", "Medium", "Hard"],
    on_select=difficulty_handler,
    default="Medium",
)
group.add_to_view(my_view)
```

The active option's button is highlighted. Clicking a different option deselects the previous one.

## Component Wrappers

Wrappers modify component behavior without changing the component itself. Apply them to buttons to add cross-cutting behavior.

!!! danger "Wrappers consume the interaction response"
    All three wrappers (`with_loading_state`, `with_confirmation`, `with_cooldown`) use `interaction.response` internally to show their UI (loading indicator, confirmation prompt, or cooldown message). Your wrapped callback **must** use `interaction.followup.send()` instead of `interaction.response.send_message()`. Calling `interaction.response` in a wrapped callback will raise `InteractionResponded`.

### Loading State

Shows a visual loading indicator while the callback runs:

```python
from cascadeui import with_loading_state

button = StatefulButton(label="Process", callback=my_handler)
with_loading_state(button)
```

The button is disabled and its label changes to "Loading..." while the callback runs. After the callback finishes, the original label and state are restored.

```python
async def my_handler(interaction):
    # interaction.response is already consumed -- use followup
    await asyncio.sleep(2)
    await interaction.followup.send("Done!", ephemeral=True)
```

### Confirmation Prompt

Adds a yes/no confirmation before executing the callback:

```python
from cascadeui import with_confirmation

button = StatefulButton(label="Delete", callback=handle_delete)
with_confirmation(
    button,
    message="Are you sure you want to delete this?",
    confirmed_message="Deleted.",
    cancelled_message="Kept safe.",
)
```

The confirmation dialog is sent as an ephemeral message. If the user confirms, the callback runs with the confirmation interaction. If they cancel, the dialog is edited to show the cancelled message.

### Per-User Cooldown

Enforces a per-user cooldown between clicks:

```python
from cascadeui import with_cooldown

button = StatefulButton(label="Claim", callback=handle_claim)
with_cooldown(button, seconds=10)
```

Each user has their own cooldown timer. Other users are not affected. The `scope` parameter controls cooldown isolation:

| Scope | Behavior |
|-------|----------|
| `"user"` (default) | Each user has an independent cooldown |
| `"guild"` | Shared cooldown per server |
| `"global"` | One cooldown for everyone |

```python
with_cooldown(button, seconds=30, scope="guild")
```

If a user clicks while on cooldown, they see an ephemeral message with the remaining time. The original callback is not called.

## Utilities

### ProgressBar

A text-based progress bar for embed fields (not a Discord component):

```python
from cascadeui import ProgressBar

bar = ProgressBar(total=100, width=20)
embed.add_field(name="Progress", value=bar.render(65))
# Output: ████████████░░░░░░░░ 65%
```

Configurable fill and empty characters, width, and total.
