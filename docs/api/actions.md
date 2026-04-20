# API: Built-in Actions

CascadeUI dispatches these actions internally to manage the state tree. They
are handled by built-in reducers and cannot be overridden with
`@cascade_reducer`. User-defined actions use separate type strings and
coexist without conflict.

Every action is a dict with four top-level keys:

```python
{
    "type": "VIEW_CREATED",       # Action type constant
    "payload": { ... },           # Action-specific data
    "source": "abc123",           # View ID of the dispatcher (or None)
    "timestamp": 1712764800.0,    # time.time() at dispatch
}
```

For the quick-reference table, see
[State Management -- Built-in Actions](../guide/state.md#built-in-actions).

---

## Lifecycle

### `VIEW_CREATED`

Dispatched by `_register_state()` during `send()`. Creates the view's entry in
`state["views"]` and associates it with its session.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `view_id` | `str` | Unique view instance ID |
| `view_type` | `str` | Class name (e.g. `"SettingsView"`) |
| `user_id` | `int \| None` | Owner's Discord user ID |
| `session_id` | `str \| None` | Session this view belongs to |
| `props` | `dict` | Extra properties passed at creation |
| `message_id` | `str \| None` | Discord message ID after send |
| `channel_id` | `str \| None` | Discord channel ID |

**State change:** Writes to `state["views"][view_id]` and appends `view_id` to
`state["sessions"][session_id]["views"]`.

---

### `VIEW_UPDATED`

Dispatched by `dispatch()` when updating view-specific state. Merges payload
fields into the existing view entry.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `view_id` | `str` | Target view ID |
| `*` | any | Additional fields merged into the view's state |

**State change:** Updates `state["views"][view_id]` with all payload keys
(except `view_id` itself) and sets `updated_at`.

---

### `VIEW_DESTROYED`

Dispatched by `exit()`, `on_timeout()`, and `_navigate_to()`. Removes the view
from the state tree and cleans up associated data.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `view_id` | `str` | View being destroyed |
| `clear_nav_stack` | `bool` | If `True`, wipes the session's nav stack before checking emptiness |

**State change:**

- Deletes `state["views"][view_id]`
- Removes component interaction entries owned by this view
- Removes modal submission entries owned by this view
- Removes `view_id` from its session's view list
- Deletes the session entirely if no views remain and no nav stack exists

The `clear_nav_stack` flag is set by `exit()` and `on_timeout()` so the session
is cleaned up even when a pushed sub-view exits directly. Push/pop transitions
leave it `False` to keep the session alive between the old view's destruction
and the new view's registration.

---

### `SESSION_CREATED`

Dispatched by `_register_state()` alongside `VIEW_CREATED`. Creates a new
session entry if one does not already exist.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `session_id` | `str` | Session identifier. Default shape is `<module.QualName>:user_<id>:<8hex>` (the 8-hex suffix isolates repeat opens); views that set `session_continuity = True` drop the suffix and produce `<module.QualName>:user_<id>` |
| `user_id` | `int \| None` | Session owner |
| `data` | `dict` | Initial session data |

**State change:** Writes to `state["sessions"][session_id]` with empty `views`,
`history`, and `data` fields. Skips if the session already exists (idempotent
for push/pop chains that share a session).

---

### `SESSION_UPDATED`

Dispatched when session-level shared data changes. Use `update_session()` on
any view to dispatch this action with the correct `session_id`:

```python
await self.update_session(lang="fr", difficulty="hard")
```

Read the result with the `shared_data` property:

```python
lang = self.shared_data.get("lang", "en")
```

Session data is shared across all views in the same push/pop chain (they
inherit the parent's `session_id`). Unlike scoped state, session data is
ephemeral and not persisted across restarts.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `session_id` | `str` | Target session |
| `data` | `dict` | Fields to shallow-merge into the session's `data` dict |

**State change:** Shallow-merges `payload["data"]` into
`state["sessions"][session_id]["data"]` and sets `updated_at`. No-op if the
session does not exist.

---

## Navigation

### `NAVIGATION_PUSH`

Dispatched by `push()`. Records the current view on the session's nav stack so
`pop()` can reconstruct it later.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `session_id` | `str` | Session owning the nav stack |
| `class_name` | `str` | Fully qualified class name of the view being pushed from |
| `module` | `str \| None` | Module path for dynamic import on pop |
| `kwargs` | `dict` | Constructor kwargs snapshot (captured by `__init_subclass__`) |
| `state_snapshot` | `any` | Optional state to restore on pop |

**State change:** Appends an entry to
`state["sessions"][session_id]["nav_stack"]`.

---

### `NAVIGATION_POP`

Dispatched by `pop()`. Removes the top entry from the nav stack.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `session_id` | `str` | Session owning the nav stack |

**State change:** Pops the last entry from
`state["sessions"][session_id]["nav_stack"]`.

---

### `NAVIGATION_REPLACE`

Dispatched by `replace()`. Records the transition in session history. Does not
modify the nav stack (replace clears it via `VIEW_DESTROYED` with
`clear_nav_stack=True` on the source view).

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `destination` | `str` | Class name of the replacement view |
| `params` | `dict` | Extra transition parameters |

**State change:** Appends a history entry to
`state["sessions"][session_id]["history"]` with `from_view`, `to_view_type`,
`timestamp`, and `params`.

---

## State

### `SCOPED_UPDATE`

Dispatched by `dispatch_scoped()`. Merges data into a namespaced slice of
application state, keyed by scope type and identifiers.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `scope` | `str` | Scope type: `"user"`, `"guild"`, `"user_guild"`, or `"global"` |
| `identifiers` | `dict` | IDs for key construction (e.g. `{"user_id": 123}`) |
| `data` | `dict` | Fields to merge into the scoped slice |

**State change:** Shallow-merges `data` into
`state["application"]["scoped"][scope_key]`, where `scope_key` is built
by `StateStore._build_scope_key()`. Scoped data lives inside the
`application` namespace so it shares the same persistence plumbing as
named application slots.

---

### `COMPONENT_INTERACTION`

Dispatched by `StatefulButton` and `StatefulSelect` after every callback
invocation. Records the interaction for devtools history.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `component_id` | `str` | The component's `custom_id` |
| `view_id` | `str` | Parent view ID |
| `user_id` | `int \| None` | User who clicked |
| `value` | `dict` | Interaction-specific values |

**State change:** Appends to
`state["components"][component_id]["interactions"]`, capped at 50 entries.
Sets `last_interaction` timestamp.

---

### `MODAL_SUBMITTED`

Dispatched by `Modal.on_submit()` when `view_id` is set. Records the
submission for devtools history.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `view_id` | `str` | Parent view ID |
| `user_id` | `int \| None` | User who submitted |
| `values` | `dict` | Field values from the modal |

**State change:** Appends to
`state["modals"][view_id]["submissions"]`, capped at 50 entries. Sets
`last_submission` timestamp.

---

## Persistence

### `PERSISTENT_VIEW_REGISTERED`

Dispatched by `PersistentView.send()` after the message is sent. Stores the
information needed to re-attach the view after a bot restart.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `persistence_key` | `str` | Dedupe key for the persistent view |
| `class_name` | `str` | View class name |
| `message_id` | `str` | Discord message ID |
| `channel_id` | `str` | Discord channel ID |
| `guild_id` | `str \| None` | Guild ID (DMs are `None`) |
| `user_id` | `str \| None` | Owner user ID |

**State change:** Writes to `state["persistent_views"][persistence_key]`.

`PersistenceMiddleware` flushes the registry namespace to disk immediately on this action.

---

### `PERSISTENT_VIEW_UNREGISTERED`

Dispatched by `PersistentView.exit()`. Removes the persistent view from the
registry.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `persistence_key` | `str` | The view's dedupe key |

**State change:** Deletes `state["persistent_views"][persistence_key]`.

`PersistenceMiddleware` flushes the registry namespace to disk immediately on this action.

---

## Undo / Redo

### `UNDO`

Dispatched by `undo()` on views with `enable_undo = True`. Restores the
previous application state snapshot.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `view_id` | `str` | View owning the undo stack |
| `session_id` | `str` | Session for shared_data restoration |

**State change:**

- Pushes current `state["application"]` and `session["shared_data"]` onto the view's `redo_stack`
- Pops the top entry from the view's `undo_stack` and restores both

The restored snapshot covers the full `application` subtree (including
the nested `scoped` namespace), so `dispatch_scoped()` changes round-trip
correctly. Session data from `update_session()` is also
restored.

---

### `REDO`

Dispatched by `redo()` on views with `enable_undo = True`. Re-applies a
previously undone snapshot.

**Payload:**

| Key | Type | Description |
|-----|------|-------------|
| `view_id` | `str` | View owning the redo stack |
| `session_id` | `str` | Session for shared_data restoration |

**State change:**

- Pushes current `state["application"]` and `session["shared_data"]` onto the view's `undo_stack`
- Pops the top entry from the view's `redo_stack` and restores both `state["application"]` and `session["shared_data"]`

---

## Notification-only

### `BATCH_COMPLETE`

Fired after a `batch()` context manager exits. Has no reducer and does not
modify state. Subscribers listening for `BATCH_COMPLETE` can use it as a
signal to rebuild UI once after a group of actions.

---

## Middleware Behavior

Three middleware components interact with these actions:

- **`PersistenceMiddleware`** fans writes across two namespaces (registry,
  application) with independent debounce windows per namespace. Scoped state
  rides under the application namespace; a scoped slot persists when its slot
  name is opted in via `persistent_slots` on the view class or via
  `SlotPolicy(persistent=True)` at setup time.
  Flushes immediately on `VIEW_DESTROYED`, `PERSISTENT_VIEW_REGISTERED`, and
  `PERSISTENT_VIEW_UNREGISTERED`. Skips bookkeeping actions
  (`SESSION_CREATED`, `SESSION_UPDATED`, `VIEW_CREATED`, `VIEW_UPDATED`,
  `COMPONENT_INTERACTION`, `NAVIGATION_PUSH`, `NAVIGATION_POP`,
  `NAVIGATION_REPLACE`, `UNDO`, `REDO`, `BATCH_COMPLETE`) that don't carry
  application state changes. Also skips any dispatch-only action (no registered
  reducer) where the state reference is unchanged. Slots default to in-memory;
  the middleware only writes slots opted in through `_PERSISTENT_SLOTS`.

- **`UndoMiddleware`** snapshots `state["application"]` and `session["shared_data"]` into the source view's `undo_stack`
  before the reducer runs. Skips bookkeeping actions (`VIEW_CREATED`,
  `VIEW_UPDATED`, `VIEW_DESTROYED`, `SESSION_CREATED`, `NAVIGATION_PUSH`,
  `NAVIGATION_POP`, `NAVIGATION_REPLACE`, `COMPONENT_INTERACTION`,
  `MODAL_SUBMITTED`, `UNDO`, `REDO`, `BATCH_COMPLETE`,
  `PERSISTENT_VIEW_REGISTERED`, `PERSISTENT_VIEW_UNREGISTERED`). Only
  snapshots when the dispatching view has `enable_undo = True`.
  `SESSION_UPDATED` is **not** skipped - `update_session()` changes are
  captured and restored by undo/redo.

- **`LoggingMiddleware`** logs every dispatched action. Default level is `INFO`; pass `level="DEBUG"` for full action tracing or `level="WARNING"` to suppress routine traffic.

## Action Filters and Undo

`_notify_subscribers` skips the `action_filter` gate for `UNDO` and `REDO`
actions. All subscribers become candidates regardless of their
`subscribed_actions` set. The selector comparison still runs, so only views
whose selected state actually changed are notified. This enables cross-view
undo reactivity without requiring views to subscribe to `UNDO`/`REDO`
explicitly.
