# DevTools

CascadeUI includes built-in developer tools for inspecting and managing
state at runtime. Two entry points: a `/cascadeui` command group for
quick CLI-style operations, and a visual `InspectorView` for interactive
exploration.

---

## Recommended dev-bot setup

A minimal bot layout for local development. Enables debug logging, an
in-memory persistence backend so views survive reloads within a session,
and the `DevToolsCog` so `/cascadeui inspect` is available from the
first run:

```python
import discord
from discord.ext import commands

from cascadeui import (
    DevToolsCog,
    InMemoryBackend,
    PersistenceMiddleware,
    setup_logging,
    setup_middleware,
)


class DevBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=discord.Intents.all(),
        )

    async def setup_hook(self):
        setup_logging(level="DEBUG")
        await setup_middleware(
            PersistenceMiddleware(backend=InMemoryBackend(), bot=self),
        )
        await self.add_cog(DevToolsCog(self))


DevBot().run("YOUR_BOT_TOKEN")
```

Swap `InMemoryBackend` for `SQLiteBackend("cascadeui.db")` when you need
persistence across restarts.

### When to enable tracing

`setup_logging()` accepts a `trace=True` flag that installs a wrapper
around discord.py's `ViewStore` and logs every interaction-dispatch
attempt: which `custom_id` the gateway received, which view matched,
whether the handler ran, and why it was skipped if it didn't. This is
useful when a specific symptom appears:

- A button click produces no response and no error in the normal logs.
- A `PersistentView` fails to re-attach on restart and you want to see
  which `custom_id`s the dispatcher is looking for.
- A modal submission seems to be routed to the wrong view.

The tracer emits a line per dispatch attempt across every active view,
so the volume is high enough to bury a typical debug session. Leave
`trace=False` during general development and flip it on only when
chasing a routing question. Turn it back off once the symptom is
resolved.

---

## DevToolsCog

Add the cog in `setup_hook`:

```python
from cascadeui import DevToolsCog

class MyBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(DevToolsCog(self))
```

This registers the `/cascadeui` hybrid command group. All subcommands
are owner-only -- the `is_owner()` check on the group propagates to
every subcommand automatically.

---

## `/cascadeui` Commands

| Command | Description |
|---------|-------------|
| `/cascadeui inspect` | Open the visual state inspector (see below) |
| `/cascadeui views` | List active views with live/ghost status indicators |
| `/cascadeui exit <id>` | Exit a specific view by ID (partial match, 8+ chars) |
| `/cascadeui exitall` | Exit all live views and clean ghost state entries |
| `/cascadeui sessions` | List active sessions with view counts and nav depth |
| `/cascadeui clear <id>` | Clear a session -- exits all views and removes the entry |
| `/cascadeui flush` | Force an immediate persistence write to disk |
| `/cascadeui purge` | Remove stale component and modal interaction entries |
| `/cascadeui reset` | Reset the entire state store (requires `confirm:True`) |

`exit` supports partial ID matching -- pass the first 8+ characters of
a view ID. If the view is live, it calls `exit()` for a clean shutdown.
If only a ghost state entry remains (no live instance), the entry is
cleaned up directly.

`reset` is a destructive operation that exits all views and clears the
state dict. It requires `confirm:True` as a parameter to prevent
accidental use.

---

## InspectorView

The visual inspector is a `TabLayoutView` with six tabs. It uses
CascadeUI's own V2 component system -- `card()`, `key_value()`,
`action_section()`, `alert()`, `divider()` -- and stays within
Discord's 40-component limit by rendering lists as markdown inside
single `TextDisplay` components.

### Overview Tab

A high-level summary of the state store:

- Active view count, session count, tracked component count
- Application state top-level keys
- Total state size in KB, history buffer usage

**Interactive controls:**

| Button | Action |
|--------|--------|
| Purge Stale | Remove orphaned component/modal entries |
| Flush to Disk | Force an immediate persistence write |
| Clear History | Clear the action history buffer |

