# The Five Pillar Model

Every CascadeUI class attribute, method hook, and runtime behavior maps to
exactly one of five pillars. If a new feature cannot be placed in exactly one
pillar, either the feature or the pillar definition is wrong.

This page is the canonical reference for the model. The attribute tables here
are summaries -- see the [Views Guide](views.md) and
[API Reference](../api/views.md) for full usage and code examples.

---

## Why Pillars?

CascadeUI assigns each view-layer concern to exactly one domain. The naming
convention reveals the pillar from the attribute name alone --
`instance_limit` is Pillar 2 (Instance Constraints), `exit_policy` is Pillar 3
(View Lifecycle), `shared_data` is Pillar 4 (Session Membership). No attribute
straddles two pillars, so reading the source produces a stable mental model of
which data lives where.

---

## Pillar 1 -- Access Control

**Question answered:** *Who can interact with this view?*

Governs interaction-time authorization. Runs on every click via
`interaction_check()`. This is the security boundary between users --
ephemerality is privacy, instance limits are cardinality, and Access Control
is what prevents player A from clicking player B's panel.

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `owner_only` | `True` | Reject interactions from non-owners |
| `allowed_users` | `set()` | Explicit allow-list (set in `__init__`, not on the class) |
| `unauthorized_message` | `"You cannot interact with this."` | Static rejection text |

| Method Hook | When it fires |
|-------------|---------------|
| `on_unauthorized(interaction)` | Dynamic override for rejection behavior |

`owner_only` and `allowed_users` are complementary. `owner_only = True` with an
empty `allowed_users` restricts to the view creator. `owner_only = False` with a
populated `allowed_users` restricts to a named set. `owner_only = False` with no
`allowed_users` allows everyone.

---

## Pillar 2 -- Instance Constraints {#pillar-2-instance-constraints}

**Question answered:** *How many of this view can exist, and what happens when
the limit is hit?*

Governs creation cardinality. Runs at `send()` time (not on every click). Its
job is to decide whether a new view can come into existence, and if an existing
one must yield.

### Creation cardinality

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `instance_limit` | `None` (unlimited) | Max concurrent instances per scope |
| `instance_scope` | `"user_guild"` | Scope key for limit counting: `"user"`, `"guild"`, `"user_guild"`, `"global"` |
| `instance_policy` | `"replace"` | What happens when the limit is exceeded: `"replace"` or `"reject"` |
| `instance_limit_message` | `None` | Static rejection text on limit hit |

| Method Hook | When it fires |
|-------------|---------------|
| `on_instance_limit(user_id, interaction)` | Dynamic override when limit blocks creation |

### Replacement behavior

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `replace_policy` | `"delete"` | What happens to the old view on replacement: `"delete"` or `"disable"` |
| `protect_attached` | `True` | Block replacement when other users are attached (see below) |
| `replaced_message` | `None` | Static channel notification fired on the old view when replaced |

| Method Hook | When it fires |
|-------------|---------------|
| `on_replaced()` | Dynamic override, fires on the old view before teardown |

### Participant capacity

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `participant_limit` | `None` (unlimited) | Max total occupants (owner + participants) |
| `participant_limit_message` | `"This session is full."` | Static rejection text on capacity hit |
| `auto_register_participants` | `False` | Auto-register `allowed_users` on `send()` |

| Method Hook | When it fires |
|-------------|---------------|
| `on_participant_limit(user_id, interaction)` | Dynamic override when a view is full |

### `protect_attached` semantics

Blocks replacement when the existing view has participants OR attached children
belonging to a *different* user than the replacement requester. Same-user
attachments do not trigger protection -- the owner can always replace their own
views. Only other users' investment is protected.

Two sources are checked:

- `_participants` -- user IDs registered via `register_participant()`
- `_attached_children` -- view instances registered via `attach_child()`

Both are filtered by `user_id != requester_id`. When no replaceable candidates
remain after filtering, the policy falls back to reject behavior
(`on_instance_limit` fires on the new view).

---

## Pillar 3 -- View Lifecycle

**Question answered:** *What happens when this specific view times out, errors,
or exits?*

Governs a single view's birth, activity, and death. Per-view -- a view's death
does not by itself tear down the session. Pillar 4 decides whether the session
dies with its last member.

### Timeout and exit

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `timeout` | `180` | Seconds of inactivity before `on_timeout()` fires (`None` = no timeout) |
| `exit_policy` | `"disable"` | Bare `exit()` behavior: `"disable"` (freeze components) or `"delete"` (delete message) |

| Method Hook | When it fires |
|-------------|---------------|
| `on_timeout()` | Inactivity timeout reached |
| `exit(delete_message=None)` | The exit method -- explicit `delete_message` arg overrides `exit_policy` |

### Error handling

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `error_message` | `"An unexpected error occurred..."` | Static error embed description |

