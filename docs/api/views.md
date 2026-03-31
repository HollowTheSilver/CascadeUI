# API: Views

All view classes share a common mixin (`_StatefulMixin`) that provides state management, navigation, session limiting, undo/redo, and lifecycle handling. The mixin is combined with either `discord.ui.View` (V1) or `discord.ui.LayoutView` (V2).

---

## Shared Constructor Parameters

These parameters apply to all view classes:

```python
context=None,          # commands.Context — extracts user/guild/interaction
interaction=None,      # discord.Interaction — alternative to context
timeout=180,           # Seconds before timeout (None = no timeout)
state_key=None,        # Stable identity for persistent data
theme=None,            # Per-view Theme override
```

Pass either `context` or `interaction` — both extract the user, guild, and interaction for `send()`. Use `context` from prefix/hybrid commands, `interaction` from app commands or component callbacks.

## Shared Methods

These methods are available on all view classes (V1 and V2):

#### `send(...)`

Sends the view as a message. V1 accepts `content`, `embed`, `embeds`, `ephemeral`. V2 sends the view as its own content (no content/embed params).

#### `dispatch(action_type, payload=None)`

Dispatches an action through the store with `source=self.id`.

#### `replace(view_class, interaction=None, **kwargs)`

Replaces the current view with another view class. One-way (no stack history saved).

#### `push(view_class, interaction, *, rebuild=None, **kwargs)`

Pushes the current view onto the navigation stack and navigates to a new instance of `view_class`. All constructor kwargs are auto-captured so `pop()` can reconstruct the view faithfully.

The optional `rebuild` callback is called with the new view after construction. For V2, return `None` (e.g., `rebuild=lambda v: v._build_ui()`). For V1, return a dict of kwargs for `edit_original_response` (e.g., `rebuild=lambda v: {"embed": v.build_embed()}`). Can be sync or async.

#### `pop(interaction, *, rebuild=None)`

Pops the top entry from the navigation stack, reconstructs that view with its original kwargs, and returns it. Returns `None` if the stack is empty. Non-reconstructible kwargs (`context`, `interaction`, etc.) are re-supplied by the framework.

#### `batch()`

Returns an async context manager for batched dispatch. Convenience for `self.state_store.batch()`.

#### `undo(interaction=None)`

Undoes the last state change for this view's session (requires `enable_undo = True` and `UndoMiddleware`).

#### `redo(interaction=None)`

Redoes the last undone state change.

#### `dispatch_scoped(data)`

Updates scoped state (requires `scope` to be set on the view class).

#### `add_exit_button(label="Exit", style=ButtonStyle.secondary, row=None, emoji="❌", delete_message=False, custom_id=None)`

Adds an exit button that calls `self.exit()`. In V2 views, the button is wrapped in an `ActionRow`. Set `delete_message=True` to delete the message instead of disabling components. Pass `custom_id` for persistent views.

#### `exit(delete_message=False)`

Cleans up the view: cancels tasks, unsubscribes, disables components. Optionally deletes the message. V2 views freeze components in place (since `edit(view=None)` would empty the message). V1 views strip the view entirely.

#### `get_theme()`

Returns the view's theme (per-view override or global default).

#### `update_from_state(state)` *(override)*

Called when a matching state change occurs. Override to react to state updates.

#### `state_selector(state)` *(override)*

Returns a slice of state. If the return value hasn't changed, `update_from_state` won't fire.

#### `interaction_check(interaction)` *(override)*

Called before every component callback. Returns `True` to allow, `False` to block. By default, rejects non-owners with an ephemeral message when `owner_only` is `True`.

### Shared Properties

- `id` (str): UUID instance identifier
- `state_key` (str | None): Stable data identity key
- `message` (Message | None): The sent message, if any
- `state_store` (StateStore): The singleton store
- `session_id` (str | None): Session ID for this view
- `scoped_state` (dict): The scoped state for this view's user/guild (empty dict if no scope)

### Shared Class Attributes

