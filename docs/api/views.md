# API: Views

## `StatefulView`

Base class for all CascadeUI views. Extends `discord.ui.View`.

### Constructor

```python
StatefulView(
    context=None,          # commands.Context — extracts user/guild/interaction
    interaction=None,      # discord.Interaction — alternative to context
    timeout=180,           # Seconds before timeout (None = no timeout)
    state_key=None,        # Stable identity for persistent data
    theme=None,            # Per-view Theme override
)
```

Pass either `context` or `interaction` — both extract the user, guild, and interaction for `send()`. Use `context` from prefix/hybrid commands, `interaction` from app commands or component callbacks.

### Methods

#### `send(content=None, *, embed=None, embeds=None, ephemeral=False)`

Sends the view as a message. Handles state registration and message tracking.

#### `dispatch(action_type, payload=None)`

Dispatches an action through the store with `source=self.id`.

#### `replace(view_class, interaction=None, **kwargs)`

Replaces the current view with another view class. One-way (no stack history saved).

#### `push(view_class, interaction, **kwargs)`

Pushes the current view onto the navigation stack and returns a new instance of `view_class`. All constructor kwargs are auto-captured so `pop()` can reconstruct the view faithfully.

#### `pop(interaction)`

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

Adds an exit button that calls `self.exit()`. Set `delete_message=True` to delete the message instead of disabling components. Pass `custom_id` for `PersistentView` subclasses.

#### `clear_row(row: int)`

Removes all components on the given row number. Useful for dynamically rebuilding a specific section of the view without affecting other rows.

#### `exit(delete_message=False)`

Cleans up the view: cancels tasks, unsubscribes, disables components. Optionally deletes the message.

#### `get_theme()`

Returns the view's theme (per-view override or global default).

#### `update_from_state(state)` *(override)*

Called when a matching state change occurs. Override to react to state updates.

#### `state_selector(state)` *(override)*

Returns a slice of state. If the return value hasn't changed, `update_from_state` won't fire.

#### `interaction_check(interaction)` *(override)*

Called before every component callback. Returns `True` to allow the interaction, `False` to block it. By default, rejects non-owners with an ephemeral message when `owner_only` is `True`. Override for custom access control (call `await super().interaction_check(interaction)` to preserve the ownership check).

### Properties

- `id` (str): UUID instance identifier
- `state_key` (str | None): Stable data identity key
- `message` (Message | None): The sent message, if any
- `state_store` (StateStore): The singleton store
- `session_id` (str | None): Session ID for this view
- `scoped_state` (dict): The scoped state for this view's user/guild (empty dict if no scope)

### Class Attributes

- `subscribed_actions` (set[str] | None): Action types to listen for. Default includes `VIEW_DESTROYED`, `SESSION_UPDATED`. Set to `None` for all actions.
- `scope` (str | None): `"user"`, `"guild"`, or `None`. Determines state scoping.
- `enable_undo` (bool): Enable undo/redo for this view (default: `False`).
- `undo_limit` (int): Max undo stack depth (default: `20`).
- `auto_back_button` (bool): Automatically add a back button when pushed (default: `False`).
- `session_limit` (int | None): Maximum number of active instances within the session scope. `None` (default) means unlimited.
- `session_scope` (str): How instances are grouped for limit counting. One of `"user"`, `"guild"`, `"user_guild"` (default), or `"global"`.
- `session_policy` (str): What to do when the limit is exceeded. `"replace"` (default) exits the oldest instances. `"reject"` raises `SessionLimitError`.
- `owner_only` (bool): Only the creating user can interact with the view (default: `True`). Set to `False` for shared views like polls or dashboards.
- `owner_only_message` (str): Ephemeral message sent to non-owners when `owner_only` is `True` (default: `"You cannot interact with this."`).
- `auto_defer` (bool): Enable the auto-defer safety net (default: `True`). Automatically defers interactions when callbacks are slow.
- `auto_defer_delay` (float): Seconds to wait before auto-deferring (default: `2.5`).

---

## `PersistentView`

Subclass of `StatefulView` for views that survive bot restarts.

### Constructor

