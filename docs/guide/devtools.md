# DevTools

CascadeUI includes a built-in state inspector for debugging. It uses CascadeUI's own V2 component system — a tabbed dashboard showing active views, sessions, action history, and store configuration in real time.

## DevToolsCog

The quickest way to add the inspector is as a cog:

```python
from cascadeui import DevToolsCog

class MyBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(DevToolsCog(self))
```

This adds an `/inspect` command (owner-only) that opens the inspector as a V2 tabbed view. The inspector is session-limited to one per user per guild — running the command again replaces the existing inspector.

## Inspector Tabs

The inspector is a `TabLayoutView` with five tabs. Each tab is built from V2 helpers (`card`, `key_value`, `action_section`, `alert`, etc.) and stays within Discord's 40-component limit by rendering lists as markdown inside single `TextDisplay` components.

### Overview

A high-level summary of the state store:

- **Views** — active view count (excluding the inspector itself)
- **Sessions** — active session count (excluding the inspector's session)
- **Components** — tracked component count
- **Application state** — top-level keys, total state size in KB, history buffer usage

The Refresh button regenerates the tab from the current store state.

### Views

Lists every view currently registered with the store:

- View type (class name)
- View ID (truncated)
- User ID, Channel ID, Message ID

Up to 8 views are shown per page. Below the list, a registry stats card shows active instance count, session index entries, and subscriber count.

If no views are active (besides the inspector), an info alert is shown instead.

### Sessions

Lists active user sessions:

- Session ID (e.g., `MyView:user_123`)
- View count, navigation stack depth, creation timestamp
- Data keys stored in the session

Up to 6 sessions are shown. Empty state shows an info alert.

### History

Shows the last 20 dispatched actions in reverse chronological order:

- Timestamp (HH:MM:SS)
- Action type (e.g., `COUNTER_UPDATED`)
- Source view ID (truncated)

The Refresh button on this tab is useful during active debugging — click it to see the latest actions without switching tabs. Empty state shows an info alert.

### Config

Shows the current store setup across three cards:

- **Reducers** — core (built-in) and custom (registered via `@cascade_reducer`) action handlers
- **Middleware & Hooks** — middleware pipeline names, hook count, computed value count
- **Persistence** — enabled/disabled with backend type, persistent view count. The card is green when persistence is active, red when disabled.

## Self-Filtering

The inspector excludes its own view and session from all displayed data. This prevents "observer effect" noise — opening the inspector doesn't add an extra entry to the Views or Sessions tabs.

Four internal filters handle this:

| Filter | Excludes |
|--------|----------|
| `_filtered_views()` | Inspector's view ID from `state["views"]` |
| `_filtered_sessions()` | Inspector's session ID from `state["sessions"]` |
| `_filtered_history()` | Actions where `source` matches the inspector's view ID |
| `_filtered_active_views()` | Inspector from `store._active_views` |

## Live Auto-Refresh

The inspector subscribes to `VIEW_CREATED` and `VIEW_DESTROYED` actions. When another view is created or destroyed, the inspector automatically refreshes its active tab — no manual Refresh click needed.

The `state_selector` tracks filtered view and session counts, so the refresh only fires when external state actually changes.

## Direct Use

`InspectorView` is a regular `TabLayoutView` subclass. You can create it directly without the cog:

```python
from cascadeui.devtools import InspectorView

view = InspectorView(context=ctx)
await view.send()
```

`StateInspector` is an alias for `InspectorView` for backwards compatibility:

```python
from cascadeui import StateInspector

view = StateInspector(context=ctx)
await view.send()
```

## Debugging Tips

- **View not updating?** Check the History tab to see if the action was dispatched, then the Config tab to verify the reducer is registered.
- **Stale views?** Check the Views tab to see if old views are still registered. Views that don't clean up properly will accumulate here.
- **Session issues?** Check the Sessions tab to verify sessions are being created and that the navigation stack depth matches your expectations.
- **Middleware not running?** Check the Config tab to confirm your middleware appears in the list and is in the correct order.
- **Inspector shows stale data?** The Views and Sessions tabs auto-refresh on view lifecycle events. The History and Config tabs have manual Refresh buttons. Click Refresh or switch tabs to get the latest state.
