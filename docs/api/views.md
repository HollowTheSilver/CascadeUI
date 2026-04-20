# API: Views

All view classes share a common mixin (`_StatefulMixin`) that provides state management, navigation, instance limiting, undo/redo, and lifecycle handling. The mixin is combined with either `discord.ui.View` (V1) or `discord.ui.LayoutView` (V2).

---

## Shared Constructor Parameters

These parameters apply to all view classes:

```python
context=None,          # commands.Context -- extracts user/guild/interaction
interaction=None,      # discord.Interaction -- alternative to context
timeout=180,           # Seconds before timeout (None = no timeout)
persistence_key=None,        # Stable identity for persistent data
theme=None,            # Per-view Theme override
```

Pass either `context` or `interaction` -- both extract the user, guild, and interaction for `send()`. Use `context` from prefix/hybrid commands, `interaction` from app commands or component callbacks.

## Shared Methods

These methods are available on all view classes (V1 and V2):

#### `send(...)`

Sends the view as a message. V1 accepts `content`, `embed`, `embeds`, `ephemeral`. V2 sends the view as its own content (no content/embed params).

**Return value:** the sent `discord.Message` on success, or `None` when the view was blocked before reaching Discord. Two conditions produce `None`:

1. **Instance limit rejection** -- `instance_policy = "reject"` and the user has hit `instance_limit`. The `on_instance_limit` hook fires and handles the response automatically.
2. **Participant registration failure** -- `auto_register_participants = True` and a user in `allowed_users` already occupies an instance of this view type. Rollback removes all side effects (registry, state tree, participants).

