# Views

Views are the primary UI containers in CascadeUI. They wrap discord.py's `View` with state integration, lifecycle management, and task tracking.

## StatefulView

The base class for all CascadeUI views:

```python
from cascadeui import StatefulView, StatefulButton

class MyView(StatefulView):
    def __init__(self, context):
        super().__init__(context=context)
        self.add_item(StatefulButton(
            label="Click Me",
            callback=self.on_click,
        ))

    async def on_click(self, interaction):
        await interaction.response.send_message("Clicked!", ephemeral=True)
```

### Sending a View

Use `view.send()` instead of manually sending messages. It handles state registration and message tracking:

```python
view = MyView(context=ctx)
await view.send(
    content="Hello!",
    embed=discord.Embed(title="My View"),
)
```

!!! warning "Ephemeral views"
    `view.send(ephemeral=True)` sends an ephemeral message. Ephemeral messages are tied to the 15-minute interaction token. Sending an ephemeral view with `timeout=None` is almost always a bug because the view will lose editability after the token expires. CascadeUI logs a warning if you do this.

### Lifecycle

1. **Init** - view is created, components added, subscribed to store
2. **Send** - message is sent, state registered (`VIEW_CREATED`)
3. **Interact** - user clicks buttons/selects, callbacks fire
4. **Exit/Timeout** - components disabled, state cleaned up (`VIEW_DESTROYED`)

### Timeout Handling

Views timeout after 180 seconds by default. On timeout, CascadeUI:

- Disables all components
- Edits the message to show the disabled state
- Unsubscribes from the state store
- Cancels any running background tasks

Override the timeout:

```python
super().__init__(context=context, timeout=300)  # 5 minutes
super().__init__(context=context, timeout=None)  # Never timeout
```

### Reacting to State Changes

Override `update_from_state()` to react when state changes:

```python
class MyView(StatefulView):
    subscribed_actions = {"MY_ACTION"}  # Only listen for specific actions

    async def update_from_state(self, state):
        # Called when a matching action is dispatched
        data = state.get("my_data")
        if self.message and data:
            await self.message.edit(embed=self.build_embed(data))
```

Set `subscribed_actions = None` to receive all actions (not recommended for performance).

### View Transitions

Navigate between views with `replace()`:

```python
async def go_to_settings(self, interaction):
    await interaction.response.defer()
    new_view = await self.replace(SettingsView)
    await interaction.edit_original_response(view=new_view)
```

`replace` is a one-way transition: it stops the current view and starts the new one. There is no implicit "back" capability. For multi-level navigation, use the navigation stack instead.

### Auto-Defer Safety Net

CascadeUI automatically defers interactions when callbacks are slow, preventing the "This interaction failed" error that Discord shows when a response takes longer than 3 seconds.

This is enabled by default on all `StatefulView` subclasses. If a callback hasn't responded within 2.5 seconds, the framework automatically calls `interaction.response.defer()` in the background. If the callback responds before the timer fires, the timer is cancelled harmlessly.

```python
class MyView(StatefulView):
    auto_defer = True        # Default — enabled for all views
    auto_defer_delay = 2.5   # Seconds before auto-defer fires
```

#### How it works

1. When a component callback starts, a background timer is spawned
2. The timer waits `auto_defer_delay` seconds, then checks `interaction.response.is_done()`
3. If the callback hasn't responded yet, the timer calls `interaction.response.defer()`
4. If the callback responds before the timer, the timer is cancelled

#### Compatibility

Auto-defer is safe with all existing patterns:

- **Manual `defer()` calls**: The callback sets `is_done()` to `True`, so the timer skips
- **`with_loading_state`**: Consumes the response immediately, timer skips
- **`with_confirmation`**: Sends a confirmation prompt immediately, timer skips
- **`with_cooldown`**: Sends a cooldown message or passes through, timer skips

!!! warning "Modal callbacks"
    `interaction.response.send_modal()` must be the first response to an interaction. If auto-defer fires before you call `send_modal()`, the interaction is consumed and the modal cannot be sent. If your callback opens a modal, ensure it does so quickly (within `auto_defer_delay`). You can also disable auto-defer for modal-heavy views with `auto_defer = False`.

#### Disabling auto-defer

Set `auto_defer = False` on a view class to opt out entirely:

```python
class ManualDeferView(StatefulView):
    auto_defer = False  # Handle all deferrals manually
```

### Error Handling

