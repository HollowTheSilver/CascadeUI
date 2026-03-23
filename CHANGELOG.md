# Changelog

All notable changes to CascadeUI are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

---

## [1.0.0] - 2026-03-23

First public release. CascadeUI is a Redux-inspired UI framework for discord.py
that provides state management, component composition, theming, and persistence.

### Core Architecture

- **StateStore** singleton with pub/sub subscriptions, action filtering, and
  state selectors for fine-grained notification control
- **Unidirectional data flow**: action → middleware → reducer → state → UI
- **Custom reducers** via `@cascade_reducer` decorator
- **Action batching** with `batch()` context manager for atomic multi-dispatch
  operations with a single subscriber notification
- **Event hooks** via `on()`/`off()` for lifecycle observation
- **Computed state** with `@computed` decorator and `ComputedValue` for cached
  derived values that invalidate when dependencies change
- **Middleware pipeline** with chain composition; built-in `DebouncedPersistence`
  (batched writes with configurable interval) and `logging_middleware`
- 14 built-in action types: `VIEW_CREATED`, `VIEW_UPDATED`, `VIEW_DESTROYED`,
  `SESSION_CREATED`, `SESSION_UPDATED`, `NAVIGATION_PUSH`, `NAVIGATION_POP`,
  `NAVIGATION_REPLACE`, `SCOPED_UPDATE`, `COMPONENT_INTERACTION`,
  `MODAL_SUBMITTED`, `PERSISTENT_VIEW_REGISTERED`, `PERSISTENT_VIEW_UNREGISTERED`,
  `UNDO`, `REDO`

### Views

- **StatefulView** base class extending `discord.ui.View` with state integration,
  message lifecycle management, automatic cleanup on exit/timeout
- **Navigation stack** with `push()`, `pop()`, and `replace()` for multi-level
  view hierarchies; shared cleanup path via `_navigate_to()`
- **Per-user and per-guild state scoping** via `scope="user"` or `scope="guild"`
  with `scoped_state` property and `dispatch_scoped()` method
- **Undo/redo** via `UndoMiddleware` with configurable history depth per view session
- **Session limiting** with declarative `session_limit`, `session_scope`
  (`"user"`, `"guild"`, `"user_guild"`, `"global"`), and `session_policy`
  (`"replace"`, `"reject"`); active view registry with O(1) scope lookups;
  `SessionLimitError` for reject policy and PersistentView protection;
  session origin tracking through push/pop navigation chains
- **Interaction ownership** with `owner_only` (default `True`) and customizable
  rejection message; `PersistentView` defaults to `owner_only = False`
- **Auto-defer safety net** that automatically defers slow interactions after
  a configurable delay (default 2.5s), preventing "This interaction failed"
  errors; safe with all wrappers and manual defer calls
- **State selectors** via `state_selector()` override for efficient re-rendering;
  views only receive notifications when their selected state slice changes

### View Patterns

- **TabView**: Button-based tab switching with active tab highlighting
- **WizardView**: Multi-step form with Back/Next/Finish navigation and per-step
  validation
- **FormView**: Declarative form with field definitions, built-in validation,
  and submit callback; supports `"select"` and `"boolean"` field types
- **PaginatedView**: Page navigation with first/last jump buttons and go-to-page
  modal for large datasets (controlled by `jump_threshold`, default 5);
  `from_data(items, per_page, formatter)` async factory; supports Embed, string,
  and dict pages with mixed embed + content; `refresh_data(items)` for live
  re-pagination; `_build_extra_items()` hook for subclass components on rows 1-4
- **`clear_row(n)`** utility on StatefulView for row-level component management
- **Transparent kwargs auto-capture** via `__init_subclass__` -- all subclass
  constructor kwargs are automatically preserved for `push()`/`pop()` navigation
  reconstruction without any manual effort from subclass authors

### Persistence

- **`setup_persistence()`** single entry point for all persistence; call once
  in `setup_hook`; supports data-only mode (no bot) or full view re-attachment
- **PersistentView** subclass for views that survive bot restarts; forces
  `timeout=None`, requires explicit `custom_id` on all components, auto-registers
  subclasses via `__init_subclass__`
- **Storage backends**: `FileStorageBackend` (JSON with `.bak` backup),
  `SQLiteBackend` (aiosqlite, WAL mode), `RedisBackend` (redis.asyncio)
- **`migrate_storage(source, target)`** utility for moving state between backends
- **`state_key`** for stable data identity across view lifetimes (vs ephemeral
  UUID view IDs)
- **Identity persistence**: `user_id` and `guild_id` stored in persistent view
  registry and restored on bot restart so session limiting works across restarts
- **Duplicate state_key cleanup**: Two-tier orphan handling when re-registering
  a `state_key`: exits live view instances via the active registry, falls back
  to message-only cleanup for cross-restart orphans
- **Stale entry handling**: Deleted messages/channels cleaned from state on
  restore; missing view classes skipped but kept for next restart;
  non-messageable channels detected and removed
- **`on_restore(bot)`** hook for post-restore setup on PersistentView

### Components

- **StatefulButton** and **StatefulSelect** extending discord.py UI components
  with automatic `COMPONENT_INTERACTION` action dispatching
- **CompositeComponent** for grouping related components with a registry for
  reusable compositions
