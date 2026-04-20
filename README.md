<p align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/docs/assets/banner.png" alt="CascadeUI - A Redux-Inspired Framework for Discord.py" width="100%">
</p>

<p align="center">
  <a href="https://github.com/HollowTheSilver/CascadeUI/stargazers"><img src="https://img.shields.io/github/stars/HollowTheSilver/CascadeUI?style=flat&logo=github&label=stars" alt="Stars"></a>
  <a href="https://github.com/sponsors/HollowTheSilver"><img src="https://img.shields.io/badge/Sponsor-%E2%9D%A4-ea4aaa?logo=githubsponsors&logoColor=white" alt="Sponsor"></a>
  <a href="https://pypi.org/project/pycascadeui/"><img src="https://img.shields.io/pypi/dm/pycascadeui?logo=pypi&logoColor=white&label=downloads" alt="Downloads"></a>
  <a href="https://pypi.org/project/pycascadeui/"><img src="https://img.shields.io/pypi/v/pycascadeui?logo=pypi&logoColor=white" alt="PyPI"></a>
  <a href="https://github.com/Rapptz/discord.py"><img src="https://img.shields.io/badge/discord.py-2.7+-738adb.svg?logo=discord&logoColor=white" alt="discord.py 2.7+"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12%20|%203.13%20|%203.14-blue.svg?logo=python&logoColor=white" alt="Python 3.10-3.14"></a>
  <a href="https://discord.com/invite/9Xj68BpKRb"><img src="https://img.shields.io/discord/1405822635920855040?logo=discord&logoColor=white&label=Discord&color=5865F2" alt="Discord"></a>
  <a href="https://hollowthesilver.github.io/CascadeUI/"><img src="https://img.shields.io/badge/docs-GitHub%20Pages-8A2BE2?logo=readthedocs" alt="Docs"></a>
  <a href="https://github.com/HollowTheSilver/CascadeUI/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/HollowTheSilver/CascadeUI/ci.yml?logo=github&label=CI" alt="CI"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

<p align="center">
  <strong>Build predictable, state-driven interfaces with <a href="https://github.com/Rapptz/discord.py">discord.py</a>.</strong><br>
  A flexible, Redux-inspired UI framework that introduces centralized state, composable components, ownership control, and predictable data flow to Discord applications.<br>
</p>

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-hero.gif" alt="CascadeUI Hero Demo" width="600">
</div>

<p align="center">
  <a href="https://hollowthesilver.github.io/CascadeUI/"><strong>Read the Docs</strong></a>
</p>

---

## Why CascadeUI

> Interactive Discord UIs become difficult to manage as they grow. State accumulates across `View` subclass attributes, components stop responding after bot restarts, multi-step forms lose data between pages, and sharing data between views requires manual `message.edit()` plumbing in every callback.

CascadeUI introduces structure built on a Redux-inspired core:

- **Centralized state** instead of scattered view attributes, so every view reads from a single source of truth and stays in sync automatically.
- **Predictable updates** through dispatched actions, with one way to change state and one way to read it. No callback spaghetti.
- **Clear separation** between logic and presentation. Reducers handle data, views render it, and neither knows about the other.
- **Reusable UI patterns** instead of one-off implementations. Menus, pagination, forms, wizards, tabs, and persistent panels are first-class library primitives.
- **Built-in interaction control** for ownership, instance limits, and navigation. Restrict who can click what, cap how many concurrent instances a user or guild can hold, and push, pop, or replace views without tracking message history by hand.
- **Persistence, undo/redo, and lifecycle handling** without the boilerplate. Components survive bot restarts, state history is one method call away, and session cleanup happens automatically.

The pattern scales from simple panels to full application-style interfaces.

---

## Architecture

CascadeUI follows a unidirectional data flow model:

```
User interaction -> dispatch(action)
  -> middleware
  -> reducer (state update)
  -> subscribers notified
  -> views re-render
```

