# Changelog

All notable changes to CascadeUI are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [2.0.0] - 2026-03-31

### Added

- **V2 component system support** — full support for Discord's V2 message components
  (Container, Section, TextDisplay, MediaGallery, Separator, Thumbnail, File) alongside
  the existing V1 (View/embeds) system. Both coexist per-message; V1 is not deprecated.

- **`StatefulLayoutView`** base class extending `discord.ui.LayoutView` with all the
  same state management features as `StatefulView`: subscriptions, navigation stack,
  undo/redo, session limiting, auto-defer, owner_only. V2 views carry their display
  content as children (no content/embed/embeds parameters).

- **`PersistentLayoutView`** for V2 views that survive bot restarts. Shares the same
  persistent view registry as `PersistentView`. Uses `walk_children()` for custom_id
  validation in the V2 component tree.

- **V2 pre-built patterns:**
    - `PaginatedLayoutView` — pages as V2 component trees with the same nav controls,
      go-to-page modal, `from_data()` factory, and `refresh_data()` as V1
    - `FormLayoutView` — Container+TextDisplay display with the same validation pipeline
    - `TabLayoutView` — button-based tab switching with async builders
    - `WizardLayoutView` — multi-step with back/next navigation, step validators,
      and `on_finish` callback

- **V2 convenience helpers** (`cascadeui.components.v2_patterns`): `card()`,
  `action_section()`, `toggle_section()`, `key_value()`, `alert()`, `divider()`,
  `gap()`, `image_section()`, `gallery()`. Assembly shortcuts for common V2
  patterns, all returning standard discord.py V2 components.

- **V2 theming:** `accent_colour` and `separator_spacing` properties on `Theme`.
  `apply_to_container()` method for applying theme accent color to V2 Containers.
  All built-in themes include `accent_colour`.

- **Navigation version enforcement:** `push()`/`pop()` between V1 and V2 views
  raises `TypeError` because Discord's `IS_COMPONENTS_V2` flag is one-way per message.
  `replace()` is allowed for cross-version transitions.

- **`_freeze_components()`** on `_StatefulMixin` — single method that disables all
  interactive items, handling both V1 flat children and V2 recursive tree traversal.

- **Cross-view reactivity documentation** in the state management guide, explaining
  `dispatch()` vs `dispatch_scoped()` for multi-view UIs.

- **V2 examples:** `v2_counter.py`, `v2_dashboard.py`, `v2_settings.py`,
  `v2_form.py`, `v2_pagination.py`, `v2_wizard.py`, `v2_persistence.py` —
  covering all V2 patterns with session limiting and V2 helpers throughout.

- **`rebuild=` callback on `push()`/`pop()`** — optional kwarg that auto-defers
  the interaction, calls the callback with the new view, and edits the message.
  V2: `rebuild=lambda v: v._build_ui()`. V1: `rebuild=lambda v: {"embed": v.build_embed()}`.

- **Interaction serialization** (`serialize_interactions = True` by default) —
  `asyncio.Lock` in `_scheduled_task` serializes rapid button clicks, preventing
  racing `message.edit()` calls. Auto-defer runs outside the lock.

- **`slugify()` utility** for converting display strings to safe `custom_id` fragments.

- **DevTools V2 rebuild:** `InspectorView` is now a `TabLayoutView` with 5 tabs
  (Overview, Views, Sessions, History, Config), self-filtering, and live auto-refresh
  via `VIEW_CREATED`/`VIEW_DESTROYED` subscriptions. `StateInspector` is an alias.

### Changed

- **`_StatefulMixin` extraction:** ~840 lines of view-agnostic state management
  extracted from `StatefulView` into `_StatefulMixin`. `StatefulView` is now
  `_StatefulMixin + View`; `StatefulLayoutView` is `_StatefulMixin + LayoutView`.
  No public API changes for V1 users.

- **Session isolation:** Auto-derived `session_id` now includes the class name
  (e.g. `MyView:user_123`) so independent view hierarchies get separate nav stacks
  and undo history. Pushed/popped views inherit `session_id` from their parent.

- **Button consistency:** All interactive buttons (library internals and examples)
  use `StatefulButton(callback=...)`. `StatefulButton` skips `COMPONENT_INTERACTION`
  dispatch when `view.is_finished()`, preventing extra state noise on exit/back.

- **Message propagation:** `_navigate_to()` automatically transfers the message
  reference to pushed/popped views so `update_from_state()` can edit the message
  without manual wiring.

- **V2 exit behavior:** `exit()` freezes V2 views and edits with the frozen view
  (preserving visual content) instead of `edit(view=None)` which would produce an
  empty message. V1 behavior unchanged.

- **Settings examples** (V1 and V2) now use `dispatch("SETTINGS_UPDATED")` across
  all sub-pages for consistent live updates on the hub view.

- **`@cascade_reducer` auto-deepcopy:** The decorator now deep-copies state before
  passing it to the reducer function. Custom reducers no longer need `import copy`
  or `copy.deepcopy(state)`. All example reducers updated to use `state` directly.

- **Component module naming:** Version-specific files renamed to `v1_composition.py`,
  `v1_patterns.py`, `v2_patterns.py` for clarity. Shared files have no prefix.

- **`DebouncedPersistence` double-write fix:** Store's built-in per-dispatch
  `_persist_state()` is automatically skipped when `DebouncedPersistence` middleware
  is installed, preventing redundant disk writes.

- **Session cleanup on view destroy:** Empty sessions (no views, no nav stack) are
  automatically deleted when the last view exits.

- **Documentation overhaul:** All guide and API reference pages rewritten V2-first
  with tabbed V2/V1 examples. Fixed `@cascade_reducer` examples that incorrectly
  showed manual `copy.deepcopy()`. Added Known Limitations page.

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