```python
PersistentView(
    *args,
    state_key=...,    # Required (raises ValueError if missing)
    **kwargs,
)
```

`timeout` is forced to `None`.

### Methods

#### `on_restore(bot)` *(override)*

Called after the view is restored on bot restart. Override for post-restore setup (e.g., fetching fresh data or updating the embed).

!!! warning
    If `on_restore` raises an exception, the view is unregistered from CascadeUI's state system but remains in discord.py's internal view store (there is no public API to remove it). Avoid raising from this method unless recovery is not possible.

### Class Attribute Overrides

- `timeout` is forced to `None`
- `owner_only` defaults to `False` (persistent views are typically shared)

### Requirements

- `state_key` must be provided
- All components must have explicit `custom_id` values
- Auto-registers subclasses via `__init_subclass__`
- Cannot be sent as ephemeral (`send(ephemeral=True)` raises `ValueError`)
- Duplicate `state_key` registration automatically exits the previous view instance and cleans up its message

---

## `setup_persistence(bot=None, *, file_path=None, backend=None)`

Single entry point for all persistence. Call once in `setup_hook`, after loading cogs.

- Without `bot`: data-only persistence
- With `bot`: also re-attaches PersistentView instances
- `backend`: a `StorageBackend` instance (e.g., `SQLiteBackend`, `RedisBackend`)
- `file_path`: shorthand for `FileStorageBackend(file_path)` (used when `backend` is not provided)

Returns a dict: `{"restored": [...], "skipped": [...], "failed": [...], "removed": [...]}`

---

## View Patterns

### `TabView`

```python
TabView(context=None, tabs={"Name": async_builder_fn, ...}, **kwargs)
```

### `WizardView`

```python
WizardView(
    context=None,
    steps=[{"name": str, "builder": async_fn, "validator": async_fn (optional)}, ...],
    on_finish=async_fn,
    **kwargs,
)
```

### `FormView`

```python
FormView(
    context=None,
    fields=[{"id": str, "type": "select"|"boolean", "label": str, "validators": [...], ...}, ...],
    on_submit=async_fn,
    **kwargs,
)
```

### `PaginatedView`

```python
PaginatedView(context=None, pages=[Embed | str | dict, ...], **kwargs)
```

Pages can be `Embed` objects, strings, or dicts with `"embed"` and/or `"content"` keys.

#### Class Attributes

- `jump_threshold` (int): Page count above which first/last and go-to-page buttons appear (default: `5`).

#### Class Methods

##### `await PaginatedView.from_data(items, per_page, formatter, **kwargs)`

Creates a `PaginatedView` by chunking `items` into groups of `per_page` and applying `formatter` (sync or async) to each chunk. Returns a ready-to-send instance. Stores `per_page` and `formatter` on the instance for use by `refresh_data()`.

#### Instance Methods

##### `await refresh_data(items)`

Re-paginates with new data using the original `per_page` and `formatter` from `from_data()`. Rebuilds pages, clamps the current page index, and calls `_update_page()` (which triggers `_build_extra_items()` and edits the message). Raises `RuntimeError` if the view was not created via `from_data()`.

##### `_build_extra_items()` *(override)*

Hook for subclasses to add components below the navigation buttons (rows 1-4). Called after `_add_navigation_buttons()` during init and during every `_update_page()` call (page turns, `refresh_data()`). Use `clear_row()` at the start to remove stale components before re-adding.

#### Navigation Buttons

- **Previous/Next** (`◀`/`▶`): Always shown. Disabled at boundaries.
- **First/Last** (`⏮`/`⏭`): Shown when `len(pages) > jump_threshold`. Disabled at boundaries.
- **Go-to-page**: Shown when `len(pages) > jump_threshold`. Opens a modal for direct page input. Replaces the disabled page indicator.
- **Page indicator**: Shown when `len(pages) <= jump_threshold`. Disabled button displaying "Page X/Y".

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

```python
try:
    view = MyView(interaction=interaction)
    await view.send(embed=embed)
except SessionLimitError as e:
    print(f"Blocked: {e.view_type} limit is {e.limit}")
```
