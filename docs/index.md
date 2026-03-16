# CascadeUI

Redux-inspired UI framework for [discord.py](https://github.com/Rapptz/discord.py).

CascadeUI brings a Redux-inspired architecture to Discord bot interfaces. Views, buttons, selects, and forms are backed by a centralized state store with dispatched actions, reducers, and subscriber notifications. The result is predictable state flow and composable UI patterns that scale beyond simple one-off views.

## Features

- **Centralized State Store** with dispatch/reducer cycle, action history, and filtered subscriptions
- **Stateful Views** with lifecycle management, view transitions, and pre-built patterns (tabs, wizards, forms, pagination)
- **Stateful Components** with automatic action dispatching and behavioral wrappers (loading, confirmation, cooldowns)
- **Persistence** that lets state survive bot restarts, with two patterns for different use cases
- **Theming** with global defaults and per-view overrides
- **Middleware** for intercepting and transforming actions in the dispatch pipeline
- **DevTools** with a built-in state inspector for debugging

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
  Middleware pipeline (logging, persistence, custom)
       |
  Reducer transforms state immutably
       |
  Subscribers notified (filtered by action type + selector)
       |
  Views update their UI from new state
```

## Requirements

- Python 3.10+
- discord.py 2.1+

## Quick Links

- [Installation](guide/installation.md)
- [Quick Start](guide/quickstart.md)
- [GitHub Repository](https://github.com/HollowTheSilver/CascadeUI)