### Views Tab

Lists every view currently registered with the store:

- View type (class name), view ID (truncated)
- User ID, channel ID, message ID

Up to 8 views per page. A registry stats card shows active instance
count, session index entries, and subscriber count.

**Interactive controls:**

| Control | Action |
|---------|--------|
| Select menu | Choose a view by type and ID |
| Exit Selected | Exit the selected view (clean shutdown or ghost cleanup) |
| Exit All | Exit all registered views at once |

### Sessions Tab

Lists active user sessions:

- Session ID (e.g., `MyView:user_123`)
- View count, navigation stack depth, creation timestamp

Up to 6 sessions shown.

**Interactive controls:**

| Control | Action |
|---------|--------|
| Select menu | Choose a session by ID |
| Clear Selected | Exit all views in the session and remove the entry |

### History Tab

Shows the last 20 dispatched actions in reverse chronological order:

- Timestamp (HH:MM:SS), action type, source view ID (truncated)

Click **Refresh** to see the latest actions during active debugging.

### Config Tab

Shows the current store configuration across three cards:

- **Reducers** -- core (built-in) and custom (via `@cascade_reducer`)
- **Middleware & Hooks** -- middleware pipeline names, hook count,
  computed value count
- **Persistence** -- enabled/disabled with backend type and persistent
  view count. Card is green when active, red when disabled.

### Performance Tab

Opt-in profiling for dispatch, subscriber, and refresh timings.
Disabled by default -- click **Enable** to begin recording samples,
then interact with views in another channel.

**Interactive controls:**

| Button | Action |
|--------|--------|
| Enable / Disable | Toggle profiling on/off |
| Export Report | Download full raw samples as a markdown file attachment |
| Clear Samples | Drop every recorded sample |
| Refresh | Re-render the tab with the latest samples |

The tab itself shows aggregated percentiles and top-N subscribers to
stay within Discord's component and message limits. **Export Report**
produces a complete snapshot -- every dispatch, every subscriber
timing, and every refresh sample -- as a markdown file with a trailing
JSON appendix, delivered as an ephemeral attachment. Attach the file
to a bug report or review comment when a screenshot's summary data is
not enough.

See [Performance](performance.md) for the full breakdown of what each
metric means and how to use selectors to reduce subscriber fan-out.

---

## Self-Filtering

The inspector excludes its own view and session from all displayed data.
Opening the inspector does not add an extra entry to the Views or
Sessions tabs:

| Filter | Excludes |
|--------|----------|
| `_filtered_views()` | Inspector's view ID from `state["views"]` |
| `_filtered_sessions()` | Inspector's session ID from `state["sessions"]` |
| `_filtered_history()` | Actions where `source` matches the inspector's ID |
| `_filtered_active_views()` | Inspector from `store._active_views` |

---

## Live Auto-Refresh

The inspector subscribes to `VIEW_CREATED` and `VIEW_DESTROYED` actions.
When another view is created or destroyed, the inspector automatically
refreshes its active tab. The `state_selector` tracks filtered view and
session counts, so the refresh only fires when external state actually
changes.

---

## Direct Use

`InspectorView` is a regular `TabLayoutView` subclass. Create it
directly without the cog:

```python
from cascadeui.devtools import InspectorView

view = InspectorView(context=ctx)
await view.send()
```

---

## Debugging Tips

- **View not updating?** Check the History tab to confirm the action
  was dispatched, then Config to verify the reducer is registered.
- **Stale views accumulating?** The Views tab shows live vs ghost
  status. Use Exit All or `/cascadeui exitall` to clean up.
- **Session issues?** The Sessions tab shows view counts and nav stack
  depth -- compare against expectations.
- **Middleware not running?** The Config tab lists the middleware
  pipeline in registration order. Verify your middleware appears and is
  positioned correctly.
- **Inspector shows stale data?** Views and Sessions tabs auto-refresh
  on lifecycle events. History and Config have manual Refresh buttons.