| Method Hook | When it fires |
|-------------|---------------|
| `on_error(interaction, error, item)` | Exception in a component callback |

### Ephemeral management

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `auto_refresh_ephemeral` | `None` | Engages the 15-minute webhook handoff. `None` (default) derives from `timeout`: in-window (`<= 900`) declines, longer timeouts or `None` engage. Set `True`/`False` to pin the behavior. |
| `reopen_failure_message` | `"Could not refresh this view..."` | Static text when ephemeral refresh fails |

| Method Hook | When it fires |
|-------------|---------------|
| `on_reopen_failure(interaction, error)` | Ephemeral refresh factory failed or returned `None` |
| `on_message_delete()` | External message deletion detected (admin delete, bulk purge) |

### Interaction machinery

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `auto_defer` | `True` | Auto-defer unacknowledged interactions |
| `auto_defer_delay` | `2.5` | Seconds before auto-defer fires |
| `serialize_interactions` | `True` | Process clicks sequentially via asyncio.Lock |

### Undo/redo

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `enable_undo` | `False` | Track undo/redo history for this view |
| `undo_limit` | `20` | Max undo snapshots |

Undo stacks are view-local. Each view carries its own `undo_stack` and
`redo_stack`, forward-transferred through push/pop chains so the timeline stays
continuous across navigation. `replace()` does not transfer (clean break).

---

## Pillar 4 -- Session Membership

**Question answered:** *Which views are grouped together, and what data do they
share?*

Governs which views belong to a shared coordination unit. A session is created
automatically when a root view calls `send()` and dies when its last member
exits.

| Concept | Purpose |
|---------|---------|
| `session_id` | The session identity (per-instance by default; see **Session continuity** below) |
| `session["members"]` | All views currently belonging to this session |
| `shared_data` (property) | Cross-view metadata, read via the property, written via `update_session()` |
| `attach_child(child_view)` | Register a child view for cleanup cascade |
| `register_participant(user_id)` | Add a user to the attendance set |

| Method | Purpose |
|--------|---------|
| `update_session(**data)` | Write to `shared_data`, dispatches `SESSION_UPDATED` |
| `attach_child(child)` | Parent-local cleanup dependency -- children keep independent sessions |

### Session vs attachment

Sessions group views in a single user's push/pop flow. Views pushed onto a
nav stack share a `session_id` and the same `shared_data` namespace.

Attachment (`attach_child`) is a parent-child cleanup dependency for cross-user
scenarios. When the parent exits, all attached children exit too. Attached
children keep their own independent sessions -- no shared data leaks between
parent and child, no nav stack interference.

The `parent=` kwarg on the constructor automates attachment: `send()` calls
`attach_child` on success, skips on failure. `attach_child()` still works
standalone for manual use cases.

### Session continuity {#session-continuity}

Each view invocation gets its own session by default: the auto-derived
`session_id` carries a per-instance UUID suffix, so repeat opens of the same
view class are independent workflows with their own nav stack, undo timeline,
and `shared_data`. Push/pop chains still stay on one session because
`_navigate_to` forwards `session_id` explicitly -- isolation is per-root, not
per-hop.

Views that want repeat-open state coalescing (undo history surviving
close-and-reopen, `shared_data` continuity across gestures) opt in:

```python
class SettingsView(StatefulLayoutView):
    session_continuity: ClassVar[bool] = True  # coalesce repeat opens
```

The opt-in collapses derivation back to the class-coalesced shape
(`<module.QualName>:user_<id>` with no UUID suffix), so the next invocation
of the same view for the same user rejoins the prior session if one still
exists. Most workflow-oriented views (wizards, forms, ephemeral panels) keep
the default; views that conceptually *are* a single long-lived surface for
the user take the opt-in.

---

## Pillar 5 -- Navigation

**Question answered:** *What view was the user looking at before this one, and
how is it reconstructed?*

Governs the push/pop nav stack for sequential view chaining within a session.

| Method | Purpose |
|--------|---------|
| `push(new_view, rebuild=None)` | Replace current view with `new_view`, record stack entry |
| `pop(rebuild=None)` | Restore the previous view from the stack |
| `replace(new_view)` | One-way transition -- clears the stack, no return path |

| Attribute | Default | Purpose |
|-----------|---------|---------|
| `auto_back_button` | `False` | Add a back button when pushed onto a nav stack |

### View-local nav stack

Each view carries its own `nav_stack` as a view-local list. On push, the new
view inherits `parent.nav_stack + [entry_for_parent]`. On pop, the
reconstructed view receives `current.nav_stack[:-1]`. This forward-transfer
design means multi-user attached children can maintain independent nav
lineages within the same session.

Push/pop kwargs are auto-captured by `__init_subclass__`, so subclass authors
write normal `__init__` methods and push/pop works transparently.

### Navigation vs attachment

