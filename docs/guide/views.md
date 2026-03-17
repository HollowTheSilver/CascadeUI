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

Navigate between views with `transition_to()`:

```python
async def go_to_settings(self, interaction):
    await interaction.response.defer()
    new_view = await self.transition_to(SettingsView)
    await interaction.edit_original_response(view=new_view)
```

`transition_to` is a one-way transition: it stops the current view and starts the new one. There is no implicit "back" capability. For multi-level navigation, use the navigation stack instead.

### Error Handling

`StatefulView` includes a built-in `on_error` handler that shows a red ephemeral embed when an interaction callback raises an exception. This prevents silent failures and gives users visible feedback.

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

### Push vs. transition_to

| | `push()` | `transition_to()` |
|---|----------|-------------------|
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

Paginated content with Previous/Next navigation:

```python
from cascadeui import PaginatedView

class HelpView(PaginatedView):
    def __init__(self, context, pages):
        super().__init__(context=context, pages=pages)

    # pages is a list of embeds, one per page
```

## PersistentView

For views that need to stay interactive across bot restarts, see [Persistence](persistence.md).
