# Changelog

All notable changes to CascadeUI are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

CascadeUI follows [Semantic Versioning](https://semver.org/) starting at
**3.0.0**. Earlier 1.x and 2.x releases were pre-stable; their entries are
preserved below for historical reference but are not the supported baseline.

---

## [Unreleased]

### Added

- **Image Renderer for leaderboards.** Optional `[images]` extra shipping
  `ImageLeaderboardLayoutView` with a `render: Callable[[entries, page],
  PIL.Image]` hook. Attachment-based rendering for ranked displays that
  outgrow pure-text pagination -- avatars, gradients, custom fonts.
  Pillow-backed; core install stays lean because the extra is opt-in.
- **Redis persistence backend.** Capability-flag conformant `RedisBackend`
  with multi-process coordination and pub/sub for scoped invalidation.

---

## [3.3.0] - 2026-05-12

### Added

- **`PostgresBackend` for PostgreSQL persistence** via the new
  `pycascadeui[postgres]` extra. asyncpg-backed connection pool with
  `LISTEN` / `NOTIFY` for cross-process scoped invalidation. Configure
  with `PostgresBackend(dsn=...)`; tuning kwargs and deployment hints
  in the persistence guide.
- **Backend extensibility via `Capability.RAW_SQL`.** SQL-capable
  backends (`SQLiteBackend`, `PostgresBackend`) declare the capability
  and expose `execute` / `fetch` / `executemany` / `fetch_one` / async
  `transaction()` for user domain tables and vendor-specific features.
  `placeholder_style` ClassVar reports the parameter syntax for
  portable SQL. `InMemoryBackend` does not declare the capability.
- **V2 placement validator on `StatefulLayoutView`.** Walks the
  component tree before every Discord round-trip and raises
  `ValueError` with a path string + fix on placements Discord 400s on
  (nesting, accessory misuse, Modal-only types, size bound violations).
  Opt out with `validate_placement = False`.
- **`file_attachment(url, *, spoiler=False)` V2 builder.** Wraps the
  `File` primitive for inline attachment cards; completes the V2 media
  family alongside `gallery()`.
- **Local file attachments on `view.send()` and `view.refresh()`.** Both
  V1 and V2 send accept `file=` / `files=`; mid-session swaps go through
  `refresh(attachments=[...])`. V2 media builders (`gallery`,
  `image_section`, `file_attachment`) accept `discord.File` directly and
  resolve through `.uri`. New `MediaInput` type alias
  (`Union[str, discord.File]`) exported at the package root. New
  `cascadeui.fetch_as_file(url, filename, *, session=None, spoiler=False,
  description=None)` helper absorbs the aiohttp + BytesIO + discord.File
  construction pattern. New `examples/v2_attachments.py` covers all four
  shapes end-to-end.

---

## [3.2.0] - 2026-04-30

### Added

- **`DynamicPersistentButton` primitive.** New
  `cascadeui.components.base.DynamicPersistentButton` class wrapping
  `discord.ui.DynamicItem[discord.ui.Button]`. Supports persistent
  buttons whose handler depends only on IDs encoded in the
  `custom_id`, with no view-level state involved -- each click
  re-instantiates the class from the matched template regex. Snowflake
  capture coercion is automatic for groups named `user_id`,
  `guild_id`, `channel_id`, `role_id`, or `message_id`. Subclasses
  auto-register at class-definition time; `setup_middleware(
  PersistenceMiddleware(..., bot=bot))` calls
  `bot.add_dynamic_items(*subclasses)` so every subclass routes
  correctly after a restart with no additional user setup.
- **`coerce_snowflake_match(match_dict, snowflake_keys)` helper** in
  `cascadeui.utils.coercion`. Coerces named regex capture groups to
  `int` for known snowflake keys; complements the existing
  `coerce_snowflake_id` / `coerce_snowflake_id_set` helpers for the
  `DynamicPersistentButton` from-custom_id path.
- **`RolesLayoutView` / `PersistentRolesLayoutView` pattern.** New
  V2 multi-category role-assign panel built on top of
  `DynamicPersistentButton`. Cardinality flags on `RoleCategory`
  (`exclusive`, `required`, and the four combinations) are enforced
  inside the pattern; users only declare `categories` and the pattern
  handles button rendering, custom_id encoding, click routing,
  cardinality logic, response messages, and restart re-attachment.
  Three-tier customization surface: class attributes for static text /
  hints / colors (`title`, `subtitle`, four `hint_*`, five `*_message`,
  per-category `color` / `button_style` / `icon` / `description`),
  classmethod hooks for dynamic rendering (`format_category_title`,
  `format_category_hint`, `format_button_label`, `format_button_emoji`,
  `format_button_style`), and method override for full layout control
  (`build_category_card`). Five event hooks
  (`on_role_assigned`, `on_role_removed`, `on_role_swap`,
  `on_role_required_block`, `on_role_error`).
- **`RoleCategory` typed schema** in
  `cascadeui.views.patterns.types`. Dataclass with
  `__post_init__` validation; required `name` and `roles` (dict
  mapping role label to role ID), optional `exclusive` / `required`
  cardinality flags, `color`, `button_style`, `icon`, `description`.
- **`SelectDefaultValue` adoption** on the four specialized selects
  (`RoleSelect`, `UserSelect`, `ChannelSelect`, `MentionableSelect`).
  New `default_values=` constructor kwarg + `set_default_values(values)`
  method on each select; CascadeUI coerces raw `int` IDs / Discord
  objects (Member, User, Role, GuildChannel) / pre-built
  `discord.SelectDefaultValue` instances to the discord.py shape with
  the right type per select class. `MentionableSelect` infers type
  from the object class (`Member`/`User` -> `"user"`, `Role` ->
  `"role"`); raw `int` IDs are rejected because the type cannot be
  inferred.
- **Optional `description=` kwarg on every modal input wrapper**
  (`TextInput`, `Checkbox`, `CheckboxGroup`, `RadioGroup`,
  `FileUpload`). Populates `discord.ui.Label.description` for an
  optional secondary helper line beneath the field title.
- **Four new class attributes on `LeaderboardLayoutView`** for
  single-line customization of ranking display. Each closes a gap
  where users previously had to override a whole method to tweak one
  literal:
  - `podium_emojis: Dict[int, str]` -- rank-keyed glyphs (default
    gold/silver/bronze). Override the dict to change podium treatment
    or extend past rank 3 without overriding `format_rank`.
  - `entry_separator: str = " -- "` -- separator between name and
    stat columns inside `format_entry`.
  - `card_color: Optional[discord.Color] = None` -- accent color for
    the rankings card; `None` falls through to the active theme.
  - `show_title_divider: bool = True` -- toggle the divider rendered
    below the title.
- **`EmojiInput` type alias** in `cascadeui.components.types`,
  defined as `Optional[Union[str, discord.Emoji, discord.PartialEmoji]]`.
  Mirrors the union accepted by `discord.ui.Button` and
  `discord.SelectOption`. Adopted across every typed `emoji=` slot
  in the library (button builders, pattern ClassVar attributes,
  the refresh handoff). Strings, live `Emoji` instances, and
  `PartialEmoji` instances all flow through unchanged.
- **Custom emoji documentation** in the components guide and a
  `Type Aliases` entry in the API reference covering the three
  string forms (unicode, custom static, custom animated) and
  application-owned emojis.
- **`nav_inside_container` ClassVar on `PaginatedLayoutView`**
  (V2-only, default `False`). When `True`, the page content and the
  navigation `ActionRow` are wrapped in a single `Container` so the
  paginator renders as one cohesive card with built-in navigation.
  Items added via `_build_extra_items` remain outside the wrapping
  Container in either mode. Single-page views render no nav row, so
  the flag has no effect when only one page is displayed.
- **Migration guide for users coming from Soheab's
  [CV2 paginator gist](https://gist.github.com/Soheab/891c39d7294b1bdbadc7ecf35ce51cc5)
  and [classic paginator gist](https://gist.github.com/Soheab/f226fc06a3468af01ea3168c95b30af8)**
  in the patterns guide, mapping the gist API to CascadeUI's grammar.
- **`push()` and `replace()` accept pre-constructed view instances**
  in addition to view classes. The instance form pairs with async
  classmethod constructors (`PaginatedLayoutView.from_data`,
  `from_cursor`) where the view is built before the navigation
  call. Extra kwargs alongside an instance raise `TypeError` --
  the instance is already initialized. Backward compat preserved:
  passing a class continues to construct the view via
  `view_class(**kwargs)`.
- **`StatefulButton` and `StatefulSelect` accept `owner_only=False`
  kwarg.** When `True`, mismatched clicks route through
  `view.on_unauthorized(interaction)` instead of the user callback.
  Pairs with view-level `owner_only=False` to express open-view +
  host-only-button -- the canonical shape for lobby Start/Disband
  buttons, ticket Close buttons, poll End buttons.
- **Closure-factory subsection** in the patterns guide for paginator
  formatters that need per-instance state.
- **`push()` and `pop()` now edit the Discord message regardless of
  whether `rebuild` is supplied.** Previously, omitting `rebuild` left
  the message showing the parent view -- the navigation transition
  completed in state but the user saw no change. The `rebuild` kwarg
  is now an optional pre-edit hook for views that need
  post-construction setup (V2 views with empty trees that need
  `v.build_ui()`; V1 views that return an `embed` / `content` dict for
  the edit). Views built by async classmethods like `from_data` come
  fully populated and need no rebuild. The shared
  `_apply_navigation_edit` helper handles the defer + optional rebuild
  + edit cycle for both `push` and `pop`. The new view's `_message`
  ref preserves the parent's plain `Message` (no 15-minute interaction
  token cliff) -- the edit response is dropped on the floor when a
  ref is already present.
- **`examples/v2_library.py`** demonstrating pagination + drill-down
  navigation with auto back buttons.

### Changed

- **Modal input wrappers now render through `discord.ui.Label`**
  (discord.py 2.5+ pattern). Each of the five wrappers
  (`TextInput`, `Checkbox`, `CheckboxGroup`, `RadioGroup`,
  `FileUpload`) produces a `ui.Label` containing the inner discord.py
  input via `create_discord_component()`. The label string moves to
  `Label.text`; the new `description=` kwarg populates
  `Label.description`. User-facing API unchanged: wrapper construction
  still takes `label=` as the first positional arg, validators still
  attach to the wrapper, `Modal.on_submit` still writes back
  `.value` / `.values` on the wrapper. Only difference visible to
  user code: `modal.children` carries `ui.Label` wrappers around
  inputs instead of raw inputs at the top level. CascadeUI internally
  unwraps via `_unwrap_label` during submit collection.
- **`v2_persistence.py` example rewritten** to use
  `PersistentRolesLayoutView`. The hand-written cardinality logic
  collapses from ~200 lines to ~60 with no behavior change.
- **Every typed `emoji=` slot widened from `Optional[str]` to
  `EmojiInput`.** No runtime behavior change -- discord.py was
  already accepting the wider union at the boundary; only the type
  annotations were narrower than reality. Markdown-routed sites
  (`LeaderboardLayoutView.podium_emojis`, `RoleCategory.icon`)
  stay `str`-typed because they render into TextDisplay markdown
  rather than piping to `discord.ui.Button`'s `emoji=` parameter.
- **`WizardLayoutView` next-button emoji assignment simplified.**
  Previously wrapped the class-attribute string in
  `discord.PartialEmoji.from_str` before assigning to
  `Button.emoji`. With `EmojiInput`, the wider union is assigned
  directly and discord.py's `Button.emoji` setter performs the
  type discrimination internally.
- **`_reattach_one` batches the registration dispatches and
  `on_restore`** in `store.batch(source_id=view.id)`. The three
  startup actions (`SESSION_CREATED + VIEW_CREATED + VIEW_UPDATED`)
  plus any `on_restore` dispatches collapse into one
  `BATCH_COMPLETE` notification per restored view; previously each
  fired its own subscriber fan-out cycle. Linear savings as
  persistent-view count scales. Also tightens rollback atomicity --
  failed reattach no longer leaks partial-state notifications to
  subscribers.

### Fixed

- **Acting-view fast-path stall no longer falls through to the
  channel endpoint.** The previous fall-through consumed the
  auto-defer timer's ack budget and produced visible *"interaction
  failed"* toasts under Discord-side latency. `refresh()` now
  returns on `wait_for` cancellation; the timer acks with full
  remaining budget and the visible UI update arrives on the next
  state-change refresh.
- **`format_rank` and `format_entry` previously hardcoded literals**
  that subclasses could only change by overriding the entire method.
  Both methods now read class attributes (`podium_emojis` and
  `entry_separator` respectively) so subclasses change one literal
  with a one-line attribute override.
- **`LeaderboardLayoutView.send()` no longer double-attaches the
  navigation ActionRow.** The send-time tree layout used to call
  `_add_page_content()` (which after the `_compose_pagination_tree`
  refactor adds the nav row inline) and then `add_item(self._nav_row)`
  separately, attaching the row twice. Switched the send path to a
  single `_compose_pagination_tree()` call matching the pattern in
  `__init__` and `_update_page`.
- **Back button no longer ships a redundant `edit_original_response`
  call.** After the push/pop unconditional-edit decoupling,
  `_add_back_button`'s `back_callback` was still calling
  `interaction.edit_original_response(view=prev_view)` after `pop()`
  had already routed through `_apply_navigation_edit` (which performs
  the same edit). Both V1 (`_navigation.py`) and V2 (`layout.py`)
  back-button callbacks now only edit when `pop()` returns `None`
  (empty stack cleanup); successful pops let `_apply_navigation_edit`
  handle the message swap.
- **`_reattach_one` registration order corrected.** Persistent view
  rehydration now registers the view in `_active_views` before
  dispatching `VIEW_CREATED` via `_register_state`, mirroring the
  `_send_pipeline` ordering contract. The reversed order created a
  window where the view existed in `state["views"]` but not in the
  instance index, so concurrent `send()` from another shard could
  bypass the instance-limit check during the brief reattach gap.
- **`InspectorView.state_selector` signature widened** to
  `(view_id, message_id, channel_id)` tuples per view. The previous
  ID-only signature equality-skipped `VIEW_UPDATED` notifications at
  the subscriber gate, so the Views tab's `Channel / Msg` columns
  rendered as `None / None` until the user manually refreshed.
- **Auto back button survives pattern rebuilds.** Paginated page
  turns, tab switches, form re-layout, menu refresh, role panel
  rebuild, and wizard step advance all call `clear_items()` and
  recompose. The auto back button injected by `push()` was lost on
  every rebuild. Added `_restore_navigation_artifacts()` on
  `_NavigationMixin` (idempotent, no-op when no back button is
  registered); each pattern's rebuild path calls it after recomposing.
- **Quickstart counter example was missing `subscribed_actions`**,
  so the reducer ran but `on_state_changed` never fired and the
  message stayed stale. The V2 and V1 examples now declare the
  attribute, and the data-flow diagram surfaces the subscriber
  filter step.
- **Undo + slot-touching reducer chain no longer crashes** with
  `TypeError: argument of type 'object' is not iterable`. The undo
  middleware's `_MISSING` sentinel now survives `@cascade_reducer`'s
  state deep-copy boundary; previously the deep copy invalidated the
  `is _MISSING` identity check, leaving a bare object as the slot
  value for the next reducer to read.

---

## [3.1.0] - 2026-04-21

### Breaking

- **`session_start` friendly-name hook removed.** The `SESSION_CREATED`
  action was previously reachable via `store.on("session_start", cb)`; the
  friendly name did not match the `view_created` / `view_updated` /
  `view_destroyed` grammar used elsewhere. Use `session_created` instead.
  No deprecation alias: the name was a grammar outlier, not a supported
  surface. Migration: `store.on("session_start", cb)` ->
  `store.on("session_created", cb)`.

### Added

- **`StateStore.get_active_views()`.** Public accessor returning a
  read-only `MappingProxyType` over the active-view registry
  (`view_id -> view instance`). The returned mapping is live but rejects
  mutation, so user code can iterate or count live views without
  reaching into `store._active_views` and without risk of corrupting
  registry invariants.
- **`StateStore._build_initial_state()`.** Single source of truth for
  the canonical top-level state shape (`sessions`, `views`,
  `components`, `application`). Both `StateStore.__init__` and
  `DevToolsCog.reset` consume it, eliminating the "new top-level key
  added to `__init__` but not to `reset`" drift class.
- **DevTools command surface expanded from 9 to 17.** Eight new
  subcommands land under `/cascadeui`:
  - **Registry introspection:** `/cascadeui persistent` (registered
    `PersistentView` classes), `/cascadeui scoped [slot]` (scoped bucket
    contents grouped by scope kind), `/cascadeui computed [name]`
    (`@computed` registrations with cache-primed status), and
    `/cascadeui middleware` (installed middleware in dispatch order).
  - **Diagnostics:** `/cascadeui history [n]` (recent dispatched
    actions), `/cascadeui perf [action]` (toggle perf sampling:
    `on`/`off`/`clear`/`status`), `/cascadeui trace [action]` (toggle
    ViewStore dispatch tracing), and `/cascadeui subscribers` (active
    subscribers with action-filter breakdown).
- **`DevToolsCog` group listing auto-derived.** The group's `ctx.invoke`
  fallback now reads `self.cascadeui_group.commands` at call time
  instead of a hardcoded listing, so adding a new subcommand does not
  require a parallel edit to keep the help banner accurate.
- **`/cascadeui reset` observability.** Reset now invalidates every
  `@computed` cache (via `ComputedValue.invalidate()`), clears
  subscriber selector memoization (`store._last_selected`), counts
  per-view exit failures, and surfaces the failure count in the
  response. Routes through `StateStore._build_initial_state()` so the
  reset shape matches `__init__` exactly.

### Fixed

- **Action-registry drift across coupled tables.** `INSPECTOR_PURGED_STALE`
  was missing from `_BOOKKEEPING_ACTIONS` (persistence middleware) and
  `_SKIP_ACTIONS` (undo middleware), and five action types
  (`SCOPED_UPDATE`, `PERSISTENT_VIEW_REGISTERED`,
  `PERSISTENT_VIEW_UNREGISTERED`, `INSPECTOR_PURGED_STALE`,
  `APPLICATION_SLOTS_PRUNED`, `REGISTRY_PRUNED`) had no corresponding
  friendly-name entry in `_HOOK_ACTION_MAP`. Internal table consistency;
  no user-facing API change beyond the now-reachable friendly names.

---

## [3.0.0] - 2026-04-16 -- Stable Release

CascadeUI 3.0.0 is the first stable release. The view layer was reorganized
around a five-pillar model (Access Control, Instance Constraints, Lifecycle,
Session Membership, Navigation), the persistence machinery was rebuilt around
a capability-flag backend Protocol with per-namespace isolation, and the
public API was sharpened so every hook and class attribute follows a single
naming grammar (`on_<event>` for hooks, `*_message` for static text,
`*_policy` for behavior switches).

The entries below describe the feature surface of the library at this
cut; future releases will document changes as standard Keep a Changelog
diffs against this baseline.

### View Layer -- Five Pillar Model

- **Five Pillar refactor.** Every view-layer feature now belongs to exactly
  one of: Access Control, Instance Constraints, View Lifecycle, Session
  Membership, Navigation. Documented in `docs/guide/five-pillars.md`.
- **`instance_*` family** (`instance_limit`, `instance_scope`, `instance_policy`,
  `instance_limit_message`, `on_instance_limit`, `InstanceLimitError`) replaces
  the older `session_*` capacity vocabulary. `state_scope` replaces `scope`
  to disambiguate from `instance_scope`. `replace_policy` replaces `on_replace`
  to keep `on_*` reserved for hooks.
- **API grammar enforcement.** `on_<event>` for override hooks, `*_message`
  for static text, `*_policy` for behavior switches. Every public attribute
  follows the three-tier precedence model: class attribute ->
  method override -> explicit argument.
- **Render hook rename.** `build_ui` (no underscore) is the canonical render
  method. `on_state_changed` (replacing `update_from_state`) is the public
  state-change hook; default implementation calls `build_ui()` then `refresh()`.
- **Session model overhaul.** `shared_data` (was `session_data`),
  `session["members"]` (was `session["views"]`), `session["shared_data"]`
  (was `session["data"]`). Session keys now use `module.qualname` to avoid
  short-name collisions. Nav/undo/redo stacks are view-local with
  forward-transfer on push/pop.
- **`session_continuity` opt-in.** Default auto-derived `session_id` carries
  a per-instance UUID suffix, so each view invocation gets its own session,
  navigation chain, and undo timeline. Views that want repeat-open state
  coalescing (undo history surviving close-and-reopen, shared_data continuity
  across gestures) set `session_continuity: ClassVar[bool] = True` on the
  class. The opt-in collapses the derivation back to the class-coalesced
  shape. Navigation inheritance is unchanged -- push/pop chains stay on one
  session regardless of polarity because `_navigate_to` forwards `session_id`
  explicitly.

### View Layer -- Capacity, Lifecycle, and Interaction

- **`participant_limit` trio.** Class-level cap on total view occupants paired
  with `participant_limit_message` (static) and `on_participant_limit` (dynamic).
  `register_participant()` returns `bool`; `auto_register_participants` flag
  performs all-or-nothing rollback before any Discord side effects.
- **`set_class_attribute()`** for per-instance policy override of class-level
  attributes; runs the same validation pipeline as `__init_subclass__` and
  rejects descriptor shadowing.
- **`check_instance_available()`** classmethod for sync pre-checks before
  expensive `__init__`.
- **`attach_child()` and `parent=` kwarg.** Parent-local cleanup cascade with
  optional auto-attachment on successful `send()`. `protect_attached` excludes
  views with active participants or attached children from replacement
  candidates.
- **Hook surface expansion.** `on_unauthorized` / `unauthorized_message`,
  `on_replaced` / `replaced_message`, `on_message_delete`, `on_reopen_failure` /
  `reopen_failure_message`, `error_message` for the default `on_error` embed.
- **`exit_policy`** (default `"delete"` for replaces) governs whether bare
  `exit()` deletes, disables, or leaves the message untouched.
- **`send()` rollback.** Three-tier teardown (instance-limit, participant
  registration, Discord HTTP) with full `exit()`-mirror cleanup. Returns
  `None` on instance-limit rejection rather than raising.
- **Ephemeral session handling.** Timeout derivation is driven by the user's
  `timeout` value. When `timeout <= 900` the view lives inside the 15-minute
  webhook token and expires normally. When `timeout > 900` the library
  auto-engages `auto_refresh_ephemeral`, installs a refresh button at T+810s,
  and freezes `on_state_changed` rebuilds once armed so state notifications
  cannot clobber the refresh button before the T+900s cliff.
  `on_reopen_failure` / `reopen_failure_message` control what the user sees
  when reconstruction fails. `exit()` distinguishes `NotFound` (silent),
  ephemeral `401` token expiry (DEBUG), and other `HTTPException` errors
  (ERROR), matching the three-tier precedent established by `refresh()`.
- **Push/pop kwarg auto-capture.** `__init_subclass__` snapshots constructor
  kwargs so navigation works transparently without manual wiring.
- **Message handling.** `send()` re-fetches the message via the channel
  endpoint to avoid the 15-minute interaction-token expiry.
  `_webhook_message` dual-reference preserves embed-edit capability for V1.
  `refresh()` replaces manual `message.edit` patterns.
- **Interaction helpers.** `respond()`, `open_modal()`, and `_safe_defer()`
  with `is_done()` fallback. Auto-defer safety net (timer + post-callback
  defer) plus `serialize_interactions = True` default.
- **Refresh throttling.** Rate-limit-aware: reactive 429 backoff is always
  on; opt-in `refresh_cooldown_ms` sets a proactive cooldown window.
  Deferred refreshes re-enter `on_state_changed` against the latest store
  state.

### View Layer -- Decomposition

- **`StatefulView` extracted** to `cascadeui/views/view.py`, mirroring
  `StatefulLayoutView` in `layout.py`.
- **`_InteractionMixin`** (`_interaction.py`) and **`_NavigationMixin`**
  (`_navigation.py`) separate the interaction and navigation concerns from
  the core mixin.
- **Shared `_send_pipeline()`** deduplicates the V1/V2 send logic so rollback
  and registration are written once.
- **`cascadeui/views/patterns/`** package groups V1 and V2 variants per
  pattern file, with `_Base{Pattern}Mixin` extracted in each.
- **`cascadeui/exceptions.py`** at the package root holds runtime exception
  types callers catch programmatically (currently `InstanceLimitError`).

### View Patterns

- **`MenuView` / `MenuLayoutView`** -- category-based navigation hub with
  push/pop drill-down, themed cards, and per-category style customization.
- **`LeaderboardLayoutView` / `PersistentLeaderboardLayoutView`** -- V2-only
  paginated ranked display with `get_entries()`, `format_entry()`, and
  `build_summary()` hooks.
- **Leaderboard Section render mode.** `entry_layout = "sections"` renders
  each rank as a Discord `Section` with split `format_primary()` /
  `format_secondary()` hooks and an async `get_avatar_url()` accessory.
  Falls back to a stacked two-line `TextDisplay` when the avatar hook
  returns `None`, so a platform requirement (Section requires `accessory=`)
  never forces a subclass override. `entry_layout = "sections"` is coupled
  to `leaderboard_per_page <= 5` to stay inside Discord's component
  caps, validated at class-definition time.
- **`PaginatedView.from_cursor()` lazy pagination.** Classmethod for
  paginating over an async `fetch(page_idx) -> PageResult` cursor
  instead of an eager in-memory list. Fetched pages ride an LRU cache
  that protects the current page from eviction, so navigation away and
  back never shows "Loading..." on the page the user is looking at.
  `refresh_pages()` clears the cache and re-fetches the current page
  in place.
- **`DisplayLayoutView`** -- one-shot V2 send shorthand that accepts a
  `container=` kwarg without requiring a subclass.
- **Pattern customization.** Wizard navigation, Tab active/inactive style,
  and Pagination navigation each gain customization triples plus
  `on_finish()`, `on_tab_switched()`, and `on_page_changed()` method hooks
  (replacing constructor-parameter callbacks).
- **Wizard navigation hooks.** `on_step_entered(step_index)` fires after
  forward/back navigation settles on a new step; `on_step_exited(step_index)`
  fires before the wizard leaves a step; `on_validation_failed(step_index,
  error, interaction)` fires when a step validator returns `(False, error)`.
  Default `on_validation_failed` responds ephemerally with the error text.
- **Wizard conditional steps.** A step dict accepts an optional
  `"condition": Callable[[], bool]` predicate; Back and Next skip past
  steps whose condition returns False at navigation time. A condition
  that raises is treated as visible (safe fallback) and logs a warning.
  Step-indicator counts and `is_last` resolution use the visible-step
  count, not the declared-step count.
- **Wizard progress header (V2 only).** `WizardLayoutView` renders a
  themed `card()` containing a `progress_bar` above step content when
  more than one visible step exists. Override `_build_progress_header`
  to customize the container or return `None` to suppress. Disable
  globally via `show_progress_bar = False`.
- **Wizard step-builder theme context.** `_rebuild_step_content` runs
  inside the view's theme context, so any `card()` call in the user's
  step builder (or in `_build_progress_header`) inherits the view's
  accent colour automatically.
- **Form field-change hook.** `on_field_changed(field_name, old, new)` fires
  on every field transition (select, boolean, modal text). The hook is
  gated on `old != new`, so repeated identical submissions do not trigger
  redundant work.
- **Form inline validation errors.** Validator failures surface on the
  form body instead of an ephemeral message. `_field_errors` maps field
  id to error list; `_form_error` holds a form-level message. V1 renders
  errors as a red-tinted embed with a warning line under each failing
  field; V2 renders a top-level `alert()` container plus inline warning
  lines inside each field's card. Errors clear automatically on any
  field-change gesture, so the UI stays in sync with the latest input.
- **Form field groups.** A field dict accepts an optional
  `"group": "Section Name"` key. Consecutive fields sharing the same
  group collect into a single run (no merging across interleaved groups,
  so declaration order is the UI contract). V1 renders each run as a
  bold-headed embed field; V2 renders each run as its own themed `card()`.
- **Form typed field types.** `integer`, `float`, and `date` join
  `text`, `boolean`, and `select` as first-class field types. Typed
  modal fields ride the shared form modal alongside `text` (5-input
  Discord cap is now measured against the union). Parse failures
  surface as inline field errors while preserving the user's raw input
  so the next modal open shows what they typed. `min_value` / `max_value`
  field keys clamp `integer` and `float`; `date` accepts ISO 8601
  (`YYYY-MM-DD`). The submit callback short-circuits when
  `_field_errors` is populated, so validators never fire against
  unparsed strings.
- **Form `multi_select` field type.** Renders a `StatefulSelect` with
  `min_values=1` when required, `max_values=max(1, len(options))` by
  default (override via the optional `max_values` field key). Selected
  options survive rebuilds via `SelectOption(default=...)`, and the
  callback writes a `list` to `form.values`. Required-field checks
  count an empty list as unset.
- **Typed form / wizard schemas.** `FormField`, `FormSchema`, `WizardStep`,
  and `WizardSchema` (all exported from `cascadeui`) give form and wizard
  patterns a typed construction path with IDE auto-complete and
  class-definition-time validation. Patterns accept either the existing
  dict API (`fields=[{...}]`, `steps=[{...}]`) or the typed variant
  (`fields=[FormField(...)]`, `schema=MySchema()`); passing both raises
  `ValueError`. Typed entries lower to the same internal dict shape
  through `.to_dict()`, so every downstream helper keeps working against
  one canonical representation.
- **`on_category_selected`** (was `_on_category_selected`). MenuView /
  MenuLayoutView pre-push hook now follows the public `on_<event>` grammar.
  No deprecation alias -- override sites rename in place.
- **V2 button-mutation parity.** Wizard / Tab / Paginated V2 variants now
  rebuild navigation buttons in the component tree instead of mutating
  individual button attributes.
- **`PaginatedLayoutView._build_extra_items()`** preserved across page
  navigation.

### Components

- **Grid helpers.** `emoji_grid()` returns a live `EmojiGrid` (TextDisplay
  subclass) supporting cell, row, rectangle, and bulk assignment with
  optional `"alpha"` / `"numeric"` axis labels. `button_grid()` packs a
  `(row, col) -> Button` factory into ActionRows enforcing Discord's 5x5
  component cap. See `examples/v2_grids.py` for the standalone showcase.
- **V2 builder expansion.** `stats_card()` (themed key/value summary
  panel), `progress_bar()` (text-rendered bar readable inside any
  TextDisplay), `confirm_section()` (Section + confirm/cancel button
  pair), `link_section()` (Section + `LinkButton` accessory),
  `button_row()` (ActionRow-wrapped button sequence), `cycle_button()`
  (button that rotates a state value through a preset list),
  `toggle_button()` (one-button on/off toggle), and `tab_nav()` (tab
  strip renderer for hand-rolled tab views). All return standard
  discord.py V2 components and compose cleanly with `card()`.
- **Modal input wrappers.** `Checkbox`, `CheckboxGroup`, `RadioGroup`,
  `FileUpload`, plus `TextInput(validators=[...])` as the canonical
  validator-attachment shape. `Modal` auto-collects validators from all
  five wrapper types and writes submitted values back to the wrapper
  instances.
- **`FormView` native `"text"` field support** via grouped modal collection
  (the `FormLayout` composite is removed as redundant).
- **`StatefulSelect`** gains `set_selected()` / `get_selected()` for
  state-driven defaults and a disabled-placeholder fallback when options
  are empty. Select callbacks may declare an optional second `values`
  parameter.

### State System

- **`@computed`** decorator with cached selectors. Module-level registry
  preserves registrations across store resets so tests that replace the
  singleton no longer lose computed values.
- **`access_slot()`** auto-vivifies `state["application"][name][key]` with
  a `default_factory`; **`read_slot()`** is the variadic pure-read
  counterpart for selectors and `@computed`. **`slot_property`**
  descriptor reads slots from instance state. **`persistent=True`** on
  `access_slot` registers the slot name for write-through persistence via
  `PersistenceMiddleware`.
- **Scoped state accessors.** `StateStore.get_scoped()`, `get_scoped_from()`
  (staticmethod for use inside reducers), `iter_scoped()`, `set_scoped()`,
  `merge_scoped()`. Four-axis scoping (`user`, `guild`, `user_guild`,
  `global`) with named-slot routing via `scoped_slot` class attribute.
- **`seed_initial_state(state)` hook** on `_StatefulMixin` for views that
  need to seed slots before the first render.
- **`@cascade_reducer`** raises on built-in action collision and warns on
  same-namespace overwrite. `register_reducer` exposes the same warning at
  the store level.
- **Hybrid subscriber notifications.** `_notify_subscribers` runs a
  two-path fan-out keyed on `action["source"]`: the acting subscriber
  (whose id matches the action source) is awaited inline so its refresh
  rides the interaction's ack cycle, while every other subscriber is
  scheduled as a background task via `task_manager`. State snapshots
  are passed by argument so every task sees the state that matched its
  notification. `BatchContext.source_id` threads the same contract
  through batched regimes. `store._flush_notifications()` drains
  in-flight tasks for tests; production code never needs to flush.
- **Acting-view `interaction.response.edit_message` fast path.** The
  acting view's refresh ships as one REST round-trip instead of two
  (ack + channel `PATCH`). `_CURRENT_INTERACTION` contextvar bound in
  `StatefulComponent.create_stateful_callback` lets `refresh()` route
  the edit through the interaction response slot when the handled
  interaction is a component click targeting this view's message and
  its response is still open. Disqualified cases (modal submits,
  cross-view dispatches, missing message, already-deferred responses)
  fall through to the channel endpoint with no behavior change; 429
  arms the reactive backoff window; non-429 HTTP errors fall through
  to the channel path so a transient interaction-endpoint failure
  never loses the refresh.
- **`batch()` is transitive.** `store.dispatch()` queues actions when a
  batch is active; nested batches absorb into the outer batch. Per-action
  profiling samples are suppressed inside batches and one synthetic
  `BATCH_COMPLETE` action carries the rolled-up sample with `batch_size`.
- **Library uses `batch()` internally** in `_send_pipeline`, `_navigate_to`, and
  `_cleanup_attached_children`.
- **Undo coverage expanded** to include `dispatch_scoped` and
  `update_session`. Snapshots store a per-slot diff of the `application`
  keys the action actually touched (paired with the session's
  `shared_data`) rather than a wholesale copy, using a `_MISSING`
  identity sentinel for added slots. Sibling views writing to their own
  slots survive this view's undo path, closing a cross-view contamination
  class that wholesale snapshots could not. UNDO/REDO bypass subscriber
  action filters so cross-view reactivity works without explicit
  subscription.
- **`subscribed_actions` default** is now an empty set (subscribe to
  everything by default).
- **StateStore public/private boundary.** Registration plumbing
  (`_register_reducer`, `_register_computed`, `_register_view`,
  `_register_participant`, `_add_middleware`, `_unsubscribe`, and their
  counterparts) is single-underscore-prefixed; the public surface is
  `dispatch`, `subscribe`, `batch`, `on`/`off`, `has_middleware`, the
  scoped accessor family, and the `state`/`computed` properties. The
  `BatchContext.dispatch(...)` legacy shim is removed -- use
  `store.dispatch(...)` inside `async with store.batch()`. The live
  `PersistenceManager` stashed by `PersistenceMiddleware.initialize` is
  now published as `store.persistence_manager` (previously
  `store._persistence_manager`); the public name matches the rest of the
  store's read surface and is the documented path for manual prune,
  flush, and reattach-summary access.

### Persistence

- **Two-namespace persistence.** Registry and application each write as
  an independent stream with its own debounce window. Scoped data rides
  under the application namespace -- the middleware routes scoped writes
  through the same application-diff pipeline, so per-user/guild buckets
  persist when a view opts its scoped slot in via `persistent_slots`.
- **`PersistenceBackend` Protocol** with `Capability` flags (`KV`,
  `RELATIONAL`, `SCHEMA_META`). Built-in backends: `InMemoryBackend`
  (always available, reference implementation; reused as the default
  testing seam) and `SQLiteBackend` (optional via `aiosqlite`).
- **Per-namespace configs** (`RegistryPersistence`, `ApplicationPersistence`)
  govern slot policies and debounce windows per namespace. Configured
  once at `PersistenceMiddleware` construction; shorthand `backend=`
  fills any namespace the caller leaves unconfigured. The registry is
  always persisted; application slots (including scoped) are opt-in via
  the `persistent_slots` class attribute or `SlotPolicy(persistent=True)`
  at setup, so nothing is retained by accident.
- **Typed persistence exceptions.** `PersistenceError` base plus
  `PersistenceConfigError`, `PersistenceInitError`, `PersistenceSchemaError`,
  and `PersistenceRehydrateError` for the four failure classes (bad
  construction config, backend init failure, schema migration failure,
  view rehydration failure). Exported from the package root.
- **Schema migration registry.** `register_migrator(namespace, version,
  fn)` for payload-shape migrations between schema versions;
  `register_kwargs_migrator(view_cls, fn)` for rewriting captured
  constructor kwargs when a view's `__init__` signature changes.
- **`PersistenceMiddleware`** replaces `DebouncedPersistence` with
  identity-diff dirty tracking, max-age ceilings, retry backoff on backend
  failure, and observability hooks. Skips bookkeeping actions and
  dispatch-only actions whose state reference is unchanged.
- **Unified middleware install.** `setup_middleware(*middlewares, store=None)`
  installs every middleware through one async helper: add to the dispatch
  chain (guarded by class so repeat calls are no-ops), then await each
  middleware's ``initialize(store)`` if present. `PersistenceMiddleware` is
  now constructor-configurable (`backend=`, `registry=`, `application=`,
  `bot=`, `migrators=`) and owns its own seven-phase startup pipeline;
  the old top-level `setup_persistence()` entry point is gone. Bot-type
  validation fires at construction so misuse points at the user's call site.
- **`setup_logging(actions=True)`** auto-installs `LoggingMiddleware` for
  observability without a separate `add_middleware` step.
- **Restore-time hardening.** Ghost `views` / `sessions` / `components` /
  `modals` entries pruned on load. `PersistentView` re-derives `session_id`
  after identity restoration. Messageable guard on orphan cleanup avoids
  crashes on `CategoryChannel` / `ForumChannel`.

### DevTools and Performance

- **`InspectorView`** rebuilt as a `TabLayoutView` with six tabs
  (Overview, Views, Sessions, History, Performance, Config),
  self-filtering to avoid observer-effect noise, and live auto-refresh
  via `VIEW_CREATED` / `VIEW_DESTROYED` subscriptions. Uses `@computed`
  internally for the Overview aggregations.
- **Interactive controls.** Purge Stale, Flush to Disk, Clear History,
  Exit Selected / Exit All, Clear Selected.
- **`/cascadeui` hybrid command group** with nine owner-only subcommands
  (`inspect`, `views`, `exit`, `exitall`, `sessions`, `clear`, `flush`,
  `purge`, `reset`).
- **Profiling tab** with reducer / middleware / notify sample breakdown,
  per-subscriber timing inside the notify fan-out, per-dispatch edit counter,
  and split `middleware_ms` / `reducer_ms` columns.
- **Render-hash short-circuit** in `refresh()` skips `message.edit` when
  the rendered tree digest is unchanged.
- **Reducer shallow-copy** replaces `copy.deepcopy` in the dispatch path.
- **Persistence no-op** when state is unchanged.
- **Leaderboard rebuild short-circuit.** A semantic `_entries_signature_for`
  guard skips the page-list rebuild when the source entries have not
  changed, and `asyncio.gather` resolves avatars concurrently during a
  single page build so the Section render mode does not serialize
  per-entry CDN fetches.
- **Stable `custom_id`s across rebuilds.** `_stabilize_custom_ids()` runs
  at two seams -- after every `build_ui()` and at the top of `refresh()` --
  so pattern rebuilds that bypass `build_ui()` still emit the same
  `custom_id` for the same logical component across re-renders. Prevents
  Discord's `ViewStore` from dropping in-flight clicks when a rebuild
  swaps in a new button instance.
- **Automatic message-deletion cleanup** via gateway listeners
  (`on_raw_message_delete`, `on_raw_bulk_message_delete`).

### Theming

- **Theme context propagation** via `contextvars.ContextVar`. Builder
  functions like `card()` and `stats_card()` auto-read the active view's
  theme as a fallback when no explicit `color=` is passed.
- **Class-level `theme = my_theme`** preserved alongside the kwarg path;
  validated in `__init_subclass__`.
- **`get_current_theme()`** returns the active theme inside a view context.
- Removed unused theme methods, style keys, and the "Powered by CascadeUI"
  footer default.

### Defensive Input Handling

- **Class-attribute validation.** `__init_subclass__` validates enum, int,
  bool, and float class attributes at subclass-definition time. Catches
  `instance_policy = "rejct"` or `owner_only = 1` before any user clicks
  a button.
- **Snowflake coercion** at `__init__`, the `allowed_users` setter, and
  `register_participant`. Accepts either `int` or `discord.abc.Snowflake`;
  raises `TypeError` for genuinely unrecognizable values. Helper lives in
  `cascadeui/utils/coercion.py`.
- **`set_class_attribute()` guards.** `_INSTANCE_DATA_ATTRS` rejects
  snowflake-domain data; descriptor walk rejects shadowing methods or
  properties.
- **`attach_child()` invariants.** Self-attachment and circular-chain
  attachments raise `ValueError`; re-parenting detaches cleanly from the
  old parent.

### Examples

New examples introduced in 3.0.0:

- **`v2_hello_world.py`** -- the canonical minimal example. A single-button
  counter showing `state_scope = "user"`, `subscribed_actions`, the default
  `on_state_changed()`, and the smallest viable CascadeUI cog.
- **`v2_grids.py`** -- standalone showcase for `emoji_grid()` and
  `button_grid()`, exercising axis labels, bulk assignment, and the eight
  preset variations in `/grid_gallery`.
- **`v2_battleship.py`** -- 10x10 two-player game. Debut platform for
  `emoji_grid()` (live cell mutation), `auto_register_participants`,
  `@computed` aggregations, `user_guild` scope, and cross-view reactivity
  through ephemeral fleet panels.
- **`v2_tictactoe.py`** -- configurable board size (3x3 to 5x5) with the
  `button_grid()` debut, configurable win length (minimum corrected to 3),
  and per-player scoped stats via `user_guild` scope.
- **`v2_lobby.py`** -- open-join Werewolf-style flow keeping
  `auto_register_participants=False` for the documented exception case.
- **`v2_computed.py`** -- memoized derived state demo using the `@computed`
  registry.

### Project

- Test suite expanded to **1509 tests**, all green, covering the new
  view-layer decomposition, persistence rebuild, computed selectors,
  scoped state accessors, refresh throttling, and the multi-user
  participant model.
- Python 3.10, 3.11, 3.12, 3.14 support continues; discord.py 2.7+ remains
  the only runtime dependency.

---

## Pre-stable history

The releases below predate the 3.0.0 stable cut. They are kept for
historical reference. New users should pin to 3.0.0 or later.

---

## [2.1.0] - 2026-04-05

### Added

- **`allowed_users` on `_StatefulMixin`** -- `Optional[Set[int]]` attribute that overrides
  `owner_only` when set. Provides declarative access control for multi-user views (games,
  polls, collaborative tools). Supports runtime mutation for "join this game" patterns.
  Precedence: `allowed_users` > `owner_only` > allow all. Reuses `owner_only_message`
  for rejections.

- **Participant-aware sessions** -- `register_participant(user_id)` and
  `unregister_participant(user_id)` on views. Participants get their own scope keys in
  the session index so that `session_limit` applies to all users in a multi-user view,
  not just the owner. Always-reject policy for participants (never exits someone else's
  view to make room). `_enforce_session_limit` now only replaces views owned by the
  same user. `SessionLimitError` gains optional `blocked_user_id` for cog-level error
  handling. Participants propagate on push/pop, cleared on replace. Scope key deduplication
  for guild/global scopes prevents duplicate index entries.

- **TicTacToe example** (`examples/v2_tictactoe.py`) -- two-player V2 game demonstrating
  challenge acceptance flow, dynamic board size (3x3 to 5x5), configurable win length
  (e.g. 3-in-a-row on a 5x5 board), Discord mentions, mutual rematch agreement,
  participant session integration, forfeit tracking, and a custom game statistics reducer.

### Changed

- **`card()` accepts mixed children** -- `card()` now takes `*children` instead of a
  separate `title` parameter. Strings anywhere in the argument list are automatically
  wrapped in `TextDisplay`. This allows building a list of components and unpacking it
  (`card(*items)`) without worrying about whether the first item is a string or
  `TextDisplay`. Fully backward compatible with existing `card("## Title", ...)` usage.

- **`_enforce_session_limit` replace policy refinement** -- under replace policy, only
  views owned by the current user are replaceable. Views where the user is a participant
  (owned by someone else) are never replaced. Previously irrelevant because participant
  scope keys didn't exist; now load-bearing with participant sessions.

- **Documentation updates** -- API reference covers `allowed_users`, `register_participant`,
  `unregister_participant`, and `SessionLimitError.blocked_user_id`. Views guide adds
  "Multi-User Access Control" and "Participants and Multi-User Views" sections. Examples
  page includes TicTacToe. README adds Developer Tools showcase and multi-user feature bullet.

### Removed

- **Unnecessary `on_state_changed` stubs** -- removed ~14 redundant `pass` overrides
  from library pattern views (`TabView`, `WizardView`, `TabLayoutView`, `WizardLayoutView`)
  and examples. The base class `_StatefulMixin.on_state_changed()` is already a no-op.

---

## [2.0.0] - 2026-03-31

### Added

- **V2 component system support** -- full support for Discord's V2 message components
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
    - `PaginatedLayoutView` -- pages as V2 component trees with the same nav controls,
      go-to-page modal, `from_data()` factory, and `refresh_data()` as V1
    - `FormLayoutView` -- Container+TextDisplay display with the same validation pipeline
    - `TabLayoutView` -- button-based tab switching with async builders
    - `WizardLayoutView` -- multi-step with back/next navigation, step validators,
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

- **`_freeze_components()`** on `_StatefulMixin` -- single method that disables all
  interactive items, handling both V1 flat children and V2 recursive tree traversal.

- **Cross-view reactivity documentation** in the state management guide, explaining
  `dispatch()` vs `dispatch_scoped()` for multi-view UIs.

- **V2 examples:** `v2_counter.py`, `v2_dashboard.py`, `v2_settings.py`,
  `v2_form.py`, `v2_pagination.py`, `v2_wizard.py`, `v2_persistence.py`  -- 
  covering all V2 patterns with session limiting and V2 helpers throughout.

- **`rebuild=` callback on `push()`/`pop()`** -- optional kwarg that auto-defers
  the interaction, calls the callback with the new view, and edits the message.
  V2: `rebuild=lambda v: v.build_ui()`. V1: `rebuild=lambda v: {"embed": v.build_embed()}`.

- **Interaction serialization** (`serialize_interactions = True` by default)  -- 
  `asyncio.Lock` in `_scheduled_task` serializes rapid button clicks, preventing
  racing `message.edit()` calls. Auto-defer runs outside the lock.

- **`slugify()` utility** for converting display strings to safe `custom_id` fragments.

- **DevTools V2 rebuild:** `InspectorView` is now a `TabLayoutView` with 5 tabs
  (Overview, Views, Sessions, History, Config), self-filtering, and live auto-refresh
  via `VIEW_CREATED`/`VIEW_DESTROYED` subscriptions.

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
  reference to pushed/popped views so `on_state_changed()` can edit the message
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
- **Unidirectional data flow**: action -> middleware -> reducer -> state -> UI
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