`StatefulView` includes a built-in `on_error` handler that shows a red ephemeral embed when an interaction callback raises an exception. This prevents silent failures and gives users visible feedback.

### Interaction Ownership

By default, only the user who created a view can interact with it. If another user clicks a button on someone else's view, they receive an ephemeral rejection message and the callback does not run.

```python
class MyView(StatefulView):
    owner_only = True                                    # Default
    owner_only_message = "You cannot interact with this."  # Default
```

This prevents a common class of bugs where another user accidentally modifies someone else's state by clicking their buttons.

To make a view accessible to everyone, set `owner_only = False`:

```python
class PollView(StatefulView):
    owner_only = False  # Anyone can vote
```

`PersistentView` defaults to `owner_only = False` since persistent views are typically shared panels like role selectors or dashboards.

#### Custom Access Control

Override `interaction_check()` for more advanced logic, like role-based access:

```python
class AdminView(StatefulView):
    async def interaction_check(self, interaction):
        # Keep the ownership check from StatefulView
        if not await super().interaction_check(interaction):
            return False

        # Add role-based check
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Admins only.", ephemeral=True
            )
            return False
        return True
```

!!! note "Views without a user context"
    When `user_id` is `None` (e.g. restored `PersistentView` instances or views created without a context/interaction), the ownership check is skipped and all users can interact. This is the correct behavior for restored views since the original user may not even be online.

### Exit Button

Add a standard exit button that cleans up the view:

```python
self.add_exit_button()  # Adds a gray "Exit" button

# For PersistentView, you must pass a custom_id:
self.add_exit_button(custom_id="my_view:exit")
```

## Navigation Stack

Push views onto a stack and pop them to go back. This is the standard pattern for multi-level UIs like settings menus with sub-pages:

```python
class MainMenuView(StatefulView):
    async def go_settings(self, interaction):
        await interaction.response.defer()
        new_view = await self.push(SettingsView, interaction)
        await interaction.edit_original_response(
            embed=new_view.build_embed(), view=new_view,
        )

class SettingsView(StatefulView):
    async def go_back(self, interaction):
        await interaction.response.defer()
        prev_view = await self.pop(interaction)
        if prev_view:
            await interaction.edit_original_response(view=prev_view)
```

### How it works

- `push(ViewClass, interaction)` stops the current view, stacks it, and returns a new instance of the target class.
- `pop(interaction)` stops the current view, pops the top of the stack, reconstructs that view, and returns it.
- The stack is stored in session state and cleaned up automatically when the session ends.
- The new view inherits the same `session_id`, so all navigation within a session shares state.

### Auto Back Button

Set `auto_back_button = True` on a view class to automatically add a back button when the view is pushed:

```python
class SettingsView(StatefulView):
    auto_back_button = True  # Back button added automatically when pushed
```

!!! warning "Pop on empty stack"
    If `pop()` is called when the stack is empty, it returns `None`. The auto back button handles this gracefully by removing the dead view from the message.

### Push vs. Replace

| | `push()` | `replace()` |
|---|----------|-------------|
| Stack | Adds entry, supports back | No stack, one-way |
| Session | Shared | Shared |
| Use case | Menu hierarchy | Replacing the current view entirely |

## State Scoping

Isolate state per user or per guild so concurrent users don't overwrite each other:

```python
class ScopedCounterView(StatefulView):
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

!!! note "Scoping doesn't affect flat state"
    Non-scoped state (`state["application"]`, `state["scores"]`, etc.) is completely unaffected. Scoping only applies to views that set `scope`.

## Undo/Redo

Enable undo/redo on any view by setting `enable_undo = True`. Each dispatched action creates a state snapshot that can be reverted:

```python
from cascadeui import UndoMiddleware, get_store

# Add the middleware once (e.g., in your cog's setup function)
store = get_store()
store.add_middleware(UndoMiddleware(store))

class EditableView(StatefulView):
    enable_undo = True
    undo_limit = 20  # Max snapshots to keep (default: 20)

    async def undo_action(self, interaction):
        await interaction.response.defer()
        await self.undo(interaction)
        # Re-read state and update UI after undo

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

!!! info "Batched actions"
    When actions are dispatched inside a `batch()`, the undo middleware takes one snapshot before the batch starts. Undoing reverts everything the batch did in one step.

## Session Limiting

