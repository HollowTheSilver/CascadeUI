# Views

Views are the primary UI containers in CascadeUI. They integrate discord.py's view system with a centralized state store, lifecycle management, and task tracking.

CascadeUI supports two component systems:

- **V2 (recommended)** — `StatefulLayoutView` wraps discord.py's `LayoutView`. Content and controls live together in containers with accent colors. The view IS the message content.
- **V1 (classic)** — `StatefulView` wraps discord.py's `View`. Embeds sit on top, buttons float below. Content and controls are always visually separated.

Both share the same state integration, navigation stack, session limiting, undo/redo, and all other framework features through a shared `_StatefulMixin`.

---

## V2 Views (LayoutView)

### StatefulLayoutView

The base class for V2 views. Unlike V1, there are no `content` or `embed` parameters on `send()` — the component tree IS the message:

```python
from cascadeui import StatefulLayoutView, StatefulButton, card, divider
from discord.ui import ActionRow, TextDisplay

class MyView(StatefulLayoutView):
    session_limit = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_ui()

    def _build_ui(self):
        self.clear_items()
        self.add_item(
            card(
                "## My Dashboard",
                TextDisplay("Welcome to the V2 interface."),
                divider(),
                ActionRow(
                    StatefulButton(
                        label="Click Me",
                        style=discord.ButtonStyle.primary,
                        callback=self.on_click,
                    ),
                ),
                color=discord.Color.blurple(),
            )
        )
        self.add_exit_button()

    async def on_click(self, interaction):
        await interaction.response.defer()
        # Update state, rebuild UI, edit message
        self._build_ui()
        if self.message:
            await self.message.edit(view=self)

    async def update_from_state(self, state):
        self._build_ui()
        if self.message:
            await self.message.edit(view=self)
```

#### Sending a V2 view

```python
view = MyView(context=ctx)
await view.send()  # No content/embed params — the component tree is the content
```

#### Key differences from V1

| | V2 (`StatefulLayoutView`) | V1 (`StatefulView`) |
|---|---|---|
| Content | Component tree (Containers, TextDisplay) | Embeds + content string |
| `send()` | No content/embed params | Accepts content, embed, embeds |
| Interactive items | Must be wrapped in `ActionRow` | Can be added directly |
| Exit behavior | Freezes components in place | Strips view, keeps embed |
| Accent colors | Per-container via `card(color=...)` | One embed color |
| Components per message | Up to 40 | Up to 25 (5 rows x 5) |

!!! warning "ActionRow wrapping"
    Buttons and selects cannot be top-level children of a `LayoutView`. Always wrap them in `ActionRow` before calling `add_item()`. The V2 helper functions (`card`, `action_section`, `toggle_section`) handle this automatically when buttons are part of a container.

!!! warning "V2 exit behavior"
    Calling `message.edit(view=None)` on a V2 message produces an empty message (Discord error 50006) because the view IS the content. CascadeUI handles this automatically — `exit()` freezes all components with `_freeze_components()` and edits with the frozen view, preserving visual content.

### V2 View Patterns

CascadeUI includes V2-specific patterns that mirror the V1 patterns with container-based presentation.

#### TabLayoutView

Button-based tab switching where each tab builds a V2 component tree:

```python
from cascadeui import TabLayoutView, card, key_value, divider
from discord.ui import TextDisplay

class DashboardView(TabLayoutView):
    session_limit = 1

    def __init__(self, *args, **kwargs):
        tabs = {
            "Overview": self.build_overview,
            "Settings": self.build_settings,
        }
        super().__init__(*args, tabs=tabs, **kwargs)

    async def build_overview(self):
        return [
            card(
                "## Overview",
                key_value({"Users": "42", "Uptime": "3h 12m"}),
                color=discord.Color.green(),
            ),
        ]

    async def build_settings(self):
        return [
            card(
                "## Settings",
                TextDisplay("Configure your preferences here."),
                color=discord.Color.og_blurple(),
            ),
        ]
```

Tab builders are async functions that return a list of V2 components. The active tab's button is highlighted. An exit button is added automatically.

#### WizardLayoutView

Multi-step flow with Back/Next/Finish navigation and per-step validation:

