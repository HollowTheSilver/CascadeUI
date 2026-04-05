<p align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/docs/assets/banner.png" alt="CascadeUI - A Redux-Inspired Framework for Discord.py" width="100%">
</p>

<p align="center">
  <a href="https://github.com/HollowTheSilver/CascadeUI/stargazers"><img src="https://img.shields.io/github/stars/HollowTheSilver/CascadeUI?style=flat&logo=github&label=stars" alt="Stars"></a>
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
  Design complex interactive systems with centralized state, composable UI patterns, and a clear data flow.<br>
</p>

<div align="center">
  <img src="https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-hero.gif" alt="CascadeUI TicTacToe" width="600">
</div>

<p align="center">
  <a href="https://hollowthesilver.github.io/CascadeUI/"><strong>Read the Docs</strong></a>
</p>

---

## Why CascadeUI

> Interactive Discord UIs become difficult to manage as they grow. State is scattered across callbacks, views become tightly coupled, and behavior becomes harder to reason about.

CascadeUI introduces structure:

- Centralized state instead of scattered variables
- Predictable updates through dispatched actions
- Clear separation between logic and presentation
- Reusable UI patterns instead of one-off implementations
- Built-in solutions for persistence, navigation, and state history

This approach scales from simple panels to full application-style interfaces.

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

---

## When to Use

> CascadeUI is designed for building complex interfaces that go beyond simple interactions.

A powerful, fully featured UI library should be leveraged when your app requires:

- Shared state across multiple views
- Real data and message persistence
- Maintainable complex interaction logic
- Message lifecycle and ownership control
- Consistent UI composition
- Cross-view reactivity
- Multi-step flows and validation

It may be unnecessary for small or simple interfaces.

---

## Features

> For full details, see the official <a href="https://hollowthesilver.github.io/CascadeUI/"><strong>documentation</strong></a>.

### State and Data Flow
- Centralized store with dispatch and reducer cycle
- Custom reducers via `@cascade_reducer` decorator with automatic deep copy
- Action batching for grouped, atomic updates
- Computed state and derived values
- Selector-based subscriptions for targeted re-renders
- Cross-view reactivity: dispatch from any view, all subscribers update instantly
- Middleware pipeline for logging, persistence, and transformation
- Event hooks for lifecycle observation and side effects

### Views and Interaction Patterns
- Layout-based V2 system for structured interfaces
- Full support for traditional discord.py Views (V1)
- Pre-built patterns: tabs, wizards, forms, pagination
- Navigation stack with push, pop, and replace
- Session limiting per user, guild, or globally with replace or reject policies
- Multi-user access control via `allowed_users` with participant-aware session limiting
- Interaction ownership control (owner-only by default)
- Auto-defer for slow callbacks and interaction serialization for rapid input
- Theming with per-view overrides and V2 accent colors

### Components and Composition
- Stateful buttons, selects, and modals with state integration
- V2 layout helpers: `card()`, `key_value()`, `alert()`, `action_section()`, and more
- Built-in form system with validation and per-field error handling
- Component wrappers: loading states, confirmation dialogs, cooldowns

### Persistence and Infrastructure
- Persistent views that survive bot restarts with automatic message re-attachment
- State persistence backends: JSON, SQLite, Redis
- Undo and redo via snapshot-based state history
- Scoped state isolation (user, guild, global)
- Developer tools for live state inspection and debugging

---

## Showcase

### Dashboard Pattern

> Structured, multi-section interfaces with tab-based navigation and composable layouts.

![Dashboard](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-dashboard.gif)

---

### Navigation and Flow

> Navigate between views without sending new messages. Maintain context across layered interfaces.

![Navigation](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-settings.gif)

---

### State History (Undo/Redo)

> Snapshot-based state history per session with built-in undo and redo support.

![Undo/Redo](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-undo-redo.gif)

---

### Cross-View Reactivity

> Dispatch actions from any view and update all subscribers instantly across the interface.

