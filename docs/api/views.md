# API: Views

## `StatefulView`

Base class for all CascadeUI views. Extends `discord.ui.View`.

### Constructor

```python
StatefulView(
    context=None,          # commands.Context or Interaction
    timeout=180,           # Seconds before timeout (None = no timeout)
    state_key=None,        # Stable identity for persistent data
    theme=None,            # Per-view Theme override
)
```

### Methods

#### `send(content=None, *, embed=None, embeds=None, ephemeral=False)`

Sends the view as a message. Handles state registration and message tracking.

#### `dispatch(action_type, payload=None)`

Dispatches an action through the store with `source=self.id`.

#### `transition_to(view_class, interaction=None, **kwargs)`

Transitions to another view class, cleaning up the current one. One-way (no stack).

#### `push(view_class, interaction, **kwargs)`

Pushes the current view onto the navigation stack and returns a new instance of `view_class`.

#### `pop(interaction)`

Pops the top entry from the navigation stack, reconstructs that view, and returns it. Returns `None` if the stack is empty.

#### `batch()`

Returns an async context manager for batched dispatch. Convenience for `self.state_store.batch()`.

#### `undo(interaction=None)`

Undoes the last state change for this view's session (requires `enable_undo = True` and `UndoMiddleware`).

#### `redo(interaction=None)`

Redoes the last undone state change.

#### `dispatch_scoped(data)`

Updates scoped state (requires `scope` to be set on the view class).

#### `add_exit_button(label="Exit", style=ButtonStyle.secondary, row=None, custom_id=None)`

Adds a gray exit button that calls `self.exit()`. Pass `custom_id` for `PersistentView` subclasses.

#### `exit(delete_message=False)`

Cleans up the view: cancels tasks, unsubscribes, disables components. Optionally deletes the message.

#### `get_theme()`

Returns the view's theme (per-view override or global default).

#### `update_from_state(state)` *(override)*

Called when a matching state change occurs. Override to react to state updates.

#### `state_selector(state)` *(override)*

Returns a slice of state. If the return value hasn't changed, `update_from_state` won't fire.

### Properties

- `id` (str): UUID instance identifier
- `state_key` (str | None): Stable data identity key
- `message` (Message | None): The sent message, if any
- `state_store` (StateStore): The singleton store
- `session_id` (str | None): Session ID for this view
- `scoped_state` (dict): The scoped state for this view's user/guild (empty dict if no scope)

### Class Attributes

- `subscribed_actions` (set[str] | None): Action types to listen for. Default includes `VIEW_UPDATED`, `VIEW_DESTROYED`, `COMPONENT_INTERACTION`, `SESSION_UPDATED`. Set to `None` for all actions.
- `scope` (str | None): `"user"`, `"guild"`, or `None`. Determines state scoping.
- `enable_undo` (bool): Enable undo/redo for this view (default: `False`).
- `undo_limit` (int): Max undo stack depth (default: `20`).
- `auto_back_button` (bool): Automatically add a back button when pushed (default: `False`).

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

Called after the view is restored on bot restart. Override for post-restore setup.

### Requirements

- `state_key` must be provided
- All components must have explicit `custom_id` values
- Auto-registers subclasses via `__init_subclass__`
- Cannot be sent as ephemeral (`send(ephemeral=True)` raises `ValueError`)

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
PaginatedView(context=None, pages=[Embed, ...], **kwargs)
```
