# API: Components

## `StatefulButton`

Extends `discord.ui.Button` with automatic state dispatching.

```python
StatefulButton(
    label=None,
    style=ButtonStyle.secondary,
    custom_id=None,        # Required for PersistentView
    callback=async_fn,     # async def callback(interaction)
    emoji=None,
    disabled=False,
    row=None,
)
```

Every click dispatches a `COMPONENT_INTERACTION` action.

---

## `StatefulSelect`

Extends `discord.ui.Select` with state integration.

```python
StatefulSelect(
    placeholder=None,
    options=[SelectOption(...)],
    callback=async_fn,
    min_values=1,
    max_values=1,
    row=None,
)
```

---

## Composite Components

### `ConfirmationButtons`

```python
ConfirmationButtons(on_confirm=async_fn, on_cancel=async_fn)
buttons.add_to_view(view)
```

### `PaginationControls`

```python
PaginationControls(page_count=int, on_page_change=async_fn)
controls.add_to_view(view)
```

### `FormLayout`

```python
FormLayout(fields=[{"id": str, "type": str, "label": str, ...}])
layout.add_to_view(view)
```

### `ToggleGroup`

```python
ToggleGroup(
    options=["A", "B", "C"],
    on_select=async_fn,
    default="B",           # Optional
)
group.add_to_view(view)
```

---

## Wrappers

All wrappers consume the interaction response internally. The wrapped callback **must** use `interaction.followup` for any messages it sends.

### `with_loading_state(button, loading_label="Loading...", loading_emoji=None)`

Shows a loading indicator while the callback runs. The button is disabled and its label is replaced during execution.

### `with_confirmation(button, title="Confirm Action", message="Are you sure?", ...)`

Adds an ephemeral yes/no prompt before the callback runs. Additional parameters:

- `color` - embed color (default: yellow)
- `confirm_label` / `cancel_label` - button labels
- `confirm_style` / `cancel_style` - button styles
- `confirmed_message` / `cancelled_message` - text shown after choice
- `on_cancel` - optional async callback on cancel
- `timeout` - prompt timeout in seconds (default: 60)

### `with_cooldown(button, seconds=5, message=None, scope="user")`

Enforces a cooldown between clicks. Expired entries are automatically cleaned up.

- `seconds` - cooldown duration
- `message` - custom message (use `{remaining}` for time left)
- `scope` - `"user"` (default), `"guild"`, or `"global"`

---

## Utilities

### `ProgressBar`

```python
bar = ProgressBar(total=100, width=20, fill="█", empty="░")
bar.render(current)  # Returns string like "████████░░░░░░░░░░░░ 40%"
```
