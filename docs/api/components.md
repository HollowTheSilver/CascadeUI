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

### `with_loading_state(button)`

Shows a loading indicator while the callback runs. The callback should use `interaction.followup` for messages.

### `with_confirmation(button, message="Are you sure?")`

Adds a yes/no prompt before the callback runs. The fresh confirmation interaction is passed to the callback.

### `with_cooldown(button, seconds=5)`

Enforces a per-user cooldown between clicks.

---

## Utilities

### `ProgressBar`

```python
bar = ProgressBar(total=100, width=20, fill="█", empty="░")
bar.render(current)  # Returns string like "████████░░░░░░░░░░░░ 40%"
```
