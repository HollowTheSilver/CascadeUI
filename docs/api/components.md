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

### `MAX_SELECT_OPTIONS`

```python
MAX_SELECT_OPTIONS = 25
```

Defined in `cascadeui.components.types`, exported from the package root.
Discord's hard cap on the number of options in a single select menu.
`choice_row` enforces it, raising `ValueError` past the cap.

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

### `card(*children, color=None, spoiler=False)`

Creates a `Container` with children and an optional accent color. Strings are automatically wrapped in `TextDisplay`. Pass `spoiler=True` to hide the entire container behind a spoiler overlay.

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

### `action_section(text, *, label, callback, emoji=None, style=secondary, custom_id=None, disabled=False)`

Creates a `Section` with text and a `StatefulButton` accessory. Pass `disabled=True` to render the button greyed out and non-interactive.

```python
action_section(
    "Click to refresh",
    label="Refresh",
    callback=self.refresh,
    emoji="\U0001f504",
)
```

### `toggle_section(text, *, active, callback, labels=("Enabled", "Disabled"), emoji=None, custom_id=None, disabled=False)`

Creates a `Section` with a green/red toggle button. `labels` sets the (active, inactive) button text -- pass `("On", "Off")` to relabel. `emoji` adds a button emoji. Pass `disabled=True` to render the button greyed out and non-interactive.

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

### `image_section(text, *, url, description=None, spoiler=False)`

A `Section` with a `Thumbnail` image accessory. `description` sets the thumbnail's alt text (up to 256 chars); `spoiler=True` hides the thumbnail behind a spoiler. `url` accepts a URL string or a `discord.File` (`MediaInput`).

```python
image_section("User avatar", url="https://example.com/avatar.png")
```

### `link_section(text, *, label, url, emoji=None)`

A `Section` with a link-style `Button` accessory that opens a URL. Completes the `*_section` family (action / image / link); link buttons carry no callback because the platform handles navigation directly.

```python
link_section(
    "Full documentation is on GitHub Pages.",
    label="Open Docs",
    url="https://hollowthesilver.github.io/CascadeUI/",
)
```

### `confirm_section(text, *, on_confirm, on_cancel, confirm_label="Confirm", cancel_label="Cancel", confirm_emoji="✅", cancel_emoji="❌")`

A confirm/cancel prompt. Returns a `[TextDisplay, ActionRow]` list (not a single component) -- the prompt text plus a paired success/danger button row -- so splat it into `card(...)` or add it directly to a view.

```python
card(
    "## Reset settings?",
    *confirm_section(
        "This cannot be undone.",
        on_confirm=self._do_reset,
        on_cancel=self._cancel,
    ),
)
```

### `gallery(*media, descriptions=None)`

A `MediaGallery` from one or more images passed as positional arguments (not a list). Each item is a URL string or a `discord.File` (`MediaInput`); `descriptions` is an optional parallel sequence of alt-text strings.

```python
gallery(
    "https://example.com/img1.png",
    "https://example.com/img2.png",
)
```

### `emoji_grid(rows, cols, *, fill="⬛", row_labels=None, col_labels=None, corner=None, cell_sep=" ")`

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

### `choice_row(options, *, on_select, selected=None, multi=False, disabled=False, button_threshold=5, active_style=primary, inactive_style=secondary, placeholder=None, custom_id="choice")`

A single-select (or multi-select) "choose one/any" control. Renders a segmented button `ActionRow` at or below `button_threshold` options (active = highlighted, and disabled in single-select), or a `StatefulSelect` dropdown for 6-25 options. Raises `ValueError` past 25. `on_select` receives the picked value (single) or the list of selected values (multi); the builder handles the string round-trip Discord forces on select option values, so the callback always gets the real Python value. `disabled=True` greys out the whole control (every button, or the dropdown) for a read-only or locked state.

```python
choice_row(
    {"Easy": Difficulty.EASY, "Hard": Difficulty.HARD},
    selected=self.difficulty,
    on_select=self._set_difficulty,   # async (interaction, value) -> None
)
```

`options` is a `{label: value}` dict or a sequence of `Choice`. `multi=True` makes `selected` a set, turns the buttons into toggles, and delivers a list to `on_select`. `active_style` / `inactive_style` set the button colors (button form only; dropdowns have no per-option style), and `placeholder` sets the dropdown's placeholder text. Two controls in one view need distinct `custom_id=` values. Raises `ValueError` for an empty `options`, more than 25 options, or a `button_threshold` outside 0-5; raises `TypeError` if `on_select` is not callable.

