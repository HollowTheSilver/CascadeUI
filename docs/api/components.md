# API: Components

## Type Aliases

### `EmojiInput`

```python
EmojiInput = Optional[Union[str, discord.Emoji, discord.PartialEmoji]]
```

Defined in `cascadeui.components.types`. Used by every `emoji=` parameter
in CascadeUI's typed surface (button builders, pattern ClassVar
attributes, the refresh handoff). Mirrors the union accepted by
`discord.ui.Button`. See [Custom Emoji](../guide/components.md#custom-emoji)
for the three string forms and application emoji setup.

---

## `StatefulButton`

Extends `discord.ui.Button` with automatic state dispatching.

```python
StatefulButton(
    label=None,
    style=ButtonStyle.secondary,
    custom_id=None,        # Required for PersistentView
    callback=async_fn,     # async def callback(interaction)
    owner_only=False,      # When True, only view.user_id can click; non-owner clicks route to view.on_unauthorized
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

### `set_selected(value)`

Sets which options are marked as `default=True`. Accepts:

- `None`, `""`, or an empty iterable -- clears all selections
- A single `str` -- marks the matching option (the single-select common case)
- An iterable of strings -- marks every matching option (for `max_values > 1`)

Values that don't match any existing option are silently ignored, so state-driven rebuilds survive config migrations that drop enum variants.

### `get_selected() -> list[str]`

Returns a `list[str]` of all option values currently marked `default=True`, matching discord.py's `Select.values` always-list convention.

### Two-Parameter Callbacks

`StatefulSelect` callbacks may accept an optional second positional parameter `values`:

```python
async def on_select(interaction, values):
    selected = values[0]  # list[str] of selected option values
    ...
```

Detection happens at creation time via `inspect.signature`. Old single-parameter callbacks (`async def cb(interaction)`) still work -- only callbacks declaring 2+ positional parameters receive `values`.

### Select Variants

- `Dropdown` -- alias for `StatefulSelect`
- `RoleSelect` -- extends `discord.ui.RoleSelect`
- `ChannelSelect` -- extends `discord.ui.ChannelSelect`
- `UserSelect` -- extends `discord.ui.UserSelect`
- `MentionableSelect` -- extends `discord.ui.MentionableSelect`

### Pre-populated defaults (specialized selects)

`RoleSelect`, `UserSelect`, `ChannelSelect`, and `MentionableSelect` accept a `default_values=` constructor kwarg and a `set_default_values(values)` method. CascadeUI coerces input to discord.py's `SelectDefaultValue` shape with the right `type` per select class.

```python
RoleSelect(default_values=[123456789, role_obj])           # auto-typed 'role'
UserSelect(default_values=[member_obj])                    # auto-typed 'user'
ChannelSelect(default_values=[channel_id])                 # auto-typed 'channel'
MentionableSelect(default_values=[member_obj, role_obj])   # type inferred per object
```

Accepted input per entry:

- Raw `int` IDs (RoleSelect / UserSelect / ChannelSelect only -- `MentionableSelect` rejects bare ints because the type cannot be inferred).
- Discord.py objects with `.id` attributes (Member, User, Role, GuildChannel).
- Pre-built `discord.SelectDefaultValue` instances (passed through unchanged).

`set_default_values(values)` replaces the current list. Pass `None` or `[]` to clear.

---

## `DynamicPersistentButton`

Extends `discord.ui.DynamicItem[discord.ui.Button]` for persistent
buttons whose handler depends only on IDs encoded in the
`custom_id`. No view-level state involved; each click re-instantiates
the class from the matched `custom_id`.

```python
class RoleToggleButton(
    DynamicPersistentButton,
    template=r"roles:(?P<category>[a-z_]+):(?P<role_id>[0-9]+)",
):
    def __init__(self, *, category: str, role_id: int):
        button = discord.ui.Button(
            label=f"Toggle {category}",
            custom_id=f"roles:{category}:{role_id}",
            style=discord.ButtonStyle.primary,
        )
        super().__init__(button)
        self.category = category
        self.role_id = role_id

    async def on_click(self, interaction):
        ...
```

`discord.py` requires `template=` on every subclass at class-
definition time; abstract intermediate bases are not supported.

### `on_click(interaction) -> None`

Override hook for click handling. Default: no-op. Captured values
from the `custom_id` template are available as instance attributes
set by the subclass `__init__`.

### `from_custom_id(cls, interaction, item, match) -> cls` (classmethod)

Default reconstructs the instance from a matched `custom_id`.
Extracts `match.groupdict()`, coerces any capture named `user_id`,
`guild_id`, `channel_id`, `role_id`, or `message_id` to `int`, and
calls `cls(**captures)`. Override when the subclass needs custom
extraction (non-snowflake coercion, combined keys, lookup-based
restoration).

### Auto-registration

Every subclass declaring a `template=` registers into a module-level
registry at class-definition time. `setup_middleware(
PersistenceMiddleware(..., bot=bot))` then calls
`bot.add_dynamic_items(*subclasses)` once during initialization, so
every click routes correctly after a restart with no additional
user setup.

---

## `TextInput`

Wraps `discord.ui.TextInput` with a stable `custom_id` derived from the label. Renders inside a `Modal` as a `discord.ui.Label` containing the inner `discord.ui.TextInput`.

```python
TextInput(
    label=str,               # Required; renders as ui.Label.text
    description=None,        # Optional: ui.Label.description (helper text)
    placeholder=None,
    default=None,
    required=True,
    min_length=None,
    max_length=None,
    style=TextStyle.short,   # or TextStyle.long for multi-line
    validators=None,         # Optional: list of validator functions
)
```

The `custom_id` is auto-generated as `"input_{label}"` (lowercased, spaces replaced with underscores). Use `TextInput._slug(label)` to reproduce the same transformation externally.

`description=` populates `ui.Label.description` for an optional secondary helper line beneath the title. Available on every wrapped input type (Checkbox, CheckboxGroup, RadioGroup, FileUpload all accept the same kwarg).

`validators` attaches a list of validator functions directly to the input. `Modal` auto-collects them at construction time, keyed by each input's `custom_id`. This is the canonical attachment shape -- there is no separate modal-level validators dict.

---

## `Modal`

Wraps `discord.ui.Modal` with state integration and automatic validator collection.

```python
Modal(
    title=str,               # Required
    inputs=[...],            # TextInput, Checkbox, CheckboxGroup, RadioGroup, FileUpload
    callback=async_fn,       # async def callback(interaction, values)
    timeout=None,
    view_id=None,            # If set, dispatches MODAL_SUBMITTED action
)
```

- `inputs` accepts any combination of CascadeUI input wrappers (`TextInput`, `Checkbox`, `CheckboxGroup`, `RadioGroup`, `FileUpload`) or raw `discord.ui.TextInput` instances.
- Validators are read from each input's `validators` list and collected internally. On failure, an ephemeral error message is sent and the callback is skipped.
- `view_id` -- links the modal to a view's state. A `MODAL_SUBMITTED` action is dispatched before the callback runs.
- If no `callback` is provided, the interaction is deferred automatically.

**Opening modals from CascadeUI callbacks:** use [`self.open_modal(interaction, modal)`](views.md#open_modal) instead of `interaction.response.send_modal()`. It handles the case where auto-defer has already consumed the response slot by sending an ephemeral fallback.

---

## `Checkbox`

Wraps `discord.ui.Checkbox` with a stable `custom_id` derived from the label.

```python
Checkbox(
    label=str,               # Required
    default=False,
    validators=None,
    description=None,        # Optional: ui.Label.description
)
```

After submit: `.value` -> `bool`.

---

## `CheckboxGroup`

Wraps `discord.ui.CheckboxGroup` with stable `custom_id` and dict shorthand for options.

```python
CheckboxGroup(
    label=str,               # Required
    options=[{"label": str, "value": str, "default": bool}, ...],
    min_values=0,
    max_values=None,         # Defaults to len(options)
    validators=None,
    description=None,        # Optional: ui.Label.description
)
```

Options accept dict shorthand or native `discord.CheckboxGroupOption` instances.
After submit: `.values` -> `list[str]`.

---

## `RadioGroup`

Wraps `discord.ui.RadioGroup` with stable `custom_id` and dict shorthand for options.

```python
RadioGroup(
    label=str,               # Required
    options=[{"label": str, "value": str, "default": bool}, ...],
    validators=None,
    description=None,        # Optional: ui.Label.description
)
```

After submit: `.value` -> `str`.

---

## `FileUpload`

Wraps `discord.ui.FileUpload` with stable `custom_id`.

```python
FileUpload(
    label=str,               # Required
    max_values=10,
    validators=None,
    description=None,        # Optional: ui.Label.description
)
```

After submit: `.values` -> `list[discord.Attachment]`.

!!! warning "Ephemeral attachment URLs"
    `discord.Attachment` URLs expire. Read attachment data in the modal
    callback -- do not store attachments in the state store.

---

## V2 Helpers

Convenience functions for building V2 component trees. All return standard discord.py V2 components -- no custom classes needed. These work inside `StatefulLayoutView` and its subclasses.

### `card(*children, color=None)`

Creates a `Container` with children and an optional accent color. Strings are automatically wrapped in `TextDisplay`.

```python
card(
    "## Title",              # Strings become TextDisplay automatically
    TextDisplay("Content"),  # V2 components pass through as-is
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

### `emoji_grid(rows, cols, *, fill, row_labels=None, col_labels=None, corner=None, cell_sep=" ")`

Returns an `EmojiGrid` -- a live subclass of `discord.ui.TextDisplay` that renders a rectangular cell grid with optional axis labels. Supports assignment by int index, `(row, col)` tuple, or iterable of keys. Provides `fill_rect(top_left, bottom_right, value)` and `clear()`. Plugs directly into `card()` and `Container()`.

Axis label presets: `"alpha"` (regional indicator glyphs, max 26), `"numeric"` (keycap emoji, max 10). Pass a list of custom emoji for other label styles.

```python
grid = emoji_grid(10, 10, fill="🟦", row_labels="alpha", col_labels="numeric")
grid[3, 5] = "💥"  # Hit at row 3, column 5
```

### `button_grid(rows, cols, cell_factory)`

Packs a `(row, col) -> Button` factory into a list of `ActionRow` components, enforcing Discord's 5x5 LayoutView component limit.

```python
rows = button_grid(3, 3, lambda r, c: StatefulButton(
    label=board[r][c], callback=self.on_cell_click,
))
for row in rows:
    self.add_item(row)
```

---

## Convenience Buttons

Subclasses of `StatefulButton` with preset styles:

- `PrimaryButton` -- `ButtonStyle.primary`
- `SecondaryButton` -- `ButtonStyle.secondary`
- `SuccessButton` -- `ButtonStyle.success`
- `DangerButton` -- `ButtonStyle.danger`
- `LinkButton` -- `ButtonStyle.link`
- `ToggleButton` -- Toggles between two states on click

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

All wrappers attempt to use `interaction.response` internally, with an `is_done()` fallback for auto-defer compatibility. Wrapped callbacks should use `self.respond(interaction, ...)` for any replies -- it handles the response/followup routing automatically.

### `with_loading_state(button, loading_label="Loading...", loading_emoji=None)`

Shows a loading indicator while the callback runs. The button is disabled and its label is replaced during execution.

### `with_confirmation(button, title="Confirm Action", message="Are you sure?", ...)`

Adds an ephemeral yes/no prompt before the callback runs. Additional parameters:

- `color` -- embed color (default: yellow)
- `confirm_label` / `cancel_label` -- button labels
- `confirm_style` / `cancel_style` -- button styles
- `confirmed_message` / `cancelled_message` -- text shown after choice
- `on_cancel` -- optional async callback on cancel
- `timeout` -- prompt timeout in seconds (default: 60)

### `with_cooldown(button, seconds=5, message=None, scope="user")`

Enforces a cooldown between clicks. Expired entries are automatically cleaned up.

- `seconds` -- cooldown duration
- `message` -- custom message (use `{remaining}` for time left)
- `scope` -- `"user"` (default), `"guild"`, or `"global"`

---

## Utilities

### `slugify(text)`

Converts display strings to safe `custom_id` fragments.

```python
slugify("Color Roles")    # "color_roles"
slugify("He/Him")         # "he_him"
slugify("Tickets #1")     # "tickets_1"
```

### `@cascade_component(component_id=None)`

Decorator for registering a component callback in the shared component registry. Pair with `get_component(component_id)` to retrieve registered callbacks.

```python
from cascadeui import cascade_component, get_component

@cascade_component("reroll")
async def reroll_callback(interaction):
    ...

callback = get_component("reroll")
```

When `component_id` is omitted, the decorated function's `__name__` is used.