In both cases, the view is fully cleaned up -- no message was sent, no state remains. See [send() and Rollback](../guide/views.md#send-and-rollback) for usage patterns.

#### `dispatch(action_type, payload=None)`

Dispatches an action through the store with `source=self.id`. Subscriber failures are caught and logged internally -- `dispatch()` does not raise from subscriber errors.

#### `refresh(**kwargs)`

Edits the view's message with `view=self` plus any extra kwargs forwarded to `message.edit()`. Does NOT rebuild components -- call your rebuild method (e.g. `build_ui()`) first. Handles `discord.NotFound` silently if the message has been deleted. V2 callers pass no args; V1 callers pass `embed=` or `content=`.

#### `respond(interaction, content=None, *, ephemeral=False, **kwargs)`

Sends an interaction response, falling back to `interaction.followup.send()` when the response slot is already consumed. Under `serialize_interactions`, queued interactions may be auto-deferred before their callback runs; direct calls to `interaction.response.send_message()` raise `InteractionResponded` in that case. This method checks `interaction.response.is_done()` and routes transparently.

```python
# Always works, no manual is_done() check needed
await self.respond(interaction, "Not your turn!", ephemeral=True)

# Works with embeds, views, files -- any send_message kwarg
await self.respond(interaction, embed=my_embed, ephemeral=True)
```

Use `self.respond()` instead of `interaction.response.send_message()` in any CascadeUI callback that needs to send feedback to the user.

#### `open_modal(interaction, modal, *, fallback_message=None)` {#open_modal}

Opens a modal dialog, with a graceful fallback if the response slot is already consumed. `send_modal()` must be the first response to an interaction -- it cannot follow a `defer()`. Under `serialize_interactions`, a queued interaction may be auto-deferred before the callback runs. This method checks `is_done()` and sends an ephemeral fallback instead of raising `InteractionResponded`.

Returns `True` if the modal was sent, `False` if the fallback fired.

```python
await self.open_modal(interaction, modal)

# Custom fallback text
await self.open_modal(interaction, modal, fallback_message="Try again.")
```

Use `self.open_modal()` instead of `interaction.response.send_modal()` in any CascadeUI callback that needs to open a modal.

#### `attach_child(child_view)`

Registers a child view for automatic cleanup. When the parent exits or times out, all attached children that haven't finished are exited with `delete_message=True`. Enforces three invariants: self-attachment raises `ValueError`, circular chains raise `ValueError`, and re-parenting detaches from the old parent cleanly. The `parent=` kwarg on the child's constructor automates this -- `send()` calls `attach_child` on success. See [Child Attachment](../guide/views.md#child-attachment).

#### `on_message_delete()` *(async, override)*

Called when the view's Discord message is deleted externally (admin delete, bulk purge, channel delete). Default calls `exit(delete_message=False)`. Override for custom behavior (logging, re-sending). If overriding without calling `exit()`, the view remains as a ghost in the state store.

#### `on_replaced()` *(async, override)*

Called on the old view when `instance_policy = "replace"` is about to evict it. Fires before `exit()` while the view is fully intact (message, participants, channel). Default sends `replaced_message` to the channel when set and the view has participants. Override for custom notification (DMs, embeds, mentions). Errors are logged but never block the new view's `send()`.

#### `check_instance_available(*, user_id=None, guild_id=None, session_origin=None, state_store=None)` *(classmethod)*

Sync pre-check that returns `True` if a new instance slot is available, `False` if the limit would be exceeded. Counts both owners and participants. Avoids constructing the view when `__init__` is expensive. Returns `True` when no `instance_limit` is set or when scope can't be determined (missing `user_id`/`guild_id`).

#### `auto_refresh_ephemeral` *(class attribute)*

Engages the 15-minute ephemeral refresh handoff. Default `None` derives from `timeout`: ephemeral views with `timeout > 900` (or `timeout=None`) engage the handoff; shorter timeouts decline it. Set `True` to pin on, `False` to pin off.

Customization knobs (all class attributes):

| Attribute | Default | Purpose |
|---|---|---|
| `refresh_warning_seconds` | `90` | How early to swap before the 900s wall |
| `refresh_button_label` | `"Continue Session"` | Button label text |
| `refresh_button_emoji` | `"🔄"` | Button emoji (must be a valid Discord button emoji) |
| `refresh_button_style` | `ButtonStyle.primary` | Button style |

!!! warning "Emoji must be a valid Discord button emoji"
    Discord rejects Unicode *symbols* (like `↻` U+21BB from the Arrows block) as invalid button emoji even though they render as glyphs in some fonts. Valid values are Unicode *emoji* code points (typically U+1F000+) or custom Discord emoji. If the library sees Discord return error 50035 for the emoji at arming time, the library retries once without the emoji and logs a warning -- the handoff still works, but the button loses its icon.

See [Auto-Refresh for Long-Lived Ephemerals](../guide/views.md#auto-refresh-for-long-lived-ephemerals) in the guide for the full rationale, advanced customization, and ghost-panel behavior. See [Ephemeral Editability Expires After 15 Minutes](../guide/known-limitations.md#ephemeral-editability-expires-after-15-minutes) for the platform constraint.

#### `replace(view_class, interaction=None, **kwargs)`

Replaces the current view with another view class. One-way (no stack history saved).

#### `push(view_class, interaction, *, rebuild=None, **kwargs)`

Pushes the current view onto the navigation stack and navigates to a new instance of `view_class`. All constructor kwargs are auto-captured so `pop()` can reconstruct the view faithfully.

The optional `rebuild` callback is called with the new view after construction. For V2, return `None` (e.g., `rebuild=lambda v: v.build_ui()`). For V1, return a dict of kwargs for `edit_original_response` (e.g., `rebuild=lambda v: {"embed": v.build_embed()}`). Can be sync or async.

#### `pop(interaction, *, rebuild=None)`

Pops the top entry from the navigation stack, reconstructs that view with its original kwargs, and returns it. Returns `None` if the stack is empty. Non-reconstructible kwargs (`context`, `interaction`, etc.) are re-supplied by the framework.

#### `batch()`

Returns an async context manager for batched dispatch. Convenience for `self.state_store.batch()`.

#### `undo()`

Undoes the last state change for this view (requires `enable_undo = True` and `UndoMiddleware`).

#### `redo()`

Redoes the last undone state change.

#### `dispatch_scoped(data)`

Updates scoped state (requires `state_scope` to be set on the view class).

#### Named Scoped-State Accessors

Four convenience methods for reading scoped state without raw dict-chain traversal. Each defaults to the view's own `user_id`/`guild_id` and accepts explicit overrides for hub views reading other users'/guilds' slices:

- `user_scoped_state(user_id=None) -> dict` -- reads the `"user"` scope slice
- `guild_scoped_state(guild_id=None) -> dict` -- reads the `"guild"` scope slice
- `user_guild_scoped_state(user_id=None, guild_id=None) -> dict` -- reads the `"user_guild"` composite scope slice
- `global_scoped_state() -> dict` -- reads the `"global"` scope slice (single shared slot)

All return `{}` when identifiers are missing, matching `scoped_state` semantics.

#### Session Data

- `shared_data` (property, dict) -- reads the current session's `shared_data` dict. Returns `{}` if the session does not exist or has no data. Shared across all views in the same push/pop chain.
- `update_session(**data)` -- merges key-value pairs into the session's `shared_data` dict. Dispatches `SESSION_UPDATED`.

```python
# Read
lang = self.shared_data.get("lang", "en")

# Write
await self.update_session(lang="fr", difficulty="hard")
```

#### `set_class_attribute(name, value)`

Overrides a class-level policy attribute (`participant_limit`, `instance_limit`, `instance_policy`, etc.) with a per-invocation value while running the same `__init_subclass__` validator pipeline. Resolves the grammar tension where views need to parameterize a policy from a slash-command argument without bypassing validation.

```python
view = LobbyView(context=ctx)
view.set_class_attribute("participant_limit", player_count)
```

#### `make_exit_button(label="Exit", style=ButtonStyle.secondary, emoji="❌", delete_message=False, custom_id=None)`

Returns a pre-configured `StatefulButton` without adding it to the view. Use in V2 views that need to place exit buttons inside specific `ActionRow` or `Container` subtrees rather than at the top level. `add_exit_button()` continues to work for top-level placement.

#### `add_exit_button(label="Exit", style=ButtonStyle.secondary, row=None, emoji="❌", delete_message=False, custom_id=None)`

Adds an exit button that calls `self.exit()`. In V2 views, the button is wrapped in an `ActionRow`. Set `delete_message=True` to delete the message instead of disabling components. Pass `custom_id` for persistent views.

#### `await exit(delete_message=None)`

Cleans up the view: cancels tasks, unsubscribes, disables components. When `delete_message` is `None` (the default), behavior is resolved from the `exit_policy` class attribute (`"disable"` freezes, `"delete"` deletes). Pass `True` or `False` explicitly to override the policy at any call site. V2 views freeze components in place (since `edit(view=None)` would empty the message); V1 views strip the view entirely.

#### `get_theme()`

Returns the view's theme (per-view override or global default).

#### `await seed_initial_state(state)` *(override)*

Initializes per-view state slots before the first subscriber notification. Called once during `send()`, inside the registration batch, after the view is registered but before participant claiming and before the batch's `BATCH_COMPLETE` fires. Override to dispatch actions or write to `state["application"]` so subscribers see the seeded state from frame one instead of an empty slot followed by a separate seeding dispatch.

```python
async def seed_initial_state(self, state):
    if "leaderboard" not in state["application"]:
        await self.dispatch("LEADERBOARD_SEED", {"entries": []})
```

The hook receives the live store state dict. Dispatches issued from inside join the surrounding batch, so seed work collapses into the view's `VIEW_CREATED` notification cycle. Default is a no-op.

#### `on_state_changed(state)` *(override)*

Called when a matching state change occurs. The default implementation looks up `build_ui()` on the subclass and, if present, calls it followed by `refresh()`. Both sync and async `build_ui()` are supported.

If `build_ui()` returns a `dict`, the dict is splatted as keyword arguments into `refresh()`. This is the V1 idiom for re-rendering an embed:

```python
def build_ui(self):
    return {"embed": self._build_embed()}
```

V2 views return `None` (the default) and mutate the component tree directly inside `build_ui()`. Override `on_state_changed()` itself only when you need behavior beyond rebuild + refresh.

Concurrent calls are coalesced automatically - if a second state change arrives while the first is being processed, the update re-runs once with the latest state after completing. See [Concurrent Updates](../guide/state.md#concurrent-updates).

#### `state_selector(state)` *(override)*

Returns a slice of state. If the return value hasn't changed, `on_state_changed` won't fire.

#### `await register_participant(user_id, *, interaction=None) -> bool`

Registers a non-owner user in the instance index so that `instance_limit` and `participant_limit` apply to them. Returns `True` on success (including the owner short-circuit), `False` on rejection. Never raises.

`user_id` accepts either an `int` or any object with an `int .id` attribute (`discord.Member`, `discord.User`, `discord.Object`) -- coercion happens silently at the entry point.

Two rejection paths fire automatically:

- **Per-user instance collision** (the joiner already holds an instance of this view type): the library calls `self.on_instance_limit(error)` with the joiner's interaction temporarily swapped in, so the rejection ephemeral targets the joiner -- not the view owner.
- **View capacity overflow** (the view is at `participant_limit`): the library calls `self.on_participant_limit(user_id, interaction=interaction)`.

Pass the `interaction` keyword when the registration is driven by a button or select callback so the rejection hooks can respond ephemerally on the right interaction. Skips silently when `user_id` matches the view owner.

#### `unregister_participant(user_id)`

Removes a participant from the session index. Use when a participant leaves a multi-user view (e.g., a player disconnects mid-game).

#### `interaction_check(interaction)` *(override)*

Called before every component callback. Returns `True` to allow, `False` to block. By default, checks `allowed_users` first (if set), then falls back to `owner_only`.

### Shared Properties

- `id` (str): UUID instance identifier
- `persistence_key` (str | None): Stable data identity key
- `message` (Message | None): The sent message, if any
- `state_store` (StateStore): The singleton store
- `session_id` (str | None): Session identity for this view. Auto-derived at `__init__` as `<module.QualName>:user_<id>:<8hex>` unless `session_continuity = True` is set on the class (which drops the `:<8hex>` suffix) or an explicit `session_id=` kwarg is passed.
- `scoped_state` (dict): The scoped state for this view's user/guild (empty dict if no state_scope)
- `shared_data` (dict): The current session's `shared_data` dict (empty dict if no session or no data)

### Shared Class Attributes

!!! note "Validated at subclass-definition time"
    Class attributes whose values are bounded -- string enums (`instance_policy`, `instance_scope`, `state_scope`, `replace_policy`, `exit_policy`), positive integers (`instance_limit`, `participant_limit`, `undo_limit`), positive floats (`auto_defer_delay`), and booleans (`owner_only`, `auto_defer`, `auto_register_participants`, etc.) -- are validated by `_StatefulMixin.__init_subclass__` when a subclass is defined. A typo like `instance_policy = "rejct"` raises `ValueError` at module import with a message naming the class, the attribute, the bad value, and the valid options. Validation runs once per subclass at class-definition time and inspects only `cls.__dict__`, so per-subclass cost is `O(overrides-on-this-subclass)` -- inherited defaults pay zero cost. There is no per-instantiation overhead.

- `subscribed_actions` (set[str] | None): Action types to listen for. Default is an empty set (no notifications). Set the actions your view needs to react to. Set to `None` to receive all actions (not recommended). Every matching dispatch fires the view's `on_state_changed()`, so subscribe only to actions the view reads.
- `state_scope` (str | None): `"user"`, `"guild"`, `"user_guild"`, `"global"`, or `None`. Determines state scoping.
- `enable_undo` (bool): Enable undo/redo for this view (default: `False`).
- `undo_limit` (int): Max undo stack depth (default: `20`).
- `auto_back_button` (bool): Automatically add a back button when pushed (default: `False`).
- `instance_limit` (int | None): Maximum active instances within the instance scope. `None` (default) means unlimited.
- `instance_scope` (str): How instances are grouped for limit counting. One of `"user"`, `"guild"`, `"user_guild"` (default), or `"global"`.
- `instance_policy` (str): What to do when the limit is exceeded. `"replace"` (default) exits the oldest instances. `"reject"` blocks `send()` -- `on_instance_limit` fires and `send()` returns `None`.
- `owner_only` (bool): Only the creating user can interact with the view (default: `True`). Set to `False` for shared views.
- `unauthorized_message` (str): Ephemeral message sent to non-owners (default: `"You cannot interact with this."`).
- `error_message` (str): Description used in the default `on_error` red embed (default: `"An unexpected error occurred while processing your interaction."`).
- `reopen_failure_message` (str): Ephemeral message sent when the ephemeral refresh button fails to reconstruct the view (default: `"Could not refresh this view. Please reopen from the original command."`). Used by the default `on_reopen_failure` hook. Only relevant for ephemeral views where the auto-refresh handoff is engaged (either `auto_refresh_ephemeral = True` or derived from a timeout greater than `900`).
- `allowed_users` (frozenset[int]): When non-empty, only these user IDs can interact. Overrides `owner_only` completely. Empty (default) defers to `owner_only`. Stored as a `frozenset` and exposed via a property pair: assignment coerces both `int` and snowflake-shaped objects (`Member`, `User`, `Object`) at the setter, so `view.allowed_users = {member, 12345}` works. Direct mutation is unsupported -- to add a user after construction, use `await view.register_participant(user_id)` (which writes to `_participants`, not `allowed_users`) or rebind the attribute: `view.allowed_users = view.allowed_users | {new_id}`.
- `participant_limit` (int | None): Maximum total view occupants (owner + participants). `None` (default) means unlimited. Owner counts toward the cap, so `participant_limit = 8` admits one host plus seven joiners. Enforced inside `register_participant`.
- `participant_limit_message` (str): Ephemeral message sent when `register_participant` rejects a joiner due to view-capacity overflow (default: `"This session is full."`). Used by the default `on_participant_limit` hook.
- `auto_register_participants` (bool): When `True`, `send()` iterates `allowed_users` and calls `register_participant` for each non-owner before the Discord send. All-or-nothing rollback: any rejection unregisters every previously-claimed slot AND `state_store.unregister_view(self.id)`, then `send()` returns `None`. A rejection therefore leaves zero side effects -- no message, no registry entry, no half-claimed participants. Default: `False`.
- `protect_attached` (bool): When `True` (default), views with active participants or attached children from other users are excluded from replacement candidates during instance enforcement. If no replaceable views remain, falls back to reject behavior (`on_instance_limit` fires). Same-user attachments do not trigger protection. Has no effect on views without attachments or when `instance_policy = "reject"`. Set to `False` for views where silent replacement is expected (e.g. spectator panels).
- `replaced_message` (str | None): Static message sent to the channel when this view is replaced and has active participants. `None` (default) means silent replacement. Used by the default `on_replaced` hook.
- `replace_policy` (str): What `instance_policy="replace"` does to the old view's message. `"delete"` (default) removes it; `"disable"` freezes its components in place. Only governs the instance-replace transition.
- `exit_policy` (str): What bare `exit()` calls do when no `delete_message` argument is supplied. `"disable"` (default) freezes the components in place; `"delete"` removes the message. Always overridden by an explicit `delete_message=` argument or by an `exit()` method override. Independent of `replace_policy`.
- `auto_defer` (bool): Enable the auto-defer safety net (default: `True`).
- `auto_defer_delay` (float): Seconds before auto-deferring (default: `2.5`).
- `serialize_interactions` (bool): Serialize rapid button clicks with an `asyncio.Lock` (default: `True`). Set to `False` for views that handle parallel callbacks.
- `session_continuity` (bool): Governs `session_id` auto-derivation polarity. Default `False` gives every invocation a per-instance UUID suffix, so repeat opens of the same view class are independent sessions with their own nav stack, undo timeline, and `shared_data`. Set to `True` on views that want repeat-open state coalescing (undo history surviving close-and-reopen, `shared_data` continuity across gestures); the opt-in collapses derivation back to the class-coalesced shape. Push/pop chains stay on one session regardless because `_navigate_to` forwards `session_id` explicitly.

---

## V2 Views

### `StatefulLayoutView`

Base class for V2 views. Extends `discord.ui.LayoutView`.

```python
StatefulLayoutView(context=None, **kwargs)
```

V2 views ARE the message content -- `send()` takes no `content` or `embed` params. Build the component tree in `__init__` or an async builder, then call `send()`.

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

#### Class Attributes

| Attribute | Default | Purpose |
|---|---|---|
| `active_tab_style` | `ButtonStyle.primary` | Style for the currently active tab button |
| `inactive_tab_style` | `ButtonStyle.secondary` | Style for inactive tab buttons |
| `tab_overflow_policy` | `"fill"` | Row-distribution strategy when tabs exceed the five-per-row ActionRow cap. Presets: `"fill"`, `"balance"`, `"pin_first"`, `"pin_last"`. Or `tuple[int, ...]` for explicit per-row widths. |

#### Methods

##### `await _refresh_tabs()`

Re-runs the current tab's builder and edits the message. Use from Refresh button callbacks.

##### `on_tab_switched(self, index)` *(override)*

Called after a tab switch completes. Override to inject analytics, async setup, or validation logic without reimplementing the tab-switch closure.

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
    **kwargs,
)
```

- `builder(self)` -- async, returns a list of V2 components for the step
- `validator(self, interaction)` -- async, returns `True` to proceed or `False` to block

##### `on_finish(self, interaction)` *(override)*

Called when the final step passes validation. Default implementation calls `self.exit()`. Override to customize the finish behavior (e.g. build a summary card, save data, navigate).

#### Navigation Button Customization

Back, Next, and Finish buttons are added automatically. Back is disabled on the first step. Next is replaced with Finish on the last step. All three support class-attribute customization:

| Attribute | Default | Purpose |
|---|---|---|
| `back_button_label` | `"Back"` | Back button label |
| `back_button_emoji` | `None` | Back button emoji |
| `back_button_style` | `ButtonStyle.secondary` | Back button style |
| `next_button_label` | `"Next"` | Next button label |
| `next_button_emoji` | `None` | Next button emoji |
| `next_button_style` | `ButtonStyle.primary` | Next button style |
| `finish_button_label` | `"Finish"` | Finish button label |
| `finish_button_emoji` | `None` | Finish button emoji |
| `finish_button_style` | `ButtonStyle.success` | Finish button style |
| `step_indicator_label` | `None` | Callable `(current, total) -> str`. Default: `"Step {current}/{total}"` |

---

### `FormLayoutView`

V2 form with native text, select, and boolean fields.

```python
FormLayoutView(
    context=None,
    title="Form",
    fields=[
        {"id": str, "type": "text"|"select"|"boolean", "label": str,
         "validators": [...], "placeholder": str, "default": Any, "required": bool},
        ...
    ],
    **kwargs,
)
```

Displays form state as a V2 component tree (Container + TextDisplay). `text` fields are grouped behind a single "Edit Text Fields" button that opens a `Modal` populated with one `TextInput` per declared text field (Discord caps this at **5 text fields per form**; construction raises `ValueError` above the limit). Submitted values flow back into `form.values` and the view rebuilds. `select` and `boolean` fields render inline as interactive components.

Validators declared in the field dict attach directly to the generated `TextInput` and run server-side after submission.

#### Text-Edit Button Customization

Three class attributes mirror the `refresh_button_*` grammar:

| Attribute | Default | Purpose |
|---|---|---|
| `text_edit_button_label` | `None` | `None` → smart default: `"Edit {label}"` for one text field, `"Edit Text Fields"` for multiple. |
| `text_edit_button_emoji` | `"\u270f\ufe0f"` (✏️) | Emoji on the grouped button. Set `None` to disable. |
| `text_edit_button_style` | `ButtonStyle.secondary` | Button style. |

`FormView` (V1) exposes the same three attributes and 5-field ceiling.

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

##### `on_page_changed(self, page)` *(override)*

Called after a page change completes. Override to react to page changes without reimplementing the navigation wiring.

#### Navigation Button Customization

All five navigation buttons (first, previous, indicator, next, last) support label/emoji/style class-attribute overrides. See `PaginatedView` below for the shared attribute names.

---

### `MenuLayoutView`

V2 category-based navigation hub with push/pop drill-down.

```python
MenuLayoutView(
    context=None,
    categories=[
        {"label": str, "view": ViewClass, "emoji": str,
         "description": str, "style": ButtonStyle, "rebuild": callable},
        ...
    ],
    **kwargs,
)
```

Each category generates an `action_section()` item that pushes to the specified view class when clicked. The `description`, `emoji`, `style`, and `rebuild` keys are optional.

#### Class Attributes

| Attribute | Default | Purpose |
|---|---|---|
| `menu_style` | `ButtonStyle.primary` | Default button style for all category items |
| `auto_exit_button` | `True` | Whether to add an exit button at the bottom |

#### Override Hooks

##### `_build_header()` *(override)*

Returns V2 components (list or single) for the area above category items. Default returns `[]`.

##### `_build_footer()` *(override)*

Returns V2 components (list or single) for the area below category items. Default returns `[]`.

##### `_build_category_item(category, index)` *(override)*

Controls how a single category is rendered. Default creates an `action_section()`.

##### `on_category_selected(category, index, interaction)` *(override)*

Called before pushing to the selected category's view. Default is a no-op. Override for analytics, guards, or pre-push setup.

#### Properties

- `categories` (list[dict]): The category list this menu was constructed with.

---

### `PersistentLayoutView`

V2 persistent view that survives bot restarts.

```python
PersistentLayoutView(
    *args,
    persistence_key=...,    # Required
    **kwargs,
)
```

Same requirements and behavior as `PersistentView` -- `persistence_key` required, all interactive components need explicit `custom_id`, `timeout` forced to `None`, `owner_only` defaults to `False`. Auto-registers subclasses via `__init_subclass__` into the same registry as `PersistentView`.

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
    persistence_key=...,    # Required
    **kwargs,
)
```

- `timeout` is forced to `None`
- `owner_only` defaults to `False`
- `persistence_key` must be provided (raises `ValueError`)
- All components must have explicit `custom_id` values
- Cannot be sent as ephemeral (`send(ephemeral=True)` raises `ValueError`)
- Duplicate `persistence_key` registration exits the previous view instance

#### Methods

##### `on_restore(bot)` *(override)*

Called after the view is restored on bot restart.

---

### V1 Patterns

#### `MenuView`

```python
MenuView(
    context=None,
    categories=[
        {"label": str, "view": ViewClass, "emoji": str,
         "style": ButtonStyle, "rebuild": callable},
        ...
    ],
    **kwargs,
)
```

V1 equivalent of `MenuLayoutView`. Each category generates a `StatefulButton`. Override `build_embed()` for the hub card. Override `_build_extra_items()` to add controls alongside category buttons. Override `_build_category_button(category, index)` to customize individual buttons.

Supports the same `menu_style`, `auto_exit_button`, and `on_category_selected` as `MenuLayoutView`.

#### `TabView`

```python
TabView(context=None, tabs={"Name": async_builder_fn, ...}, **kwargs)
```

Supports the same `tab_overflow_policy`, `active_tab_style`, `inactive_tab_style`, `on_tab_switched`, and `_build_tab_rows` as `TabLayoutView`. V1 applies the per-row split by assigning `button.row`; V2 wraps each row in an `ActionRow`.

#### `WizardView`

```python
WizardView(
    context=None,
    steps=[{"name": str, "builder": async_fn, "validator": async_fn}, ...],
    **kwargs,
)
```

Override `async def on_finish(self, interaction)` to customize finish behavior. Supports the same navigation button customization attributes as `WizardLayoutView`.

#### `FormView`

```python
FormView(
    context=None,
    title="Form",
    fields=[{"id": str, "type": "text"|"select"|"boolean", "label": str, "validators": [...], ...}, ...],
    **kwargs,
)
```

#### `PaginatedView`

```python
PaginatedView(context=None, pages=[Embed | str | dict, ...], **kwargs)
```

Pages can be `Embed` objects, strings, or dicts with `"embed"` and/or `"content"` keys.

**Class Attributes:**

- `jump_threshold` (int): Minimum page count at which first/last and go-to-page buttons appear (default: `5`). A view with five or more pages surfaces the jump controls.

**Class Methods:**

- `await PaginatedView.from_data(items, per_page, formatter, **kwargs)` -- Chunks items and applies formatter (returns embed/str/dict). Stores `per_page` and `formatter` for `refresh_data()`.

**Instance Methods:**

- `await refresh_data(items)` -- Re-paginates with new data. Raises `RuntimeError` if not created via `from_data()`.
- `_build_extra_items()` *(override)* -- Hook for adding components below navigation buttons (rows 1-4).

---

## `PersistenceMiddleware(manager=None, *, backend=None, registry=None, application=None, bot=None, migrators=None)`

Write-through middleware that owns the full persistence pipeline. Install via `setup_middleware` once in `setup_hook`, after loading cogs.

- Without `bot`: data-only persistence
- With `bot`: also re-attaches PersistentView and PersistentLayoutView instances, and installs the message-deletion cleanup listener
- `backend`: a `PersistenceBackend` instance (e.g. `SQLiteBackend`, `InMemoryBackend`) used as the shorthand for any namespace not configured explicitly
- `registry`, `application`: per-namespace configs (`RegistryPersistence`, `ApplicationPersistence`) that override the shorthand. Scoped state rides under the application namespace -- opt a scoped slot in via `persistent_slots = ("scoped",)` on the view class.

```python
from cascadeui import setup_middleware
from cascadeui.state.middleware import PersistenceMiddleware
from cascadeui.persistence import SQLiteBackend

await setup_middleware(
    PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=self),
)
```

The reattach summary (`{"restored": [...], "skipped": [...], "failed": [...], "removed": [...]}`) is available via `await store.persistence_manager.reattach_persistent_views()`.

See [docs/api/persistence.md](persistence.md) for the full API reference.

---

## `InstanceLimitError`

Exception raised when an instance limit is reached.

```python
from cascadeui import InstanceLimitError
```

### Attributes

- `view_type` (str): The class name of the view that hit the limit
- `limit` (int): The instance limit value that was exceeded
- `blocked_user_id` (int | None): The user ID that was blocked. Set when raised by `register_participant()`, `None` when raised by `send()`.

### When it is raised

- **Reject policy**: Always raised when a new view would exceed `instance_limit` with `instance_policy = "reject"`.
- **PersistentView protection**: Raised when a non-persistent view attempts to replace a `PersistentView` under the replace policy.
- **Participant registration**: Not raised -- `register_participant()` returns `bool` instead. Per-user instance collisions fire `on_instance_limit` and return `False`.

---

## Utility Decorators

Optional decorators for wrapping callbacks in error boundaries, retry logic, or safe execution. All are exported from the package root.

### `@with_error_boundary(name=None)`

Wraps an async callable so exceptions are logged with context instead of raised. Returns `None` when the wrapped callable raises. Use on background or fire-and-forget paths where a raised exception would otherwise be swallowed by the asyncio event loop.

```python
from cascadeui import with_error_boundary

@with_error_boundary("sync_scores")
async def sync_scores(user_id):
    ...
```

### `@with_retry(config=None)`

Retries an async callable on failure with exponential backoff. Accepts an optional `RetryConfig(max_attempts, base_delay, max_delay, exceptions)`; defaults to three attempts with a 1-second base delay.

```python
from cascadeui import with_retry
from cascadeui.utils.errors import RetryConfig

@with_retry(RetryConfig(max_attempts=5, base_delay=2.0))
async def fetch_profile(user_id):
    ...
```

### `safe_execute(coro, default=None, name=None)`

One-shot wrapper that awaits a coroutine and returns `default` on exception (with a logged traceback). Pair with the decorators when the call site is not the right place to attach an error boundary.

```python
from cascadeui import safe_execute

result = await safe_execute(fetch_profile(user_id), default={})
```

---

## Task Manager

### `get_task_manager()`

Returns the process-wide `TaskManager` singleton. The manager tracks background tasks per owner and cancels them cleanly on view exit or bot shutdown. Views own task creation and cancellation implicitly through `_StatefulMixin` -- direct use is only needed for standalone background work outside a view's lifecycle.

```python
from cascadeui import get_task_manager

tm = get_task_manager()
tm.create_task("my_worker", poll_loop())
# Later:
await tm.cancel_tasks("my_worker")
```

See `cascadeui/utils/tasks.py` for the full TaskManager API.