```python
from cascadeui import WizardLayoutView, card, divider
from discord.ui import TextDisplay

class SetupWizard(WizardLayoutView):
    session_limit = 1

    def __init__(self, *args, **kwargs):
        steps = [
            {"name": "Welcome", "builder": self.build_welcome},
            {"name": "Config", "builder": self.build_config,
             "validator": self.validate_config},
            {"name": "Confirm", "builder": self.build_confirm},
        ]
        super().__init__(*args, steps=steps, on_finish=self.finish, **kwargs)
        self._mod_level = None

    async def build_welcome(self):
        return [card("## Welcome", TextDisplay("Let's set up your server."),
                      color=discord.Color.blurple())]

    async def build_config(self):
        return [card("## Configuration", TextDisplay("Select your moderation level."),
                      color=discord.Color.gold())]

    async def validate_config(self):
        return (self._mod_level is not None), "Please select a moderation level."

    async def build_confirm(self):
        return [card("## Confirm", TextDisplay(f"Level: {self._mod_level}"),
                      color=discord.Color.green())]

    async def finish(self, interaction):
        await interaction.response.send_message("Setup complete!", ephemeral=True)
        await self.exit()
```

Validators return `True` to advance, `False` to block, or a `(bool, str)` tuple where the string is an error message shown to the user.

#### FormLayoutView

Form with select menus, boolean toggles, and validation. Text input is handled via Modal:

```python
from cascadeui import FormLayoutView

fields = [
    {
        "id": "role", "type": "select", "label": "Role",
        "required": True,
        "options": [
            {"label": "Developer", "value": "dev"},
            {"label": "Designer", "value": "design"},
        ],
    },
    {"id": "notify", "type": "boolean", "label": "Notifications"},
]

async def on_submit(interaction, values):
    await interaction.response.send_message(f"Saved: {values}", ephemeral=True)

view = FormLayoutView(context=ctx, fields=fields, on_submit=on_submit)
```

Override `_rebuild_display()` to customize the form's visual presentation using V2 helpers like `card()`, `key_value()`, and `divider()`. See the `v2_form.py` example for a full implementation.

#### PaginatedLayoutView

Pages as V2 component trees instead of embeds:

```python
from cascadeui import PaginatedLayoutView, card
from discord.ui import TextDisplay

async def format_page(items):
    lines = "\n".join(f"**{i['name']}** — {i['rarity']}" for i in items)
    return [card("## Inventory", TextDisplay(lines), color=discord.Color.gold())]

view = await PaginatedLayoutView.from_data(
    items=all_items, per_page=5, formatter=format_page, context=ctx,
)
await view.send()
```

Navigation controls, jump buttons, go-to-page modal, and `refresh_data()` all work identically to the V1 `PaginatedView`.

### PersistentLayoutView

V2 views that survive bot restarts. See [Persistence](persistence.md) for setup.

```python
from cascadeui import PersistentLayoutView, StatefulButton, card
from discord.ui import ActionRow

class RolePanel(PersistentLayoutView):
    session_limit = 1
    session_scope = "guild"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(
            card(
                "## Role Selector",
                ActionRow(
                    StatefulButton(
                        label="Get Role",
                        custom_id="roles:get",  # Required for persistent views
                        callback=self.toggle_role,
                    ),
                ),
                color=discord.Color.blurple(),
            )
        )

    async def toggle_role(self, interaction):
        # Toggle role logic here
        ...
```

!!! warning "custom_id required"
    All interactive components in a `PersistentLayoutView` must have an explicit `custom_id`. Discord uses these to route interactions back to the view after a restart.

---

## Shared Features

These features work identically on both V1 and V2 views.

### Lifecycle

1. **Init** — view is created, components added, subscribed to store
2. **Send** — message sent, state registered (`VIEW_CREATED`)
3. **Interact** — user clicks buttons/selects, callbacks fire
4. **Exit/Timeout** — components disabled, state cleaned up (`VIEW_DESTROYED`)

### Timeout Handling

Views timeout after 180 seconds by default. On timeout, CascadeUI disables all components, edits the message, unsubscribes from the store, and cancels background tasks.

```python
super().__init__(*args, timeout=300, **kwargs)   # 5 minutes
super().__init__(*args, timeout=None, **kwargs)  # Never timeout
```

