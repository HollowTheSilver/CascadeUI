# DevTools

CascadeUI includes a built-in state inspector for debugging. It renders paginated embeds showing active views, sessions, action history, and store configuration.

## DevToolsCog

The quickest way to add the inspector is as a cog:

```python
from cascadeui import DevToolsCog

async def setup_hook(self):
    await self.add_cog(DevToolsCog(self))
```

This adds an `/inspect` command (owner-only) that opens the inspector.

### Inspector Pages

The inspector has five pages, navigated with Previous/Next/Refresh buttons:

1. **State Overview** - key counts, total state size
2. **Active Views** - list of registered views with their IDs and types
3. **Sessions** - active user sessions
4. **Action History** - last 15 dispatched actions with types and timestamps
5. **Store Config** - registered reducers, active subscribers, middleware, persistence status

## StateInspector (Direct Use)

For custom integrations, use `StateInspector` directly:

```python
from cascadeui import StateInspector

inspector = StateInspector()
pages = inspector.build_pages()  # Returns a list of discord.Embed objects
```

You can then display these embeds however you want (custom views, logging, API responses, etc).

## InspectorView

`InspectorView` is a plain `discord.ui.View` (not a `StatefulView`) to avoid polluting the state store with its own state. It provides Previous/Next/Refresh buttons for browsing the inspector pages interactively.