- `subscribed_actions` (set[str] | None): Action types to listen for. Default includes `VIEW_DESTROYED`, `SESSION_UPDATED`. Set to `None` for all actions.
- `scope` (str | None): `"user"`, `"guild"`, or `None`. Determines state scoping.
- `enable_undo` (bool): Enable undo/redo for this view (default: `False`).
- `undo_limit` (int): Max undo stack depth (default: `20`).
- `auto_back_button` (bool): Automatically add a back button when pushed (default: `False`).
- `session_limit` (int | None): Maximum active instances within the session scope. `None` (default) means unlimited.
- `session_scope` (str): How instances are grouped for limit counting. One of `"user"`, `"guild"`, `"user_guild"` (default), or `"global"`.
- `session_policy` (str): What to do when the limit is exceeded. `"replace"` (default) exits the oldest instances. `"reject"` raises `SessionLimitError`.
- `owner_only` (bool): Only the creating user can interact with the view (default: `True`). Set to `False` for shared views.
- `owner_only_message` (str): Ephemeral message sent to non-owners (default: `"You cannot interact with this."`).
- `auto_defer` (bool): Enable the auto-defer safety net (default: `True`).
- `auto_defer_delay` (float): Seconds before auto-deferring (default: `2.5`).
- `serialize_interactions` (bool): Serialize rapid button clicks with an `asyncio.Lock` (default: `True`). Set to `False` for views that handle parallel callbacks.

---

## V2 Views

### `StatefulLayoutView`

Base class for V2 views. Extends `discord.ui.LayoutView`.

```python
StatefulLayoutView(context=None, **kwargs)
```

V2 views ARE the message content — `send()` takes no `content` or `embed` params. Build the component tree in `__init__` or an async builder, then call `send()`.

#### V2-Specific Methods

##### `clear_row(row)`

No-op on V2 views. V2 uses a tree structure rather than rows.

---

### `TabLayoutView`

Tab-based navigation using button switching.

```python
TabLayoutView(
    context=None,
    tabs={"Tab Name": async_builder_fn, ...},
    **kwargs,
)
```

Each tab builder is an async function that returns a list of V2 components. The first tab is displayed on send.

#### Methods

##### `await _refresh_tabs()`

Re-runs the current tab's builder and edits the message. Use from Refresh button callbacks.

---

### `WizardLayoutView`

Multi-step wizard with back/next navigation and per-step validation.

```python
WizardLayoutView(
    context=None,
    steps=[
        {"name": str, "builder": async_fn, "validator": async_fn},
        ...
    ],
    on_finish=async_fn,
    **kwargs,
)
```

- `builder(self)` — async, returns a list of V2 components for the step
- `validator(self, interaction)` — async, returns `True` to proceed or `False` to block
- `on_finish(self, interaction)` — async, called when the final step passes validation

#### Navigation Buttons

Back, Next, and Finish buttons are added automatically. Back is disabled on the first step. Next is replaced with Finish on the last step.

---

### `FormLayoutView`

V2 form with modal-based text input and validation.

```python
FormLayoutView(
    context=None,
    fields=[
        {"id": str, "type": "text"|"select"|"boolean", "label": str, "validators": [...]},
        ...
    ],
    on_submit=async_fn,
    **kwargs,
)
```

Displays form state as a V2 component tree (Container + TextDisplay). Text fields open a modal for input. Select and boolean fields use interactive components.

---

### `PaginatedLayoutView`

V2 paginated view with component-tree pages.

```python
PaginatedLayoutView(context=None, pages=[list_of_components, ...], **kwargs)
```

Each page is a list of V2 components. Navigation buttons (Previous, Next, First, Last, Go-to-page) work identically to V1's `PaginatedView`.

#### Class Methods

##### `await PaginatedLayoutView.from_data(items, per_page, formatter, **kwargs)`

Creates a paginated view by chunking `items` and applying `formatter` to each chunk. The formatter should return a list of V2 components.

#### Instance Methods

##### `await refresh_data(items)`

Re-paginates with new data using the original `per_page` and `formatter`.

##### `_build_extra_items()` *(override)*

Hook for adding components after the navigation row.