### Reacting to State Changes

Override `update_from_state()` to react when state changes:

```python
class MyView(StatefulLayoutView):
    subscribed_actions = {"MY_ACTION"}  # Only listen for specific actions

    async def update_from_state(self, state):
        self._build_ui()
        if self.message:
            await self.message.edit(view=self)
```

Set `subscribed_actions = None` to receive all actions (not recommended for performance).

### Auto-Defer Safety Net

CascadeUI automatically defers interactions when callbacks are slow, preventing the "This interaction failed" error when a response takes longer than 3 seconds.

```python
class MyView(StatefulLayoutView):
    auto_defer = True        # Default — enabled for all views
    auto_defer_delay = 2.5   # Seconds before auto-defer fires
```

1. When a component callback starts, a background timer is spawned
2. The timer waits `auto_defer_delay` seconds, then checks `interaction.response.is_done()`
3. If the callback hasn't responded yet, the timer calls `interaction.response.defer()`
4. If the callback responds before the timer, the timer is cancelled

Auto-defer is safe with all existing patterns — manual `defer()` calls, `with_loading_state`, `with_confirmation`, and `with_cooldown` all set `is_done()` before the timer fires.

!!! warning "Modal callbacks"
    `interaction.response.send_modal()` must be the first response to an interaction. If auto-defer fires before you call `send_modal()`, the interaction is consumed and the modal cannot be sent. If your callback opens a modal, ensure it does so quickly (within `auto_defer_delay`). You can also disable auto-defer for modal-heavy views with `auto_defer = False`.

### Interaction Serialization

By default, CascadeUI serializes interaction processing per view using an `asyncio.Lock`. When a user clicks buttons rapidly, each click waits for the previous one to finish instead of racing. This prevents overlapping `message.edit()` calls that cause "This interaction failed" errors.

```python
class MyView(StatefulLayoutView):
    serialize_interactions = True  # Default — prevents racing edits
```

The auto-defer timer runs outside the lock, so queued interactions are deferred before Discord's 3-second timeout even while waiting for the lock.

Set `serialize_interactions = False` on views where parallel callback processing is acceptable.

### Interaction Ownership

By default, only the user who created a view can interact with it:

```python
class MyView(StatefulLayoutView):
    owner_only = True                                    # Default
    owner_only_message = "You cannot interact with this."  # Default
```

To make a view accessible to everyone:

```python
class PollView(StatefulLayoutView):
    owner_only = False  # Anyone can vote
```

`PersistentView` and `PersistentLayoutView` default to `owner_only = False` since persistent views are typically shared panels.

#### Multi-User Access Control

For views shared between specific users (games, collaborative tools), set `allowed_users` to a set of user IDs. This overrides `owner_only` completely:

```python
class GameView(StatefulLayoutView):
    owner_only_message = "You're not part of this game."
    session_limit = 1

    def __init__(self, *args, opponent_id: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_users = {self.user_id, opponent_id}
```

You can mutate `allowed_users` at runtime (e.g., `view.allowed_users.add(new_player_id)`) for join-in-progress patterns.

To make session limiting apply to all participants (not just the view owner), register them after sending:

```python
view = GameView(context=ctx, opponent_id=opponent.id)
await view.send()
await view.register_participant(opponent.id)  # Raises SessionLimitError if they're in a game
```

#### Custom Access Control

Override `interaction_check()` for role-based or other advanced access logic:

```python
class AdminView(StatefulLayoutView):
    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return False
        return True
```

### Exit Button

```python
self.add_exit_button()  # Adds an "Exit" button with ❌ emoji

# Customize:
self.add_exit_button(label="Close", emoji=None, delete_message=True)

# PersistentView/PersistentLayoutView require a custom_id:
self.add_exit_button(custom_id="my_view:exit")
```

### Error Handling

All CascadeUI views include a built-in `on_error` handler that shows a red ephemeral embed when a callback raises an exception, preventing silent failures.

## Navigation Stack

Push views onto a stack and pop them to go back. This is the standard pattern for multi-level UIs like settings menus:

