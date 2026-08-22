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

Pass either `context` or `interaction` -- both extract the user, guild, and interaction for `send()`. Use `context` from prefix/hybrid commands, `interaction` from app commands or component callbacks. A bare `discord.TextChannel` has no `.author`, so passing one as `context` derives no `user_id` and no `session_id`; the view is ownerless. See [View identity](../guide/persistence.md#view-identity-user_id-and-session_id-follow-the-construction-context) for the full model.

!!! warning "Reserved constructor parameters"
    Eight constructor kwargs already mean something to the library: `context`,
    `interaction`, `message`, `state_store`, `session_id`, `user_id`,
    `guild_id`, and `parent`. Application data under one of these names is not
    stored, it is read: `user_id` names the view's owner, and access control,
    session derivation, and instance scoping all consult it. An internal
    account id passed as `user_id` turns the access check into one no clicker
    can pass. Give application IDs a kwarg name of your own.

    Naming an owner who is not the clicker is a supported shape: widen
    `allowed_users` to whoever should be able to click. The library warns at
    send only when both halves of the mistake are present: the id is not a
    Discord ID, and it locks out the author who built the view.

    `is_snowflake(value)` is the check behind that warning. It decodes the
    creation timestamp packed into an integer and reports whether it falls in
    the window real Discord IDs occupy. It confirms an id could be a Discord
    ID, never that the user exists; call it directly to screen an untrusted
    integer before trusting it as a `user_id`.

## Shared Methods

These methods are available on all view classes (V1 and V2):

#### `send(...)`

Sends the view as a message. V1 accepts `content`, `embed`, `embeds`, `file`, `files`, `allowed_mentions`, `ephemeral`. V2 accepts `file`, `files`, `allowed_mentions`, `ephemeral` (V2 sends the view as its own content, so no content/embed params). The `file` / `files` pair mirrors discord.py's `Messageable.send` signature and pairs with the V2 media builders (`gallery`, `image_section`, `file_attachment`) for `attachment://` references. See [Local file attachments](../guide/components.md#local-file-attachments).

`allowed_mentions` overrides the [`allowed_mentions` class attribute](#allowed_mentions) for one send. When both are `None`, the bot's client-level rules apply.

The V1 patterns (`PaginatedView`, `TabView`, `WizardView`, `FormView`, `MenuView`) supply their own content when the caller passes neither `embed` nor `content`, so `await view.send()` renders the first page, tab, step, or hub card. An explicit `embed=` or `content=` wins. See [View Patterns](../guide/patterns.md).

**Return value:** the sent `discord.Message` on success, or `None` when the view was blocked before reaching Discord. Three conditions produce `None`:

1. **`on_pre_send` veto** -- the override returned `False`. No message ships, no state registers, and the interaction's response slot stays open for the override to respond to the user.
2. **Instance limit rejection** -- `instance_policy = "reject"` and the user has hit `instance_limit`. The `on_instance_limit` hook fires and handles the response automatically.
3. **Participant registration failure** -- `auto_register_participants = True` and a user in `allowed_users` already occupies an instance of this view type. Rollback removes all side effects (registry, state tree, participants).

In each case, the view is fully cleaned up -- no message was sent, no state remains. See [send() and Rollback](../guide/views.md#send-and-rollback) for usage patterns.

All three are decided before the Discord call, which is what makes `None` mean "nothing happened". Once the message is live the rollback path is behind it, so a failure in the bookkeeping that follows the send degrades and logs what is inactive on the message rather than reporting the send as failed. `send()` still returns the message. Retrying on the return value therefore never posts a second copy.

#### `dispatch(action_type, payload=None)`

Dispatches an action through the store with `source=self.id`. Subscriber failures are caught and logged internally -- `dispatch()` does not raise from subscriber errors.

#### `validate()`

Raises `ValueError` if the view's component tree is one Discord would reject: `custom_id` uniqueness and length on every interactive node, and, for V2 views, the structural placement walk.

```python
from cascadeui import MAX_MESSAGE_COMPONENTS
from cascadeui.testing import stub_client

view = MyLeaderboard(user_id=1, guild_id=2, bot=stub_client())
await view.on_load()          # composes the tree
view.validate()               # raises if Discord would reject it
assert view.total_components <= MAX_MESSAGE_COMPONENTS
```

These are the same checks the library runs itself at three seams: the initial send, every `refresh()`, and every push/pop edit. `validate()` adds no check of its own and enforces nothing at runtime that was not already enforced; it exists so a test can reach them without a Discord connection.

Bind a client to any pattern whose composition depends on one, or the tree measured is not the tree that ships. A section-mode leaderboard renders a four-node `image_section` per row with a client bound and a one-node `TextDisplay` without, so a five-row page differs by fifteen components. [`stub_client()`](#stub_client) opens no connection and reports the empty user cache a live bot reports for a member it has not seen, so the composed tree matches the rendered one. A persistent view takes its client through `on_bind(stub_client())` rather than a `bot=` kwarg.

`validate()` counts nothing, by design: a tree can never exist over the per-message cap, because `add_item` refuses the node that would cross it. `total_components` is the budget read, and `count_components(item)` reports what a subtree would add before you add it.

#### `total_components` *(V2 property)* {#total_components}

How many components the view currently holds, counted recursively: every Container, Section, ActionRow, button, select, and text node, a Section's accessory included, and never the view itself. This is the number Discord counts and discord.py enforces against.

```python
from cascadeui import MAX_MESSAGE_COMPONENTS, count_components

if view.total_components + count_components(card) > MAX_MESSAGE_COMPONENTS:
    card = trimmed_card()
view.add_item(card)
```

A tree can never exist over the cap, since `add_item` refuses the node that would cross it, so this reports a live count rather than a violation to find later. Exactly `MAX_MESSAGE_COMPONENTS` is legal; the next node raises.

#### `count_components(item)` {#count_components}

Module-level function, exported from the package root. Counts `item` and everything under it the way Discord counts, so the two numbers in a budget check come from the same source the library's own enforcement message reports.

#### `count_characters(item)` {#count_characters}

Module-level function, exported from the package root. Sums the display text in `item` and everything under it. Only `TextDisplay` content counts, the same accounting `content_length()` uses, so a button label or a select placeholder occupies the message without reaching either total.

The character counterpart of [`count_components`](#count_components), so both budgets read from one place. It also retires the workaround of adding a subtree to a throwaway view to reach `content_length()`, which borrows that view's component limit and raises about components when the subtree is over that budget.

```python
if view.content_length() + count_characters(card) > MAX_MESSAGE_CHARACTERS:
    card = shorter_card()
```

Passing a view raises `TypeError`; read `view.content_length()` for a view's own total.

#### `stub_client()` {#stub_client}

`from cascadeui.testing import stub_client`

Returns a `StubClient` (a `discord.Client` subclass importable from the same module) that never connects, for measuring a view offline. Composition that depends on a client differs without one: a section-mode leaderboard row resolves an avatar into a four-node `image_section` when a client is bound and renders a one-node `TextDisplay` when none is, so a five-row page differs by fifteen components between the tree a test builds and the tree that ships. The stub reports the empty user cache a live bot reports for a member it has not seen, so the composed tree matches.

Construction opens no connection and needs no token. A persistent view takes its client through `on_bind(stub_client())` rather than a `bot=` kwarg.

#### `stub_interaction()` {#stub_interaction}

`from cascadeui.testing import stub_interaction`

Returns a `StubInteraction` that records what is said through it instead of sending it: the double [`Modal.submit`](components.md#await-modalsubmitinteraction-values-bool) expects. It carries a response slot that tracks whether it is spent, a followup, and a user id, which is what the library reads off a real one.

```python
interaction = stub_interaction()

assert await modal.submit(interaction, {"Name": "ab"}) is False
assert "at least 5" in interaction.replies[0]
```

`replies` merges the response slot and the followup into one list. Which of the two carries a rejection depends on the ack backstop's timing, an ordering internal to the library, so a test asserts on the message without predicting it. `deferred` reports whether the submission was acknowledged, and `answered` is true when either happened.

#### `refresh(**kwargs)`

Edits the view's message with `view=self` plus any extra kwargs forwarded to `message.edit()`. Does NOT rebuild components -- call your rebuild method (e.g. `build_ui()`) first. Handles `discord.NotFound` silently if the message has been deleted. V2 callers pass no args; V1 callers pass `embed=` or `content=`.

A transport failure (a request that never reached Discord, which raises from aiohttp and carries no HTTP status) is swallowed rather than raised: the edit is dropped, the render baseline is cleared so the next state change re-ships, and `refresh_degraded` reports it. Raising would surface a network blip as `on_error`'s failure card over a repaint that merely needs repeating. See [Transport Failures Degrade Quietly](../guide/known-limitations.md#transport-failures-degrade-quietly).

#### `restore_on_dropped_render(*attributes)`

Context manager. Snapshots each named view attribute on entry and rebinds it if the render inside the block was dropped by a transport failure, so a view-local write and the screen cannot disagree.

Pass `rebuild=` to recompose the tree from the restored values. A V2 tree *is* the content, so rebinding the attribute is only half the rollback: without a rebuild the tree keeps what the dropped render composed, and the next `refresh()` that does not rebuild first ships a screen the restored attribute no longer names. `rebuild` runs only on a drop, after the attributes are back, and is not re-rendered -- the render being undone is the one that failed, and the next one carries the corrected tree. It must be synchronous, since the restore happens at a synchronous context-manager exit; an async callable is rejected at the call with a message naming the alternative.

```python
with self.restore_on_dropped_render("_confirming", rebuild=self.build_ui):
    self._confirming = True
    self.build_ui()
    await self.refresh()
```

A V1 view carrying its body on an `embed` or `content` kwarg needs no rebuild -- the tree is not the content there.

The snapshot holds each attribute's value. A name rebound inside the block comes back; a list or dict edited in place does not, because the snapshot and the attribute are the same object. Flags and cursors are what this is for. Rebuild a collection from the restored cursor rather than editing it under the manager.

When two views are involved, the manager reads the flag of the view it is called on. A caller deciding on one view while a sibling's render is the one that matters opens the block on whichever view performs the refresh.

The shape it exists for is a control armed on one press and executed on the next. If the arming render never reaches Discord, the button on screen still looks unarmed, so the obvious response is to press it again -- and that press executes, because the flag is already set. A dropped packet turns a two-press confirmation into one, on exactly the controls that ask for confirmation because they are destructive.

```python
with self.restore_on_dropped_render("_confirming"):
    self._confirming = True
    self.build_ui()
    await self.refresh()
```

Nothing is restored when the render lands, nor when the block raises: an exception is its own signal and the caller owns the recovery. Entry clears [`refresh_degraded`](#refresh_degraded), so a block whose refresh sits behind a conditional that did not run keeps its write instead of answering for an earlier drop. The flag stays available directly for anything the snapshot shape does not fit.

#### `refresh_degraded`

Read-only `bool`. `True` when the most recent `refresh()` was dropped by a transport failure, `False` otherwise. Reset at the top of every `refresh()`, so it always describes the latest call.

Read it when the caller changed something *before* the render that should not stand if the render never landed. A paginated view is the worked example: the page cursor advances first, so a dropped edit otherwise leaves the reader on the previous page with the cursor already moved.

```python
before = self.current_page
await self.refresh()
if self.refresh_degraded:
    self.current_page = before
```

Only kwargs every edit endpoint accepts are allowed: `content`, `embed`, `embeds`, `attachments`, `allowed_mentions`. `refresh()` picks between the interaction, webhook, and channel endpoints at runtime, so a kwarg only one of them takes (`suppress`, `suppress_embeds`, `delete_after`) raises `TypeError` rather than working intermittently. Edit `view.message` directly for a one-off that needs a non-portable field.

#### `respond(interaction, content=None, *, ephemeral=False, **kwargs)`

Sends an interaction response, falling back to `interaction.followup.send()` when the response slot is already consumed. Under `serialize_interactions`, queued interactions may be auto-deferred before their callback runs; direct calls to `interaction.response.send_message()` raise `InteractionResponded` in that case. This method checks `interaction.response.is_done()` and routes transparently.

```python
# Always works, no manual is_done() check needed
await self.respond(interaction, "Not your turn!", ephemeral=True)

# Works with embeds, views, files -- any send_message kwarg
await self.respond(interaction, embed=my_embed, ephemeral=True)
```

Use `self.respond()` instead of `interaction.response.send_message()` in any CascadeUI callback that needs to send feedback to the user.

!!! warning "Don't pass a CascadeUI view as `view=`"
    `respond()` forwards `view=` straight to `send_message`, which bypasses the
    view's own `send()` and its registration. A stateful view sent that way is
    live but invisible to the inspector, instance limits, and state cleanup, and
    its timeout later fires a destroy for a view that was never created. Send a
    CascadeUI view through its own `send()`
    (`await MyView(interaction=interaction).send(ephemeral=True)`); `respond()`
    warns when it detects one. `view=` is fine for a plain `discord.ui.View`.

#### `respond_safe(interaction, content=None, *, ephemeral=False, **kwargs)` {#respond_safe}

The free-function sibling of `respond()`, for contexts with no view instance -- chiefly the `@classmethod` hooks on `RolesLayoutView` (`on_role_assigned` and siblings), which dispatch through `DynamicPersistentButton` and have no `self`. Same `is_done()`-aware behavior: it sends the response, or falls back to a followup when the ack backstop has already fired. Import from the package root (`from cascadeui import respond_safe`).

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

#### `parent`

Read-only property holding the view this one is attached to, or `None` for a root. A child constructed with `parent=` reads it before the send that attaches it, and every child reads it after, so a child panel that needs its parent to read state or call `respond` has it without storing the same view a second time under its own name. Mutation goes through `attach_child` on the parent, which enforces the invariants above.

#### `on_message_delete()` *(async, override)*

Called when the view's Discord message is deleted externally (admin delete, bulk purge, channel delete). Default calls `exit(delete_message=False)`. Override for custom behavior (logging, re-sending). If overriding without calling `exit()`, the view remains as a ghost in the state store.

#### `on_message_gone()` *(async, override)*

Called when `refresh()` issues an edit that returns `discord.NotFound` (the message was deleted out from under the view). The library nulls `self._message` and fires this hook so a consumer tracking the message in its own store can reconcile that reference; key the reconcile on the view's stable identity (`persistence_key`), since `self._message` is already nulled. Default is a no-op. Unlike `on_message_delete()`, this hook does not exit the view (the gateway event owns teardown), which keeps it safe to fire from the reactive refresh path. The edit-path counterpart to the gateway-driven `on_message_delete()`; both can fire for one deletion, so make the reconcile idempotent. It may run while the view's update lock is held, so an override may do I/O but should not dispatch a state change back into this view.

#### `on_replaced()` *(async, override)*

Called on the old view when `instance_policy = "replace"` is about to evict it. Fires before `exit()` while the view is fully intact (message, participants, channel). Default sends `replaced_message` to the channel when set and the view has participants. Override for custom notification (DMs, embeds, mentions). Errors are logged but never block the new view's `send()`.

#### `check_instance_available(*, user_id=None, guild_id=None, session_origin=None, state_store=None)` *(classmethod)*

Sync pre-check that returns `True` if a new instance slot is available, `False` if the limit would be exceeded. Counts both owners and participants. Avoids constructing the view when `__init__` is expensive. Returns `True` when no `instance_limit` is set or when scope can't be determined (missing `user_id`/`guild_id`).

#### `auto_refresh_ephemeral` *(class attribute)*

Engages the 15-minute ephemeral refresh handoff. Default `None` derives from `timeout`: ephemeral views with `timeout > 900` (or `timeout=None`) engage the handoff; shorter timeouts decline it. Set `True` to pin on, `False` to pin off. The attribute is a declaration the library never modifies: reading it always returns what the class or the caller set, and the derived answer is tracked internally.

The derivation and the arming deadline are fixed at `send()`; navigation never restarts the clock. A `push()`/`pop()` destination arms against the chain's deadline under its own flag: an explicit `False` pins the handoff off on every path, an explicit `True` engages it even when the original send declined, and `None` inherits the effective policy of the view it was pushed or popped from -- the nearest hop's explicit setting when one exists, else the answer the original send derived. The destination's own `timeout` is not consulted, and its `auto_refresh_ephemeral` is not modified. Whatever engages the handoff, the button arms before the original send's 15-minute window closes.

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

#### `replace(view_or_class, interaction=None, **kwargs)`

Replaces the current view with another view. One-way (no stack history saved). `view_or_class` accepts either a view class (constructed internally with `**kwargs`) or a pre-constructed view instance (used directly; `**kwargs` must be empty).

#### `push(view_or_class, interaction, *, rebuild=None, **kwargs)`

Pushes the current view onto the navigation stack and navigates to the next view. `view_or_class` accepts either a view class (constructed internally with `**kwargs`; constructor kwargs auto-captured so `pop()` can reconstruct faithfully) or a pre-constructed view instance (used directly; `**kwargs` must be empty). The instance form pairs with the classmethod constructors, like `PaginatedLayoutView.from_data` (awaited) and `from_cursor` (called bare), where the view is built before the navigation call.

Passing extra kwargs alongside an instance raises `TypeError` -- the instance is already initialized.

The Discord message edit fires on every push regardless of whether `rebuild` is supplied. `rebuild` is an optional pre-edit hook for views that need post-construction setup: V2 views with empty trees can run `v.build_ui()`, V1 views can return a dict of edit kwargs (e.g., `rebuild=lambda v: {"embed": v.build_embed()}`). Views built by async classmethods like `from_data` come fully populated and need no rebuild. Sync or async callables both work.

Omitting `rebuild` does not mean no rebuild runs: the destination's own [`nav_rebuild`](#nav_rebuild) applies instead, which is how every V1 pattern renders itself on arrival. An explicit `rebuild=` wins over it.

#### `pop(interaction, *, rebuild=None)`

Pops the top entry from the navigation stack, reconstructs that view with its original kwargs, and returns it. Returns `None` if the stack is empty. Non-reconstructible kwargs (`context`, `interaction`, etc.) are re-supplied by the framework. `rebuild` takes the same shape as `push(rebuild=...)` and is rarely needed, since the restored view's [`nav_rebuild`](#nav_rebuild) already names how it renders.

#### `batch()`

Returns an async context manager for batched dispatch. Calls `self.state_store.batch(source_id=self.id)`, so the batch's single `BATCH_COMPLETE` notification reaches this view on the inline acting-view path. Calling `store.batch()` directly instead loses that and the view's own refresh joins the background fan-out.

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

#### `make_exit_button(label="Exit", style=ButtonStyle.secondary, emoji="❌", delete_message=None, custom_id=None, row=None)`

Returns a pre-configured `StatefulButton` without adding it to the view. Use in V2 views that need to place exit buttons inside specific `ActionRow` or `Container` subtrees rather than at the top level. `add_exit_button()` continues to work for top-level placement. `delete_message=None` (the default) defers to the `exit_policy` class attribute.

#### `make_back_button(label="Back", style=ButtonStyle.secondary, emoji="◀", custom_id=None, row=None)`

Returns an unattached `StatefulButton` whose callback pops the navigation stack. The matched pair to `make_exit_button()` -- pack it into a caller-owned `ActionRow` or `Container` subtree. For the top-level auto-injected case, set `auto_back_button = True` instead.

The button renders disabled while the navigation stack is empty, which is the state a root view sits in. That is resolved at each render seam rather than at construction, because a pushed view is built before `_navigate_to` hands it its stack. Should a press still reach an empty stack, it acknowledges the interaction and leaves the message alone -- tearing the panel down there would destroy a working message on a press that asked for nothing. Override `_clear_on_empty_back` to close the panel instead, though `exit()` and the Exit button are the surfaces built for that.

#### `nav_depth`

Read-only `int`: how many views sit beneath this one on the navigation stack. Zero on a view opened directly, one on the first push. Sits alongside `undo_depth` and `redo_depth`.

Disabled and absent are different answers. The library disables a Back button with nowhere to go, but a screen reachable both by a push and by its own slash command usually wants that button gone on the root entry rather than greyed:

```python
async def on_load(self):
    self.clear_items()
    self.add_item(card(*self.rows()))
    self.add_item(self.make_nav_row(back=bool(self.nav_depth)))
```

Safe to read inside `on_load`, which is where a view composes its tree: `_navigate_to` assigns the stack before it runs the destination's load hook, so the count is already correct.

#### `add_exit_button(label="Exit", style=ButtonStyle.secondary, row=None, emoji="❌", delete_message=None, custom_id=None)`

Adds an exit button that calls `self.exit()`. In V2 views, the button is wrapped in an `ActionRow`. `delete_message=None` (the default) defers to the `exit_policy` class attribute; pass `True` or `False` to override it for this button. Pass `custom_id` for persistent views.

`row` is V1-only. V2 lays out by tree position rather than row index, so the V2 override does not accept it and raises `TypeError` if it is passed.

#### `await exit(delete_message=None)`

Cleans up the view: cancels tasks, unsubscribes, disables components. When `delete_message` is `None` (the default), behavior is resolved from the `exit_policy` class attribute (`"disable"` freezes, `"delete"` deletes). Pass `True` or `False` explicitly to override the policy at any call site. V2 views freeze components in place (since `edit(view=None)` would empty the message); V1 views strip the view entirely.

#### `get_theme()`

Returns the view's theme (per-view override or global default).

#### `await on_pre_send(interaction)` *(override)*

Pre-send veto gate. Default returns `True`. The library calls it first in the send pipeline (before `on_load()`, placement validation, instance enforcement, and the Discord call), so a `False` return aborts the send before any of that work. An abort is clean: no message ships and no state registers, so a vetoed send leaves zero side effects. The interaction's response slot is still open, so an override can `respond()` to explain the veto; on a proceed, the slot stays available for the actual send (no forced `defer` onto the followup path). `interaction` is the triggering interaction, or `None` for a channel/context send.

```python
async def on_pre_send(self, interaction):
    if not await self.repo.user_has_access(self.user_id):
        await self.respond(interaction, "You don't have access.", ephemeral=True)
        return False
    return True
```

#### `await on_load()` *(override)*

Async preload hook. The default is a no-op on `StatefulView` and `StatefulLayoutView`, but several built-in patterns override it: `TabLayoutView`, `WizardLayoutView`, and `LeaderboardLayoutView` build their content here, and `PaginatedView` / `PaginatedLayoutView` fetch the current page in cursor mode. A subclass of any of those calls `super().on_load()` before its own work, or it loses what the base does and renders its controls above nothing. The library calls the hook automatically before the initial send (inside `send()`) and before every push/pop edit, so navigating to a child or back to a parent re-fetches its source. Override to load from a database or other async source and build the view's component tree against the result. A data-loading view fetches its own source through `on_load()` on navigation, so its push and pop calls carry no `rebuild=` argument. See [Navigating database-backed views](../guide/views.md#navigating-database-backed-views).

```python
async def on_load(self):
    self.rows = await self.repo.list_tasks()
    self.build_ui()
```

#### `nav_rebuild` *(class attribute)* {#nav_rebuild}

The rebuild this view supplies for its own navigation edits, used whenever the caller passes no `rebuild=`. A callable taking the destination view; a returned dict splats into the edit. `None` by default.

V2 views leave it `None` and should: a V2 view *is* its component tree, so swapping `view=` is the whole render. A **V1** view's content lives in its embed, and `pop()` has no `rebuild=` to pass (the back button is library code with nothing to hand it), so a V1 view that renders an embed names its own, or the pop swaps the buttons and leaves the child's content on the message:

```python
class Hub(StatefulView):
    nav_rebuild = staticmethod(lambda v: {"embed": v.build_embed()})
```

Wrap it in `staticmethod()`; a bare lambda on a class body binds as a method and receives `self`. An explicit `rebuild=` always wins, matching the class-attribute-then-argument precedence the policy attributes use. The V1 patterns (`PaginatedView`, `TabView`, `WizardView`, `FormView`, `MenuView`) all set one already.

#### `get_nav_state()` *(override)*

Returns the view state that should survive a `pop()`. Default returns `{}`, so a view that needs none of this pays nothing. `push()` captures it on the view being pushed away from; the matching `pop()` hands it to `restore_nav_state()`.

`pop()` reconstructs the parent from its constructor kwargs and re-runs `on_load()`: data comes back fresh, but anything the view *selected* since construction was never a kwarg and reverts to its default. Override this pair to name what should carry. The returned mapping rides the navigation stack and is never serialized, so it may hold live objects.

The built-in patterns implement it for their own cursors: `PaginatedView` and `PaginatedLayoutView` carry `current_page`, `TabView`/`TabLayoutView` the active tab, `WizardView`/`WizardLayoutView` the current step (snapped to a visible one), and `FormView`/`FormLayoutView` the entered values. Call `super()` when overriding one of those. See [What a pop restores](../guide/views.md#what-a-pop-restores-and-what-it-does-not).

```python
def get_nav_state(self):
    return {"severity": self._severity}
```

#### `restore_nav_state(state)` *(override)*

Reapplies what `get_nav_state()` captured. Called on a view reconstructed by `pop()`, after `__init__` and **before** `on_load()`, so a preload reads the restored selection rather than the constructor's default. Default is a no-op.

Read defensively: treat every key as optional. The stack entry was written by an earlier version of this view, and a key the class no longer sets is the caller's to tolerate. An override that raises costs the restore, not the navigation: the user still lands on the view, on its defaults.

```python
def restore_nav_state(self, state):
    self._severity = state.get("severity", self._severity)
```

#### `await reload()`

Runs `on_load()` followed by `refresh()`. The out-of-band counterpart to the automatic `on_load()` calls: use it inside a callback that mutated the view's data source and needs an immediate re-fetch and re-render. Under an active `refresh_cooldown_ms` window (or a 429 backoff), the whole reload, including the `on_load()` fetch, is deferred to the window boundary and coalesced with any other pending reload. Boolean keywords OR across a coalesced window, so a `force=True` is not lost to a later unforced call.

Reloads on one view run one at a time. A reload arriving while another is mid-fetch waits its turn and then re-fetches, so the last to run renders the freshest data. Calling `reload()` from inside the view's own `on_load` raises `RuntimeError`.

Returns a `RenderOutcome`: `RENDERED` (the edit reached Discord), `SKIPPED` (the tree matches the last shipped render), `DEFERRED` (a scheduled task re-renders at the throttle boundary), `DROPPED` (attempted, not known to have landed, nothing scheduled), or `NO_MESSAGE` (no editable message remains, so retrying cannot help). `None` when a subclass render override reports nothing. Members compare equal to their string values, so `outcome == "deferred"` works. `refresh()` returns the same type.

```python
async def on_refresh(self, interaction):
    await self.reload()
```

#### `await seed_initial_state(state)` *(override)*

Initializes per-view state slots before the first subscriber notification. Called once during `send()`, inside the registration batch, after the view is registered but before participant claiming and before the batch's `BATCH_COMPLETE` fires. Override to dispatch actions or write to `state["application"]` so subscribers see the seeded state from frame one instead of an empty slot followed by a separate seeding dispatch.

```python
async def seed_initial_state(self, state):
    if "leaderboard" not in state["application"]:
        await self.dispatch("LEADERBOARD_SEED", {"entries": []})
```

The hook receives the live store state dict. Dispatches issued from inside join the surrounding batch, so seed work collapses into the view's `VIEW_CREATED` notification cycle. Default is a no-op. A subclass may also build its component tree here when the initial render depends on seeded state: pre-flight placement validation runs on the tree the hook produces, not the empty pre-seed tree.

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

Select the state your view *displays*. A theme resolved through `get_theme()` is added to the comparison for you, so a theme switch re-renders even when the value you selected is unchanged: a theme is a render input that appears in no view's data, and a selector tracking content alone never sees it change. A view with a fixed theme adds no notifications. A view that resolves its theme without returning it from `get_theme()` hands the library nothing to carry. See [Dynamic Themes](../guide/theming.md#dynamic-themes).

#### `await register_participant(user_id, *, interaction=None) -> bool`

Registers a non-owner user in the instance index so that `instance_limit` and `participant_limit` apply to them. Returns `True` on success (including the owner short-circuit), `False` on rejection. Rejections have no exception path; check the bool. It does raise `TypeError` for a `user_id` that cannot be coerced to a snowflake, and the default `on_instance_limit` re-raises `InstanceLimitError` when the view has neither an interaction nor a context to answer on.

`user_id` accepts either an `int` or any object with an `int .id` attribute (`discord.Member`, `discord.User`, `discord.Object`) -- coercion happens silently at the entry point.

Two rejection paths fire automatically:

- **Per-user instance collision** (the joiner already holds an instance of this view type): the library calls `self.on_instance_limit(error)` with the joiner's interaction temporarily swapped in, so the rejection ephemeral targets the joiner -- not the view owner.
- **View capacity overflow** (the view is at `participant_limit`): the library calls `self.on_participant_limit(user_id, interaction=interaction)`.

Pass the `interaction` keyword when the registration is driven by a button or select callback so the rejection hooks can respond ephemerally on the right interaction. Skips silently when `user_id` matches the view owner.

#### `unregister_participant(user_id)`

Removes a participant from the instance index. Use when a participant leaves a multi-user view (e.g., a player disconnects mid-game).

#### `interaction_check(interaction)` *(override)*

Called before every component callback. Returns `True` to allow, `False` to block. By default, checks `allowed_users` first (if set), then falls back to `owner_only`.

### Shared Properties

- `id` (str): UUID instance identifier
- `persistence_key` (str | None): Stable data identity key
- `message` (Message | None): The sent message, if any
- `state_store` (StateStore): The singleton store
- `session_id` (str | None): Session identity for this view. `None` when no `user_id` could be derived (channel-posted views have no `.author`, so they carry no session). When `user_id` is present, auto-derived as `<module.QualName>:user_<id>:<8hex>` unless `session_continuity = True` is set on the class (which drops the `:<8hex>` suffix) or an explicit `session_id=` kwarg is passed.
- `scoped_state` (dict): The scoped state for this view's user/guild (empty dict if no state_scope)
- `shared_data` (dict): The current session's `shared_data` dict (empty dict if no session or no data)

### Shared Class Attributes

!!! note "Validated at subclass-definition time"
    Class attributes whose values are bounded -- string enums (`instance_policy`, `instance_scope`, `state_scope`, `replace_policy`, `exit_policy`), positive integers (`instance_limit`, `participant_limit`, `undo_limit`), positive floats (`auto_defer_delay`, `edit_timeout`), and booleans (`owner_only`, `auto_defer`, `auto_register_participants`, etc.) -- are validated by `_StatefulMixin.__init_subclass__` when a subclass is defined. A typo like `instance_policy = "rejct"` raises `ValueError` at module import with a message naming the class, the attribute, the bad value, and the valid options. Validation runs once per subclass at class-definition time and inspects only `cls.__dict__`, so per-subclass cost is `O(overrides-on-this-subclass)` -- inherited defaults pay zero cost. There is no per-instantiation overhead.

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
- `allowed_users` (frozenset[int] | None): `None` (default) defers to `owner_only`. Once set to anything else, only those user IDs can interact and `owner_only` is ignored entirely. The gate tests `is not None`, so an explicitly assigned empty set admits nobody rather than falling back: clear the list by assigning `None`, not `set()`. Because the default is `None`, guard membership checks (`if view.allowed_users and uid in view.allowed_users`) rather than testing `in` directly. Stored as a `frozenset` and exposed via a property pair: assignment coerces both `int` and snowflake-shaped objects (`Member`, `User`, `Object`) at the setter, so `view.allowed_users = {member, 12345}` works. Direct mutation is unsupported -- to add a user after construction, use `await view.register_participant(user_id)` (which writes to `_participants`, not `allowed_users`) or rebind the attribute: `view.allowed_users = view.allowed_users | {new_id}`.
- `participant_limit` (int | None): Maximum total view occupants (owner + participants). `None` (default) means unlimited. Owner counts toward the cap, so `participant_limit = 8` admits one host plus seven joiners. Enforced inside `register_participant`.
- `participant_limit_message` (str): Ephemeral message sent when `register_participant` rejects a joiner due to view-capacity overflow (default: `"This session is full."`). Used by the default `on_participant_limit` hook.
- `auto_register_participants` (bool): When `True`, `send()` iterates `allowed_users` and calls `register_participant` for each non-owner before the Discord send. All-or-nothing rollback: any rejection unregisters every previously-claimed slot and tears the view back out of the registry, then `send()` returns `None`. A rejection therefore leaves zero side effects: no message, no registry entry, no half-claimed participants. Default: `False`.
- `protect_attached` (bool): When `True` (default), views with active participants or attached children from other users are excluded from replacement candidates during instance enforcement. If no replaceable views remain, falls back to reject behavior (`on_instance_limit` fires). Same-user attachments do not trigger protection. Has no effect on views without attachments or when `instance_policy = "reject"`. Set to `False` for views where silent replacement is expected (e.g. spectator panels).
- `replaced_message` (str | None): Static message sent to the channel when this view is replaced and has active participants. `None` (default) means silent replacement. Used by the default `on_replaced` hook.
- `replace_policy` (str): What `instance_policy="replace"` does to the old view's message. `"delete"` (default) removes it; `"disable"` freezes its components in place. Only governs the instance-replace transition.
- `exit_policy` (str): What bare `exit()` calls do when no `delete_message` argument is supplied. `"disable"` (default) freezes the components in place; `"delete"` removes the message. Always overridden by an explicit `delete_message=` argument or by an `exit()` method override. Independent of `replace_policy`.
- `auto_defer` (bool): Enable the auto-defer safety net (default: `True`).
- `auto_defer_delay` (float): Seconds before auto-deferring (default: `2.5`).
- `ack_first` (bool): Acknowledge the interaction immediately, before the access checks and the callback run (default: `False`). An advanced escape hatch for callbacks that synchronously block the event loop past `auto_defer_delay`; it trades the acting-view one-request refresh for a guaranteed early ack. Do not combine with `open_modal()` in the same callback: a modal must be the first response, and the early ack has already consumed the slot.
- `refresh_cooldown_ms` (int | None): Proactive minimum gap between successive **background** Discord edits, in milliseconds. State-driven refreshes arriving inside an active window are coalesced into one deferred re-render that fires at the window boundary. Edits made in direct answer to a click on this view's own message are exempt and ship immediately. This paces the re-renders the library starts, and is not a spam guard. To throttle one expensive control per clicker, wrap it with `with_cooldown`. `None` (default) disables the proactive cooldown; the reactive 429 backoff is always active regardless. Validated as a positive int (`0` is rejected).
- `serialize_interactions` (bool): Serialize rapid button clicks with an `asyncio.Lock` (default: `True`). Set to `False` for views that handle parallel callbacks.
- `edit_timeout` (float | None): Maximum seconds any single Discord edit may stall before it is cancelled. Bounds the edits the library issues after the initial send -- state-driven refresh, exit/teardown, and navigation edits. discord.py issues edits with no total HTTP timeout, so without this a stalled connection would pin the view until the socket drops. Default `60.0` (clears realistic attachment uploads while capping a true hang). Set to `None` to disable the bound (unbounded, matching discord.py's own default). The acting-view fast path keeps its own tighter bound, which protects the 3-second ack deadline rather than guarding against a hang.
- `session_continuity` (bool): Governs `session_id` auto-derivation polarity. Default `False` gives every invocation a per-instance UUID suffix, so repeat opens of the same view class are independent sessions with their own nav stack, undo timeline, and `shared_data`. Set to `True` on views that want repeat-open state coalescing (undo history surviving close-and-reopen, `shared_data` continuity across gestures); the opt-in collapses derivation back to the class-coalesced shape. Push/pop chains stay on one session regardless because `_navigate_to` forwards `session_id` explicitly.
- `session_class_key` (str, unset): Overrides the derived class-identity string (`f"{cls.__module__}.{cls.__qualname__}"`) used for session IDs, the instance index, session origin tracking, and the `view_class` column persistent registry rows store. Read from the class's own body, so it does not inherit. Pin it to the previously stored name when moving or renaming a persistent view class; two persistent classes sharing one pin raise at class definition. See [Class identity](../guide/persistence.md#class-identity-rows-resolve-by-the-name-they-recorded).

#### `allowed_mentions`

`discord.AllowedMentions | None`, default `None`. Mention rules applied to this view's own message, on the initial send and on every subsequent refresh. `None` defers to the bot's client-level `AllowedMentions`, which discord.py already threads into each send path.

Reach for it when the rendered body carries user or role mentions that should not notify. A roster or leaderboard re-renders on every state change, and without a rule each render is a fresh chance to ping everyone named. `LeaderboardLayoutView` therefore ships `AllowedMentions.none()` as its own default, since its `format_name` renders entries without a `display_name` as `<@id>`.

```python
class RosterPanel(StatefulLayoutView):
    allowed_mentions = discord.AllowedMentions.none()   # names render, nobody pings
```

Pass `allowed_mentions=` to `send()` to override the attribute for one message, a winner announcement that genuinely should notify the person it names:

```python
await Notice(context=ctx).send(
    allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=False),
)
```

Name every field you care about. `discord.AllowedMentions(users=True)` leaves the other fields at a sentinel meaning "inherit", so with no client-level default configured it permits `@everyone` and role pings as well.

This governs Discord payload formatting only. It is unrelated to `allowed_users`, which is access control.

---

## V2 Views

### `StatefulLayoutView`

Base class for V2 views. Extends `discord.ui.LayoutView`.

```python
StatefulLayoutView(context=None, **kwargs)
```

V2 views ARE the message content -- `send()` takes no `content` or `embed` params. Build the component tree in `__init__` or an async builder, then call `send()`.

#### V2-Specific Class Attributes

- `validate_placement` (bool): Run the V2 placement validator before every Discord round-trip. When `True` (default), the assembled component tree is walked at three seams (the initial `send()`, every state-driven `refresh()` after the render-hash short-circuit, and the in-place edits from `push()` / `pop()` navigation), and any composition Discord rejects with HTTP 400 raises `ValueError` with a path string identifying the violation node and a suggested fix. Type rejections cover Container nesting, Section nesting, Section accessory not in `{Button, Thumbnail}`, standalone `Button` / `Select` / `Thumbnail` at LayoutView or Container level, Modal-only types (`Label`, `RadioGroup`, `CheckboxGroup`, `Checkbox`, `FileUpload`) anywhere in the tree, and the Modal-only and display types in an ActionRow. An unknown `Item` subclass passes, so a component discord.py adds later is not rejected before the matrix learns it. Size rejections cover empty Containers, empty Sections, empty ActionRows, MediaGallery items outside the 1-10 range, empty text on a TextDisplay or a SelectOption label or value, empty media URLs on Thumbnail / MediaGalleryItem / File, an empty link-button `url`, and length caps on TextDisplay content (4000 chars), Button label (80), Button url (512), `custom_id` (100), Select placeholder (150), SelectOption label / value / description (100), and Thumbnail / MediaGalleryItem description (1024). Set to `False` only when the validator's matrix lags a discord.py or Discord update; opting out otherwise signals an actual placement bug, prefer fixing the tree. See [V2 Placement Rules](../guide/components.md#v2-placement-rules) for the full matrix and the builders-as-guardrails framing.

#### V2-Specific Methods

##### `make_nav_row(*, back=True, exit=True, back_label="Back", exit_label="Exit", back_style=secondary, back_emoji="◀", exit_style=secondary, exit_emoji="❌", delete_message=None, back_custom_id=None, exit_custom_id=None)`

Returns one `ActionRow` containing a Back button and/or an Exit button: the V2 navigation footer helper. Raises `ValueError` if both `back` and `exit` are `False`. Back pops the navigation stack and renders disabled while that stack is empty (see `make_back_button` above); Exit calls `self.exit()` (`delete_message=None` defers to `exit_policy`; `True` or `False` overrides it). When the popped view defines `on_load()`, that hook runs on the restored view before the edit ships, re-fetching its source on render. The `back_*` / `exit_*` label, style, and emoji kwargs forward to `make_back_button` / `make_exit_button`, so a relabeled Back (`make_nav_row(back_label="Leagues", back_emoji="🏠")`) needs no manual composition.

```python
def build_ui(self):
    self.clear_items()
    self.add_item(card("## Inventory", ...))
    self.add_item(self.make_nav_row())
```

##### `clear_row(row)`

No-op on V2 views. V2 uses a tree structure rather than rows.

---

### `DisplayLayoutView`

```python
DisplayLayoutView(context=None, *, container)
```

A parameterized concrete variant of `StatefulLayoutView` that renders a supplied `container` without requiring a subclass. It has no state machine and no hook surface: a shorthand for one-shot ephemeral V2 sends where a full subclass would be overkill.

```python
view = DisplayLayoutView(context=ctx, container=card("Done!"))
await view.send(ephemeral=True)
```

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

Each tab builder returns a list of V2 components; it may be `async def` or a plain `def`. `tabs=` takes a `{name: builder}` mapping or a sequence of `(name, builder)` pairs. The first tab is displayed on send.

#### Class Attributes

| Attribute | Default | Purpose |
|---|---|---|
| `active_tab_style` | `ButtonStyle.primary` | Style for the currently active tab button |
| `inactive_tab_style` | `ButtonStyle.secondary` | Style for inactive tab buttons |
| `tab_overflow_policy` | `"fill"` | Row-distribution strategy when tabs exceed the five-per-row ActionRow cap. Presets: `"fill"`, `"balance"`, `"pin_first"`, `"pin_last"`. Or `tuple[int, ...]` for explicit per-row widths. |

#### Methods

##### `await refresh_content()`

Re-renders the current tab's content in place (V1 rebuilds the embed, V2 recomposes the tree). Use from a subclass callback that mutated data and needs the active tab redrawn.

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

- `builder()` -- takes no arguments, returns a list of V2 components for the
  step. Sync or async; the result is awaited only if it is awaitable.
- `validator()` -- takes no arguments, returns `(valid: bool, error: str)`.
  Sync or async. A bare `bool` is read as the answer it plainly is, with no
  message. Anything else that is not a pair is refused, naming the step and
  the shape it owes: a two-character string and a two-key dict both unpack
  without complaint and hand back a truthy first element, which would
  advance the wizard past the step the validator was rejecting.
- `condition(view)` -- must be synchronous and able to accept the view. An
  async predicate returns a coroutine, which is always truthy, so the step
  would render regardless of the answer; both declaration forms reject one
  with `TypeError` at construction. A predicate that cannot take the view
  (a zero-argument lambda) is rejected the same way, because the visibility
  check treats a raising predicate as visible: the step it meant to hide
  would render, with a warning as the only trace.

#### Methods

##### `await refresh_content()`

Re-renders the current step's content in place (V1 rebuilds the embed, V2 recomposes the tree). Use from a subclass callback that mutated data and needs the active step redrawn without re-fetching.

##### `on_finish(self, interaction)` *(override)*

Called when the final step passes validation. Default implementation calls `self.exit()`. Override to customize the finish behavior (e.g. build a summary card, save data, navigate).

#### Navigation Button Customization

Back, Next, and Finish buttons are added automatically. Back is disabled on the first step. Next is replaced with Finish on the last step. All three support class-attribute customization:

| Attribute | Default | Purpose |
|---|---|---|
| `back_button_label` | `None` | Back button label. `None` renders "Back" |
| `back_button_emoji` | `None` | Back button emoji |
| `back_button_style` | `ButtonStyle.secondary` | Back button style |
| `next_button_label` | `None` | Next button label. `None` renders "Next" |
| `next_button_emoji` | `None` | Next button emoji |
| `next_button_style` | `ButtonStyle.primary` | Next button style |
| `finish_button_label` | `None` | Finish button label. `None` renders "Finish" |
| `finish_button_emoji` | `None` | Finish button emoji |
| `finish_button_style` | `ButtonStyle.success` | Finish button style |
| `step_indicator_label` | `None` | Callable `(current, total) -> str`. Default: `"Step {current}/{total}"`. Must be synchronous; an `async def` is refused at class definition |

---

### `FormLayoutView`

V2 form with native text, select, and boolean fields.

```python
FormLayoutView(
    context=None,
    title="Form",
    fields=[
        {"id": str, "type": "text"|"integer"|"float"|"date"|"boolean"|"select"|"multi_select", "label": str,
         "validators": [...], "placeholder": str, "default": Any, "required": bool},
        ...
    ],
    **kwargs,
)
```

Displays form state as a V2 component tree (Container + TextDisplay). `text` fields are grouped behind a single "Edit Text Fields" button that opens a `Modal` populated with one `TextInput` per declared text field (Discord caps this at **5 text fields per form**; construction raises `ValueError` above the limit). Submitted values flow back into `form.values` and the view rebuilds. `select` and `boolean` fields render inline as interactive components.

Validators declared in the field dict attach directly to the generated `TextInput` and run server-side after submission.

#### Text-Edit Button Customization

Three class attributes mirror the `refresh_button_*` grammar; a fourth (`text_edit_modal_auto_defer_delay`) tunes the modal the button opens:

| Attribute | Default | Purpose |
|---|---|---|
| `text_edit_button_label` | `None` | `None` → smart default: `"Edit {label}"` for one text field, `"Edit Text Fields"` for multiple. |
| `text_edit_button_emoji` | `"\u270f\ufe0f"` (✏️) | Emoji on the grouped button. Set `None` to disable. |
| `text_edit_button_style` | `ButtonStyle.secondary` | Button style. |
| `text_edit_modal_auto_defer_delay` | `2.5` | Ack backstop in seconds for the grouped text-edit modal. Raise for a slow async field validator. |

`FormView` (V1) exposes the same four attributes and 5-field ceiling.

#### Instance Methods

##### `set_form_error(message)`

Sets a form-level error banner and re-renders the form so it shows. Use this instead of assigning the private `_form_error` and calling `refresh()`, which set the state but never rendered it.

##### `set_field_error(field_id, *messages)`

Sets one or more error messages on a single field and re-renders. Pass the field's `id` and the message(s). Both methods live on the shared form mixin, so `FormView` (V1) and `FormLayoutView` (V2) expose them identically.

---

### `PaginatedLayoutView`

V2 paginated view with component-tree pages.

```python
PaginatedLayoutView(context=None, pages=[list_of_components, ...], **kwargs)
```

Each page is a list of V2 components. Navigation buttons (Previous, Next, First, Last, Go-to-page) work identically to V1's `PaginatedView`.

#### Class Attributes

##### `nav_inside_container` *(bool, default `False`)*

When `True` and multiple pages exist, wraps the page content and the navigation row together in a single `Container`. Default `False` keeps them as separate top-level children. Single-page views render no navigation row, so the flag has no visible effect there. When the page formatter returns a single `Container`, the wrap builds a fresh Container (copying its accent color and spoiler) rather than nesting one inside another, which Discord rejects. A page that mixes a `Container` with other top-level items cannot be wrapped at all; it falls back to the sibling layout with the navigation row as a separate row.

##### `nav_divider` *(bool, default `False`)*

When `True` and `nav_inside_container` is also `True`, renders a divider between the page content and the in-card navigation row. No effect in the sibling layout (`nav_inside_container = False`).

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

### `LeaderboardLayoutView` / `PersistentLeaderboardLayoutView`

V2-only paginated ranked-display pattern. Subclass of `PaginatedLayoutView`. Renders a sorted list of `(user_id, stats_dict)` entries across one or more pages. Each page is a card with ranked entry lines; the optional `build_header` / `build_footer` hooks add content above and below the card (an Overview stats card, an identity caption), on whichever pages the override chooses.

```python
class ServerLeaderboard(LeaderboardLayoutView):
    leaderboard_top_n = 25
    leaderboard_per_page = 10

    def format_stats(self, user_id, stats):
        return f"{stats['wins']}W / {stats['games']}G"

view = ServerLeaderboard(context=ctx, entries=entries, title="Server Rankings")
await view.send()
```

#### Class Attributes

| Attribute | Default | Purpose |
|---|---|---|
| `leaderboard_top_n` | `10` | Total entries to consider from the data source. |
| `leaderboard_per_page` | `5` | Entries per page. `None` collapses into a single page equal to `top_n`. |
| `title` | `"Leaderboard"` | H2 heading on the rankings card. Constructor `title=` kwarg overrides; `None` or `""` renders no text heading (with no `banner` either, the title divider is skipped too). |
| `banner` | `None` | Full-width image at the top of the rankings card, above the title heading when both are set. URL string, `discord.File`, or anything with a string `.url` (`guild.icon` works directly). Constructor `banner=` kwarg overrides. |
| `subtitle` | `"Rankings"` | H3 above the ranked rows. Set to `None` or `""` to skip. |
| `leaderboard_empty_message` | `"No entries recorded yet."` | Static text when no entries exist. |
| `entry_layout` | `"lines"` | `"lines"` packs entries into a single TextDisplay; `"sections"` renders each entry as a `Section` with optional avatar Thumbnail. Section mode caps `leaderboard_per_page` at 5. |
| `podium_emojis` | `{1: "🥇", 2: "🥈", 3: "🥉"}` | Rank-keyed glyphs for `format_rank`. Override the dict to change the podium treatment without overriding `format_rank`. |
| `entry_separator` | `" -- "` | Separator between name and stat columns inside `format_entry` (lines mode). |
| `card_color` | `None` | Optional `discord.Color` for the rankings card accent. `None` falls through to the active theme. |
| `show_title_divider` | `True` | Toggle the divider rendered below the title. |
| `avatar_backfill` | `False` | Section mode only. Renders default avatars immediately, resolves the real ones off the render path via `resolve_avatar_urls`, then reloads. Avoids a blocking first render and per-row fetches on large guilds. |

**What `entries=` accepts.** A sequence of `(user_id, stats_dict)` pairs is
the documented shape, and `get_entries()` returns the same. Three other
shapes are converted rather than refused, because each carries exactly that
data in a different container: a `{user_id: stats_dict}` mapping, a
generator or other one-pass iterable, and a sequence of two-element lists.
Anything else raises `TypeError` where it was supplied, naming the class,
the argument, and the index of the entry that was wrong -- rather than
failing later inside the page build.

**Constructor.** Besides `entries=` / `title=` / `subtitle=` / `banner=`, `LeaderboardLayoutView` accepts an optional `bot=` kwarg. Passing it lets the default `get_avatar_url` resolve avatars from the bot's user cache in Section mode. The persistent variant receives the bot through `on_bind` instead (it is stripped from the persistence round-trip).

#### Override Hooks

| Hook | Purpose |
|---|---|
| `get_entries()` | Data source. Sync or `async def`; an awaited override is read once per rebuild. Default returns the constructor `entries=` kwarg. |
| `format_rank(rank)` | Rank column. Default reads `podium_emojis` for ranks 1-3, falls back to `f"**{rank}.**"`. |
| `format_name(user_id, stats)` | Name column. Default mentions the user (`<@user_id>`) or returns `stats['display_name']` when present. |
| `format_stats(user_id, stats)` | Inline stat column. Default `f"{wins}W / {games}G"`. |
| `format_accessory(user_id, stats)` | Optional right-side accessory. Default `None`. |
| `format_entry(rank, user_id, stats)` | Composes the four column hooks. Override only when row layout itself needs to change. |
| `format_primary` / `format_secondary` | Section-mode two-line body. |
| `get_avatar_url(user_id, stats)` *(async)* | Section-mode `Thumbnail` URL. The default resolves from the bot's user cache when a `bot=` kwarg is passed (member avatar on a cache hit, a Discord default avatar on a miss), and returns `None` without a bot so the entry falls back to the two-line `TextDisplay`. Override to resolve from another source. |
| `resolve_avatar_urls(user_ids)` *(async)* | Section-mode avatar backfill (with `avatar_backfill = True`). Takes a list of user ids, returns a `{user_id: url}` dict, resolved off the render path. Default resolves per user; override for a custom source. |
| `build_title(page)` | Optional components replacing the rankings card's masthead (the `banner` image + `## title` heading) inside the Container. `None` (default) composes the masthead from the declarative `banner` / `title` pair. Same return shapes and `page` semantics as `build_header`. |
| `build_header(page)` | Content above the rankings card. The value is prepended as-is: a `Container` renders as its own card (an Overview `stats_card`, a banner), anything else floats as a bare top-level item (no return-type branching, unlike `build_footer`). Read `ranked_entries` for aggregate stats. Returns a component, a list, or `None` (default). `page` is the zero-based page index. |
| `build_footer(page)` | Content below the rankings, placed by return type: a raw component folds inside the rankings card below the entries, a `Container` renders as its own standalone card below it. Same return shapes and `page` semantics as `build_header`. |
| `on_leaderboard_empty()` | Returns the V2 component list shown when `entries` is empty. The masthead composes above this return, so a board keeps its `banner` / `title` while empty and an override inherits it; `build_title` returning `[]` renders no masthead on that page. `ranked_entries` is empty here. `build_header` and `build_footer` run on this page too: header content renders above the masthead, footer content below the empty-state card, each in the order returned. |
| `ranked_entries` *(property)* | The loaded top-N `(user_id, stats)` slice for the current render; read it in `build_header` / `build_footer` / `build_title` to compute aggregate stats without re-fetching. |
| `bot` *(property, read-only)* | The client passed via `bot=` (or injected by `on_bind`). Read it in a `resolve_avatar_urls` / `get_avatar_url` override to resolve avatars through `self.bot` instead of a private attribute. `None` when no bot was supplied. |
| `on_state_changed(state)` *(async, override)* | Calls `rebuild_pages()` then the paginated refresh; live-data subclasses subscribe to data actions and override `get_entries()`. |

`format_rank`, `format_name`, `format_stats`, and `format_accessory` compose the row inside the synchronous `format_entry` and are refused at class definition when `async def`. The frame hooks (`build_title`, `build_header`, `build_footer`), `format_entry`, `format_primary`, `format_secondary`, and `on_leaderboard_empty` accept either shape.

**Out-of-band refresh.** `reload()` re-fetches entries, recomposes the tree, and edits the message (the public path for a manual refresh button or `on_restore`); `rebuild_pages()` rebuilds only the page list. Both accept `force=True` to bypass the entry-signature short-circuit when something outside the row data changed the render: a filter, or a select's highlighted option read by `build_header`.

#### Persistent Variant

`PersistentLeaderboardLayoutView` composes `_PersistentMixin` with `LeaderboardLayoutView` for admin-posted permanent panels. Defaults: `owner_only = False`, `exit_policy = "disable"`, `timeout = None`. Requires `persistence_key=` at construction. `on_restore` calls `reload()` after a bot restart to re-fetch entries, recompose the tree, and edit the message, which re-stores the panel's components so clicks route immediately.

See [`docs/guide/patterns.md`](../guide/patterns.md#leaderboardlayoutview-persistentleaderboardlayoutview) for cardinality model, customization tiers, and section-mode rendering details.

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

Each category generates an `action_section()` item that pushes to the specified view class when clicked. The `description`, `emoji`, `style`, and `rebuild` keys are optional. A category with no `description` renders its label as the section text.

A category whose `"view"` is the other component version raises `TypeError` at construction rather than on the click (see [V1 and V2 Views Cannot Push/Pop Between Each Other](../guide/known-limitations.md#v1-and-v2-views-cannot-pushpop-between-each-other)).

#### Class Attributes

| Attribute | Default | Purpose |
|---|---|---|
| `menu_style` | `ButtonStyle.primary` | Default button style for all category items |
| `auto_exit_button` | `True` | Whether to add an exit button at the bottom |

#### Override Hooks

##### `build_header()` *(override)*

Returns V2 components (list or single) for the area above category items. Default returns `[]`.

##### `build_footer()` *(override)*

Returns V2 components (list or single) for the area below category items. Default returns `[]`.

##### `_build_category_item(category, index)` *(override)*

Controls how a single category is rendered. Default creates an `action_section()`.

##### `on_category_selected(category, index, interaction)` *(override)*

Called before pushing to the selected category's view. Default is a no-op. Override for analytics, guards, or pre-push setup.

#### Properties

- `categories` (list[dict]): The category list this menu was constructed with.

---

### `RolesLayoutView` / `PersistentRolesLayoutView`

V2-only role self-assign panel pattern. Each category renders as a Container with toggle buttons; cardinality (at-most-one / at-least-one) is enforced inside the pattern without per-role callback boilerplate. Role buttons are `DynamicPersistentButton` subclasses declared once at module import -- clicks route by `custom_id` template match regardless of view lifecycle.

```python
class MyRoles(PersistentRolesLayoutView):
    categories = [
        RoleCategory(
            name="Colors",
            roles={"Red": 111, "Blue": 222, "Green": 333},
            exclusive=True,
            color=discord.Color.red(),
        ),
    ]
    title = "Server Roles"

view = MyRoles(context=ctx, persistence_key=f"roles:{ctx.guild.id}")
await view.send()
```

#### `FormField` (typed schema)

```python
FormField(
    id: str,
    label: str,
    type: str = "text",
    required: bool = False,
    default: Any = None,
    placeholder: Optional[str] = None,
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    options: Optional[list[Any]] = None,        # select/radio choices
    max_values: Optional[int] = None,           # multi-select cap
    validators: Optional[list[Callable]] = None,
    style: Optional[TextStyle] = None,          # short vs paragraph
    group: Optional[str] = None,                # co-edit in one modal
    secret: bool = False,
)
```

The typed alternative to the field dicts in the
[patterns guide](../guide/patterns.md#typed-schemas-formfield-formschema). Both
forms are accepted by `FormView` and `FormLayoutView`.

#### `FormSchema` (typed schema)

Base class for declarative form definitions. Override `get_fields()` to return
the `FormField` list; pass an instance as the pattern's `schema=`.

#### `WizardStep` (typed schema)

```python
WizardStep(
    name: str,
    builder: Callable,                    # builds the step's content
    validator: Optional[Callable] = None, # gates advancing past this step
    condition: Optional[Callable] = None, # skip the step when it returns False
)
```

#### `WizardSchema` (typed schema)

Base class for declarative wizard definitions. Override `get_steps()` to return
the `WizardStep` list; pass an instance as the pattern's `schema=`.

#### `RoleCategory` (typed schema)

```python
RoleCategory(
    name: str,
    roles: dict[str, int],                         # label -> role_id
    exclusive: bool = False,                       # at most one active
    required: bool = False,                        # at least one active
    color: Optional[discord.Color] = None,         # container accent
    button_style: Optional[ButtonStyle] = None,    # default secondary
    icon: Optional[str] = None,                    # title prefix emoji
    description: Optional[str] = None,             # text between heading and buttons
)
```

#### Class Attributes

| Attribute | Default | Purpose |
|---|---|---|
| `title` | `"Server Roles"` | H2 above all categories. `None` suppresses. |
| `subtitle` | `None` | Optional H3 below title. Set to a string to render. |
| `hint_normal` | `None` | Hint for free-multi-select categories. |
| `hint_exclusive` | `"◉"` | Hint for exclusive-only categories (U+25C9 fisheye, text-size filled circle). |
| `hint_required` | `"*"` | Hint for required-only categories. |
| `hint_exclusive_required` | `"◉ *"` | Hint for exclusive+required categories. Both indicators render at text-size for visual consistency. |
| `assigned_message` | `"Gave you **{role}**."` | Response after role added (no swap). |
| `removed_message` | `"Removed **{role}**."` | Response after role removed. |
| `required_message` | `"You must keep at least one **{category}** role."` | Response when required-last removal rejected. |
| `swap_message` | `"Switched to **{role}** (removed {removed})."` | Response after exclusive swap. |
| `role_error_message` | `"Could not update roles: {error}"` | Response on role mutation failure. |
| `categories` | `[]` | List of `RoleCategory`. Declared on the subclass. |

Each `*_message` template is validated at class definition against its
documented placeholders (`{role}`, `{category}`, `{removed}`, `{error}`)
-- a typo'd or unbalanced placeholder raises `ValueError` naming the
attribute and the placeholders it accepts, instead of a bare `KeyError`
from inside the click that renders it.

#### Override Hooks

Hooks on `RolesLayoutView` are `@classmethod` (not instance methods). The dispatch path routes through `DynamicPersistentButton` which has no view instance at click time; hook classmethods read class attributes and respond to the interaction directly. An override sends its own reply through the public `respond_safe(interaction, ...)` helper (`from cascadeui import respond_safe`), which falls back to a followup when the ack backstop has already fired. `super()` works normally.

| Hook | Purpose |
|---|---|
| `format_category_title(category)` | Heading line for the category. Default: `f"### {category.name}"` plus optional `category.icon` prefix. |
| `format_category_hint(category)` | Hint rendered below the heading. Default routes to `hint_*` attribute. Return `None` to skip. |
| `format_button_label(role_name, role_id, category)` | Button label. Default: `role_name`. |
| `format_button_emoji(role_name, role_id, category)` | Button emoji. Default: `None`. |
| `format_button_style(role_name, role_id, category)` | Button style. Default: `category.button_style` or `ButtonStyle.secondary`. |
| `build_category_card(category)` | Render one category as a Container. Default composes the smaller `format_*` hooks. |
| `on_role_assigned(interaction, member, role, category)` | Response after role added without swap. |
| `on_role_removed(interaction, member, role, category)` | Response after role removed. |
| `on_role_swap(interaction, member, role_added, roles_removed, category)` | Response after exclusive-mode swap. |
| `on_role_required_block(interaction, member, role, category)` | Response when required-category last-role removal rejected. |
| `on_role_error(interaction, error)` | Response on role mutation failure. |

The five `format_*` hooks compose inside the synchronous `build_ui` and are refused at class definition when `async def`. The `on_role_*` hooks accept either `def` or `async def`.

#### Persistent Variant

`PersistentRolesLayoutView` composes `_PersistentMixin` with `RolesLayoutView`. Defaults: `owner_only = False`, `exit_policy = "disable"`, `timeout = None`. Requires `persistence_key=` at construction. Role buttons survive restart independent of view re-attachment because each is a globally-registered `DynamicPersistentButton` subclass. The default `on_restore` re-renders the message from the current `categories` on every restart so source-code edits propagate to the displayed message; unchanged panels pay zero API cost via the render-hash short-circuit in `refresh()`.

See [`docs/guide/patterns.md`](../guide/patterns.md#roleslayoutview-persistentroleslayoutview) for detailed cardinality behavior, customization tiers, and examples.

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

##### `on_bind(bot)` *(override)*

Inject non-serializable runtime dependencies (a database pool, the bot, a service client) from `bot`. The library calls it during `send()` when the bot is derivable from the construction context, and during restore before `on_restore`. Shared by every persistent view through `_PersistentMixin`. See [Runtime dependencies via `on_bind`](../guide/persistence.md#runtime-dependencies-via-on_bind).

##### `on_restore(bot)` *(override)*

Called after the view is restored on bot restart, once `bot.wait_until_ready()` resolves so the gateway cache (`get_user`, members, channels) is warm. A render here resolves real values instead of cold defaults. Interaction routing registers earlier, during reattach, so the view is clickable before this render runs.

---

## V1 Views (Classic)

### `StatefulView`

Base class for V1 views. Extends `discord.ui.View`.

```python
StatefulView(context=None, **kwargs)
```

#### V1-Specific Methods

##### `send(content=None, *, embed=None, embeds=None, file=None, files=None, allowed_mentions=None, ephemeral=False)`

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

Called after the view is restored on bot restart, once `bot.wait_until_ready()` resolves so the gateway cache (`get_user`, members, channels) is warm. A render here resolves real values instead of cold defaults. Interaction routing registers earlier, during reattach, so the view is clickable before this render runs.

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

Supports the same `menu_style`, `auto_exit_button`, `on_category_selected`, and cross-version category validation as `MenuLayoutView`.

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
    fields=[{"id": str, "type": "text"|"integer"|"float"|"date"|"boolean"|"select"|"multi_select", "label": str, "validators": [...], ...}, ...],
    **kwargs,
)
```

Non-text (`select`, `boolean`) fields share V1's five action rows; a field that overflows the budget raises `ValueError` at construction, naming the field. `FormLayoutView` has no equivalent row cap.

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

- `await set_page(n)` -- Jumps to zero-based page `n`, clamped to range, fires `on_page_changed`, and re-renders. The supported cursor move; setting `current_page` directly does not re-render.
- `await refresh_data(items)` -- Re-paginates with new data. Raises `RuntimeError` if not created via `from_data()`.
- `_build_extra_items()` *(override)* -- Hook for adding components below navigation buttons (rows 1-4).

---

## `PersistenceMiddleware(manager=None, *, backend=None, registry=None, application=None, bot=None, migrators=None, restore_concurrency=8)`

Write-through middleware that owns the full persistence pipeline. Install via `setup_middleware` once in `setup_hook`, after loading cogs.

- Without `bot`: data-only persistence
- With `bot`: also re-attaches PersistentView and PersistentLayoutView instances, and installs the message-deletion cleanup listener
- `backend`: a `PersistenceBackend` instance (e.g. `SQLiteBackend`, `InMemoryBackend`) used as the shorthand for any namespace not configured explicitly
- `registry`, `application`: per-namespace configs (`RegistryPersistence`, `ApplicationPersistence`) that override the shorthand. Scoped state rides under the application namespace -- opt a scoped slot in via `persistent_slots = ("scoped",)` on the view class.
- `migrators`: optional dict with `"schema"` and/or `"kwargs"` keys, each mapping a `(name, from_version)` tuple to an async migrator callable. When omitted, no migrators are registered through this kwarg; the `@register_migrator` / `@register_kwargs_migrator` decorators are the canonical registration path, and this dict is the programmatic bulk alternative.
- `restore_concurrency`: positive int bounding concurrency in both restore phases: the channel and message fetches during startup reattach, and the post-ready `on_restore` repaint that follows (default `8`). The repaint additionally serializes panels sharing a channel (message edits rate-bucket per channel) while panels in distinct channels fan out under this bound.

```python
from cascadeui import PersistenceMiddleware, setup_middleware
from cascadeui.persistence import SQLiteBackend

await setup_middleware(
    PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=self),
)
```

The reattach summary (`{"restored": [...], "skipped": [...], "failed": [...], "removed": [...], "unreachable": [...]}`) is available via `await store.persistence_manager.reattach_persistent_views()`.

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

Wraps a callable (`async def` or a plain `def`) so exceptions are logged with context (message at ERROR, traceback at DEBUG) and then **re-raised** for the caller to handle. Use where a raised exception would otherwise reach the asyncio event loop without any indication of which call site produced it. Reach for `safe_execute` instead when the exception should be absorbed rather than propagated.

```python
from cascadeui import with_error_boundary

@with_error_boundary("sync_scores")
async def sync_scores(user_id):
    ...
```

### `@with_retry(config=None)`

Retries a callable (`async def` or a plain `def`) on failure with exponential backoff. The decorated result is always awaitable, since the retry loop is. Accepts an optional `RetryConfig(max_retries=3, backoff_factor=1.0, exceptions_to_retry=(Exception,), max_backoff=30.0)`.

```python
from cascadeui import RetryConfig, with_retry

@with_retry(RetryConfig(max_retries=5, backoff_factor=2.0))
async def fetch_profile(user_id):
    ...
```

### `safe_execute(coro, fallback=None, log_error=True)`

One-shot wrapper that awaits a coroutine and returns `fallback` on exception (with a logged traceback). Pair with the decorators when the call site is not the right place to attach an error boundary.

```python
from cascadeui import safe_execute

result = await safe_execute(fetch_profile(user_id), fallback={})
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

The manager exposes four methods: `create_task(owner_id, coro)`,
`cancel_tasks(owner_id)`, `wait_tasks(owner_id)`, and `get_task_count(owner_id=None)`.

---

## Snowflake Coercion

The library accepts either an `int` or any object carrying an `.id` wherever a
Discord ID is expected, and coerces at the boundary. These helpers are that
coercion, exposed for user code doing the same normalization.

### `is_snowflake(value) -> bool`

Whether `value` is plausibly a Discord snowflake. Decodes the creation
timestamp packed into the id's high bits and checks it lands between
Discord's epoch and now, so an oversized integer fails on the date it
decodes to rather than on its size. Test fixtures
using small integers read as `False`.

### `coerce_snowflake_id(value) -> int`

Returns `value` if it is an `int`, or `value.id` if it carries one. Raises
`TypeError` for anything else, so a string or dict fails where it is passed
rather than corrupting a state key later.

### `coerce_snowflake_id_set(values) -> set[int]`

The same coercion across an iterable. This is what the `allowed_users` setter
applies, which is why `{ctx.author, opponent}` works alongside
`{ctx.author.id, opponent.id}`.

### `coerce_snowflake_match(match_dict, snowflake_keys) -> dict`

Converts named regex capture groups to `int` for known snowflake keys, leaving
other keys unchanged. For `DynamicItem` subclasses, whose `match.groupdict()`
yields strings (or `None` for optional groups):

```python
from cascadeui import coerce_snowflake_match

data = coerce_snowflake_match(match.groupdict(), frozenset({"user_id", "guild_id"}))
```

---

## Logging

CascadeUI is silent until `setup_logging()` is called: the package root carries a
`NullHandler`, and every module logs through `logging.getLogger(__name__)`.

### `setup_logging(*, level=logging.INFO, actions=True, file=True, stream=True, trace=False, path="logs", max_files=10, prefix="cascadeui", mode="a", encoding="utf-8", colors=None, color=None, template=None, stream_formatter=None, file_formatter=None, handler=None)`

Attaches a colored console sink and a date-stamped file sink to the `cascadeui`
logger. Call it once, at startup, with no arguments:

```python
from cascadeui import setup_logging

setup_logging()
```

That gives a colored console sink, a rotating file in `./logs`, and the action
log, all at `INFO`. Everything below is optional tuning.

Both sinks run behind a `QueueHandler` / `QueueListener`, so log I/O happens on a
background thread rather than the calling one, and the queue drains at
interpreter exit. Calling it again reconfigures from scratch, removing the
handlers a previous call attached, so repeat calls never double-log.

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `level` | `logging.INFO` | Level for the `cascadeui` logger |
| `actions` | `True` | Install `LoggingMiddleware` so every dispatched action is logged. `False` drops the action log; a level string (`"DEBUG"`) places it lower so it stays out of INFO logs |
| `file` | `True` | Write the date-stamped file sink |
| `stream` | `True` | Write the console sink |
| `trace` | `False` | Install ViewStore dispatch tracing |
| `path` | `"logs"` | Directory for file sinks |
| `max_files` | `10` | Rotation ceiling; older files are dropped past this count |
| `prefix` | `"cascadeui"` | Filename prefix for file sinks |
| `mode` | `"a"` | File open mode |
| `encoding` | `"utf-8"` | File encoding |
| `colors` | `None` | A `ColorScheme`, or a preset name: `"default"`, `"ocean"`, `"forest"`, `"none"` |
| `color` | `None` | Force color on or off; `None` auto-detects |
| `template` | `None` | A `FormatTemplate`, or a preset name: `"default"`, `"minimal"`, `"detailed"`, `"compact"` |
| `stream_formatter` | `None` | Replace the console formatter outright |
| `file_formatter` | `None` | Replace the file formatter outright |
| `handler` | `None` | Attach an additional handler alongside the built-in sinks |

```python
from cascadeui import setup_logging

setup_logging(level="DEBUG", colors="ocean", template="minimal", max_files=30)
```

Standard Python control still works afterwards, so a single noisy module can be
raised or lowered on its own:

```python
logging.getLogger("cascadeui.state.store").setLevel(logging.DEBUG)
```

The listener thread does not survive `os.fork()`; a forked child calls
`setup_logging()` again.

### `ColorScheme(debug=..., info=..., warning=..., error=..., critical=..., timestamp=..., function=..., name=..., reset=...)`

Raw ANSI escape strings, one per level plus the three field slots the console
template highlights. Set any to `""` to drop coloring for that element. Pass an
instance to `setup_logging(colors=...)`, or name a preset.

```python
from cascadeui import ColorScheme, setup_logging

setup_logging(colors=ColorScheme(info="\x1b[36m"))
```

### `FormatTemplate(stream_fmt=..., file_fmt=..., datefmt="%Y-%m-%d %H:%M:%S", capitalize_module=True)`

The layout of a log line. Both strings use `{`-style placeholders (`{asctime}`,
`{levelname}`, `{funcName}`, `{name}`, `{message}`). `stream_fmt` also accepts
the color tokens `$ts$`, `$lvl$`, `$fn$`, `$name$`, and `$r$`, replaced
at build time from the active `ColorScheme`; `file_fmt` is plain.

### `JSONFormatter(fields=None, indent=None)`

One JSON object per record, for a log aggregator. `fields` selects which record
attributes are included (a standard set by default); `indent` pretty-prints.

Under the queue that `setup_logging` installs, the `QueueHandler` pre-formats
each record and clears `exc_info`, so a traceback lands inside `message` rather
than a separate `exception` key. Attach a synchronous handler carrying this
formatter through `setup_logging(handler=...)` to get the structured key.