---

### `PersistentLayoutView`

V2 persistent view that survives bot restarts.

```python
PersistentLayoutView(
    *args,
    state_key=...,    # Required
    **kwargs,
)
```

Same requirements and behavior as `PersistentView` — `state_key` required, all interactive components need explicit `custom_id`, `timeout` forced to `None`, `owner_only` defaults to `False`. Auto-registers subclasses via `__init_subclass__` into the same registry as `PersistentView`.

#### Methods

##### `on_restore(bot)` *(override)*

Called after the view is restored on bot restart.

---

## V1 Views (Classic)

### `StatefulView`

Base class for V1 views. Extends `discord.ui.View`.

```python
StatefulView(context=None, **kwargs)
```

#### V1-Specific Methods

##### `send(content=None, *, embed=None, embeds=None, ephemeral=False)`

Sends the view with optional content and embeds.

##### `clear_row(row: int)`

Removes all components on the given row number. Useful for dynamically rebuilding a specific section.

---

### `PersistentView`

V1 persistent view that survives bot restarts.

```python
PersistentView(
    *args,
    state_key=...,    # Required
    **kwargs,
)
```

- `timeout` is forced to `None`
- `owner_only` defaults to `False`
- `state_key` must be provided (raises `ValueError`)
- All components must have explicit `custom_id` values
- Cannot be sent as ephemeral (`send(ephemeral=True)` raises `ValueError`)
- Duplicate `state_key` registration exits the previous view instance

#### Methods

##### `on_restore(bot)` *(override)*

Called after the view is restored on bot restart.

---

### V1 Patterns

#### `TabView`

```python
TabView(context=None, tabs={"Name": async_builder_fn, ...}, **kwargs)
```

#### `WizardView`

```python
WizardView(
    context=None,
    steps=[{"name": str, "builder": async_fn, "validator": async_fn}, ...],
    on_finish=async_fn,
    **kwargs,
)
```

#### `FormView`

```python
FormView(
    context=None,
    fields=[{"id": str, "type": "select"|"boolean", "label": str, "validators": [...], ...}, ...],
    on_submit=async_fn,
    **kwargs,
)
```

#### `PaginatedView`

```python
PaginatedView(context=None, pages=[Embed | str | dict, ...], **kwargs)
```

Pages can be `Embed` objects, strings, or dicts with `"embed"` and/or `"content"` keys.

**Class Attributes:**

- `jump_threshold` (int): Page count above which first/last and go-to-page buttons appear (default: `5`).

**Class Methods:**

- `await PaginatedView.from_data(items, per_page, formatter, **kwargs)` — Chunks items and applies formatter (returns embed/str/dict). Stores `per_page` and `formatter` for `refresh_data()`.

**Instance Methods:**

- `await refresh_data(items)` — Re-paginates with new data. Raises `RuntimeError` if not created via `from_data()`.
- `_build_extra_items()` *(override)* — Hook for adding components below navigation buttons (rows 1-4).

---

## `setup_persistence(bot=None, *, file_path=None, backend=None)`

Single entry point for all persistence. Call once in `setup_hook`, after loading cogs.

- Without `bot`: data-only persistence
- With `bot`: also re-attaches PersistentView and PersistentLayoutView instances
- `backend`: a `StorageBackend` instance (e.g., `SQLiteBackend`, `RedisBackend`)
- `file_path`: shorthand for `FileStorageBackend(file_path)` (used when `backend` is not provided)

Returns a dict: `{"restored": [...], "skipped": [...], "failed": [...], "removed": [...]}`

---

## `SessionLimitError`

Exception raised when `send()` is blocked by session limiting.

```python
from cascadeui import SessionLimitError
```

### Attributes

- `view_type` (str): The class name of the view that hit the limit
- `limit` (int): The session limit value that was exceeded

### When it is raised

- **Reject policy**: Always raised when a new view would exceed `session_limit` with `session_policy = "reject"`.
- **PersistentView protection**: Raised when a non-persistent view attempts to replace a `PersistentView` under the replace policy.