### `Choice`

The rich option form for `choice_row` (a `NamedTuple`). Use it instead of a plain dict entry when an option needs an emoji or a per-option dropdown description.

```python
Choice(label="Goals", value=Event.GOAL, emoji="⚽", description="Match goals")
```

`label` and `value` are required; `emoji` and `description` default to `None`. `description` renders on the dropdown form and is ignored when the control renders as buttons.

### `toggle_button(*, active, on_toggle, labels=("Enabled", "Disabled"), emoji=None)`

A standalone boolean toggle button -- the `ActionRow` form of `toggle_section` (no accompanying text). Renders green when `active`, relabels between the two `labels` on each click, and calls `on_toggle` with the new state.

```python
ActionRow(toggle_button(active=self.notify, on_toggle=self._set_notify))
```

### `cycle_button(*, values, on_change, labels=None, style=secondary, emoji=None, start=0)`

A button that cycles through a fixed list of `values` on each click, advancing (and wrapping) the index before calling `on_change` with the new value. Use it when a setting has three or more options but a full select is overkill -- a single "Preset" button cycling `["Low", "Medium", "High"]` instead of three toggles. `labels` defaults to `str(value)` per entry; `start` is the initial index.

```python
cycle_button(
    values=["Low", "Medium", "High"],
    on_change=self._set_preset,   # async (interaction, value) -> None
)
```

### `tab_nav(tabs, *, active=None, active_style=primary, inactive_style=secondary)`

An `ActionRow` of tab buttons for inner-view navigation -- a lighter alternative to `TabLayoutView`. `tabs` maps each label to a callback; the `active` tab renders in `active_style`, the rest in `inactive_style`.

```python
tab_nav(
    {"Overview": self._show_overview, "Settings": self._show_settings},
    active="Overview",
)
```

### `stats_card(title, stats, *, color=None, footer=None)`

A titled `Container` rendering a `{label: value}` dict as key-value lines, with an optional `footer`. Reads the active theme's `accent_colour` when `color=None`, the same as `card`.

```python
stats_card("Match Stats", {"Goals": 3, "Shots": 11}, footer="Updated live")
```

### `progress_bar(value, max_value, *, width=20, filled="█", empty="░", show_percent=True)`

A text progress bar rendered into a `TextDisplay`. `width` is the bar length in characters; `filled` / `empty` are the cell glyphs; `show_percent` appends the percentage.

```python
progress_bar(7, 10)   # [██████████████░░░░░░] 70%
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

## V2 Composite Components

Stateful helpers that live *inside* a `StatefulLayoutView` and hold their own state across interactions, rather than owning the message (a view) or returning a one-shot tree (a builder).

### `PaginatedRegion`

Pages one slice of a host view's tree while the host owns the rest. The V2 sibling of `PaginationControls`. Each instance holds its own page index, so two regions can live in one view (give them distinct `key` values).

```python
PaginatedRegion(
    *,
    items=None,    # initial item list; or set later via .items =
    per_page=10,   # items per page (positive int); per_page=1 is a carousel
    key="page",    # custom_id disambiguator; distinct per region in one view
)
```

`items` holds the full list, `page_items` exposes the current slice, and `controls(view)` captures the host and returns the nav row. Drive all three from the host's `build_ui()` or `on_load()`:

```python
def build_ui(self):
    self.clear_items()
    self.pager.items = self.tasks
    rows = [action_section(t.title, label="Open", callback=self._open(t))
            for t in self.pager.page_items]
    self.add_item(card("## Tasks", *rows, *self.pager.controls(self)))
