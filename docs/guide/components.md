# Components

CascadeUI components extend discord.py's built-in UI components with automatic state dispatching and composability.

## Stateful Components

These work in both V1 and V2 views.

### StatefulButton

Extends `discord.ui.Button` with automatic `COMPONENT_INTERACTION` dispatching:

```python
from cascadeui import StatefulButton

button = StatefulButton(
    label="Click Me",
    style=discord.ButtonStyle.primary,
    callback=my_handler,
)
```

Every click dispatches a `COMPONENT_INTERACTION` action to the state store, tracking the interaction for debugging and state history.

In V2 views, buttons must be wrapped in `ActionRow`:

```python
from discord.ui import ActionRow

self.add_item(ActionRow(
    StatefulButton(label="Save", callback=self.save),
    StatefulButton(label="Cancel", callback=self.cancel),
))
```

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
```

Specialized select variants are also available: `Dropdown`, `RoleSelect`, `ChannelSelect`, `UserSelect`, `MentionableSelect`.

---

## V2 Helpers

Convenience functions for building V2 component trees. These return standard discord.py V2 components — no custom classes needed.

### card()

Creates a `Container` with an optional title, children, and accent color:

```python
from cascadeui import card, divider
from discord.ui import TextDisplay

self.add_item(
    card(
        "## My Card Title",
        TextDisplay("Card content goes here."),
        divider(),
        TextDisplay("-# Footer text"),
        color=discord.Color.blurple(),
    )
)
```

String arguments are automatically wrapped in `TextDisplay`, so you can mix raw strings and V2 components freely. `color` sets the container's accent color (like embed color, but stackable -- multiple cards can have different colors in one message).

### key_value()

Converts a dict to a formatted `TextDisplay`:

```python
from cascadeui import key_value

self.add_item(key_value({
    "Status": "Online",
    "Users": "42",
    "Uptime": "3h 12m",
}))
```

Renders as:
```
**Status:** Online
**Users:** 42
**Uptime:** 3h 12m
```

### action_section()

Creates a `Section` with text and a button accessory (text + action on the same line):

```python
from cascadeui import action_section

self.add_item(
    action_section(
        "Click to refresh the dashboard",
        label="Refresh",
        callback=self.refresh,
        emoji="\U0001f504",
    )
)
```

### toggle_section()

Creates a `Section` with a green/red toggle button:

```python
from cascadeui import toggle_section

self.add_item(
    toggle_section(
        "**Dark Mode**\nEnable dark theme",
        active=self.dark_mode,
        callback=self.toggle_dark,
    )
)
```

When `active=True`, the button shows a green checkmark. When `False`, a red X.

### alert()

A colored status container for success, warning, error, or info messages:

```python
from cascadeui import alert

self.add_item(alert("Settings saved successfully!", level="success"))
self.add_item(alert("No data found.", level="info"))
```

| Level | Color |
|-------|-------|
| `"success"` | Green |
| `"warning"` | Gold |
| `"error"` | Red |
| `"info"` | Blue |

### divider() and gap()

Visual separators inside containers:

```python
from cascadeui import divider, gap

# Thin line separator
self.add_item(divider())

# Larger spacing between content blocks
self.add_item(gap())

# Large spacing variant
self.add_item(gap(large=True))
```

`divider()` is an alias for `Separator(spacing=SeparatorSpacing.small)`. `gap()` creates spacing without a visible line.

### image_section()

A `Section` with a `Thumbnail` image:

```python
from cascadeui import image_section

self.add_item(
    image_section("User avatar", url="https://example.com/avatar.png")
)
```

### gallery()

A `MediaGallery` from a list of image URLs:

```python
from cascadeui import gallery

self.add_item(gallery(["https://example.com/img1.png", "https://example.com/img2.png"]))
```

---

## Utilities

### slugify()

Converts display strings to safe `custom_id` fragments:

```python
from cascadeui import slugify

slugify("Color Roles")    # "color-roles"
slugify("He/Him")         # "hehim"
```

Useful for building stable `custom_id` values in persistent views where the ID must survive restarts:

```python
custom_id=f"roles:{slugify(category)}:{slugify(role_name)}"
```

---

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

### Validation

Pass a `validators` dict mapping `custom_id` to a list of validator functions:

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

All validators from the [validation system](validation.md) work here.

### State Integration

Pass `view_id` to dispatch a `MODAL_SUBMITTED` action to the state store:

```python
modal = Modal(
    title="Edit Name",
    inputs=[TextInput(label="Name")],
    callback=handle_edit,
    view_id=self.id,
)
```

---

## Component Wrappers

Wrappers modify component behavior without changing the component itself. Apply them to buttons to add cross-cutting behavior.

!!! danger "Wrappers consume the interaction response"
    All three wrappers (`with_loading_state`, `with_confirmation`, `with_cooldown`) use `interaction.response` internally to show their UI. Your wrapped callback **must** use `interaction.followup.send()` instead of `interaction.response.send_message()`.

### Loading State

```python
from cascadeui import with_loading_state

button = StatefulButton(label="Process", callback=my_handler)
with_loading_state(button)
```

The button is disabled and its label changes to "Loading..." while the callback runs.

### Confirmation Prompt

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

### Per-User Cooldown

```python
from cascadeui import with_cooldown

button = StatefulButton(label="Claim", callback=handle_claim)
with_cooldown(button, seconds=10)
```

| Scope | Behavior |
|-------|----------|
| `"user"` (default) | Each user has an independent cooldown |
| `"guild"` | Shared cooldown per server |
| `"global"` | One cooldown for everyone |

---

## V1 Composite Components

!!! note "V1 only"
    These components extend `CompositeComponent` and use row-based layout. They work with `StatefulView` but are not compatible with `StatefulLayoutView` (V2). For V2, use the helper functions above or build component trees directly.

### ConfirmationButtons

```python
from cascadeui import ConfirmationButtons

confirmation = ConfirmationButtons(on_confirm=handle_confirm, on_cancel=handle_cancel)
confirmation.add_to_view(my_view)
```

### PaginationControls

```python
from cascadeui import PaginationControls

pagination = PaginationControls(page_count=5, on_page_change=handle_page)
pagination.add_to_view(my_view)
```

### FormLayout

```python
from cascadeui import FormLayout

layout = FormLayout(fields=[
    {"id": "color", "type": "select", "label": "Color",
     "options": ["Red", "Blue", "Green"]},
    {"id": "enabled", "type": "boolean", "label": "Enabled"},
])
layout.add_to_view(my_view)
```

### ToggleGroup

```python
from cascadeui import ToggleGroup

group = ToggleGroup(options=["Easy", "Medium", "Hard"], on_select=handler, default="Medium")
group.add_to_view(my_view)
```

### ProgressBar

A text-based progress bar for embed fields (not a Discord component):

```python
from cascadeui import ProgressBar

bar = ProgressBar(total=100, width=20)
embed.add_field(name="Progress", value=bar.render(65))
# Output: ████████████░░░░░░░░ 65%
```
