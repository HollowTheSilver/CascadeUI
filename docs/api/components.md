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

Every click dispatches a `COMPONENT_INTERACTION` action. Skips dispatch when the parent view is finished.

---

## `StatefulSelect`

Extends `discord.ui.Select` with state integration.

```python
StatefulSelect(
    placeholder=None,
    options=[SelectOption(...)],
    callback=async_fn,
    custom_id=None,        # Required for PersistentView; recommended in _build_extra_items
    min_values=1,
    max_values=1,
    row=None,
)
```

### Select Variants

- `Dropdown` — alias for `StatefulSelect`
- `RoleSelect` — extends `discord.ui.RoleSelect`
- `ChannelSelect` — extends `discord.ui.ChannelSelect`
- `UserSelect` — extends `discord.ui.UserSelect`
- `MentionableSelect` — extends `discord.ui.MentionableSelect`

---

## `TextInput`

Wraps `discord.ui.TextInput` with a stable `custom_id` derived from the label.

```python
TextInput(
    label=str,               # Required
    placeholder=None,
    default=None,
    required=True,
    min_length=None,
    max_length=None,
    style=TextStyle.short,   # or TextStyle.long for multi-line
)
```

The `custom_id` is auto-generated as `"input_{label}"` (lowercased, spaces replaced with underscores).

---

## `Modal`

Wraps `discord.ui.Modal` with state integration and optional validation.

```python
Modal(
    title=str,               # Required
    inputs=[TextInput(...)], # List of TextInput or discord.ui.TextInput
    callback=async_fn,       # async def callback(interaction, values)
    validators=None,         # Optional: {custom_id: [validator, ...]}
    timeout=None,
    view_id=None,            # If set, dispatches MODAL_SUBMITTED action
)
```

- `validators` — dict mapping `custom_id` to a list of validator functions. On failure, an ephemeral error message is sent and the callback is skipped.
- `view_id` — links the modal to a view's state. A `MODAL_SUBMITTED` action is dispatched before the callback runs.
- If no `callback` is provided, the interaction is deferred automatically.

---

## V2 Helpers

Convenience functions for building V2 component trees. All return standard discord.py V2 components — no custom classes needed. These work inside `StatefulLayoutView` and its subclasses.

### `card(*children, color=None)`

Creates a `Container` with an optional title and accent color.

```python
card(
    "## Title",              # First string becomes a TextDisplay heading
    TextDisplay("Content"),  # Remaining args are child components
    divider(),
    color=discord.Color.blurple(),
)
```

### `key_value(data)`

Converts a dict to a formatted `TextDisplay`.

```python
key_value({"Status": "Online", "Users": "42"})
# Renders: **Status:** Online\n**Users:** 42
```

### `action_section(text, *, label, callback, emoji=None, style=None, custom_id=None)`

Creates a `Section` with text and a `StatefulButton` accessory.

```python
action_section(
    "Click to refresh",
    label="Refresh",
    callback=self.refresh,
    emoji="\U0001f504",
)
```

### `toggle_section(text, *, active, callback, custom_id=None)`

Creates a `Section` with a green/red toggle button.

```python
toggle_section(
    "**Dark Mode**\nEnable dark theme",
    active=self.dark_mode,
    callback=self.toggle_dark,
)
```

### `alert(message, *, level="info")`

A colored status container. Levels: `"success"` (green), `"warning"` (gold), `"error"` (red), `"info"` (blue).

```python
alert("Settings saved!", level="success")
```

### `divider(large=False)`

A `Separator` with `SeparatorSpacing.small` (default) or `SeparatorSpacing.large`.

### `gap(large=False)`

A `Separator` without a visible line. `SeparatorSpacing.small` (default) or `SeparatorSpacing.large`.

### `image_section(text, *, url)`

A `Section` with a `Thumbnail` image accessory.

```python
image_section("User avatar", url="https://example.com/avatar.png")
```

### `gallery(urls)`

A `MediaGallery` from a list of image URLs.

```python
gallery(["https://example.com/img1.png", "https://example.com/img2.png"])
```

---

## Convenience Buttons

Subclasses of `StatefulButton` with preset styles:

- `PrimaryButton` — `ButtonStyle.primary`
- `SecondaryButton` — `ButtonStyle.secondary`
- `SuccessButton` — `ButtonStyle.success`
- `DangerButton` — `ButtonStyle.danger`
- `LinkButton` — `ButtonStyle.link`
- `ToggleButton` — Toggles between two states on click

---

## V1 Composite Components

These extend `CompositeComponent` and use row-based layout. They work with `StatefulView` but are not compatible with V2 views.

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
ToggleGroup(options=["A", "B", "C"], on_select=async_fn, default="B")
group.add_to_view(view)
```

### `ProgressBar`

```python
bar = ProgressBar(total=100, width=20, fill="█", empty="░")
bar.render(current)  # Returns string like "████████░░░░░░░░░░░░ 40%"
```

---

## Wrappers

All wrappers consume the interaction response internally. The wrapped callback **must** use `interaction.followup` for any messages it sends.

### `with_loading_state(button, loading_label="Loading...", loading_emoji=None)`

Shows a loading indicator while the callback runs. The button is disabled and its label is replaced during execution.

### `with_confirmation(button, title="Confirm Action", message="Are you sure?", ...)`

Adds an ephemeral yes/no prompt before the callback runs. Additional parameters:

- `color` — embed color (default: yellow)
- `confirm_label` / `cancel_label` — button labels
- `confirm_style` / `cancel_style` — button styles
- `confirmed_message` / `cancelled_message` — text shown after choice
- `on_cancel` — optional async callback on cancel
- `timeout` — prompt timeout in seconds (default: 60)

### `with_cooldown(button, seconds=5, message=None, scope="user")`

Enforces a cooldown between clicks. Expired entries are automatically cleaned up.

- `seconds` — cooldown duration
- `message` — custom message (use `{remaining}` for time left)
- `scope` — `"user"` (default), `"guild"`, or `"global"`

---

## Utilities

### `slugify(text)`

Converts display strings to safe `custom_id` fragments.

```python
slugify("Color Roles")    # "color-roles"
slugify("He/Him")         # "hehim"
```
