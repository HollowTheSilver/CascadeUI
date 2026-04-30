# Core Concepts

This page covers the mental models behind CascadeUI. The diagrams
and vocabulary here recur across every other guide page; the
[Views](views.md) and [State](state.md) guides build on the
concepts introduced below.

---

## Data Flow

CascadeUI follows a **unidirectional data flow** pattern. Every state change
traces the same path -- no scattered mutation, no surprise side effects:

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│   User clicks button / selects option / submits modal            │
│                          │                                       │
│                          ▼                                       │
│   Callback runs (your code)                                      │
│   self.dispatch("MY_ACTION", {"key": value})                     │
│                          │                                       │
│                          ▼                                       │
│   Middleware: logging → persistence → undo → custom              │
│                          │                                       │
│                          ▼                                       │
│   Reducer transforms state                                       │
│   (pure function: old state → new state)                         │
│                          │                                       │
│                          ▼                                       │
│   Subscribers notified (filtered by action + selector)           │
│                          │                                       │
│                          ▼                                       │
│   on_state_changed() → build_ui() → refresh()                    │
│                          │                                       │
│                          ▼                                       │
│   Discord message edited with new UI                             │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**Why this matters:** mutating a variable inside a callback does not update the
UI. The state store is the single source of truth. To change what the user sees:
dispatch an action, let the reducer transform state, and let the subscriber
pipeline handle the rest.

For the full details -- subscribers, selectors, scoped state, batching, and
custom reducers for shapes that outgrow the slot model -- see the
[State Management](state.md) guide.

---

## Sessions, Views, and Navigation

CascadeUI organizes runtime state into two layers plus a navigation mechanism:

```
Session (e.g. "SettingsView:user_123_guild_456")
│
├── Members: [view_id_a, view_id_b, ...]
├── Shared Data: { cross-view metadata }
└── History: [ store debug audit trail ]

View (e.g. "SettingsHub")
│
├── Nav Stack: [parent_entry, ...]    ← view-local, forward-transferred on push/pop
├── Undo/Redo Stacks                  ← view-local, per enable_undo opt-in
└── Custom state from dispatched actions
```

### How the layers interact

| Concept | What it is | Lifetime |
|---------|-----------|----------|
| **View** | A single UI screen (one Discord message). Created on `send()`, destroyed on `exit()`, `on_timeout()`, `on_message_delete()`, or instance replacement (`on_replaced()` then `exit()`). | Until the message is exited, deleted, replaced, or times out. |
| **Session** | A coordination group for views sharing metadata. Created automatically when the root view calls `send()`. | Until the last member exits. |
| **Navigation** | View-local push/pop stack. `push()` replaces the current view, `pop()` reconstructs the previous one. `replace()` is a one-way transition with no return path. | Carried per-view, forward-transferred through push/pop chains. |

### Instance limiting

