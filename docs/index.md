<p align="center">
  <img src="assets/docs-banner.png" alt="CascadeUI — A Redux-Inspired Framework for Discord.py" width="100%">
</p>

Redux-inspired UI framework for [discord.py](https://github.com/Rapptz/discord.py).

CascadeUI brings a Redux-inspired architecture to Discord bot interfaces. Views, buttons, selects, and forms are backed by a centralized state store with dispatched actions, reducers, and subscriber notifications. The result is predictable state flow and composable UI patterns that scale beyond simple one-off views.

Supports both **V2 Components** (LayoutView, Container, Section, TextDisplay) and **V1 Components** (View, Embed, Button rows). V2 is the recommended approach for new projects — it allows content and controls in the same visual unit with per-block accent colors.

## Features

- **Centralized State Store** with dispatch/reducer cycle, action history, filtered subscriptions, action batching, event hooks, and computed/derived values
- **V2 Layout Views** with container-based UI, accent colors, inline buttons, and convenience helpers (`card`, `key_value`, `action_section`, `toggle_section`, `alert`, `divider`, `gap`, `gallery`)
- **V1 Stateful Views** with embed-based UI, lifecycle management, and the same state integration
- **View Patterns** for both V1 and V2: tabs, wizards, forms, and pagination
- **Navigation Stack** with push/pop, `rebuild` callbacks, and version enforcement
- **Session Limiting** with declarative per-view limits, scoped enforcement (user, guild, user+guild, global), and automatic cleanup or rejection policies
- **Interaction Reliability** with automatic defer safety net and serialized interaction processing to prevent racing edits
- **Stateful Components** with automatic action dispatching and behavioral wrappers (loading, confirmation, cooldowns)
- **Form Validation** with built-in validators, custom sync/async validators, and per-field error reporting
- **Persistence** with pluggable backends (JSON, SQLite, Redis), migration tools, and views that survive bot restarts
- **Theming** with global defaults, per-view overrides, and V2 accent color support
- **Middleware** for intercepting and transforming actions in the dispatch pipeline
- **DevTools** with a built-in tabbed state inspector for debugging

## Data Flow

```
User clicks button
       |
  Interaction dispatched
       |
  Original callback runs (responds to Discord)
       |
  COMPONENT_INTERACTION action dispatched
       |
  Middleware pipeline (logging, persistence, undo, custom)
       |
  Reducer transforms state immutably
       |
  Subscribers notified (filtered by action type + selector)
       |
  Views update their UI from new state
       |
  Hooks fire (read-only, post-update)
```

## Requirements

- Python 3.10+
- discord.py 2.7+

## Quick Links

- [Installation](guide/installation.md)
- [Quick Start](guide/quickstart.md)
- [Examples](examples.md)
- [GitHub Repository](https://github.com/HollowTheSilver/CascadeUI)

## Support

- [Discord Server](https://discord.com/invite/9Xj68BpKRb) for help, discussion, and updates
- [GitHub Issues](https://github.com/HollowTheSilver/CascadeUI/issues) for bug reports and feature requests