![Cross-View](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-cross-view-reactivity.gif)

---

### Lifecycle Control

> Control active sessions per user, guild, or globally with automatic cleanup and replacement policies.

```python
class DashboardView(TabLayoutView):
    session_limit = 1               # Only one open at a time
    session_scope = "user_guild"    # Per user per guild
    session_policy = "replace"      # Exit the old one, open the new one
```

![V2 Session Limiting](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-session-limiting.gif)

---

### Persistence and Continuity

> Persist views and state across restarts with automatic restoration.

```python
# Enable persistence once in your bot's setup_hook:
async def setup_hook(self):
    await setup_persistence(bot=self, backend=SQLiteBackend("cascadeui.db"))
```

![Persistence](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-persistence-restart.gif)

---

### Dynamic Pagination

> Generate paginated interfaces from raw data with built-in navigation and formatting helpers.

```python
from cascadeui import PaginatedLayoutView

def format_page(items):
    lines = [f"**{item['name']}** | {item['rarity']} | {item['value']}g" for item in items]
    return [Container(
        TextDisplay("## Inventory"),
        Separator(),
        TextDisplay("\n".join(lines)),
        accent_colour=discord.Color.blue(),
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

### Forms and Validation

> Define structured input flows with automatic validation and per-field error handling.

```python
from cascadeui import FormLayoutView, choices, card, key_value, divider

class RegistrationForm(FormLayoutView):
    session_limit = 1

    def __init__(self, *args, **kwargs):
        fields = [
            {
                "id": "role",
                "label": "Role",
                "type": "select",
                "required": True,
                "options": [
                    {"label": "Developer", "value": "developer"},
                    {"label": "Designer", "value": "designer"},
                    {"label": "Manager", "value": "manager"},
                ],
                "validators": [choices(["developer", "designer", "manager"])],
            },
            {
                "id": "terms",
                "label": "Accept Terms of Service",
                "type": "boolean",
                "required": True,
            },
        ]

        super().__init__(
            *args,
            title="Registration",
            fields=fields,
            on_submit=self.handle_submit,
            **kwargs,
        )

    def _rebuild_display(self):
        """Override display with V2 helpers for a richer presentation."""
        v = self.values
        action_rows = [c for c in self.children if isinstance(c, ActionRow)]
        self.clear_items()

        self.add_item(card(
            "## Registration Form",
            key_value({"Role": v.get("role", "-").title() if v.get("role") else "-"}),
            divider(),
            TextDisplay(f"Terms: {'Accepted' if v.get('terms') else 'Pending'}"),
        ))

        for row in action_rows:
            self.add_item(row)
```

![Forms](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-form.gif)

---

### Developer Tools

> Live state inspection and debugging with a tabbed inspector view. Add one line to your bot.

```python
from cascadeui.devtools import DevToolsCog

# In your bot's setup_hook:
await bot.add_cog(DevToolsCog(bot))
```

![DevTools](https://raw.githubusercontent.com/HollowTheSilver/CascadeUI/main/assets/gifs/v2-devtools.gif)

---

## V1 Components

> CascadeUI supports traditional discord.py Views and embeds.

Use V1 when you need:
- Embed-specific features such as fields or timestamps
- Simpler layouts without containers

All core features such as navigation, persistence, and undo/redo are supported.

---

## Examples

> The <a href="https://hollowthesilver.github.io/CascadeUI/examples/"><strong>documentation</strong></a> includes full implementations demonstrating practical usage:

- Dashboards and control panels
- Settings systems
- Pagination
- Forms and wizards
- Persistent views
- Ticket systems
- Multi-user games

---

## Getting Started

```bash
pip install pycascadeui
```

Optional dependencies:

```bash
pip install pycascadeui[sqlite]
pip install pycascadeui[redis]
```

Requirements:
- Python 3.10+
- discord.py 2.7+

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

<p align="center">
  MIT License
</p>