`instance_limit` caps how many **instances** of a view class a user can have
open. A user who opens a settings hub, pushes into a sub-page, and pushes
again still occupies one instance (pushed views inherit the root's identity).

`instance_scope` controls the indexing key:

| Scope | Meaning | Example |
|-------|---------|---------|
| `"user"` | Per-user globally | One settings panel per user, any guild |
| `"guild"` | Per-guild | One panel per guild, any user |
| `"user_guild"` | Per-user per-guild *(default)* | One panel per user in each guild |
| `"global"` | Singleton | One panel total, for everyone |

When a new instance would exceed the limit, `instance_policy` decides the
outcome:

- `"replace"` *(default)* -- the old view is exited (governed by
  `replace_policy`) and the new one takes its place. Views with active
  participants or attached children from other users are protected by default
  (`protect_attached = True`). `on_replaced()` fires on the old view before
  teardown.
- `"reject"` -- the new `send()` is blocked; `on_instance_limit` fires and
  `send()` returns `None`

For the full architectural model behind these attributes, see
[The Five Pillar Model](five-pillars.md). For the detailed Views API, see
[Views](views.md).

---

## State Topology

The state store holds a single dict. Understanding its shape makes debugging
and custom reducers straightforward:

```
state
├── views/                    (ephemeral - cleared on restart)
│   └── <view_id>/
│       ├── type, user_id, guild_id, channel_id
│       ├── message_id, created_at
│       ├── nav_stack: [...]       (view-local navigation breadcrumb)
│       ├── undo_stack: [...]      (view-local, when enable_undo=True)
│       ├── redo_stack: [...]      (view-local)
│       └── custom data from dispatched actions
│
├── sessions/                 (ephemeral - cleared on restart)
│   └── <session_id>/
│       ├── members: [view_id, ...]
│       ├── shared_data: { ... }   (cross-view metadata via update_session())
│       ├── history: [...]
│       └── created_at, updated_at
│
├── components/               (ephemeral - pruned on restart)
│   └── <component_custom_id>/
│       ├── view_id, type, last_interaction
│       └── interaction_count
│
├── modals/                   (ephemeral - pruned on restart)
│   └── <modal_view_id>/
│       └── type, submitted_at
│
├── persistent_views/         (survives restart)
│   └── <persistence_key>/
│       ├── class_name, channel_id, message_id
│       ├── user_id, guild_id
│       └── custom data
│
└── application/              (user-managed, persists via opt-in slots)
    ├── <slot_name>/          (custom keys set by dispatched actions)
    │   └── ...
    │
    ├── scoped/               (ad-hoc bucket for dispatch_scoped)
    │   ├── user:<id>/
    │   ├── guild:<id>/
    │   ├── user_guild:<uid>:<gid>/
    │   └── global/
    │
    └── <named_scoped_slot>/  (opt-in via scoped_slot class attribute)
        ├── user:<id>/
        ├── guild:<id>/
        ├── user_guild:<uid>:<gid>/
        └── global/
```

**Ephemeral** entries (`views`, `sessions`, `components`, `modals`)
are rebuilt or pruned on every restart. They represent live runtime state.

**Persistent** entries (`persistent_views`, opt-in `application` slots)
survive restarts when a [persistence backend](persistence.md) is configured.
Scoped buckets live under `application` and follow the same opt-in rules --
a scoped slot persists when its slot name is declared in `persistent_slots`
or registered via `SlotPolicy(persistent=True)`; otherwise it stays
in-memory.

See [State Management](state.md) for working with the store, and
[Persistence](persistence.md) for backend setup.

---

## Policy Surface

Every view class exposes a set of **class attributes** that control its
behavior. Set them on the class body -- the library reads them at definition
time via `__init_subclass__`. Each attribute pairs with either an `on_*` method
hook (for dynamic override) or a `*_message` attribute (for static text).

### Behavior Policies

Attributes are grouped by [pillar](five-pillars.md). See the
[Five Pillars Quick Reference](five-pillars.md#quick-reference) for the
complete list with defaults.

| Attribute | Default | Controls | Dynamic Override |
|-----------|---------|----------|-----------------|
| `owner_only` | `True` | Reject interactions from non-owners | `on_unauthorized()` |
| `unauthorized_message` | `"You cannot interact with this."` | Ephemeral rejection text | `on_unauthorized()` |
| `instance_limit` | `None` (unlimited) | Max concurrent instances per scope | `on_instance_limit()` |
| `instance_limit_message` | `None` | Rejection text on limit hit | `on_instance_limit()` |
| `instance_scope` | `"user_guild"` | Instance limit indexing key | -- |
| `instance_policy` | `"replace"` | What happens when limit exceeded: `"replace"` or `"reject"` | -- |
| `replace_policy` | `"delete"` | What happens to the old view on replace: `"delete"` or `"disable"` | -- |
| `exit_policy` | `"disable"` | Bare `exit()` behavior: `"disable"` (freeze) or `"delete"` | -- |
| `participant_limit` | `None` (unlimited) | Max total occupants (owner + participants) | `on_participant_limit()` |
| `participant_limit_message` | `"This session is full."` | Rejection text on capacity hit | `on_participant_limit()` |
| `auto_register_participants` | `False` | Auto-register `allowed_users` on `send()` | -- |
| `protect_attached` | `True` | Block replacement when other users are attached | `on_instance_limit()` (fallback) |
| `replaced_message` | `None` | Channel notification when a view is replaced | `on_replaced()` |
| `error_message` | `"An unexpected error occurred..."` | Ephemeral error embed description | `on_error()` |
| `reopen_failure_message` | `"Could not refresh this view..."` | Ephemeral text when ephemeral refresh fails | `on_reopen_failure()` |
| `state_scope` | `None` | Scoped state key: `"user"`, `"guild"`, `"user_guild"`, `"global"` | -- |
| `scoped_slot` | `None` | Named bucket for scoped writes (routes to `state["application"][slot]`); `None` uses the default `scoped` bucket | -- |

### Lifecycle and Interaction

| Attribute | Default | Controls |
|-----------|---------|----------|
| `timeout` | `180` | Seconds before `on_timeout()` fires (`None` = no timeout) |
| `auto_defer` | `True` | Auto-defer unacknowledged interactions |
| `auto_defer_delay` | `2.5` | Seconds before auto-defer fires |
| `serialize_interactions` | `True` | Process button clicks sequentially (prevents racing edits) |
| `auto_refresh_ephemeral` | `None` | Engages the 15-min ephemeral refresh handoff. `None` derives from `timeout` (in-window declines, longer engages); set `True`/`False` to pin. |
| `refresh_warning_seconds` | `90` | How early (in seconds) to swap in the refresh button before the 900s wall |
| `refresh_button_label` | `"Continue Session"` | Label on the ephemeral refresh button |
| `refresh_button_style` | `ButtonStyle.primary` | Style of the ephemeral refresh button |
| `auto_back_button` | `False` | Add a back button when pushed onto a nav stack |
| `enable_undo` | `False` | Track undo/redo history for this view |
| `undo_limit` | `20` | Max undo snapshots |
| `refresh_cooldown_ms` | `None` | Proactive edit cooldown in milliseconds; refreshes during the window schedule one deferred edit and re-read store state at fire time |

### The three-tier precedence model

Every policy attribute follows the same precedence:

1. **Class attribute** -- the default, set on the class body
2. **Method override** -- `on_*` hooks for dynamic behavior
3. **Explicit argument** -- e.g. `exit(delete_message=True)` always wins

```python
class MyView(StatefulLayoutView):
    exit_policy = "disable"             # 1. Class default: freeze on exit

    async def on_timeout(self):
        await self.exit(delete_message=True)  # 3. Explicit arg overrides policy
```

---

## Component Tiers

Discord.py components fall into three categories based on how they interact
with the state store:

### Tier 1: Interactive Components

These fire standalone `INTERACTION_CREATE` events. CascadeUI wraps them with
automatic `COMPONENT_INTERACTION` dispatching:

| CascadeUI Class | Wraps | Interaction |
|----------------|-------|-------------|
| `StatefulButton` | `discord.ui.Button` | Click → callback → `COMPONENT_INTERACTION` |
| `StatefulSelect` | `discord.ui.Select` | Selection → callback → `COMPONENT_INTERACTION` |
| `Dropdown` | alias for `StatefulSelect` | Same |
| `RoleSelect` | `discord.ui.RoleSelect` | Role selection → callback |
| `ChannelSelect` | `discord.ui.ChannelSelect` | Channel selection → callback |
| `UserSelect` | `discord.ui.UserSelect` | User selection → callback |
| `MentionableSelect` | `discord.ui.MentionableSelect` | Mentionable selection → callback |

Use these for buttons and selects in both V1 and V2 views. Each click
dispatches a `COMPONENT_INTERACTION` action to the state store for tracking
and debugging.

### Tier 2: Modal Inputs

These live inside `Modal` dialogs. They have `custom_id` attributes but
`is_dispatchable = False` -- they do not fire standalone events. Instead, the
Modal collects all input values on submit and fires a single
`MODAL_SUBMITTED` action:

| CascadeUI Class | Wraps | Value Type |
|----------------|-------|-----------|
| `TextInput` | `discord.ui.TextInput` | `str` |
| `Checkbox` | `discord.ui.Checkbox` | `bool` |
| `CheckboxGroup` | `discord.ui.CheckboxGroup` | `list[str]` |
| `RadioGroup` | `discord.ui.RadioGroup` | `str` |
| `FileUpload` | `discord.ui.FileUpload` | `list` |

All five share the same contract: `custom_id` derived from label,
optional `validators` list, and automatic value write-back on submit.
See [Components -- Modal Inputs](components.md#modal-inputs) for usage.

### Tier 3: Display-Only Components

These are pure rendering -- no `custom_id`, no interaction, no state store
involvement:

`TextDisplay`, `Container`, `Section`, `Separator`, `MediaGallery`,
`Thumbnail`, `ActionRow`, `File`

CascadeUI provides builder functions (`card()`, `key_value()`, `alert()`,
`divider()`, `gallery()`, etc.) that produce these components with less
boilerplate. See [Components -- V2 Builders](components.md#v2-builder-functions).

---

## Extension Strategies

Component Tiers groups primitives by *what they do* (fire events, submit
values, render). Extension Strategies groups them by *how CascadeUI extends
discord.py*. The two axes are orthogonal and both matter: knowing a
component's tier tells you when Discord calls it; knowing its extension
strategy tells you how to compose it into your own code.

CascadeUI uses four strategies. Each answers a different question.

| Strategy | What it does | Examples | When to reach for it |
|---|---|---|---|
| **Subclass** | Extends a `discord.ui.*` primitive with behavior baked into the type | `StatefulButton`, `StatefulSelect`, `StatefulView`, `StatefulLayoutView`, `PersistentView`, `PersistentLayoutView` | Behavior is identity-coupled: callback dispatch, state binding, `custom_id` contracts, lifecycle hooks |
| **Builder function** | Returns a bare `discord.ui.*` instance pre-wired with sensible defaults | `card()`, `action_section()`, `image_section()`, `link_section()`, `confirm_section()`, `stats_card()`, `progress_bar()`, `button_row()`, `tab_nav()` | A layout primitive just needs composition shorthand -- no new type, no state, just a cleaner call site |
| **Wrapper / decorator** | Augments an existing component instance with orthogonal behavior | `with_loading_state`, `with_confirmation`, `with_cooldown` | The behavior applies to *any* button, not just a specific subclass -- loading UI, confirmation prompts, and cooldowns all compose onto the same primitive |
| **Pre-built pattern** | Complete multi-component fragment solving a recognizable problem | `ConfirmationButtons`, `PaginationControls`, `ToggleGroup`, `ProgressBar` (V1 composites), `emoji_grid`, `button_grid` | The problem appears often enough to ship a named solution |

### What CascadeUI deliberately does NOT subclass

The V2 layout primitives stay bare: `Container`, `Section`, `Separator`,
`MediaGallery`, `Thumbnail`, `ActionRow`, `TextDisplay`, `File`.

Three reasons these stay unwrapped:

1. **Pure rendering.** No callback, no state, no lifecycle -- there is nothing
   a subclass could integrate with the store.
2. **No customization surface.** Discord renders these client-side with fixed
   layouts; discord.py exposes only the data they carry. A subclass would
   forward `__init__` to the parent and add nothing.
3. **Builders scale better.** Five builder functions around `Section`
   (`action_section`, `toggle_section`, `image_section`, `link_section`,
   `confirm_section`) produce more discoverable, more composable code than
   five `Section` subclasses. Functions show up in module listings;
   subclasses hide in inheritance trees.

If Discord ever ships a customization point for one of these primitives
(e.g. a left-aligned `Section` accessory), the response is a new builder or
a kwarg on an existing one -- not a new subclass hierarchy.

### The mental test

- **Subclass** when behavior needs to live on the type (callback dispatch,
  state binding, identity-coupled contracts).
- **Build** when a bare primitive just needs better defaults (no new type,
  no state, just composition).
- **Wrap** when behavior is orthogonal and composable (loading, cooldowns,
  confirmation -- any of which should apply to any button).
- **Pattern** when a recognizable problem is worth naming.

This ordering also tracks cost: a subclass is the heaviest commitment
(adds a type to the public surface), a builder is the lightest (adds a
function), a wrapper is mid-weight (adds behavior without a type), and a
pre-built pattern is whatever composition the problem demands.

---

## Discord Interactions

Every button click, select choice, and modal submit in Discord produces an
**interaction** -- a one-shot request from Discord to your bot. Understanding
the interaction lifecycle is worth the time: it explains why CascadeUI's
auto-defer, `respond()`, and `refresh()` work the way they do, and when you
need to step outside them.

### Interaction Types

Not all interactions are the same. Discord defines several types, and they
behave differently:

| Type | When it fires | Where you see it |
|------|--------------|-----------------|
| `APPLICATION_COMMAND` | Slash commands, context menus | Your cog command handler, before `view.send()` |
| `MESSAGE_COMPONENT` | Button clicks, select menus | CascadeUI component callbacks |
| `MODAL_SUBMIT` | Modal form submitted | CascadeUI `Modal` callback |
| `APPLICATION_COMMAND_AUTOCOMPLETE` | Autocomplete suggestions | Outside CascadeUI scope |

The type matters because `defer()` behaves differently depending on it:

| Interaction type | `defer()` behavior |
|-----------------|-------------------|
| `APPLICATION_COMMAND` | Shows "Bot is thinking..." (visible loading indicator) |
| `MESSAGE_COMPONENT` | Silent acknowledgement (no visual change) |
| `MODAL_SUBMIT` | Silent acknowledgement |

This is the most common source of confusion: `defer()` in a slash command
handler shows a thinking indicator, but `defer()` in a component callback
produces no visible change. They are different Discord response types under
the hood. discord.py's `thinking=` parameter can override this, but forcing
`thinking=True` on a component interaction creates a *second* message (the
thinking indicator) that must be manually dismissed -- it does not modify the
original component message. CascadeUI's `with_loading_state` wrapper is a
cleaner solution for visual loading feedback on components:

```python
btn = with_loading_state(
    StatefulButton(label="Generate", callback=self.on_generate),
    loading_label="Generating...",
    loading_emoji="⏳",
)
```

This disables the button and swaps its label on the *existing* message while
the callback runs, then restores it automatically. No second message, no
manual cleanup.

### The Response Slot

Each interaction has exactly **one response slot**. The bot must fill it within
3 seconds or Discord shows "This interaction failed." Four options:

| Method | What it does | Creates a new message? |
|--------|-------------|----------------------|
| `interaction.response.defer()` | Acknowledge (silent for components, "thinking" for commands) | No |
| `interaction.response.edit_message()` | Edit the message the component is on | No |
| `interaction.response.send_message()` | Send a new message as the reply | Yes |
| `interaction.response.send_modal()` | Open a modal dialog | No (opens UI) |

Once any of these is called, `interaction.response.is_done()` returns `True`.
Calling a second one raises `InteractionResponded`. The slot is consumed -- no
take-backs.

### After the Response

After the one-shot response, `interaction.followup` provides unlimited
follow-up messages for the rest of the token's 15-minute lifetime:

```python
# Response slot consumed by defer
await interaction.response.defer()

# Followups work any number of times after that
await interaction.followup.send("First followup", ephemeral=True)
await interaction.followup.send("Second followup", ephemeral=True)

# Edit the deferred response (the original message)
await interaction.edit_original_response(content="Updated")
```

### Two Ways to Edit a Message

This distinction matters for long-lived views:

| Path | Method | Token Expiry | When to use |
|------|--------|-------------|-------------|
| **Interaction endpoint** | `interaction.edit_original_response()` | 15 minutes | Immediate edits tied to a specific click |
| **Channel endpoint** | `message.edit()` | Never | Background updates, state-driven refreshes |

CascadeUI's `refresh()` prefers the acting-view fast path when possible:
if the current component click targets the view's own message and the
response slot is still open, the refresh ships through
`interaction.response.edit_message()` in one HTTP round trip (ack + edit
combined). If any gate fails -- no bound interaction, response already
deferred, cross-view dispatch, modal submit -- `refresh()` falls through
to the channel endpoint (`self._message.edit()`), which has no token
expiry and works indefinitely. `exit()` always uses the channel endpoint,
so it works whether or not the interaction has been responded to.

The fast path is also cancelled if the edit itself stalls past
`auto_defer_delay - 1.0` seconds (default 1.5s). On a stall,
`refresh()` returns immediately and lets the auto-defer timer ack the
click; the channel endpoint is NOT engaged, because a second edit on
top of the cancelled fast path would consume the timer's ack budget
under genuine Discord latency. The next state-change refresh ships
the rebuilt tree. See
[Fast-Path Stall](known-limitations.md#fast-path-stall-under-discord-edit-latency)
for the trade-off.

This is why pattern component callbacks in CascadeUI deliberately do not
pre-defer: a manual `defer()` would consume the response slot and force
every refresh through the slower two-call channel path.

#### Exception: callbacks that genuinely take more than two seconds

The fast-callback rule above assumes the callback's work plus its
state-driven refresh complete inside `auto_defer_delay` (default 2.5
seconds). When a callback genuinely needs longer -- a database query
that blocks for one to two seconds, an `asyncio.gather` over external
APIs, heavy CPU work that cannot be moved off the event loop -- the
fast path is unavailable regardless of whether the callback pre-defers.
The auto-defer timer fires at 2.5 seconds, pre-acks the click, and
disqualifies the fast path; subsequent refreshes route through the
channel endpoint anyway.

For callbacks that match this profile, `await self._safe_defer(interaction)`
at the start of the callback is the right pattern:

```python
async def slow_callback(self, interaction: discord.Interaction):
    await self._safe_defer(interaction)        # ack the click immediately
    data = await fetch_from_database(...)       # 1-2 seconds of work
    await self.dispatch("DATA_LOADED", {"data": data})
    # on_state_changed fires; refresh ships via the channel endpoint.
```

The benefit is timing predictability, not performance: the click acks
within milliseconds rather than waiting for the auto-defer timer to
fire at 2.5s. Discord's client never enters the "did the bot
acknowledge?" state, so no "This interaction failed" toast can fire.
The refresh still routes through the channel endpoint (the same path
the auto-defer timer would have produced), so the visible result is
identical to a slow callback under default behavior -- the difference
is removing the 2.5-second window where Discord's UI sits in limbo.

The `_safe_defer` helper guards against double-deferring (it checks
`is_done()` before issuing the actual defer), so it is safe to call
in any code path that genuinely needs to ack early. The cost is
unconditional, however: every call to `_safe_defer` flips
`is_done()` to True, which disqualifies the acting-view fast path on
every subsequent `refresh()` for that interaction. Calling it
unnecessarily on a fast callback trades the one-call fast path for
the two-call channel path with no upside.

The rule of thumb: callbacks whose work routinely runs longer than
about a second and a half should call `_safe_defer` at the top;
callbacks that typically complete well under a second should not.
The threshold is approximate -- it depends on Discord round-trip
latency, which varies with backend load and the bot's own
concurrency. Bot authors running at higher load than the development
bench should profile real callbacks via `/cascadeui perf` and tune
toward whatever threshold their actual `notify_ms` distribution
implies.

### Ephemeral Message Constraints

Ephemeral messages ("Only you can see this") have restrictions that affect
how CascadeUI manages them:

| Constraint | What it means |
|-----------|--------------|
| Not fetchable | `channel.fetch_message(id)` returns `NotFound`. CascadeUI skips the post-send re-fetch for ephemeral views. |
| Only editable via interaction token | The channel endpoint (`message.edit()`) cannot edit ephemeral messages. Only `interaction.edit_original_response()` works, and the token expires after 15 minutes. |
| Not deletable via channel | `message.delete()` fails. Only `interaction.delete_original_response()` works, and it shares the same 15-minute token. |
| No reactions | Discord rejects reaction adds on ephemeral messages. |
| Followups are not auto-ephemeral | `interaction.followup.send()` is public by default. Pass `ephemeral=True` explicitly on each followup. |

The 15-minute token expiry is the reason `auto_refresh_ephemeral` exists.
Without it, an ephemeral view becomes uneditable after 15 minutes -- buttons
still fire callbacks, but `refresh()` silently fails because the channel
endpoint cannot reach the message. CascadeUI's ephemeral refresh system swaps
in a "Continue Session" button before the token expires, allowing the user to
click it and spawn a fresh ephemeral view via a new interaction token.

### The CascadeUI Callback Lifecycle

When a user clicks a button or selects an option, the interaction passes
through CascadeUI's `_scheduled_task` pipeline before reaching your callback.
Here is the full sequence:

```
Interaction arrives from Discord
        │
        ▼
interaction_check(interaction)
├── allowed_users set?  → only users in the set pass
├── owner_only = True?  → only the view creator passes
└── Failed?             → on_unauthorized() fires, callback skipped
        │
        ▼ (passed)
Timeout refreshed (resets the inactivity clock)
        │
        ├── auto_defer = True?
        │   └── Background timer starts (auto_defer_delay seconds)
        │       If the callback hasn't responded by then, defer() fires
        │
        ▼
serialize_interactions = True?
├── Yes → acquire asyncio.Lock (queued clicks wait here)
│         Auto-defer timer runs OUTSIDE the lock, so queued
│         interactions are deferred before Discord's 3s timeout
└── No  → callback runs immediately (parallel processing)
        │
        ▼
Your callback(interaction) executes
        │
        ▼
Post-callback defer (auto_defer = True)
└── If interaction.response.is_done() is still False,
    defer() fires immediately. Catches fast callbacks
    that use dispatch() → on_state_changed → refresh()
    (channel endpoint, not interaction response).
        │
        ▼
Exception? → on_error(interaction, error, item)
```

The auto-defer timer and post-callback defer both check `is_done()` before
acting, so they never conflict with a response your callback already sent.

### What CascadeUI Handles for You

Most callbacks never touch `interaction.response` directly. The library
provides four tools that cover the common cases:

**Auto-defer** (`auto_defer = True`, the default) -- two mechanisms ensure
every interaction is acknowledged:

1. A background timer defers unacknowledged interactions after
   `auto_defer_delay` seconds (default 2.5), handling slow callbacks.
2. A post-callback defer fires after every callback completes, handling
   fast callbacks that edit via the channel endpoint (the common
   `dispatch() → on_state_changed → refresh()` pattern). Without this,
   fast callbacks that finish before the timer fires would leave the
   interaction unacknowledged.

**`self.respond()`** -- sends a message to the user, routing through
`interaction.response.send_message()` when the slot is available or
`interaction.followup.send()` when auto-defer already consumed it. Use
this instead of `interaction.response.send_message()` in callbacks:

```python
async def my_callback(self, interaction):
    if not valid:
        await self.respond(interaction, "Invalid input!", ephemeral=True)
        return
    # ... normal state update
```

**`self.open_modal()`** -- opens a modal dialog, with a fallback if the
response slot is already consumed. `send_modal()` must be the first
response to an interaction -- it cannot follow a `defer()`. Under
`serialize_interactions`, a queued interaction may be auto-deferred before
the callback runs, making `send_modal()` impossible. `open_modal()` checks
`is_done()` and sends an ephemeral "please try again" fallback instead of
crashing:

```python
async def on_edit(self, interaction):
    modal = Modal(title="Edit", inputs=[...], callback=self.handle_edit)
    await self.open_modal(interaction, modal)
```

Returns `True` if the modal opened, `False` if the fallback fired.

**`self.refresh()`** -- edits the message via the channel endpoint (no
interaction involvement). Call it after `build_ui()` to push the new
component tree to Discord. Works at any time, not just during a callback.

**Interaction serialization** (`serialize_interactions = True`, the default) --
an `asyncio.Lock` ensures rapid button clicks are processed one at a time,
preventing racing `message.edit()` calls. Queued interactions are auto-deferred
while waiting for the lock, so Discord never shows "This interaction failed."

**Update coalescing** -- when multiple dispatches from independent views
converge on the same subscriber simultaneously (e.g. two players clicking at
the same time in a game), the subscriber notifications are coalesced
automatically. The first notification runs `on_state_changed` normally; any
that arrive while it is running set a pending flag and return immediately.
After the first completes, it re-runs once with the latest store state.
Single-user views are unaffected - the lock is never contended.

**ViewStore interaction preservation** -- when `build_ui()` calls
`clear_items()`, discord.py internally nulls the `_view` reference on every
removed component. Components remain in discord.py's routing table until
`message.edit()` triggers a re-registration. During that async gap, any pending
interaction for the old components would be silently discarded. CascadeUI's
`clear_items()` override preserves the `_view` reference on old components, so
interactions arriving during the gap are routed normally until the
re-registration cleans them up.

Together, these mechanisms form four layers of concurrency protection:

| Layer | Scope | Protects against |
|-------|-------|------------------|
| Interaction serialization | Per-view callbacks | Rapid clicks on the same view |
| Auto-defer timer | Per-interaction | Slow callbacks missing Discord's 3-second deadline |
| Update coalescing | Per-view subscriber | Concurrent dispatches targeting the same subscriber |
| ViewStore preservation | Per-view components | Stale component routing during async rebuilds |

### The Standard Callback Pattern

Most CascadeUI callbacks follow this shape:

```python
async def on_click(self, interaction):
    # 1. Validate (optional)
    if not valid:
        await self.respond(interaction, "Error!", ephemeral=True)
        return

    # 2. Update state
    await self.dispatch("MY_ACTION", {"key": value})
    # → triggers on_state_changed() → build_ui() → refresh()
    # → post-callback defer acknowledges the interaction

    # No manual defer needed. No manual message edit needed.
```

The `dispatch() → on_state_changed → refresh()` chain edits the message
via the channel endpoint and the post-callback defer acknowledges the
interaction. Both happen automatically.

### When You Need Manual Interaction Handling

| Scenario | What to do |
|----------|-----------|
| Sending multiple followup messages | First `self.respond()`, then `interaction.followup.send()` for additional messages |
| Editing via the interaction token | `await interaction.edit_original_response(...)` -- rarely needed, `refresh()` handles most cases |
| Working outside CascadeUI | Raw discord.py callbacks have no auto-defer -- `defer()` is your responsibility |

### V1 vs V2 Editing

V1 views (embeds + buttons) and V2 views (component trees) differ in how
they handle message content:

| Aspect | V1 (`StatefulView`) | V2 (`StatefulLayoutView`) |
|--------|----|----|
| Content model | Embed(s) + buttons attached via `view=` | The component tree IS the content |
| `refresh()` | Pass `embed=` or `content=` kwargs | No kwargs needed -- `view=self` is the content |
| `exit()` with disable | `edit(view=None)` strips buttons, embed stays | `_freeze_components()` + `edit(view=self)` preserves frozen UI |
| `exit()` with delete | `message.delete()` | `message.delete()` |
| Empty message | Embed visible with no buttons | `edit(view=None)` produces an empty message (Discord error 50006) |

V2 views cannot use `edit(view=None)` to "strip components" the way V1 views
can, because the component tree *is* the entire message. `exit()` handles this
automatically -- it freezes all interactive components and edits with the
frozen view. Understanding this distinction helps when writing custom exit or
timeout behavior.

---

## What's Next?

- **[The Five Pillar Model](five-pillars.md)** -- the architectural model behind every class attribute
- **[Views](views.md)** -- lifecycle, navigation, session management, policies in action
- **[Components](components.md)** -- buttons, selects, modals, builders, grid helpers
- **[State Management](state.md)** -- reducers, subscribers, scoped state, undo/redo
- **[View Patterns](patterns.md)** -- menus, forms, wizards, tabs, pagination, leaderboards, role panels