```

`controls(view)` returns `[]` on a single page and one nav `ActionRow` otherwise. A click updates the index and re-runs the host's render path -- whichever the host provides: its `build_ui()`, a `TabLayoutView`'s tab refresh, or `reload()` -- so the new slice renders before the refresh. First/last jump buttons and a go-to-page modal appear once the page count reaches `jump_threshold`. Customization mirrors `PaginatedLayoutView`: subclass and override the `{first,prev,indicator,next,last}_button_{label,emoji,style}` class attributes or `jump_threshold`.

#### `control_buttons(view, *, compact=False) -> list`

The button-level counterpart to `controls()`: the same wired prev/next/jump buttons, returned as a bare list instead of an `ActionRow`, so a node-tight host packs them into a row it owns. Mirrors the `make_back_button` (primitive) / `make_nav_row` (wrapper) split -- `controls()` is the convenience wrapper, this is the primitive underneath it. Returns `[]` on a single page.

`compact=True` returns three buttons (prev, go-to-page, next), dropping first/last and forcing the clickable go-to middle. The compact set fuses with Back + Exit inside one five-button `ActionRow` (3 + 2 = 5), the layout a `per_page=1` carousel near the 40-component message cap needs. `compact=` is also accepted on `controls()` for a compact pager in its own row. The full set is up to five buttons and is meant for a row of its own; fusing it with other buttons overflows the per-row budget.

```python
def build_ui(self):
    self.clear_items()
    self.add_item(card("## Fight", *self._render_markets()))
    self.add_item(ActionRow(
        *self.pager.control_buttons(self, compact=True),
        self.make_back_button(),
        self.make_exit_button(),
    ))
```

#### `async on_page_changed(page) -> None`

Override hook. Called after the page index updates, before the refresh. Default is a no-op. Use for analytics, async prefetch, or per-page validation.

#### `set_page(index)`, `page`, `page_count`

`set_page(index)` jumps to a zero-based page index, clamped to the valid range -- the programmatic counterpart to the nav buttons, for resetting to page 1 after a filter change or jumping from outside the region's own controls. `page` reads the current zero-based index; `page_count` reads the total page count (minimum 1). Use them when the host renders a "page X of Y" line or gates a control on the current position.

### `Collapsible(*, label, reveal, summary=None, expanded_label=None, style=secondary, expanded_style=secondary, emoji=None, expanded_emoji=None, expanded=False, trigger_first=True, key="collapsible")`

A trigger button that toggles an inline region of revealed content (the disclosure/expander pattern). Holds its own collapsed/expanded state. `reveal` is a zero-argument synchronous callable returning the revealed component(s); the host loads any async data in `on_load()` and `reveal` reads it synchronously. `expanded_label` and `expanded_emoji` default to `label` and `emoji` -- the trigger keeps its collapsed text and icon while expanded unless you set them.

```python
self.picker = Collapsible(
    label="Edit Leagues",
    expanded_label="Done",
    reveal=lambda: choice_row(LEAGUES, selected=self.league, on_select=self._pick),
)

def build_ui(self):
    self.clear_items()
    for item in self.picker.render(self):
        self.add_item(item)
```

`render(view)` returns `[trigger]` collapsed, or the trigger plus `reveal()` (ordered by `trigger_first`) expanded. A click flips the state, fires `on_toggle`, and re-runs the host's render path (the same `build_ui`/`reload` seam `PaginatedRegion` uses). The trigger relabels/restyles via `expanded_label` / `expanded_style` / `expanded_emoji`. Two collapsibles in one view need distinct `key=` values.

By default the trigger is a bare `ActionRow(button)`. Pass `summary` -- a zero-argument synchronous callable read on every render, like `reveal` -- to fuse the trigger into an `action_section` instead: a Section carrying the summary text with the trigger button as its accessory. This is the shape a card-based disclosure wants, where the Edit button sits beside its summary line rather than in a row of its own. The whole disclosure then splats into one `card(...)`:

```python
self.rep = Collapsible(
    label="Edit", expanded_label="Done", emoji="✏️",
    summary=lambda: f"Flagged beside your name: **{self.represented_name}**.",
    reveal=lambda: ActionRow(self._represented_select()),
    key="representation",
)

def build_ui(self):
    self.clear_items()
    self.add_item(card("### 🏳️ Representation", *self.rep.render(self)))
```

When `summary` returns an empty value (data not loaded yet), the trigger falls back to the bare button rather than emitting an empty Section.

- `expand()` / `collapse()` set the state programmatically; `expanded` reads it.
- The host owns collapse policy: call `collapse()` after a revealed action, or leave it open.

#### `async on_toggle(expanded) -> None`

Override hook. Called after the state flips, before the re-render. Default is a no-op. Use to fetch async data when expanded, log toggle events, or validate on every open/close.

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