=== "V2"

    ```python
    class HubView(StatefulLayoutView):
        async def go_settings(self, interaction):
            await self.push(SettingsView, interaction,
                            rebuild=lambda v: v._build_ui())

    class SettingsView(StatefulLayoutView):
        async def go_back(self, interaction):
            await self.pop(interaction,
                           rebuild=lambda v: v._build_ui())
    ```

=== "V1"

    ```python
    class HubView(StatefulView):
        async def go_settings(self, interaction):
            await self.push(SettingsView, interaction,
                            rebuild=lambda v: {"embed": v.build_embed()})

    class SettingsView(StatefulView):
        async def go_back(self, interaction):
            await self.pop(interaction,
                           rebuild=lambda v: {"embed": v.build_embed()})
    ```

### The `rebuild` callback

The `rebuild=` parameter on `push()` and `pop()` eliminates boilerplate around deferring, rebuilding the UI, and editing the message:

1. The interaction is auto-deferred (if not already)
2. Your callback is called with the new view
3. The message is edited with the rebuilt view

For **V2 views**, `rebuild` typically calls `_build_ui()` and returns `None`. For **V1 views**, `rebuild` returns a dict of kwargs passed to `edit_original_response` (e.g., `{"embed": embed}`).

Both sync and async callables are supported.

### How it works

- `push(ViewClass, interaction, rebuild=...)` stops the current view, stacks it, and creates a new instance of the target class
- `pop(interaction, rebuild=...)` stops the current view, pops the stack, and reconstructs the previous view
- The stack is stored in session state and cleaned up when the session ends
- The new view inherits the same `session_id`, so all navigation within a session shares state

### Constructor kwargs are preserved automatically

When a view is pushed, all constructor kwargs are saved so `pop()` can reconstruct it faithfully. Non-reconstructible kwargs (`context`, `interaction`, `message`, etc.) are excluded and re-supplied by the framework. Subclasses don't need to do anything special.

### Auto Back Button

```python
class SettingsView(StatefulLayoutView):
    auto_back_button = True  # Back button added automatically when pushed
```

!!! warning "Push/pop between V1 and V2"
    `push()` and `pop()` between V1 and V2 views raises `TypeError`. Discord's `IS_COMPONENTS_V2` flag is a one-way switch per message — a V2 message cannot revert to V1 or vice versa. Use `replace()` for one-way transitions between versions. See [Known Limitations](known-limitations.md) for details.

### Push vs. Replace

| | `push()` | `replace()` |
|---|----------|-------------|
| Stack | Adds entry, supports back | No stack, one-way |
| Session | Shared | Shared |
| V1/V2 mixing | Blocked | Allowed |
| Use case | Menu hierarchy | Replacing the current view entirely |

## State Scoping

Isolate state per user or per guild so concurrent users don't overwrite each other:

```python
class ScopedCounterView(StatefulLayoutView):
    scope = "user"  # Each user gets independent state

    async def click(self, interaction):
        await interaction.response.defer()
        current = self.scoped_state.get("clicks", 0)
        await self.dispatch_scoped({"clicks": current + 1})
```

### Scope types

| Scope | Key | Use case |
|-------|-----|----------|
| `"user"` | User ID | Per-user settings, counters, preferences |
| `"guild"` | Guild ID | Per-server configuration |
| `None` (default) | N/A | Shared state (existing behavior) |

### How it works

Scoped data is stored under `state["application"]["_scoped"]["user:{id}"]` (or `"guild:{id}"`). The `scoped_state` property returns just the relevant slice, and `dispatch_scoped()` updates it through the reducer pipeline.

```python
# Read scoped state
my_data = self.scoped_state  # Dict for this user/guild

# Write scoped state (merges with existing)
await self.dispatch_scoped({"clicks": 5, "name": "Alice"})

# Store-level access
scoped = store.get_scoped("user", user_id=12345)
store.set_scoped("user", {"key": "value"}, user_id=12345)
```

### Cross-View Reactivity

`dispatch_scoped()` fires `SCOPED_UPDATE`, which other views don't subscribe to by default. For live cross-view updates, use `dispatch()` with a named action:

```python
# This does NOT notify other views:
await self.dispatch_scoped({"settings": {"theme": "dark"}})

# This notifies all views subscribing to "SETTINGS_UPDATED":
await self.dispatch("SETTINGS_UPDATED", {
    "scope_key": f"user:{self.user_id}",
    "changes": {"theme": "dark"},
})
```

