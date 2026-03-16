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
    settings_view = SettingsView(context=self._context)
    await self.transition_to(settings_view)
```

### Error Handling

`StatefulView` includes a built-in `on_error` handler that shows a red ephemeral embed when an interaction callback raises an exception. This prevents silent failures and gives users visible feedback.

### Exit Button

Add a standard exit button that cleans up the view:

```python
self.add_exit_button()  # Adds a gray "Exit" button
```

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

Form with select menus and boolean toggles:

```python
from cascadeui import FormView

class ProfileForm(FormView):
    def __init__(self, context):
        fields = [
            {"id": "role", "type": "select", "label": "Role",
             "options": ["Tank", "DPS", "Support"]},
            {"id": "notify", "type": "boolean", "label": "Notifications"},
        ]
        super().__init__(context=context, fields=fields, on_submit=self.handle_submit)

    async def handle_submit(self, interaction, data):
        await interaction.response.send_message(f"Saved: {data}")
```

!!! note
    String/text fields require a Modal workflow (Discord limitation). FormView only supports `select` and `boolean` field types inline.

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