Navigation (Pillar 5) is about sequential replacement -- push destroys the
current view's UI, pop reconstructs the previous one. Only one view in the
chain is visible at a time.

Attachment (Pillar 4) is about parallel coexistence -- the parent and child
are both alive simultaneously, each with their own message.

---

## Pillar Orthogonality

The five pillars are designed to be independently tunable. Setting one does
not imply or constrain another:

| Scenario | Pillars involved | Configuration |
|----------|-----------------|---------------|
| Private panel, one per user | 1 + 2 | `owner_only = True`, `instance_limit = 1` |
| Shared game board, two players | 1 + 2 + 4 | `owner_only = False`, `allowed_users = {p1, p2}`, `auto_register_participants = True` |
| Settings menu with sub-pages | 2 + 5 | `instance_limit = 1`, push/pop navigation |
| Ephemeral confirmation | 1 + 3 | `owner_only = True`, `timeout = 60`, `exit_policy = "delete"` |
| Persistent ticket panel | 2 + 3 | `instance_limit = 1`, `instance_scope = "global"`, `timeout = None` |

A view that is `owner_only = True` can still have `instance_limit = None`
(unlimited private panels). A view with `instance_limit = 1` can still allow
multiple users via `allowed_users`. The pillars do not collapse into each other.

---

## The State Tree

The state store's shape reflects the pillar split:

```
state
├── views/                    (per-view data, Pillar 3)
│   └── <view_id>/
│       ├── type, user_id, guild_id, channel_id
│       ├── message_id, created_at
│       ├── nav_stack: [...]       (Pillar 5 -- view-local)
│       ├── undo_stack: [...]      (Pillar 3 -- view-local)
│       ├── redo_stack: [...]      (Pillar 3 -- view-local)
│       └── custom data from dispatched actions
│
├── sessions/                 (group coordination, Pillar 4)
│   └── <session_id>/
│       ├── members: [view_id, ...]
│       ├── shared_data: { ... }
│       ├── history: [...]
│       └── created_at, updated_at
│
├── application/              (user-managed, persists via opt-in slots)
│   ├── <slot_name>/          (custom keys set by dispatched actions)
│   │
│   ├── scoped/               (per-user/guild/global via dispatch_scoped)
│   │   ├── user:<id>/
│   │   ├── guild:<id>/
│   │   ├── user_guild:<uid>:<gid>/
│   │   └── global/
│   │
│   └── <named_scoped_slot>/  (opt-in via scoped_slot class attribute)
│       └── ...               (same user/guild/user_guild/global shape)
│
├── persistent_views/         (survives restart)
│   └── <persistence_key>/
│
├── components/               (interaction tracking)
│   └── <custom_id>/
│
└── modals/                   (submission tracking)
    └── <modal_view_id>/
```

`nav_stack`, `undo_stack`, and `redo_stack` live on the view, not on the
session. This is the structural change that enables multi-user attached
children to maintain independent navigation and undo timelines.

---

## Quick Reference

Every class attribute, grouped by pillar, with its default value:

=== "Pillar 1 -- Access Control"

    ```python
    owner_only = True
    unauthorized_message = "You cannot interact with this."
    # allowed_users -- set in __init__, not on the class
    ```

=== "Pillar 2 -- Instance Constraints"

    ```python
    instance_limit = None
    instance_scope = "user_guild"
    instance_policy = "replace"
    instance_limit_message = None
    replace_policy = "delete"
    protect_attached = True
    replaced_message = None
    participant_limit = None
    participant_limit_message = "This session is full."
    auto_register_participants = False
    ```

=== "Pillar 3 -- View Lifecycle"

    ```python
    timeout = 180
    exit_policy = "disable"
    error_message = "An unexpected error occurred..."
    auto_defer = True
    auto_defer_delay = 2.5
    serialize_interactions = True
    auto_refresh_ephemeral = None  # derives from timeout; pin with True/False
    reopen_failure_message = "Could not refresh this view..."
    enable_undo = False
    undo_limit = 20
    ```

=== "Pillar 4 -- Session Membership"

    ```python
    session_continuity = False  # set True to coalesce repeat opens
    # session_id -- auto-derived, not set manually
    # shared_data -- property, reads session["shared_data"]
    # attach_child(child) -- method
    # register_participant(user_id) -- method
    # update_session(**data) -- method
    ```

=== "Pillar 5 -- Navigation"

    ```python
    auto_back_button = False
    # push(view, rebuild=) -- method
    # pop(rebuild=) -- method
    # replace(view) -- method
    ```

---

## What's Next?

- **[Core Concepts](concepts.md)** -- data flow, interaction lifecycle,
  component tiers
- **[Views](views.md)** -- lifecycle, navigation, and policies in action
- **[State Management](state.md)** -- reducers, subscribers, scoped state
- **[View Patterns](patterns.md)** -- forms, wizards, tabs, pagination
