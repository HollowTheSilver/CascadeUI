<p align="center">
  <img src="assets/docs-banner.png" alt="CascadeUI -- A Redux-Inspired Framework for Discord.py" width="100%">
</p>

A state management and UI framework for [discord.py](https://github.com/Rapptz/discord.py) bots. CascadeUI replaces ad-hoc view logic with a centralized state store, dispatched actions, and reducer-driven updates -- the same architecture that powers large-scale web frontends, adapted for Discord's component system.

## Why CascadeUI?

- **Predictable state flow** -- every UI change follows the same path: action → reducer → subscriber → refresh. No scattered mutation, no mystery state.
- **V2-first components** -- built for Discord's modern component system (LayoutView, Container, Section, TextDisplay) with full V1 support for embed-based views.
- **Pre-built patterns** -- forms, wizards, tabs, and pagination ship ready to use with customizable buttons, hooks, and per-step validation.
- **Persistence that survives restarts** -- built-in SQLite backend (via `aiosqlite`), an in-memory backend for tests, and a capability-flag Protocol for custom backends. Debounced writes, opt-in per-slot persistence, and views that re-attach to their Discord messages after a bot restart.

## Data Flow

```
User clicks button
       │
  Interaction dispatched
       │
  Callback runs → dispatch(action)
       │
  Middleware pipeline (logging, persistence, undo)
       │
  Reducer transforms state
       │
  Subscribers notified (filtered by action + selector)
       │
  on_state_changed() → refresh() → UI updated
```

## Requirements

- Python 3.10+
- discord.py 2.7+

## Get Started

- **[Installation](guide/installation.md)** -- pip install and verify
- **[Quick Start](guide/quickstart.md)** -- first working view in 5 minutes
- **[Core Concepts](guide/concepts.md)** -- mental models and architecture
- **[Five Pillars](guide/five-pillars.md)** -- the architectural model behind every class attribute
- **[Examples](examples.md)** -- working cogs from counters to Battleship

## Support

- [Discord Server](https://discord.com/invite/9Xj68BpKRb) -- help, discussion, and updates
- [GitHub Issues](https://github.com/HollowTheSilver/CascadeUI/issues) -- bug reports and feature requests