- **ConfirmationButtons**: Confirm/Cancel button pair with callbacks
- **PaginationControls**: Previous/Next with page tracking and boundary handling
- **FormLayout**: Renders form field definitions as interactive components
- **ToggleGroup**: Radio-button-like selection with on_select callback
- **ProgressBar**: Text-based progress indicator for embed fields
- **Modal** and **TextInput** with optional validation via `validators` parameter;
  exported from top-level package
- **Component wrappers**: `with_loading_state` (visual loading feedback),
  `with_confirmation` (confirmation dialog before action),
  `with_cooldown` (per-user rate limiting)

### Theming

- **Theme** class with colors, button styles, and embed styling
- **Global registry** with `register_theme()`, `get_theme()`, `set_default_theme()`
- **Per-view theming** via `theme=` kwarg on StatefulView with fallback to default
- **Built-in themes**: `default_theme`, `dark_theme`, `light_theme`

### Validation

- **ValidationResult** dataclass for structured pass/fail results
- **Built-in validators**: `min_length`, `max_length`, `regex`, `choices`,
  `min_value`, `max_value`
- **`validate_field()`** and **`validate_fields()`** supporting sync and async
  validators
- Integrates with FormView and Modal for per-field error reporting

### DevTools

- **StateInspector** generates paginated embeds showing state overview, active
  views, sessions, action history, and store configuration
- **InspectorView** for browsing inspector pages (plain `discord.ui.View` to
  avoid polluting state)
- **DevToolsCog** with `/inspect` command gated behind `@is_owner()`

### Utilities

- **`@cascade_reducer`** decorator for registering custom reducers
- **`@cascade_component`** decorator for component registration
- **`@with_error_boundary`** and **`@with_retry`** for resilient callbacks
- **`safe_execute()`** for protected async execution
- **TaskManager** for background task tracking with proper cleanup/cancellation
- **AsyncLogger** with color-coded output and file rotation

### Documentation

- MkDocs Material site at hollowthesilver.github.io/CascadeUI/
- 18 pages: installation, quick start, state management, views, components,
  persistence, theming, middleware, devtools, examples, and API reference
- 8 example cogs: counter, themed form, persistence, navigation, state features,
  undo/redo, advanced settings menu, production-style ticket system
- README with visual-first showcase and GIF demos; detailed tutorials
  on the documentation site

### Project Infrastructure

- GitHub Actions CI pipeline: pytest matrix across Python 3.10, 3.11, 3.12, 3.14;
  Black and isort formatting checks on push and PR
- CONTRIBUTING.md with development setup, code style, and bug reporting guidelines
  (including log file guidance)
- SECURITY.md with vulnerability reporting instructions
- GitHub issue templates (bug report with structured fields, feature request)
  and pull request template
- CHANGELOG.md following Keep a Changelog format

### Compatibility

- Python 3.10, 3.11, 3.12, 3.14
- discord.py 2.7+
- PEP 561 typed package (`py.typed` marker)
- PyPI classifier: Production/Stable
- 225 tests across 22 test files

### Development History

The 1.0.0 release was built through several stages of iterative development:

- **Critical bug fixes**: Resolved singleton duplication, async-in-constructor
  patterns, double interaction responses, private API usage, race conditions in
  FormView, and closure variable capture bugs.
- **Design fixes**: Fixed logger handler proliferation, orphaned confirmation
  dialog state, global-only theming (now per-view), persistence backend
  overwrites, unbounded component interaction history, over-notification of
  subscribers, and added proper timeout handling.
- **Cleanup**: Removed dead files, consolidated imports, standardized deepcopy
  usage in reducers, fixed asyncio deprecations.
- **Publish readiness**: Package restructure to standard layout, pyproject.toml
  metadata, README, test suite, .gitignore, stdlib logger migration, LICENSE.
- **Pre-release hardening**: Modal submission reducer, backup-before-save,
  dead code audit, persistence round-trip verification, stable `state_key`
  identity for persistent data.
- **Core enhancements**: Middleware system, state selectors, devtools inspector,
  TabView/WizardView patterns, ToggleGroup/ProgressBar components, PersistentView
  with `setup_persistence()`.
- **Advanced features**: SQLite/Redis backends, per-user/guild scoping, computed
  state, undo/redo, event hooks, MkDocs documentation site, action batching,
  navigation stack, form validation.
- **Production hardening**: Session limiting with scoped enforcement, pagination
  enhancements (jump buttons, go-to modal, dynamic pagination, stacked pages),
  interaction ownership, auto-defer safety net, navigation chain session tracking,
  identity persistence for restored views, orphaned view cleanup.
- **Launch preparation**: Advanced settings menu example, ticket system example,
  transparent kwargs auto-capture for push/pop, PaginatedView extensibility hooks
  (`_build_extra_items`, `refresh_data`, `clear_row`), Modal with validation,
  README overhaul with GIF demos, GitHub discoverability, documentation site polish.
- **Pre-release fixes**: Removed `VIEW_UPDATED` and `COMPONENT_INTERACTION` from
  default `subscribed_actions` (internal bookkeeping actions were triggering
  redundant re-renders); fixed navigation state registration (`_navigate_to` now
  calls `_register_state` so pushed/popped views work with undo/redo); fixed
  double-edit race in settings menu after migrating from `dispatch_scoped` to
  custom action dispatch.