Session limiting controls how many active instances of a view can exist within a given scope. Without it, users can invoke commands repeatedly, spawning duplicate views that pile up in chat. Session limiting adds declarative, per-view control with automatic cleanup.

### Basic Usage

Set three class attributes on any `StatefulView` subclass:

```python
class SettingsView(StatefulView):
    session_limit = 1              # Max active instances (None = unlimited)
    session_scope = "user_guild"   # Scope for counting instances
    session_policy = "replace"     # What to do when the limit is reached
```

With this configuration, if a user opens a second `SettingsView` in the same guild, the first one is automatically exited (components disabled, message cleaned up) before the new one is sent.

### Session Scope

The scope determines how instances are grouped when counting toward the limit:

| Scope | Groups by | Use case |
|-------|-----------|----------|
| `"user"` | User ID | Per-user views (settings, profiles) across all guilds |
| `"guild"` | Guild ID | Per-server views (config panels) shared by all users |
| `"user_guild"` (default) | User ID + Guild ID | Per-user within a specific guild |
| `"global"` | Nothing | One instance across the entire bot |

```python
class GlobalAnnouncement(StatefulView):
    session_limit = 1
    session_scope = "global"    # Only one instance of this view, anywhere

class UserProfile(StatefulView):
    session_limit = 1
    session_scope = "user"      # One per user, regardless of guild
```

### Session Policy

The policy determines what happens when a new view would exceed the limit:

| Policy | Behavior |
|--------|----------|
| `"replace"` (default) | Exits the oldest view(s) to make room for the new one |
| `"reject"` | Raises `SessionLimitError`, blocking the new view from being sent |

#### Replace policy

The default. Old views are exited silently, and the new view takes their place:

```python
class SettingsView(StatefulView):
    session_limit = 1
    session_scope = "user_guild"
    session_policy = "replace"    # Default -- exits old view automatically
```

#### Reject policy

Blocks the new view and raises `SessionLimitError`. Useful when you want the user to explicitly close the existing view first:

```python
from cascadeui import StatefulView, SessionLimitError

class ExpensiveView(StatefulView):
    session_limit = 1
    session_scope = "user_guild"
    session_policy = "reject"

# In your command:
try:
    view = ExpensiveView(interaction=interaction)
    await view.send(embed=my_embed)
except SessionLimitError:
    await interaction.response.send_message(
        "You already have this open. Close it first.",
        ephemeral=True,
    )
```

### Limits Greater Than 1

Session limits are not restricted to 1. Setting `session_limit = 3` allows up to three concurrent instances before enforcement kicks in:

```python
class NotepadView(StatefulView):
    session_limit = 3
    session_scope = "user"
    session_policy = "replace"   # Exits the oldest when a 4th is opened
```

When the limit is exceeded with the replace policy, only the oldest views are exited -- just enough to make room for the new one.

### PersistentView Protection

`PersistentView` instances are protected from being replaced by non-persistent views. If a regular `StatefulView` tries to replace a `PersistentView` that occupies a session slot, `SessionLimitError` is raised instead of replacing it. This prevents an accidental command invocation from destroying a long-lived panel.

Persistent views **can** replace other persistent views of the same type.

### Scope Resolution in DMs

Some scopes require identity fields that may not be available. For example, `session_scope = "user_guild"` needs both a user ID and a guild ID. In DMs there is no guild, so the scope key cannot be resolved. When this happens, the view is still tracked internally but session limits are not enforced -- the view sends normally without checking for existing instances.

| Scope | DM behavior |
|-------|-------------|
| `"user"` | Enforced (user ID is always available) |
| `"guild"` | Not enforced (no guild ID) |
| `"user_guild"` | Not enforced (no guild ID) |
| `"global"` | Enforced (no identity needed) |

### Interaction with Other Features

- **Navigation stack**: When a view is exited by session limiting, its entire navigation stack is cleaned up. Pushed views on the old stack are not preserved.
- **Ephemeral views**: Session limiting works with ephemeral views. The old ephemeral message's components are disabled, and the new view is sent as a fresh ephemeral message.
- **Persistence**: Restored persistent views (from `setup_persistence`) are tracked in the active view registry but are not session-indexed (they lack user/guild context). They will not block new views from being created.
- **Undo/redo**: When the replace policy exits an old view, its undo history is discarded along with the view. The new view starts fresh.

## View Patterns

CascadeUI includes pre-built view patterns for common layouts.

### TabView

Button-based tab switching where each tab renders its own content:

```python
from cascadeui import TabView

class SettingsView(TabView):
    def __init__(self, context):
        tabs = {
            "General": self.general_tab,
            "Audio": self.audio_tab,
            "Display": self.display_tab,
        }
        super().__init__(context=context, tabs=tabs)

    async def general_tab(self, embed):
        embed.description = "General settings here"
        return embed

    async def audio_tab(self, embed):
        embed.description = "Audio settings here"
        return embed

    async def display_tab(self, embed):
        embed.description = "Display settings here"
        return embed
```

The active tab's button is highlighted with the primary style.

### WizardView

Multi-step form with Back/Next/Finish navigation and per-step validation:

```python
from cascadeui import WizardView

class SetupWizard(WizardView):
    def __init__(self, context):
        steps = [
            {"name": "Welcome", "builder": self.welcome_step},
            {"name": "Config", "builder": self.config_step, "validator": self.validate_config},
            {"name": "Done", "builder": self.done_step},
        ]
        super().__init__(context=context, steps=steps, on_finish=self.finish)

    async def welcome_step(self, embed):
        embed.description = "Welcome to the setup wizard!"
        return embed

    async def config_step(self, embed):
        embed.description = "Configure your settings"
        return embed

    async def validate_config(self):
        # Return True to allow advancement, False to block
        return self.config_value is not None

    async def done_step(self, embed):
        embed.description = "Setup complete!"
        return embed

    async def finish(self, interaction):
        await interaction.response.send_message("All done!")
```

A step indicator shows progress (e.g., "Step 2/3").

### FormView

Form with select menus, boolean toggles, and per-field validation:

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

async def on_submit(interaction, values):
    await interaction.response.send_message(f"Saved: {values}", ephemeral=True)

view = FormView(context=ctx, fields=fields, on_submit=on_submit)
```

!!! warning "on_submit must acknowledge the interaction"
    The `on_submit` callback receives the raw interaction. You **must** call `interaction.response.send_message()`, `.defer()`, or similar. If your callback doesn't acknowledge the interaction within 3 seconds, the user will see "This interaction failed."

!!! note "String fields require Modals"
    Discord does not allow text input inside Views, only inside Modals. FormView supports `select` and `boolean` field types inline. For text input, use the `Modal` class from `cascadeui.components.inputs`.

See [Validation](validation.md) for details on built-in validators.

### PaginatedView

Paginated content with automatic navigation buttons. Supports plain embeds, strings, and mixed-content dicts:

```python
from cascadeui import PaginatedView

pages = [
    discord.Embed(title="Page 1", description="First page"),
    discord.Embed(title="Page 2", description="Second page"),
    discord.Embed(title="Page 3", description="Third page"),
]

view = PaginatedView(context=ctx, pages=pages)
await view.send()  # First page is displayed automatically
```

#### Page types

Pages can be any of the following:

| Type | Example | Description |
|------|---------|-------------|
| `discord.Embed` | `discord.Embed(title="Page 1")` | Embed-only page |
| `str` | `"Plain text page"` | Text-only page |
| `dict` | `{"embed": embed, "content": "text"}` | Mixed content page |

#### Jump buttons and go-to-page

When the page count exceeds `jump_threshold` (default 5), additional navigation appears automatically:

- **First/last buttons** (`⏮`/`⏭`) for jumping to the start or end
- **Go-to-page button** replaces the page indicator and opens a modal where users can type a page number directly

```
Below threshold:  [◀ Previous] [Page 1/3] [Next ▶]
Above threshold:  [⏮] [◀] [1/20] [▶] [⏭]
```

Override the threshold on a subclass:

```python
class DetailedView(PaginatedView):
    jump_threshold = 3  # Show jump buttons at 4+ pages
```

#### Dynamic pagination with `from_data`

Auto-paginate a list of items without manually building embeds:

```python
async def format_page(items):
    embed = discord.Embed(title="User List")
    embed.description = "\n".join(f"- {item}" for item in items)
    return embed

view = await PaginatedView.from_data(
    items=all_users,       # Full list of items
    per_page=10,           # Items per page
    formatter=format_page, # Sync or async callable
    context=ctx,
)
await view.send()
```

The formatter receives a list of items for one page and returns an `Embed`, `str`, or page dict. Both sync and async formatters are supported.

## PersistentView

For views that need to stay interactive across bot restarts, see [Persistence](persistence.md).
