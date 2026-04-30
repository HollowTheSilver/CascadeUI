# View Patterns

CascadeUI ships seven pre-built view patterns -- Menu, Form, Wizard, Tab,
Paginated, Leaderboard, and Roles. The first five have V1 (`StatefulView`-based)
and V2 (`StatefulLayoutView`-based) variants; Leaderboard and Roles are
V2-only. The patterns handle navigation, validation, and content rebuilding
internally; subclasses define content and hooks.

All patterns follow the same customization grammar:

- **Class-attribute triples** (`*_button_label`, `*_button_emoji`,
  `*_button_style`) control the appearance of built-in buttons.
- **Method hooks** (`on_finish`, `on_submit`, `on_tab_switched`,
  `on_page_changed`, `on_category_selected`) run at lifecycle points.
  Override to customize behavior.
- **`_build_extra_items()`** is a hook called once during init to
  register components that persist across content changes.

---

## MenuView / MenuLayoutView

Category-based navigation hub with push/pop drill-down. Each category
generates a button (V1) or `action_section()` (V2) that pushes to a
target view class. The pattern eliminates the repetitive `go_*` callback
methods that every hub view would otherwise need.

### Category Definitions

Categories are passed as a list of dicts to the `categories` constructor
parameter:

=== "V2"

    ```python
    from cascadeui import MenuLayoutView, card, key_value
    from discord.ui import TextDisplay

    class SettingsMenu(MenuLayoutView):
        instance_limit = 1
        instance_scope = "user_guild"

        def __init__(self, *args, **kwargs):
            categories = [
                {
                    "label": "Appearance",
                    "emoji": "\N{ARTIST PALETTE}",
                    "description": "Customize theme and accent colors",
                    "view": AppearanceView,
                },
                {
                    "label": "Notifications",
                    "emoji": "\N{BELL}",
                    "description": "Configure DM, mention, and event alerts",
                    "view": NotificationsView,
                },
            ]
            super().__init__(*args, categories=categories, **kwargs)

        def _build_header(self):
            return [card("## Settings", key_value(self._get_summary()))]
    ```

=== "V1"

    ```python
    from cascadeui import MenuView
    import discord

    class SettingsMenu(MenuView):
        instance_limit = 1
        instance_scope = "user_guild"

        def __init__(self, *args, **kwargs):
            categories = [
                {"label": "Appearance", "emoji": "\N{ARTIST PALETTE}",
                 "view": AppearanceView},
                {"label": "Notifications", "emoji": "\N{BELL}",
                 "view": NotificationsView},
            ]
            super().__init__(*args, categories=categories, **kwargs)

        def build_embed(self):
            return discord.Embed(title="Settings", description="Choose a category.")
    ```

| Category Key | Required | Description |
|-------------|----------|-------------|
| `label` | Yes | Button label |
| `view` | Yes | View class to push to |
| `emoji` | No | Button/section emoji |
| `description` | No | V2 only -- text displayed in the `action_section` |
| `style` | No | Per-category `ButtonStyle` override (falls back to `menu_style`) |
| `rebuild` | No | Per-category rebuild callable for `push(rebuild=...)` |

### Default Rebuild Behavior

Each category button pushes to its target view class with a default
`rebuild` callable:

- **V2:** `lambda v: v.build_ui()` -- rebuilds the component tree
- **V1:** `lambda v: {"embed": v.build_embed()}` -- rebuilds the embed

Override per category with the `"rebuild"` key when a sub-view needs
different initialization:

```python
{"label": "Stats", "view": StatsView,
 "rebuild": lambda v: v.load_and_build()}
```

### Customization

| Attribute | Default | Controls |
|-----------|---------|----------|
| `menu_style` | `primary` | Default button style for all category items |
| `auto_exit_button` | `True` | Whether an exit button is added automatically |

### Override Hooks

**`on_category_selected(category, index, interaction)`** -- fires before
the push. Default is a no-op. Override for analytics, pre-push guards,
or conditional setup:

```python
async def on_category_selected(self, category, index, interaction):
    await self.dispatch("MENU_NAVIGATE", {"target": category["label"]})
```

**`_build_header()` / `_build_footer()`** (V2 only) -- return V2
components for areas above and below the category list:

```python
def _build_header(self):
    return [card("## Dashboard", key_value(self.summary_data))]

def _build_footer(self):
    return [TextDisplay("-# Session limited: one panel per user.")]
```

**`_build_category_item(category, index)`** (V2) /
**`_build_category_button(category, index)`** (V1) -- control how a
single category is rendered. Override to customize layout per category.

**`_build_extra_items()`** (V1 only) -- add components alongside
category buttons (e.g. a Reset All button on a later row).

**`build_embed()`** (V1 only) -- the embed displayed alongside category
buttons. Default returns a minimal "Menu" embed. Override to show a
summary card.

### V1 vs V2

- **V1 (`MenuView`):** Category buttons with `build_embed()` for the
  hub card. `_build_extra_items()` adds controls alongside buttons.
- **V2 (`MenuLayoutView`):** `action_section()` items with inline
  descriptions. `_build_header()` and `_build_footer()` add content
  above and below.

---

## FormView / FormLayoutView

Collect structured input through select menus, boolean toggles, and
text fields (via modal). Text fields are grouped into a single
`Modal` -- Discord limits modals to 5 text inputs, enforced at
construction time.

### Field Definitions

Fields are passed as a list of dicts to the `fields` constructor parameter:

```python
fields = [
    {
        "id": "name",
        "label": "Character Name",
        "type": "text",
        "required": True,
        "placeholder": "Enter a name...",
        "validators": [min_length(3), max_length(20)],
    },
    {
        "id": "class",
        "label": "Class",
        "type": "select",
        "options": [
            {"label": "Warrior", "value": "warrior"},
            {"label": "Mage", "value": "mage"},
        ],
    },
    {
        "id": "pvp",
        "label": "Enable PvP",
        "type": "boolean",
    },
]
```

| Field Key | Required | Description |
|-----------|----------|-------------|
| `id` | Yes | Unique identifier, used as key in `self.values` |
| `label` | No | Display label (defaults to `id`) |
| `type` | Yes | `"text"`, `"integer"`, `"float"`, `"date"`, `"boolean"`, `"select"`, or `"multi_select"` |
| `required` | No | Whether the field must be filled before submit |
| `options` | Select only | List of `{"label", "value"}` dicts |
| `placeholder` | No | Placeholder text for selects and text inputs |
| `validators` | No | List of validator callables (see [Validation](../api/validation.md)) |
| `default` | Text only | Pre-filled value for the text input |
| `style` | Text only | `discord.TextStyle.short` (default) or `.long` |
| `min_length` / `max_length` | Text only | Character limits on text input |
| `min_value` / `max_value` | Numeric only | Range limits on `integer` and `float` fields |
| `group` | No | Field-group label (see [Field Groups](#field-groups)) |

### Typed schemas (`FormField` / `FormSchema`)

The dict API stays valid. The typed alternative gives IDE auto-complete
and class-definition-time validation: a typo in `type="interger"` raises
`ValueError` at construction rather than silently at first click.

```python
from cascadeui import FormField, FormSchema

class ProfileSchema(FormSchema):
    def get_fields(self):
        return [
            FormField(id="name", label="Your name", required=True),
            FormField(id="age", label="Your age", type="integer", min_value=0),
        ]

# Either schema= or fields= is accepted; passing both raises ValueError.
FormLayoutView(schema=ProfileSchema())

# Direct FormField list (no schema wrapper) also works.
FormLayoutView(fields=[FormField(id="name", label="Your name")])
```

`FormField.to_dict()` lowers to the same dict shape the pattern has always
consumed, so every helper (`_collect_modal_fields`, validators, parsers)
operates on one canonical representation regardless of input flavor.

### Field Groups {#field-groups}

Fields that share a `group` label render together in a visual cluster.
In V2, each group becomes its own `card()`; in V1 the groups are joined
by blank lines inside the status embed.

```python
fields = [
    {"id": "name", "label": "Name", "type": "text", "group": "Identity"},
    {"id": "pronoun", "label": "Pronouns", "type": "select",
     "options": [...], "group": "Identity"},
    {"id": "email", "label": "Email", "type": "text", "group": "Contact"},
]
```

Fields without a `group` key render ungrouped at the top of the form.

### Inline validation errors {#inline-validation}

Form validation errors are rendered inside the form rather than as
ephemeral responses. Two attributes hold error state:

| Attribute | Shape | Rendered where |
|-----------|-------|----------------|
| `_field_errors` | `dict[str, str]` | Under the offending field (V2: red `alert()`; V1: inline in the embed) |
| `_form_error` | `Optional[str]` | At the top of the form for cross-field errors |

Validators populate `_field_errors` automatically on submit. To set a
form-level error from custom code:

```python
async def on_submit(self, interaction, values):
    if values["start"] > values["end"]:
        self._form_error = "Start date must be before end date."
        await self.refresh()
        return
    ...
```

Submit is short-circuited while either attribute is non-empty.

### `on_field_changed(field_id, value)`

Fires after a field value changes (select choice, toggle flip, modal
submit write-back). The hook is fire-and-forget and does not block the
state rebuild:

```python
async def on_field_changed(self, field_id, value):
    if field_id == "class" and value == "mage":
        self.values["weapon"] = "staff"
```

Use it for dependent-field updates, analytics, or auto-save. Exceptions
inside the hook are logged and swallowed.

### Text Field Handling

Text fields cannot render inline -- Discord restricts `TextInput` to
modals. The form creates a grouped "Edit Text Fields" button that opens
a single `Modal` containing all text fields. When exactly one text field
exists, the button label auto-adapts to "Edit {label}".

Values entered in the modal are always written back to `self.values`,
even when validation fails. Reopening the modal shows previously entered
text, not stale defaults.

### Customization

| Attribute | Default | Controls |
|-----------|---------|----------|
| `text_edit_button_label` | `None` (auto) | Label for the text-edit modal button |
| `text_edit_button_emoji` | `"✏️"` | Emoji on the text-edit button |
| `text_edit_button_style` | `secondary` | Style of the text-edit button |

### `on_submit(interaction, values)`

Called when the user clicks Submit and all validators pass. Override
to persist form data, send a receipt, or transition to another view:

```python
class RegistrationForm(FormLayoutView):
    async def on_submit(self, interaction, values):
        await self.respond(
            interaction,
            f"Welcome, {values['name']}!",
            ephemeral=True,
        )
        await self.exit()
```

The default implementation sends a generic confirmation. After
`on_submit` returns, the view auto-exits unless `on_submit` already
called `exit()`, `push()`, or `replace()`.

### V1 vs V2

- **V1 (`FormView`):** Displays field status in an embed. Controls use
  row-based layout.
- **V2 (`FormLayoutView`):** Displays field status in a `Container` with
  `TextDisplay`. Controls wrapped in `ActionRow`. Full immediate-mode
  rebuild on every value change -- select `default` states are preserved
  across rebuilds via `SelectOption(default=...)`.

---

## WizardView / WizardLayoutView

Multi-step form with back/next navigation and per-step validation.

### Step Definitions

Steps are passed as a list of dicts to the `steps` constructor parameter:

```python
class SetupWizard(WizardLayoutView):
    finish_button_label = "Create Character"
    finish_button_emoji = "🎲"

    def __init__(self, *args, **kwargs):
        steps = [
            {"name": "Welcome", "builder": self.build_welcome},
            {"name": "Config", "builder": self.build_config,
             "validator": self.validate_config},
            {"name": "Confirm", "builder": self.build_confirm},
        ]
        super().__init__(*args, steps=steps, **kwargs)
```

| Step Key | Required | Description |
|----------|----------|-------------|
| `name` | Yes | Step display name (used in indicator) |
| `builder` | Yes | Async callable returning content (embed for V1, component list for V2) |
| `validator` | No | Async callable returning `(valid: bool, error: str)` -- gates the Next button |
| `condition` | No | Callable `(view) -> bool` -- step is skipped when it returns `False` |

When a validator returns `(False, "error message")`, the error is shown
as an ephemeral message and the wizard stays on the current step.

#### Conditional steps

The `condition` key gates step visibility on runtime view state. The
callable receives the view and returns `True` to include the step or
`False` to skip it. Skipped steps are hidden from navigation and the
step indicator, and step numbering re-flows automatically.

```python
steps = [
    {"name": "Basics", "builder": self.build_basics},
    {"name": "Advanced", "builder": self.build_advanced,
     "condition": lambda v: v.enable_advanced},
    {"name": "Confirm", "builder": self.build_confirm},
]
```

Condition callables are evaluated on every navigation, so toggling
`enable_advanced` mid-wizard updates the flow immediately.

### Typed schemas (`WizardStep` / `WizardSchema`)

Same pattern as the form side. Dict API stays valid; the typed variant
catches a missing `builder` or a non-callable `validator` at construction.

```python
from cascadeui import WizardStep, WizardSchema

class SetupSchema(WizardSchema):
    def __init__(self, view):
        self.view = view

    def get_steps(self):
        return [
            WizardStep(name="Welcome", builder=self.view.build_welcome),
            WizardStep(name="Config", builder=self.view.build_config,
                       validator=self.view.validate_config),
            WizardStep(name="Confirm", builder=self.view.build_confirm),
        ]

class SetupWizard(WizardLayoutView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, schema=SetupSchema(self), **kwargs)
```

The schema holds no wizard state -- it is a recipe for step construction.
Per-step values live on the view as they always have.

### Customization

| Attribute | Default | Controls |
|-----------|---------|----------|
| `back_button_label` | `"Back"` | Label for the Back button |
| `back_button_emoji` | `None` | Emoji on the Back button |
| `back_button_style` | `secondary` | Style of the Back button |
| `next_button_label` | `"Next"` | Label for the Next button |
| `next_button_emoji` | `None` | Emoji on the Next button |
| `next_button_style` | `primary` | Style of the Next button |
| `finish_button_label` | `"Finish"` | Label on the last step's button |
| `finish_button_emoji` | `None` | Emoji on the Finish button |
| `finish_button_style` | `success` | Style of the Finish button |
| `step_indicator_label` | `None` | `Callable(current, total) -> str` for custom indicator |
| `show_progress_bar` | `False` | V2 only -- when `True`, renders a progress header above the step content |

The step indicator defaults to `"Step {n}/{total}"`. Pass a callable
for custom formatting:

```python
class MyWizard(WizardLayoutView):
    step_indicator_label = lambda current, total: f"Phase {current} of {total}"
```

### Progress header (V2)

With `show_progress_bar = True`, `WizardLayoutView` renders a progress
header inside a `card()` above the step content. The default header
uses the step indicator label plus a proportional progress bar.

Override `_build_progress_header()` to customize the header component:

```python
class SetupWizard(WizardLayoutView):
    show_progress_bar = True

    def _build_progress_header(self):
        return card(
            f"## {self.step_indicator_label(self.current_step + 1, self.step_count)}",
            progress_bar(self.current_step + 1, self.step_count),
        )
```

### Lifecycle hooks

`WizardView` and `WizardLayoutView` expose three navigation hooks for
reacting to step transitions and validation outcomes:

| Hook | Fires |
|------|-------|
| `on_step_entered(step_index)` | After a step becomes active (initial send, next, back) |
| `on_step_exited(step_index)` | Before leaving a step (next or back) |
| `on_validation_failed(step_index, error)` | When the current step's validator returns `(False, error)` |

```python
async def on_step_entered(self, step_index):
    await self.analytics.log("wizard_step_entered", step=step_index)

async def on_validation_failed(self, step_index, error):
    self.failed_attempts += 1
```

Hooks are fire-and-forget -- exceptions raised inside them are logged
but do not block navigation.

### `on_finish(interaction)`

Called when the user clicks Finish (or Next on the last step). The
default implementation defers and exits. Override to persist wizard
state or transition:

```python
async def on_finish(self, interaction):
    await self.respond(interaction, "Setup complete!")
    await self.exit()
```

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `current_step` | `int` | Zero-based index of the active step |
| `step_count` | `int` | Total number of steps |

### V1 vs V2

- **V1 (`WizardView`):** Step builders return `discord.Embed`. Nav
  buttons placed on row 4.
- **V2 (`WizardLayoutView`):** Step builders return a list of V2
  components (or a single component). Nav buttons placed in an
  `ActionRow`. `send()` is overridden to build the first step's
  content before sending, since async builders cannot run in `__init__`.

---

## TabView / TabLayoutView

Tabbed interface with button-based tab switching.

### Tab Definitions

Tabs are passed as a dict mapping names to async builder functions:

```python
class DashboardView(TabLayoutView):
    active_tab_style = discord.ButtonStyle.success

    def __init__(self, *args, **kwargs):
        tabs = {
            "Overview": self.build_overview,
            "Settings": self.build_settings,
            "History": self.build_history,
        }
        super().__init__(*args, tabs=tabs, **kwargs)

    async def build_overview(self):
        return [card("## Dashboard", key_value(self.stats))]
```

### Customization

| Attribute | Default | Controls |
|-----------|---------|----------|
| `active_tab_style` | `primary` | Style of the currently selected tab button |
| `inactive_tab_style` | `secondary` | Style of unselected tab buttons |
| `tab_overflow_policy` | `"fill"` | How tab buttons distribute across ActionRows when more than five tabs are present. Accepts `"fill"` (pack greedily, five-per-row), `"balance"` (spread evenly), `"pin_first"` (first tab always alone on row 0), `"pin_last"` (last tab always alone on the final row), or a `tuple[int, ...]` naming the button count per row. Validated at class-definition time. Tuple-vs-button-count drift produces a runtime warning and auto-adjusts. |

### `on_tab_switched(index)`

Called after `_active_tab` is updated, before the content refresh.
Default is a no-op. Override for analytics, async setup, or validation:

```python
async def on_tab_switched(self, index):
    if self._tab_names[index] == "History":
        await self._load_history()
```

### Properties and Methods

| Member | Type | Description |
|--------|------|-------------|
| `active_tab` | `str` (property) | Name of the currently active tab |
| `switch_tab(name)` | async method | Switch to a tab by name programmatically |

`switch_tab()` raises `ValueError` if the tab name is not found.

### `_build_tab_rows(buttons)`

Override hook called during tab-row construction. Receives the list of
tab `StatefulButton` objects and returns `list[list[StatefulButton]]` --
each inner list becomes one row. The default implementation applies
`tab_overflow_policy`. Override when the preset strategies and the
tuple form do not match a bespoke layout:

```python
def _build_tab_rows(self, buttons):
    # Pin the first two tabs together, rest on a second row
    return [buttons[:2], buttons[2:]]
```

### V1 vs V2

- **V1 (`TabView`):** Tab builders return `discord.Embed`. Tab buttons
  distribute across rows via `tab_overflow_policy`; `button.row` is
  assigned from the row index.
- **V2 (`TabLayoutView`):** Tab builders return a list of V2 components.
  Each row produced by `_build_tab_rows` is wrapped in its own
  `ActionRow` before being added to the layout.

---

## PaginatedView / PaginatedLayoutView

Navigate through multi-page content with built-in first/prev/next/last
buttons.

### Page Data

Pages are passed to the `pages` constructor parameter. Accepted formats
differ by version:

**V1 pages:**

- `discord.Embed` objects
- `str` for plain text content
- `dict` with `"embed"` and/or `"content"` keys

**V2 pages:**

- A list of V2 components (`Container`, `TextDisplay`, etc.)
- A callable (sync or async) returning a list of V2 components
- `str` (auto-wrapped in `Container(TextDisplay(...))`)

### `from_data()` Classmethod

Auto-paginate a list of items:

```python
def format_users(chunk):
    return discord.Embed(description="\n".join(u.name for u in chunk))

view = await PaginatedView.from_data(
    items=all_users,
    per_page=10,
    formatter=format_users,
    context=ctx,
)
await view.send()
```

The formatter can be sync or async. Views created via `from_data()`
support `refresh_data(items)` -- re-paginates with new data using the
original `per_page` and `formatter`, clamps the page cursor, and
refreshes the message.

### `from_cursor()` Classmethod

For large datasets where loading every item into memory is wasteful
(database rows, API results), cursor mode fetches one page at a time
on demand:

```python
async def fetch_users(offset: int, limit: int) -> list[dict]:
    return await db.fetch(
        "SELECT id, name FROM users ORDER BY name LIMIT $1 OFFSET $2",
        limit, offset,
    )

total = await db.fetchval("SELECT count(*) FROM users")

view = PaginatedView.from_cursor(
    fetch_users,
    total=total,
    per_page=10,
    formatter=format_users_page,
)
await view.send()
```

`fetch_fn(offset, limit)` matches SQL / REST / Firestore idioms so
typical backends drop in unchanged. Pages load lazily as the caller
navigates; up to `cache_size` (default 10) recent pages stay
resident, evicted in LRU order. The page currently displayed is
never evicted -- revisiting it always avoids a refetch.

`total` is required because the `Page N/M` indicator, the goto
modal, and the first/last jump buttons all need the total page
count at construction time. Query it alongside the first page
fetch; it is cheap for most backends.

Use `refresh_pages()` when page contents change but the row count
did not (the cache flushes; the current page refetches for
immediate display). Use `refresh_pages(new_total=N)` when rows
were inserted or deleted -- the pages list resizes, jump buttons
rebuild against the new total, and `current_page` clamps to the
last valid index when the list shrinks.

`refresh_data()` and `refresh_pages()` are mode-exclusive: eager
views raise `RuntimeError` on `refresh_pages()`, cursor views
raise on `refresh_data()`. The error messages name the correct
method for the view's construction mode.

### Customization

Each navigation button exposes a `{label, emoji, style}` triple:

| Attribute | Default | Controls |
|-----------|---------|----------|
| `first_button_label` | `"⏮"` | First-page jump button |
| `first_button_emoji` | `None` | |
| `first_button_style` | `secondary` | |
| `prev_button_label` | `"◀"` | Previous page button |
| `prev_button_emoji` | `None` | |
| `prev_button_style` | `secondary` | |
| `indicator_button_label` | `None` (auto) | Page indicator / go-to button |
| `indicator_button_emoji` | `None` | |
| `indicator_button_style` | `primary` | |
| `next_button_label` | `"▶"` | Next page button |
| `next_button_emoji` | `None` | |
| `next_button_style` | `secondary` | |
| `last_button_label` | `"⏭"` | Last-page jump button |
| `last_button_emoji` | `None` | |
| `last_button_style` | `secondary` | |
| `jump_threshold` | `5` | Minimum page count at which first/last and go-to appear |

When the page count reaches `jump_threshold` or above, three extra
controls appear: first-page and last-page jump buttons, and a
go-to-page modal triggered by clicking the page indicator.

### `on_page_changed(page)`

Called after `current_page` updates, before the refresh. Default is a
no-op. Override for analytics, prefetch, or per-page validation:

```python
async def on_page_changed(self, page):
    await self.dispatch("PAGE_VIEWED", {"page": page})
```

### `_build_extra_items()`

Hook for adding components below the navigation buttons. Called once
during init. Items added here are preserved across page turns:

```python
def _build_extra_items(self):
    self.add_item(ActionRow(
        StatefulButton(label="Refresh", callback=self.reload),
    ))
```

### V1 vs V2

- **V1 (`PaginatedView`):** All nav buttons placed on row 0. Content
  displayed via embed/content kwargs to `refresh()`. `_extract_page()`
  omits absent keys so `message.edit` does not clear existing fields.
- **V2 (`PaginatedLayoutView`):** Nav buttons in a single `ActionRow`.
  Page content is a list of V2 components that replace the view's
  children on each page turn. The nav row and extra items keep their
  identity across page changes.

### `nav_inside_container`

V2-only class attribute on `PaginatedLayoutView`. When `True`, the page
content and the navigation ActionRow are wrapped in a single `Container`
so the paginator renders as one cohesive card with built-in navigation:

```python
class CardPaginator(PaginatedLayoutView):
    nav_inside_container = True
```

Default is `False`, which keeps the page content and nav row as separate
top-level children of the view -- the original layout. Items added via
`_build_extra_items` remain outside the wrapping Container in either
mode. Single-page views render no nav row, so the flag has no effect
when only one page is displayed.

### Binding per-instance state to a formatter

The `formatter=` callable receives one chunk and returns the page's V2
component list. When the formatter needs per-instance state (a category
name, a per-paginator accent color, a footer string), bind it via a
closure factory:

```python
def make_formatter(name: str, accent: discord.Color):
    def format_page(items):
        lines = [f"**{i['name']}** -- {i['detail']}" for i in items]
        return [
            card(
                f"## {name}",
                divider(),
                TextDisplay("\n".join(lines)),
                color=accent,
            )
        ]
    return format_page

view = await CategoryListView.from_data(
    items=items,
    per_page=3,
    formatter=make_formatter(name="Books", accent=discord.Color.blurple()),
    interaction=interaction,
)
```

The factory closes over `name` and `accent`, returning a `format_page`
that the paginator calls per chunk. `examples/v2_library.py` uses this
pattern with three categories, each binding its own name and color.
The same shape extends to per-page footers, conditional badges,
locale-bound strings, or any other data the formatter needs to weave
into the rendered page.

### Coming from a paginator gist?

The discord.py community frequently points new bot authors at
[@Soheab](https://github.com/Soheab)'s
[CV2 paginator gist](https://gist.github.com/Soheab/891c39d7294b1bdbadc7ecf35ce51cc5)
and [classic paginator gist](https://gist.github.com/Soheab/f226fc06a3468af01ea3168c95b30af8)
as reference implementations. CascadeUI's `PaginatedView` and
`PaginatedLayoutView` cover the same surface and several capabilities
the gists do not. Migration map for users coming from those gists:

| Soheab's gist | CascadeUI |
|---|---|
| `author_id` | `owner_only = True` (default) and the `allowed_users` set |
| `format_page()` | `formatter=` kwarg on `from_data` / `from_cursor` (sync or async, auto-detected) |
| Stop button | `add_exit_button()` inside `_build_extra_items()`; cleanup behavior controlled by `exit_policy` |
| `convert_str_to_text_display` | automatic -- string pages are wrapped in `Container(TextDisplay(s))` |
| `per_page` | same -- `from_data(items, per_page=N)` |
| Pages wrapped in a Container with buttons inside | `nav_inside_container = True` |
| Push paginator from a button click | `await self.push(await Paginator.from_data(...), interaction, ...)` -- `push` accepts the pre-constructed instance directly |
| Timeout cleanup | `exit_policy = "delete"` / `"disable"` |

CascadeUI adds beyond the gists:

- `from_cursor(fetch_fn, total, ...)` for lazy / streaming pagination
  with an LRU page cache and current-page eviction protection
- `refresh_data(items)` and `refresh_pages()` for live updates
- Render-hash short-circuit so repeat refreshes that compute the same
  tree skip the Discord REST edit
- One-HTTP-call refresh path on the acting interaction (combined ack +
  edit packet)
- Auto-defer + `serialize_interactions` safety net for rapid clicks
- Full state-store integration: subscribed actions, undo/redo, persistence
- Access control via `owner_only`, `allowed_users`, and `participant_limit`
- Instance limiting + replacement policies
- Compose with `_PersistentMixin` for paginators that survive bot restarts

[@Soheab](https://github.com/Soheab)'s gists shaped CascadeUI's paginator grammar and remain excellent
learning material for the underlying discord.py V1 and V2 primitives.

---

## LeaderboardLayoutView / PersistentLeaderboardLayoutView

V2-only paginated ranked display pattern. Accepts a list of
`(user_id, stats_dict)` tuples and renders one card-based page per
`leaderboard_per_page` chunk, with a summary header on page 1 and
cross-page rank numbering. Builds on top of `PaginatedLayoutView`, so
every paginated feature (first/last buttons, go-to modal, jump
threshold) is available.

When all entries fit on a single page, no navigation buttons render --
the view behaves as a static card.

### Minimal example

```python
from cascadeui import LeaderboardLayoutView


class ServerLeaderboard(LeaderboardLayoutView):
    leaderboard_top_n = 10
    leaderboard_per_page = 5


entries = [
    (user_id, {"wins": 12, "games": 20}),
    (user_id_2, {"wins": 8, "games": 15}),
    # ...
]

view = ServerLeaderboard(
    context=context,
    entries=entries,
    title=f"Leaderboard -- {context.guild.name}",
)
await view.send(ephemeral=True)
```

### Override hooks

Each row composes from four small hooks so subclasses can override the
smallest piece they need. The default `format_entry` stitches them
together; override it directly only when the row layout itself needs to
change (multi-line, different separator).

| Hook | Purpose |
|------|---------|
| `get_entries()` | Data source. Default returns the constructor `entries=` kwarg. Override to read from `store.computed` or `StateStore.iter_scoped`. |
| `format_rank(rank)` | Rank column. Default returns a medal emoji for ranks 1-3 (gold, silver, bronze) and `**<rank>.**` for rank 4+. |
| `format_name(user_id, stats)` | Name column. Default renders `<@user_id>`; rows carrying a `display_name` key render as an italic plain label instead. |
| `format_stats(user_id, stats)` | Inline stat column. Default returns `<W>W / <G>G`. Override to surface game-specific stats (MMR, win rate, streak). |
| `format_accessory(user_id, stats)` | Optional right-side accessory appended to the row. Default returns `None` (omitted). |
| `format_entry(rank, user_id, stats)` | Composes the four hooks above into one line. Override directly only when the row layout itself needs to change. |
| `format_primary(rank, user_id, stats)` | Section render mode only -- first line of the two-line section body. Default delegates to `format_rank` + `format_name`. |
| `format_secondary(rank, user_id, stats)` | Section render mode only -- second line of the section body. Default delegates to `format_stats`. |
| `get_avatar_url(user_id, stats)` | Async hook returning an avatar URL for the section's `Thumbnail` accessory. Default returns `None`, which triggers the stacked `TextDisplay` fallback. |
| `build_summary(entries)` | Dict rendered as a `key_value` block above the rankings. Return `{}` to suppress. |
| `on_leaderboard_empty()` | Returns the V2 component list shown when `entries` is empty. Default wraps `leaderboard_empty_message` in a single card. |
| `on_state_changed(state)` | Runs `rebuild_pages()` before the paginated refresh -- lets live-data subclasses re-fetch on every subscribed action. The rebuild short-circuits when the entries signature (user ids + stats) is unchanged, so identical re-fetches cost one comparison instead of a full page rebuild. |

```python
from cascadeui import LeaderboardLayoutView, progress_bar


class MmrBoard(LeaderboardLayoutView):
    def format_stats(self, user_id, stats):
        wins = stats["wins"]
        games = stats["games"]
        bar = progress_bar(wins, games or 1, width=6, show_percent=True).content
        return f"`{stats['mmr']}` MMR \u2022 {wins}W / {games}G \u2022 {bar}"
```

### Class attributes

- `leaderboard_top_n` (default `10`) -- how many entries to consider from the data source.
- `leaderboard_per_page` (default `5`) -- entries per page. Set to `None` to collapse the display into a single page equal to `top_n` (no navigation controls). At the default, a `top_n` of 10 produces two pages with prev/next controls, and a `top_n` of 25 surfaces the full first/last + go-to-page surface.
- `title` (default `"Leaderboard"`) -- H2 on the rankings card. Constructor `title=` kwarg overrides.
- `subtitle` (default `"Rankings"`) -- H3 above the ranked rows. Set to `None` or empty string (or pass `subtitle=None` at construction) to skip the H3 entirely, which pairs naturally with a `build_summary` override that returns a standalone Container.
- `leaderboard_empty_message` -- static text when no entries exist.
- `entry_layout` (default `"lines"`) -- controls row rendering. `"lines"` stacks entries as `TextDisplay` rows inside a single card; `"sections"` renders each entry as a `Section` with a `Thumbnail` accessory and a two-line body (`format_primary` + `format_secondary`). Section mode caps `leaderboard_per_page` at `5` -- setting a larger value with `entry_layout = "sections"` raises at class-definition time via `_validate_class_attributes`.
- `podium_emojis` (default gold/silver/bronze medals) -- dict keyed by rank number. `format_rank` reads this for ranks 1-3; ranks beyond fall back to `f"**{rank}.**"`. Override the dict on a subclass to change the podium glyphs (or extend it past rank 3) without overriding `format_rank` itself.
- `entry_separator` (default `" -- "`) -- string rendered between the name and stat columns inside `format_entry` (`"lines"` mode). Override on a subclass for visual variety (`" | "`, `" • "`, etc.) without rewriting `format_entry`.
- `card_color` (default `None`) -- optional accent color for the rankings card. `None` falls through to the active theme's accent. Set to a `discord.Color` on a subclass to give the rankings card its own accent (useful when `build_summary` returns a Container with its own color and a deliberate two-color layout is wanted).
- `show_title_divider` (default `True`) -- whether to render a horizontal divider below the title and above the rest of the card content. Set to `False` for a more compact card.

### Section render mode {#section-render-mode}

When `entry_layout = "sections"`, each leaderboard row becomes a
Discord `Section` with the user's avatar as the accessory and two lines
of text. Override the three split hooks to control each piece:

```python
class AvatarBoard(LeaderboardLayoutView):
    entry_layout = "sections"
    leaderboard_per_page = 5

    def format_primary(self, rank, user_id, stats):
        return f"{self.format_rank(rank)} {self.format_name(user_id, stats)}"

    def format_secondary(self, rank, user_id, stats):
        return f"{stats['wins']}W / {stats['games']}G"

    async def get_avatar_url(self, user_id, stats):
        user = self.context.bot.get_user(user_id)
        return user.display_avatar.url if user else None
```

`get_avatar_url` is async so subclasses can fetch from Discord if the
user is not cached. When the hook returns `None`, the library falls
back to a stacked `TextDisplay` with the two lines joined by a newline
-- the row's content stays intact without requiring a subclass to
override the accessory. Override `get_avatar_url` when every row must
render as a `Section` regardless of cache state.

### Persistent variant

`PersistentLeaderboardLayoutView` composes `_PersistentMixin` with
`LeaderboardLayoutView` for admin-posted permanent panels. Defaults:
`owner_only = False`, `exit_policy = "disable"`, `timeout = None`.
Requires `persistence_key=`. `on_restore` calls `rebuild_pages()` to
refresh from live data after a bot restart.

```python
from cascadeui import PersistentLeaderboardLayoutView, get_store


class ServerStatsBoard(PersistentLeaderboardLayoutView):
    subscribed_actions = {"SCOPED_UPDATE"}
    title = "Server Rankings"

    def get_entries(self):
        store = get_store()
        raw = store.get_scoped("guild", guild_id=self.guild_id) or {}
        return sorted(
            raw.items(),
            key=lambda kv: kv[1].get("wins", 0),
            reverse=True,
        )
```

Pair with `persistent_slots = ("...",)` on the subclass (or
`SlotPolicy(persistent=True)` at setup) to persist the underlying
data source.

---

## RolesLayoutView / PersistentRolesLayoutView

V2-only role self-assign panel pattern. Each category renders as a
Container with an accent color, a heading, an optional mode hint, and
an ActionRow of role toggle buttons. Cardinality (at-most-one /
at-least-one) is enforced automatically inside the pattern -- clicks
apply role mutation via the Discord API and send an ephemeral
response without any per-role callback boilerplate.

Underneath, each role button is a `DynamicPersistentButton` subclass
declared once at module import. Clicks route by `custom_id` template
match, so a panel with 50 roles across 6 categories tracks zero
per-button state and survives bot restarts cleanly.

### Minimal example

```python
import discord
from cascadeui import PersistentRolesLayoutView, RoleCategory


class ServerRoles(PersistentRolesLayoutView):
    categories = [
        RoleCategory(
            name="Colors",
            roles={"Red": 111, "Blue": 222, "Green": 333},
            exclusive=True,
            color=discord.Color.red(),
        ),
    ]
    title = "Server Roles"


view = ServerRoles(
    context=context,
    persistence_key=f"roles:{context.guild.id}",
)
await view.send()
```

### Cardinality model

Two orthogonal boolean flags on `RoleCategory` control cardinality:

- **`exclusive=True`** -- at most one role in this category may be
  active. Selecting another role removes the previously-active one in
  the same category first (swap). Useful for color roles, pronouns,
  team affiliation.
- **`required=True`** -- at least one role in this category must stay
  active. Removing the last role in the category is rejected.
  Useful for pronoun / region / team categories where "no selection"
  is not a meaningful state.

The four combinations (both false / one-or-the-other / both true) all
produce valid cardinality behavior:

| `exclusive` | `required` | Behavior |
|-------------|------------|----------|
| `False` | `False` | Free multi-select. Any combination of roles in the category can be active, including none. |
| `True` | `False` | Radio button. One role at a time; unchecking is allowed (zero active is valid). |
| `False` | `True` | Required checkbox. Any number of roles active, but at least one. |
| `True` | `True` | Required radio. Exactly one role active; removing the last is rejected. |

### Class attributes

**Heading:**
- `title` (default `"Server Roles"`) -- H2 rendered above all
  categories. Set to `None` to skip entirely.
- `subtitle` (default `None`) -- optional H3 rendered below the
  title. Set to a string to render (the subtitle uses raw text, so
  users can prefix with `"-# "` for small-text style).

**Mode hints** (rendered as small text under each category heading):
- `hint_normal` (default `None`) -- hint for free-multi-select
  categories.
- `hint_exclusive` (default `"◉"`) -- hint for exclusive-only
  categories. U+25C9 fisheye, a text-size filled circle.
- `hint_required` (default `"*"`) -- hint for required-only
  categories.
- `hint_exclusive_required` (default `"◉ *"`) -- hint for
  exclusive+required categories. Both indicators render at
  text-size so they sit on one line at consistent visual weight,
  rather than mixing an emoji glyph with a text character.

Each hint attribute accepts any string (including emoji), or `None`
to suppress the hint entirely. Per-category dynamic hints override
`format_category_hint(category)` at Tier 2.

**Response messages** (Python `str.format` placeholders: `{role}`,
`{category}`, `{removed}`, `{error}`):
- `assigned_message` -- sent after a role is added.
- `removed_message` -- sent after a role is removed.
- `required_message` -- sent when a required-category last-role
  removal is rejected.
- `swap_message` -- sent after an exclusive-mode swap.
- `role_error_message` -- sent on role mutation failure (forbidden,
  HTTP error).

### Override hooks

!!! note "Classmethod hook signature"
    Hook methods on `RolesLayoutView` / `PersistentRolesLayoutView`
    use `@classmethod` with a `cls` first argument, not `self`. The
    dispatch path routes through `DynamicPersistentButton` which has
    no view instance at click time -- the hook classmethods read
    class attributes (`cls.assigned_message`, etc.) and respond to
    the interaction directly. `super()` calls work normally.

| Hook | Purpose |
|------|---------|
| `format_category_title(category)` | Category heading line. Default: `f"### {category.name}"` (or prefixed with `category.icon` when set). |
| `format_category_hint(category)` | Hint rendered below the heading. Default: routes to `hint_*` attribute based on the cardinality flags. Return `None` to skip. |
| `format_button_label(role_name, role_id, category)` | Button label. Default: `role_name`. |
| `format_button_emoji(role_name, role_id, category)` | Button emoji. Default: `None`. |
| `format_button_style(role_name, role_id, category)` | Button style. Default: `category.button_style` or `ButtonStyle.secondary`. |
| `build_category_card(category)` | Render one category as a Container. Default composes the smaller `format_*` hooks; override for full layout control. |
| `on_role_assigned(interaction, member, role, category)` | Called after a role is added without a swap. Default: reads `assigned_message`, sends ephemeral response. |
| `on_role_removed(interaction, member, role, category)` | Called after a role is removed. Default: reads `removed_message`. |
| `on_role_swap(interaction, member, role_added, roles_removed, category)` | Called after an exclusive-mode swap. Default: reads `swap_message` with `{removed}` formatted as a comma-joined list of removed role names. |
| `on_role_required_block(interaction, member, role, category)` | Called when a required-category last-role removal is rejected. Default: reads `required_message`. |
| `on_role_error(interaction, error)` | Called on role mutation failure (`discord.Forbidden`, `discord.HTTPException`, or role-not-found string). Default: reads `role_error_message`. |

### Tier 1 customization (class attributes)

Every visible string is customizable without a method override:

```python
class CustomRoles(PersistentRolesLayoutView):
    categories = [...]

    title = "🎨 Pick Your Roles"
    subtitle = "-# Click any button to toggle."

    hint_exclusive = "🎯 pick one"
    hint_required = "⚠️ required"

    assigned_message = "✅ Added **{role}**."
    removed_message = "➖ Removed **{role}**."
    required_message = "You need at least one **{category}** role."
```

### Tier 2 customization (method overrides)

Override the smallest format hook that carries the tweak you need:

```python
class EmojiRoles(PersistentRolesLayoutView):
    categories = [...]

    @classmethod
    def format_button_emoji(cls, role_name, role_id, category):
        emoji_map = {"Red": "🟥", "Blue": "🟦", "Green": "🟩"}
        return emoji_map.get(role_name)
```

For richer hook behavior (logging, embeds, conditional responses),
override the `on_role_*` classmethods:

```python
class AuditedRoles(PersistentRolesLayoutView):
    categories = [...]

    @classmethod
    async def on_role_assigned(cls, interaction, member, role, category):
        await super().on_role_assigned(interaction, member, role, category)
        audit = interaction.guild.get_channel(AUDIT_CHANNEL_ID)
        if audit:
            await audit.send(f"{member} took {role.name}")
```

### Persistent variant

`PersistentRolesLayoutView` composes `_PersistentMixin` with
`RolesLayoutView`. Defaults: `owner_only = False`,
`exit_policy = "disable"`, `timeout = None`. Requires
`persistence_key=` at construction. On bot restart, role buttons
continue routing correctly because each button is a
`DynamicPersistentButton` subclass registered globally at module
import -- the panel survives restart independent of view
re-attachment. The default `on_restore` re-renders the message
from the current `categories` on every restart, so source-code
edits to role IDs, category names, or button labels propagate to
the displayed message on the next bot start. Unchanged panels
pay zero Discord API cost: `refresh()`'s render-hash short-circuit
skips the `PATCH` when the rebuilt tree matches what the message
already shows.

### Category name uniqueness

Category names must be globally unique across every
`RolesLayoutView` subclass in the process. The pattern registers
each category's slugified name in a module-level registry at class-
definition time; collisions raise `ValueError` immediately so the
error surfaces at import rather than at click time. If two panels
need similar category names, prefix them (e.g. `"ServerA Colors"` /
`"ServerB Colors"`).

---

## Common Patterns

### Cross-Step State

Wizard and form patterns store collected values on `self` -- state
survives across steps naturally:

```python
class CharacterWizard(WizardLayoutView):
    def __init__(self, *args, **kwargs):
        self.character_data = {}
        steps = [
            {"name": "Name", "builder": self.build_name_step},
            {"name": "Class", "builder": self.build_class_step},
        ]
        super().__init__(*args, steps=steps, **kwargs)
```

### Dynamic Page Content

Paginated views support dynamic data via `refresh_data()` for views
created with `from_data()`:

```python
async def reload(self, interaction):
    fresh_items = await fetch_items_from_db()
    await self.refresh_data(fresh_items)
```

For cursor-mode views (`from_cursor()`), call `refresh_pages()` or
`refresh_pages(new_total=N)` instead:

```python
async def reload(self, interaction):
    # Contents changed, row count did not
    await self.refresh_pages()

async def reload_after_insert(self, interaction):
    # Rows added; resize the pages list
    new_total = await db.fetchval("SELECT count(*) FROM users")
    await self.refresh_pages(new_total=new_total)
```

### Combining Patterns with Navigation

Patterns work with `push()` and `pop()` like any other view:

```python
async def open_settings(self, interaction):
    await self.push(SettingsTabView, interaction,
                    rebuild=lambda v: v.build_ui())
```