The named action approach requires a [custom reducer](state.md#reducers) that writes to the scoped path:

```python
@cascade_reducer("SETTINGS_UPDATED")
async def settings_reducer(action, state):
    # @cascade_reducer passes a deep copy — mutate and return directly
    state.setdefault("application", {}).setdefault("_scoped", {})
    scope_key = action["payload"]["scope_key"]
    changes = action["payload"]["changes"]
    scoped = state["application"]["_scoped"].setdefault(scope_key, {})
    scoped.setdefault("settings", {}).update(changes)
    return state
```

**When to use which:**

| Method | Other views react? | Creates undo snapshots? | Use when... |
|--------|-------------------|------------------------|-------------|
| `dispatch_scoped()` | No | No | Quick writes where only the current view needs to update |
| `dispatch("NAMED_ACTION")` | Yes | Yes | Changes should be visible to other views, or undo is needed |

See the `v2_settings.py` and `settings_menu.py` examples for the full pattern.

## Undo/Redo

Enable undo/redo on any view by setting `enable_undo = True`. Each dispatched action creates a state snapshot that can be reverted:

```python
from cascadeui import UndoMiddleware, get_store

# Add the middleware once (e.g., in your cog's setup function)
store = get_store()
store.add_middleware(UndoMiddleware(store))

class EditableView(StatefulLayoutView):
    enable_undo = True
    undo_limit = 20  # Max snapshots to keep (default: 20)

    async def undo_action(self, interaction):
        await interaction.response.defer()
        await self.undo(interaction)

    async def redo_action(self, interaction):
        await interaction.response.defer()
        await self.redo(interaction)
```

### How it works

1. `UndoMiddleware` intercepts every dispatch. If the source view has `enable_undo = True`, it takes a `deepcopy` snapshot of `state["application"]` before the reducer runs.
2. The snapshot is pushed onto the session's undo stack.
3. `undo()` restores the snapshot and pushes the current state to the redo stack.
4. `redo()` does the reverse.
5. Performing a new action after undoing clears the redo stack (standard undo/redo semantics).

### What gets snapshotted

Only `state["application"]` is snapshotted, not the full state tree. Internal state like views, sessions, and subscriptions is not affected by undo/redo.

### Actions that don't create snapshots

Internal lifecycle actions (`VIEW_CREATED`, `VIEW_DESTROYED`, `NAVIGATION_PUSH`, `COMPONENT_INTERACTION`, etc.) are excluded from undo tracking. Only your custom application actions create snapshots.

!!! warning "`dispatch_scoped()` does not create undo snapshots"
    `dispatch_scoped()` dispatches a `SCOPED_UPDATE` action, which is excluded from undo tracking. If your view uses both `scope` and `enable_undo`, dispatch a custom action type via `self.dispatch()` instead, and write a reducer that updates the scoped state path directly. See the `v2_settings.py` example for this pattern.

!!! info "Batched actions"
    When actions are dispatched inside a `batch()`, the undo middleware takes one snapshot before the batch starts. Undoing reverts everything the batch did in one step.

## Session Limiting

Session limiting controls how many active instances of a view can exist within a given scope. Without it, users can invoke commands repeatedly, spawning duplicate views that pile up in chat.

### Basic Usage

Set class attributes on any view subclass:

```python
class SettingsView(StatefulLayoutView):
    session_limit = 1              # Max active instances (None = unlimited)
    session_scope = "user_guild"   # Scope for counting instances
    session_policy = "replace"     # What to do when the limit is reached
```

With this configuration, if a user opens a second `SettingsView` in the same guild, the first one is automatically exited before the new one is sent.

### Session Scope

| Scope | Groups by | Use case |
|-------|-----------|----------|
| `"user"` | User ID | Per-user views across all guilds |
| `"guild"` | Guild ID | Per-server panels shared by all users |
| `"user_guild"` (default) | User ID + Guild ID | Per-user within a specific guild |
| `"global"` | Nothing | One instance across the entire bot |

### Session Policy

| Policy | Behavior |
|--------|----------|
| `"replace"` (default) | Exits the oldest view(s) to make room |
| `"reject"` | Raises `SessionLimitError`, blocking the new view |

```python
from cascadeui import SessionLimitError

class ExpensiveView(StatefulLayoutView):
    session_limit = 1
    session_policy = "reject"

# In your command:
try:
    view = ExpensiveView(context=ctx)
    await view.send()
except SessionLimitError:
    await ctx.send("You already have this open.", ephemeral=True)
```

### PersistentView Protection

`PersistentView` and `PersistentLayoutView` instances are protected from being replaced by non-persistent views. If a regular view tries to replace a persistent one, `SessionLimitError` is raised instead. Persistent views can replace other persistent views of the same type.

### Participants and Multi-User Views

By default, session limiting only tracks the view owner. For multi-user views (games, polls), call `register_participant(user_id)` after `send()` to add non-owner users to the session index. This prevents a participant from being in two games at once, for example.

Participant enforcement always uses reject semantics. The replace policy only targets views owned by the current user, never views where they are just a participant.

### Interaction with Other Features

- **Navigation stack**: Sub-views count against the root view's session limit. Replacing exits the entire navigation chain. Participants propagate through push/pop but not replace.
- **Persistence**: Restored persistent views are session-indexed using saved identity, so limits work correctly after restart.
- **Undo/redo**: When replace policy exits an old view, its undo history is discarded.

---

## V1 Views (Classic)

### StatefulView

The V1 base class wraps discord.py's `View` with embed-based content:

```python
from cascadeui import StatefulView, StatefulButton

class MyView(StatefulView):
    session_limit = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(StatefulButton(
            label="Click Me", callback=self.on_click,
        ))
        self.add_exit_button()

    async def on_click(self, interaction):
        await interaction.response.send_message("Clicked!", ephemeral=True)

    async def update_from_state(self, state):
        pass

# Send with embed:
view = MyView(context=ctx)
await view.send(embed=discord.Embed(title="My View"))
```

### V1 View Patterns

#### TabView

```python
from cascadeui import TabView

class SettingsView(TabView):
    def __init__(self, *args, **kwargs):
        tabs = {
            "General": self.general_tab,
            "Audio": self.audio_tab,
        }
        super().__init__(*args, tabs=tabs, **kwargs)

    async def general_tab(self, embed):
        embed.description = "General settings here"
        return embed
```

#### WizardView

```python
from cascadeui import WizardView

class SetupWizard(WizardView):
    def __init__(self, *args, **kwargs):
        steps = [
            {"name": "Welcome", "builder": self.welcome_step},
            {"name": "Config", "builder": self.config_step,
             "validator": self.validate_config},
        ]
        super().__init__(*args, steps=steps, on_finish=self.finish, **kwargs)

    async def welcome_step(self, embed):
        embed.description = "Welcome!"
        return embed

    async def validate_config(self):
        return (self.config_value is not None), "Please configure a value."
```

#### FormView

```python
from cascadeui import FormView, choices

fields = [
    {
        "id": "role", "type": "select", "label": "Role",
        "required": True,
        "options": [
            {"label": "Developer", "value": "dev"},
            {"label": "Designer", "value": "design"},
        ],
        "validators": [choices(["dev", "design"])],
    },
    {"id": "notify", "type": "boolean", "label": "Notifications"},
]

view = FormView(context=ctx, fields=fields, on_submit=on_submit)
```

!!! note "String fields require Modals"
    Discord does not allow text input inside Views, only inside Modals. `FormView` and `FormLayoutView` support `select` and `boolean` field types inline. For text input, use `Modal` and `TextInput`. See [Components](components.md#modals).

#### PaginatedView

```python
from cascadeui import PaginatedView

pages = [
    discord.Embed(title="Page 1", description="First page"),
    discord.Embed(title="Page 2", description="Second page"),
]

view = PaginatedView(context=ctx, pages=pages)
await view.send()
```

Supports jump buttons (at 5+ pages), go-to-page modal, `from_data()` factory, `refresh_data()`, and `_build_extra_items()` hook. See the `ticket_system.py` example for a full implementation.

### PersistentView

V1 views that survive bot restarts. See [Persistence](persistence.md).
