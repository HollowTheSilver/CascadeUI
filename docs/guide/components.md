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

### Loading State

Shows a visual loading indicator while the callback runs:

```python
from cascadeui import with_loading_state

button = StatefulButton(label="Process", callback=my_handler)
with_loading_state(button)
```

The wrapper uses `interaction.response.edit_message()` to show the loading state, so the original callback should use `interaction.followup` for any messages it sends.

### Confirmation Prompt

Adds a yes/no confirmation before executing the callback:

```python
from cascadeui import with_confirmation

button = StatefulButton(label="Delete", callback=handle_delete)
with_confirmation(button, message="Are you sure you want to delete this?")
```

The confirmation dialog passes the fresh confirmation interaction to the callback, not the original (expired) interaction.

### Per-User Cooldown

Enforces a per-user cooldown between clicks:

```python
from cascadeui import with_cooldown

button = StatefulButton(label="Claim", callback=handle_claim)
with_cooldown(button, seconds=10)
```

Each user has their own cooldown timer. Other users are not affected.

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