All state lives in a single store. Actions describe what happened. Reducers define how state changes. Views subscribe to relevant state and update automatically.

<details>
<summary><b>Coming from Redux or React?</b></summary>

<br>

CascadeUI ports Redux's mental model onto Discord. Most core primitives have a closest analogue in frameworks you already know:

| CascadeUI | Closest Redux / React analogue |
|-----------|-------------------------------|
| `StateStore` | Redux store |
| `@cascade_reducer` | Redux reducer |
| `@computed` | Reselect / `useMemo` |
| `build_ui()` | React component `render()` |
| `on_state_changed` | `componentDidUpdate` + auto re-render |
| `push()` / `pop()` / `replace()` | React Router navigation |
| Middleware chain | `applyMiddleware` |
| `PersistenceMiddleware` | `redux-persist` (opt-in per slot) |

**Full treatment:** [`guide/concepts.md`](https://hollowthesilver.github.io/CascadeUI/guide/concepts/) walks through each mapping in depth, including where the two diverge - middleware is async, state persists across bot restarts (Discord messages outlive your code), and Discord's platform layer (ephemeral 15-minute wall, webhook tokens, rate limits) has no React/Redux equivalent.

</details>

---

## When to Use

> Every discord.py view needs ownership control, session cleanup, and interaction safety. CascadeUI handles all of that out of the box with class-level declarations - no boilerplate, no manual checks.

Even a single-view panel benefits from `owner_only = True` and `instance_limit = 1`. As your interface grows, the same framework scales to:

- Shared state across multiple views via `StateStore`
- Real data and message persistence via `PersistenceMiddleware`
- Cross-view reactivity with `dispatch()` and `subscribed_actions`
- Multi-step flows and validation via `WizardLayoutView` and `FormLayoutView`
- Navigation stacks (`push()` / `pop()` / `replace()`), session policies, and `participant_limit`
- Grid-based game boards with `emoji_grid()` and `button_grid()`

---

## Getting Started

```bash
pip install pycascadeui
```

Optional dependencies:

```bash
pip install pycascadeui[sqlite]
```

Requirements:
- Python 3.10+
- discord.py 2.7+

### Hello World

A minimal CascadeUI view: per-user counter with ownership, instance replacement, and state-driven rebuilds - in about 20 lines.

```python
import discord
from discord.ui import ActionRow
from cascadeui import StatefulButton, StatefulLayoutView, card

class CounterView(StatefulLayoutView):
    # Class-level policy -- ownership and instance control in three lines.
    owner_only = True              # Only the opener can click
    instance_limit = 1             # One live counter per user
    instance_policy = "replace"    # Second open replaces the first

    # Reactivity -- build_ui() re-runs whenever scoped state changes.
    subscribed_actions = {"SCOPED_UPDATE"}
    state_scope = "user"

    def build_ui(self):
        self.clear_items()
        count = self.scoped_state.get("count", 0)
        self.add_item(card(f"Count: **{count}**"))
        self.add_item(ActionRow(StatefulButton(
            label="+1",
            style=discord.ButtonStyle.primary,
            callback=self._increment,
        )))

    async def _increment(self, interaction):
        count = self.scoped_state.get("count", 0)
        await self.dispatch_scoped({"count": count + 1})

# In a cog command:
#   view = CounterView(context=ctx)
#   await view.send()
```

See the [Quickstart](https://hollowthesilver.github.io/CascadeUI/guide/quickstart/) for the detailed walkthrough and [examples/v2_hello_world.py](examples/v2_hello_world.py) for the full runnable cog.

---

## Features

> For full details, see the official <a href="https://hollowthesilver.github.io/CascadeUI/"><strong>documentation</strong></a>.

### State and Data Flow
- Centralized store with dispatch and reducer cycle
- Custom reducers via `@cascade_reducer` decorator with automatic deep copy and built-in collision guards
- Action batching for grouped, atomic updates; nested batches collapse into one commit and fire a single notification cycle
- Computed state via `@computed` decorator with selector-based cache invalidation and per-store instances that survive singleton resets (≈ Reselect / React's `useMemo`)
- Selector-based subscriptions for targeted re-renders (similar to React's selective re-render optimization)
- Built-in profiler with exportable markdown + JSON reports for dispatch, subscriber, and refresh timings -- measure before you optimize, attach snapshots to PRs and bug reports
- `access_slot()` / `read_slot()` / `slot_property` helpers for auto-vivifying application buckets without hand-rolling the read/write plumbing
- Scoped state family: `get_scoped()`, `get_scoped_from()`, `iter_scoped()`, `set_scoped()`, `merge_scoped()` -- one call from inside a reducer, no private key-building required
- Cross-view reactivity: dispatch from any view, all subscribers update instantly with automatic coalescing under concurrent access
- Middleware pipeline for logging, persistence, and transformation (Redux-style, async)
- Event hooks for lifecycle observation and side effects

### Views and Patterns
- Layout-based V2 system for structured, container-driven interfaces
- Full support for traditional discord.py Views (V1)
- Pre-built patterns: menus, tabs, wizards, forms, pagination, leaderboards, persistent leaderboards
- `PaginatedView.from_cursor()` for lazy cursor-driven pagination with an LRU page cache
- `DisplayLayoutView` for one-shot V2 sends from a pre-built container, no subclass required
- Automatic state-driven rebuilds: define `build_ui()` and the library wires it into `on_state_changed()` and `refresh()` for you (declarative render, like React components)
- One hook for V1 and V2: `build_ui()` returns `None` (V2 mutates the tree) or a dict of edit kwargs like `{"embed": ...}` (V1), and the library splats it into `message.edit()`
- Theming with per-view overrides, V2 accent colors, and a `ContextVar` that propagates the active theme through `build_ui()` so builders like `card()` and `stats_card()` inherit automatically (like `React.Context`)

### Interaction Control and Sessions
- Interaction ownership control, owner-only by default and configurable via `allowed_users`
- Instance limiting per user, guild, user+guild, or globally with replace or reject policies
- Participant-aware views for multi-user scenarios like challenge flows, lobbies, and games
- `participant_limit` with `on_participant_limit` hook and `auto_register_participants` for automatic slot claiming during `send()`
- Navigation stack with `push()`, `pop()`, and `replace()`, sharing one Discord message across the chain (akin to React Router's history API)
- `check_instance_available()` for fail-fast pre-checks before constructing expensive views
- Five-pillar architecture: Access Control, Instance Constraints, View Lifecycle, Session Membership, and Navigation -- each attribute belongs to exactly one pillar
- `session_continuity` opt-in for repeat-open state coalescing; the default isolates every send as its own session
- Parent and child view lifecycle via `attach_child()` (or `parent=` kwarg) with automatic cleanup
- Automatic interaction acknowledgement via auto-defer (tunable per view), with `respond()` / `open_modal()` / `_safe_defer()` helpers that transparently route through response or followup
- Interaction serialization via an `asyncio.Lock` so rapid clicks process sequentially without racing `message.edit()` calls
- Refresh throttling: opt-in `refresh_cooldown_ms` proactively batches edits, and reactive 429 backoff honors Discord's `retry_after` automatically. Both share a single monotonic cooldown and coalesce on the latest store state at fire time
- `auto_refresh_ephemeral` flag: bypass Discord's 15-minute ephemeral editability wall with a user-driven token handoff; armed views freeze state-driven rebuilds so the refresh button cannot be clobbered between T+810s and T+900s
- Automatic message re-fetch after `send()` so long-lived views are not bound to the interaction webhook's 15-minute token window, with a `_webhook_message` dual-reference so embed edits still route through the webhook when the channel endpoint would drop them silently

### Components and Composition
- Stateful buttons, selects, and modals with state integration
- Select callbacks can opt into a `values` second parameter, no more `interaction.data["values"][0]`
- V2 layout builders: `card()`, `stats_card()`, `action_section()`, `toggle_section()`, `image_section()`, `link_section()`, `confirm_section()`, `button_row()`, `cycle_button()`, `toggle_button()`, `tab_nav()`, `key_value()`, `alert()`, `progress_bar()`, `divider()`, `gap()`, `gallery()`
- Grid helpers: `emoji_grid()` for text-rendered boards with axis labels and mutation API, `button_grid()` for interactive cell grids with Discord's 5x5 limit enforced
- Built-in form system with typed modal fields (`text`, `integer`, `float`, `date`), inline selects, per-field validation, and declarative `FormSchema` / `WizardSchema` base classes
- Component wrappers: loading states, confirmation dialogs, cooldowns

### Persistence and Infrastructure
- Persistent views that survive bot restarts with automatic message re-attachment
- Two-namespace persistence model (`registry` and `application`) with per-namespace windows, max-age ceiling, and retry backoff on backend failure
- State persistence backends: built-in SQLite (via `aiosqlite`) and an in-memory backend for tests; custom backends plug in through a capability-flag `Protocol` with documented copy-on-store and NULL-safe TTL contracts
- Opt-in application slots via `persistent_slots = ("scoped",)`. Only the slots a view declares ride to disk, the rest stay volatile (≈ `redux-persist`, opt-in per slot)
- Named scoped buckets via `scoped_slot` so each subsystem (e.g. `"battleship_stats"`, `"tictactoe_stats"`) persists into its own flat bucket instead of one monolithic `scoped` tree
- Debounced `PersistenceMiddleware` installed via `setup_middleware`, with smart filtering so bookkeeping actions do not hit disk and an identity-diff scan that skips no-op writes
- Undo and redo via snapshot-based state history (opt in with `enable_undo`); batched dispatches collapse to one undo entry per participating view
- Scoped state isolation (`user`, `guild`, `user_guild`, `global`) with automatic key derivation and a reducer-side `merge_scoped()` writer
- `DevToolsCog` with a tabbed state inspector and owner-only `/cascadeui` command group for live debugging
- Silent snowflake coercion at every public boundary (`Member` where `int` is expected just works)
- Class-attribute validation at subclass-definition time. Typos in `instance_policy`, `participant_limit`, and friends fail at import with a clear error

---

## Showcase

### Cross-View Reactivity

> Dispatch actions from any view and update all subscribers instantly across the interface.

```python
# Any view can dispatch a named action.
await self.dispatch("SETTINGS_UPDATED", {"theme": "dark"})

# Any other open view that subscribes wakes up automatically --
# no manual message.edit(), no cross-view wiring.
class NotificationPanel(StatefulLayoutView):
    subscribed_actions = {"SETTINGS_UPDATED"}
    # build_ui() re-runs whenever SETTINGS_UPDATED fires anywhere.
```

![Cross-View](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-cross-view-reactivity.gif)

---

### Dynamic Rendering

> Define `build_ui()` once. The library calls it on every relevant state change and ships the edit for you. No `on_state_changed()` override, no manual `refresh()`, no `message.edit()` plumbing.

```python
class SettingsHub(StatefulLayoutView):
    state_scope = "user"
    subscribed_actions = {"SETTINGS_UPDATED"}

    def build_ui(self):
        self.clear_items()
        settings = self.user_scoped_state()
        self.add_item(card(
            "## Settings",
            key_value({
                "Theme": settings.get("theme", "default").title(),
                "Notifications": "On" if settings.get("notify") else "Off",
            }),
        ))

# build_ui() is called automatically on state changes --
# no manual refresh() or on_state_changed() override needed.
```

![Dynamic Rendering](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-dynamic-rendering.gif)

---

### Navigation and Flow

> Push, pop, and replace views on a shared navigation stack. `MenuLayoutView` handles the wiring for category-based hubs -- declare your categories and target views, the pattern generates the push callbacks and `action_section()` items automatically.

```python
from cascadeui import MenuLayoutView

class SettingsMenu(MenuLayoutView):
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "replace"

    def __init__(self, *args, **kwargs):
        categories = [
            {"label": "Appearance", "emoji": "\N{ARTIST PALETTE}",
             "description": "Theme and accent colors", "view": AppearanceView},
            {"label": "Notifications", "emoji": "\N{BELL}",
             "description": "Alert preferences", "view": NotificationsView},
            {"label": "Locale", "emoji": "\N{GLOBE WITH MERIDIANS}",
             "description": "Language and timezone", "view": LocaleView},
        ]
        super().__init__(*args, categories=categories, **kwargs)
```

![Navigation](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-settings.gif)

---

### Ownership Control

> Views are owner-only by default - only the user who opened it can interact. For multi-user scenarios, `allowed_users` and `participant_limit` extend that control.

```python
class BattleshipView(StatefulLayoutView):
    unauthorized_message = "You're not part of this game."
    instance_limit = 1
    instance_policy = "reject"
    participant_limit = 2
    auto_register_participants = True

    def __init__(self, *args, opponent_id: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_users = {self.user_id, opponent_id}
```

![Ownership Control](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-ownership-control.gif)

---

### Lifecycle Control

> Control active sessions per user, guild, or globally with automatic cleanup, replacement policies, and view-capacity caps.

```python
class DashboardView(TabLayoutView):
    instance_limit = 1               # Only one open at a time
    instance_scope = "user_guild"    # Per user per guild
    instance_policy = "replace"      # Exit the old one, open the new one


class GameView(StatefulLayoutView):
    participant_limit = 8           # Owner + 7 joiners maximum
    auto_register_participants = True  # Claim slots from allowed_users on send()
```

![V2 Instance Limiting](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-session-limiting.gif)

---

### Persistence and Continuity

> Persist views and state across restarts with automatic restoration.

```python
from cascadeui import PersistenceMiddleware, SQLiteBackend, setup_middleware

# Install PersistenceMiddleware once in your bot's setup_hook:
async def setup_hook(self):
    await setup_middleware(
        PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=self),
    )
```

Declare `persistent_slots` on any view that should carry application state to disk. The rest stays volatile:

```python
class BattleshipView(StatefulLayoutView):
    scoped_slot = "battleship_stats"       # per-subsystem bucket
    persistent_slots = ("battleship_stats",)  # opt this slot into persistence
```

![Persistence](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-persistence-restart.gif)

---

### State History (Undo/Redo)

> Snapshot-based state history per session with built-in undo and redo support.

```python
class SettingsHub(StatefulLayoutView):
    enable_undo = True   # Every dispatch captures a snapshot

    async def _undo(self, interaction):
        await self.undo()   # Restore previous snapshot

    async def _redo(self, interaction):
        await self.redo()   # Reapply the reverted snapshot
```

![Undo/Redo](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-undo-redo.gif)

---

### Ephemeral Refresh

> Discord ephemeral messages become uneditable after 15 minutes. CascadeUI handles the token handoff automatically.

```python
class FleetView(StatefulLayoutView):
    timeout = 3600                       # Handoff auto-engages for timeout > 900s
    refresh_button_label = "Refresh"     # Default: "Continue Session"
```

![Ephemeral Refresh](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-refresh.gif)

---

### Developer Tools

> Inspect live state, session activity, and performance timings without leaving your Discord client.

```python
from cascadeui import DevToolsCog

# In your bot's setup_hook:
await bot.add_cog(DevToolsCog(bot))
```

![DevTools](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-devtools.gif)

---

## View Patterns

### Category Menu

> Category-based navigation hubs with automatic drill-down, themed cards, and declarative per-category styling.

```python
from cascadeui import MenuLayoutView, card, key_value

class ConfigHub(MenuLayoutView):
    menu_style = discord.ButtonStyle.primary
    auto_exit_button = True

    def __init__(self, *args, **kwargs):
        categories = [
            {"label": "General", "emoji": "\u2699\ufe0f",
             "description": "Core settings", "view": GeneralView},
            {"label": "Moderation", "emoji": "\U0001f6e1\ufe0f",
             "description": "AutoMod and logging", "view": ModerationView},
        ]
        super().__init__(*args, categories=categories, **kwargs)

    def _build_header(self):
        return [card("## Server Config", key_value(self._summary()))]
```

---

### Tabbed Dashboard

> Structured, multi-section interfaces with tab-based navigation and composable layouts.

![Dashboard](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-dashboard.gif)

---

### Dynamic Pagination

> Generate paginated interfaces from raw data with built-in navigation and formatting helpers.

```python
import discord
from cascadeui import PaginatedLayoutView, card, divider

def format_page(items):
    lines = [f"**{item['name']}** | {item['rarity']} | {item['value']}g" for item in items]
    return [card(
        "## Inventory",
        divider(),
        "\n".join(lines),
        color=discord.Color.blue(),
    )]

view = await PaginatedLayoutView.from_data(
    items=all_items,
    per_page=4,
    formatter=format_page,
    context=ctx,
)
await view.send()
```

![Pagination](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-pagination.gif)

---

### Leaderboards

> Paginated ranked displays with cross-page numbering, optional summary stats, and a persistent variant for admin-posted panels that refresh on live data without a bot restart.

```python
from cascadeui import LeaderboardLayoutView, PersistentLeaderboardLayoutView, get_store

class BattleshipLeaderboard(LeaderboardLayoutView):
    leaderboard_top_n = 25
    leaderboard_per_page = 10
    title = "Battleship Rankings"

    def format_stats(self, user_id, stats):
        wins = stats.get("wins", 0)
        games = stats.get("games", 0)
        return f"**{wins}W** / {games - wins}L"

    def build_summary(self, entries):
        # Each game contributes to two player rows, so halve for unique games.
        unique_games = sum(s.get("games", 0) for _, s in entries) // 2
        return {"Players": str(len(entries)), "Games Played": str(unique_games)}


# One-shot usage: fetch live entries and pass them in.
entries = get_store().computed["battleship_leaderboards"].get(guild_id, [])
view = BattleshipLeaderboard(context=ctx, entries=entries)
await view.send()


# Persistent variant -- admin-posted panel that survives bot restarts
# and re-fetches live data on every restore.
class PersistentBoard(PersistentLeaderboardLayoutView):
    persistence_key = "battleship-leaderboard-main"
```

![Leaderboards](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-leaderboard.gif)

---

### Forms and Validation

> Define structured input flows with declarative fields, native text inputs, and per-field validation.

```python
from cascadeui import FormLayoutView, min_length, regex

class RegistrationForm(FormLayoutView):
    instance_limit = 1
    instance_policy = "reject"
    exit_policy = "delete"

    def __init__(self, *args, **kwargs):
        fields = [
            {
                "id": "username", "label": "Username", "type": "text",
                "required": True, "min_length": 3, "max_length": 20,
                "validators": [
                    min_length(3),
                    regex(r"^[a-zA-Z0-9_]+$", "Alphanumeric and underscores only"),
                ],
            },
            {
                "id": "role", "label": "Role", "type": "select",
                "required": True,
                "options": [
                    {"label": "Developer", "value": "dev"},
                    {"label": "Designer", "value": "design"},
                ],
            },
        ]
        super().__init__(*args, title="Registration", fields=fields, **kwargs)

    async def on_submit(self, interaction, values):
        await self.respond(
            interaction, f"Welcome, {values['username']}!", ephemeral=True,
        )
        await self.exit()
```

![Forms](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-form.gif)

---

### Multi-Step Wizard

> Multi-step flows with back/next/finish navigation, per-step builders and validators, and fully customizable button styling.

```python
from cascadeui import WizardLayoutView

class CharacterCreator(WizardLayoutView):
    instance_limit = 1
    instance_policy = "replace"
    exit_policy = "delete"

    back_button_label = "Previous"
    next_button_label = "Continue"
    finish_button_label = "Create Character"
    finish_button_style = discord.ButtonStyle.success

    def __init__(self, *args, **kwargs):
        steps = [
            {"name": "Identity", "builder": self.build_identity},
            {"name": "Class",    "builder": self.build_class},
            {"name": "Stats",    "builder": self.build_stats},
            {"name": "Review",   "builder": self.build_review},
        ]
        super().__init__(*args, steps=steps, **kwargs)
```

![Wizard](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-wizard.gif)

---

## Component Patterns

### Emoji Grid

> Text-rendered grids with optional axis labels and a mutation API. Plugs directly into `card()` and `Container`.

```python
from cascadeui import emoji_grid, card

grid = emoji_grid(4, 4, fill="\u2b1c", col_labels="numeric")
grid.fill_rect((1, 0), (1, 3), "\U0001f7e6")
grid[(2, 2)] = "\u2764\ufe0f"

view.add_item(card(grid))
```

![Emoji Grid](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/pngs/v2-emoji-grid.PNG)

---

### Button Grid

> Interactive cell grids packed into `ActionRow` components. Discord's 5x5 limit is enforced automatically.

```python
from cascadeui import button_grid, StatefulButton

rows = button_grid(3, 3, lambda r, c: StatefulButton(
    label=f"{chr(65 + r)}{c + 1}",
    style=discord.ButtonStyle.secondary,
    callback=on_cell_click,
))
for row in rows:
    view.add_item(row)
```

![Button Grid](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/pngs/v2-button-grid.PNG)

---

## V1 Components

> CascadeUI supports traditional discord.py Views and embeds.

Use V1 when you need:
- Embed-specific features such as fields or timestamps
- Simpler layouts without containers

All core features such as navigation, persistence, and undo/redo are supported.

```python
from cascadeui import PersistentView, SuccessButton
import discord

class TicketPanel(PersistentView):
    persistence_key = "support-ticket-panel"
    owner_only = False   # Public panel -- anyone can open a ticket

    def build_embed(self):
        return discord.Embed(
            title="Support Tickets",
            description="Click below to open a private support thread.",
            color=discord.Color.blurple(),
        )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_item(SuccessButton(
            label="Open Ticket",
            custom_id="ticket-panel:open",
            callback=self._open_ticket,
        ))

    async def _open_ticket(self, interaction):
        # ... create private thread, send confirmation ...
        await self.respond(interaction, "Ticket created!", ephemeral=True)
```

![Ticket System](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v1-ticket-system.gif)

---

## Examples

> The <a href="https://hollowthesilver.github.io/CascadeUI/examples/"><strong>documentation</strong></a> includes full implementations demonstrating practical usage:

- Dashboards and control panels
- Settings systems
- Pagination
- Forms and wizards
- Persistent views
- Multi-user games with shared state, hidden information, and challenge flows (TicTacToe, Battleship)
- Open-join lobbies with capacity caps and host-vs-participant authority (Werewolf-style)

---

## Documentation

- https://hollowthesilver.github.io/CascadeUI/

---

## Support

- Discord: https://discord.com/invite/9Xj68BpKRb
- Issues: https://github.com/HollowTheSilver/CascadeUI/issues

---

## Development

```bash
git clone https://github.com/HollowTheSilver/CascadeUI.git
cd CascadeUI
pip install -e ".[dev]"

pytest tests/ -v
black cascadeui/
isort cascadeui/
```

---

## Developer's Note

> I built CascadeUI with over **ten years** of Python experience behind it. All documentation, docstrings, and the entire testing module were written and designed using my custom **Anthropic Opus 4.6** sub-agents built on **Claude Code**. I don't try to hide that. I'm a proponent of efficient and responsible agent application in software design. That experience is what makes these tools effective. They're amplifiers, not substitutes.
>
> *-- Hollow*

---

<p align="center">
  MIT License
</p>
