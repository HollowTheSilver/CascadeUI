# DevTools

CascadeUI includes a built-in state inspector for debugging. It renders paginated embeds showing active views, sessions, action history, and store configuration.

## DevToolsCog

The quickest way to add the inspector is as a cog:

```python
from cascadeui import DevToolsCog

class MyBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(DevToolsCog(self))
```

This adds an `/inspect` command (owner-only) that opens the inspector as a paginated embed with Previous, Next, and Refresh buttons.

## Inspector Pages

The inspector generates five pages, each focused on a different aspect of the state system:

### Page 1: State Overview

A high-level summary of what the store is tracking:

- **Active views** count
- **Active sessions** count
- **Tracked components** count
- **Application state keys** (first 10 listed)
- **State size** in KB

This page is the starting point for answering "is anything even registered?" during debugging.

### Page 2: Active Views

Lists every view currently registered with the store:

- View type (class name)
- View ID (truncated)
- Message ID, Channel ID, User ID

Up to 10 views are shown. If more exist, an indicator shows the total count. Use this to verify views are being created and cleaned up as expected.

### Page 3: Sessions

Lists active user sessions:

- User ID
- Number of views in the session
- Navigation history depth
- Creation timestamp

Useful for debugging navigation stack issues or verifying that sessions are shared correctly between pushed views.

### Page 4: Action History

Shows the last 15 dispatched actions in reverse chronological order:

- Timestamp (HH:MM:SS)
- Action type (e.g., `COUNTER_UPDATED`)
- Source (view ID that dispatched it)

This is the primary debugging tool for tracing state flow. If a view isn't updating, check whether the expected action appears in the history and whether the source matches.

### Page 5: Store Configuration

Shows the current store setup:

- **Core reducers** (built-in action handlers)
- **Custom reducers** (registered via `@cascade_reducer`)
- **Subscribers** (count + first 10 IDs)
- **Middleware** (ordered list with class/function names)
- **Persistence** (enabled/disabled, backend type)

Use this to verify that your reducers, middleware, and persistence backend are registered correctly.

## StateInspector (Direct Use)

For custom integrations, use `StateInspector` directly:

```python
from cascadeui import StateInspector

inspector = StateInspector()
pages = inspector.build_pages()  # Returns a list of discord.Embed objects
```

You can then display these embeds however you want: custom views, logging, API responses, or even writing them to a file for offline analysis.

### Custom Store Instance

By default, `StateInspector` uses the global singleton store. You can pass a specific store instance:

```python
inspector = StateInspector(store=my_store)
```

## InspectorView

`InspectorView` is a plain `discord.ui.View` (not a `StatefulView`) to avoid polluting the state store with its own state. It provides three buttons:

| Button | Action |
|--------|--------|
| Previous | Navigate to the previous page (disabled on first page) |
| Next | Navigate to the next page (disabled on last page) |
| Refresh | Regenerates all pages from the current store state |

The Refresh button is particularly useful during active debugging: click it to see the latest views, sessions, and action history without re-running the command.

```python
from cascadeui.devtools import InspectorView, StateInspector

inspector = StateInspector()
pages = inspector.build_pages()

# Send with the paginated view
view = InspectorView(pages=pages, timeout=120)
await ctx.send(embed=pages[0], view=view)
```

## Debugging Tips

- **View not updating?** Check Page 4 (Action History) to see if the action was dispatched, then Page 5 (Store Config) to verify the reducer is registered.
- **Stale views?** Check Page 2 (Active Views) to see if old views are still registered. Views that don't clean up properly will accumulate here.
- **Session issues?** Check Page 3 (Sessions) to verify sessions are being created and that the navigation history depth matches your expectations.
- **Middleware not running?** Check Page 5 (Store Config) to confirm your middleware appears in the list and is in the correct order.
