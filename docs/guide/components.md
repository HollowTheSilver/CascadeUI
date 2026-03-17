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
