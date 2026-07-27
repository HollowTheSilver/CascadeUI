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

## [3.8.0] - 2026-07-27

### Breaking

- **`MenuLayoutView._build_header()` / `_build_footer()` removed.** The
  underscore names were deprecated in v3.4.1 and delegated to the public
  `build_header()` / `build_footer()` since. Rename any remaining overrides;
  the public hooks take the same arguments and return the same shapes.
- **`refresh()` rejects edit kwargs that only some of its endpoints accept.**
  `refresh()` picks between the interaction, webhook, and channel endpoints at
  runtime, and `suppress`, `suppress_embeds`, and `delete_after` are each
  accepted by only one or two of them, so a call carrying one worked only
  while the ack race kept landing on a compatible path. All now raise a
  `TypeError` naming the portable set (`content`, `embed`, `embeds`,
  `attachments`, `allowed_mentions`). Edit `view.message` directly for a
  one-off that needs a non-portable field. The same guard applies to the dict
  a `rebuild=` callback returns.

### Added

- **`allowed_mentions` on views.** A class attribute governing mention
  behavior for a view's own message, plus an `allowed_mentions=` parameter on
  `send()` that overrides it per message. Applies to the initial send and to
  every refresh and navigation edit, so a panel that re-renders cannot re-ping
  the users it names. `None` (the default) defers to the bot's client-level
  rules. Reach for it on rosters, leaderboards, and turn announcements; pass
  the parameter when a one-off notice genuinely should notify.
- **`custom_id` on the interactive V2 builders.** `button_row`,
  `confirm_section`, `cycle_button`, `toggle_button`, and `tab_nav` accept a
  base id, suffixed per button (`{custom_id}_0`, `{custom_id}_confirm`).
  Without one, builder-produced buttons carry ids that change every restart,
  which made all five unusable inside a `PersistentLayoutView`.
- **`image_section()` accepts extra text lines.** `image_section(primary,
  secondary, url=...)` renders a two-line entry beside its thumbnail, the
  shape leaderboard rows previously hand-built a `Section` for. Discord's
  three-children cap is enforced at construction.
- **`link_section(disabled=...)`, `gallery(spoilers=...)`, and
  `stats_card(spoiler=...)`**, matching the parameters their sibling builders
  already took.
- **`PaginatedRegion.show_page()`.** The async jump-and-re-render counterpart
  to the view-side `set_page()`. The region's own `set_page()` stays
  synchronous and render-free for the pre-attach restore path.
- **`DynamicPersistentButton.open_modal()`.** The sibling of the button's
  `respond()` for the one response type that cannot follow a defer. A
  persistent button's click arms an ack backstop before `on_click` runs, so
  opening a modal directly raced it; both now share the view side's guard.
- **`StateStore.scope_key(scope, *, user_id=None, guild_id=None)`.** The single
  writer of the scope-key format, returning `None` when the scope's required ids
  are missing. The scoped-state readers, the instance-limit index, and the sync
  availability pre-check all route through it, so the format is defined once. `0`
  is a legitimate Discord id and is never treated as absent; a caller that wants
  falsy ids treated as missing normalizes with `or None` first.
- **`view.validate()`.** Raises if the view's tree is one Discord would
  reject, running the same custom_id and placement checks the library runs
  before every send, plus the auto-generated-id check on a persistent view
  (the one that catches a panel coming back with dead buttons after a
  restart). The point is testing a view with no Discord connection: compose
  with `on_load()` (or `build_ui()`), count with `walk_children()`, and
  validate, all offline. Previously the first and third steps had no public
  entry, so a test asserting a panel still builds had to reach into private
  methods.
- **A warning when a navigation step changes `exit_policy`.** The policy is
  declared per class but push and pop edit one message in place, so a stack
  whose screens disagree tears the same message down differently depending on
  how deep the user went. Each class is individually valid, so nothing at
  definition time can see it. Declare the policy on a shared base when every
  screen should agree; a destination that deliberately differs still works.

### Changed

- **`LeaderboardLayoutView` suppresses mentions by default.** Entries without
  a `display_name` render as `<@id>`, so a stock leaderboard notified every
  ranked player on send and again on every refresh. Rows still render as
  mention links; they no longer ping. Override `allowed_mentions` on a board
  that wants the notification.
- **The lobby, battleship, and tic-tac-toe examples suppress their own
  mentions.** Each renders player mentions in a body that rebuilds on every
  interaction, so a full game re-notified both players once per move.
- **Button labels and emoji are validated at class-definition time.** The
  paginated, wizard, and form patterns already checked the `*_style` member of
  each button triple; the `*_label` and `*_emoji` members are now checked too,
  matching what `PaginatedRegion` enforced.
- **`nav_rebuild` is validated at class-definition time.** A bare function or
  lambda assigned in a class body binds as a method and receives `self` where
  the destination view belongs, which previously surfaced as an arity
  `TypeError` at navigation. It now raises at import naming the
  `staticmethod(...)` fix. `functools.partial` is rejected on every supported
  Python rather than only where it behaves as a descriptor.
- **The navigation example adopts `nav_rebuild`.** It repeated an identical
  rebuild callback at six push and pop call sites; the destinations now name
  their own once, and one call site keeps an explicit `rebuild=` to show the
  override winning.

### Fixed

- **`exit_policy` had no effect on the exit controls the library ships.**
  `make_exit_button`, `add_exit_button`, and `make_nav_row` each passed
  `delete_message=False` explicitly, which is the one value that stops `exit()`
  from consulting the policy, so a view declaring `exit_policy = "delete"`
  froze on close instead of deleting, with nothing to indicate why. All three
  now default to `None` and defer to the policy; an explicit `True` or `False`
  still wins. A view left at the `"disable"` default is unaffected.
- **`StatefulLayoutView.add_exit_button()` silently swallowed unknown
  keywords**, including typos and the V1-only `row=`. The signature is closed
  now, so a wrong argument raises at the call site.
- **`exit_policy` was documented as governing `on_timeout` and the exit-button
  helpers.** It never governed the former and no longer fails to govern the
  latter. A timed-out view freezes regardless of the policy, because an expiry
  is not a close gesture; the guides say so, and `on_timeout` is the override
  seam for deleting instead.
- **Persistent views accepted components whose ids change on restart.**
  `_stabilize_custom_ids` anchors rewritten ids on the view instance, and the
  rewrite cleared both signals `_validate_custom_ids` used to recognize a
  missing id, so a panel built from V2 builders passed validation, shipped,
  and came back with dead buttons after the next restart. The rewrite is now
  marked and rejected at validation.
- **Custom-id stabilization stamped ids onto display components.** Every
  `discord.ui.Item` sets the attribute the walk was gated on, so
  `TextDisplay`, `Container`, `Separator`, `ActionRow`, and `Section` were all
  being rewritten. The walk discriminates by type instead.
- **A composed `custom_id` could exceed Discord's 100-character cap.** The
  builders append a suffix to the caller's base, and the composites and the
  id stabilizer compose their own, so a legal input could be pushed over the
  limit by a string the caller never sees. The builders reject at
  construction naming the length to trim to, the stabilizer folds the
  overflow into a digest (it runs inside every `build_ui` and must not
  raise), and the placement validator backstops every remaining path
  including hand-built ids.
- **An empty `TextDisplay` failed the entire message.** Discord requires
  non-empty `content` and rejects the whole payload, so a formatter hook
  returning `""` for one entry took down every component beside it. The
  condition is data-triggered: a leaderboard renders for nine players and
  fails when a tenth arrives whose optional fields all happen to be absent.
  `image_section()` now skips empty lines (a Section with one text child and
  an accessory is legal, so the entry still renders), the leaderboard's
  no-avatar fallback skips an empty half instead of joining it into a
  dangling newline, and the placement validator rejects empty content
  pre-flight for hand-assembled trees. The validator previously enforced
  TextDisplay's upper bound and every container's lower bound, so the empty
  case was the one gap in an otherwise symmetric set.
- **An empty `SelectOption` label or value failed the whole select**, the same
  shape and the same data-triggered trigger. Both are rejected pre-flight now.
- **`card`, `action_section`, and `toggle_section` accepted an empty string**
  and shipped the same doomed `TextDisplay`. All three reject at construction
  now, naming the builder and the parameter, which points at the formatter
  that produced the blank rather than at the primitive that carried it.
- **A `MenuLayoutView` category without a `description` could not be sent.**
  The field is optional, but the empty string reached a `Section`, which
  Discord rejects for having no text. The category label renders as the
  section text when no description is given.
- **`stats_card` with an empty title rendered a bare `##`.** It now renders no
  heading at all, matching how the leaderboard's `build_title` treats one.
- **Modal titles and input labels over Discord's 45-character cap, or empty,
  failed at modal-open.** Both are stored unchecked by discord.py, so a label
  built from a schema field or a record name failed only for the data that
  happened to be long. Both are now construction-time `ValueError`s, and the
  form's grouped edit label (which doubles as the modal title) falls back to
  its generic form rather than composing a title the caller never typed.
- **Refreshes on the channel endpoint ignored mention rules.** `Message.edit`
  forwards the client-level `AllowedMentions` only when `content` is
  supplied, which a V2 view never does, so the same view shipped a different
  mention payload depending on which of the three endpoints the runtime
  chose, and a bot that configured suppression globally got it on send and
  lost it on any refresh that took the channel path. All three endpoints now
  agree, falling back to the client's rules when neither the class attribute
  nor an explicit argument is set.
- **Teardown freezes ignored the view's own mention rules.** `on_timeout`,
  `exit()`'s V2 branch, the empty-stack back clear, and the reopen fallback
  each re-ship the same mention-bearing tree `refresh()` does, so the last
  edit a view ever made was the one edit that dropped its
  `allowed_mentions`.
- **`validate_placement` could not be validated on five V2 pattern classes.**
  Each pattern mixin extends the class-attribute tables by naming
  `_StatefulMixin` directly, and the mixin precedes the concrete V2 class in
  the MRO, so the entry `StatefulLayoutView` contributes was dropped, and
  `validate_placement = 0` silently disabled the pre-flight validator with no
  definition-time error. The tables are resolved across the whole MRO now.
- **`PersistenceManager.flush_all()` and `close()` were permanent no-ops.**
  The manager's middleware handle was never assigned, so the Inspector's
  "Flush to Disk" button, `/cascadeui flush`, and `/cascadeui reset` each
  reported a write that never reached the debounce buffers.
- **`InMemoryBackend.row_upsert_many` committed rows from a failed batch.**
  The SQL backends roll back; the reference implementation did not, and the
  middleware's retry re-enqueues the whole batch on failure.
- **`PersistenceSchemaError` was documented but never raised.** The
  schema-ahead-of-library and missing-migrator conditions raised
  `PersistenceInitError`, so the `except PersistenceSchemaError` handler the
  guide shows could not fire. Both now raise the documented type; it shares
  the `PersistenceError` base, so catching the family is unaffected.
- **`user_id = 0` disabled `owner_only` on a restored persistent view** and
  skipped its session re-derivation, the same falsy-versus-absent confusion
  `scope_key` above resolves.
- **A raising `on_instance_limit` override leaked the rejected view.** The
  rollback ran after the hook rather than in a `finally`, so an override that
  propagates (a documented shape, and what the default hook itself does when
  there is no interaction to answer on) left the view subscribed and never
  stopped, permanently under `timeout=None`.
- **A raising `on_page_changed` or `on_toggle` took down the render.** The V2
  composites awaited their post-event hooks bare, so a user override that
  raised left the cursor advanced and the display stale, with the click
  reported as failed. All five call sites across `PaginatedRegion` and
  `Collapsible` route through the same fire-and-forget wrapper the view
  patterns already used, logging the override's error and continuing.
- **`respond(delete_after=...)` raised on the followup path.** Only
  `send_message` takes the argument natively, so the same call failed whenever
  the ack backstop had already consumed the response slot. It works on both
  paths now.
- **`respond()` and `open_modal()` raced the same ack backstop.** Both read
  `interaction.response.is_done()` and then send, and the two are not atomic:
  a backstop armed outside the interaction lock can take the slot in between,
  so the check passing did not mean the send would. `open_modal()` then raised
  `InteractionResponded` out of the callback against a docstring promising a
  fallback instead. Both now catch it, and Discord's own report of the same
  race (HTTP 40060), and deliver through the followup path.
- **`reload()` rebuilt over an armed ephemeral refresh button.** Between the
  arming edit and the token cliff, an out-of-band reload replaced the button,
  and the armed flag then dropped every notification that could restore it.
- **`_safe_defer` propagated `InteractionResponded` on Python 3.10 and 3.11.**
  It is a sibling of `HTTPException`, not a subclass, so the handler could
  not see it; on those versions `wait_for` widens the window enough for the
  auto-defer timer to ack inside the call, turning a benign race into a
  failed navigation. The docstring already promised a failed ack is never
  propagated.
- **`RateLimited` escaped three handlers.** Also a sibling rather than a
  subclass: it could abort the post-send message re-fetch (leaving a
  successful send reported as a failure with no `_message`), bypass the
  `on_role_error` hook, and misroute a restore fetch into the never-retried
  `failed` bucket.
- **Twenty-two public names were unreachable from their own subpackage.**
  `from cascadeui import setup_logging` worked while
  `from cascadeui.utils import setup_logging` raised `ImportError`, for a name
  no less public than the thirteen that package did re-export. The same gap
  hid the grid, composite, and `choice_row` builders in `cascadeui.components`,
  `@computed` in `cascadeui.state`, and the roles views and typed schemas in
  `cascadeui.views`. Each subpackage now re-exports every public name defined
  under it, in both directions.
- **Corrected `with_error_boundary`, `RetryConfig`, and `safe_execute` in the
  API reference.** All three documented signatures that raise `TypeError` as
  written, and `with_error_boundary` was described as swallowing exceptions
  when it logs and re-raises.
- **The lobby example claimed a 10-per-Container cap** that does not exist.
  The only limit is the 40 components per message the library already enforces
  at `add_item`.
- **`ChannelSelect` documented its input coercion but not its callback value.**
  Where `RoleSelect` and `UserSelect` hand back full `Role` and `Member`
  objects, `ChannelSelect` yields discord.py's `AppCommandChannel` partials,
  which have no `permissions_for()`. Its `.permissions` attribute holds the
  invoking user's permissions rather than the bot's, so the obvious repair
  answers a different question and passes for the admin it should refuse. The
  docstring now names the callback type and the `resolve()` / `fetch()` idiom.
- **Ack diagnostics named the clock they measure against.** `elapsed_since`
  subtracts an interaction's Discord-derived creation time from the local
  clock, so the printed elapsed carries the host's skew. A host running
  behind reported under three seconds beside a message about a missed
  three-second deadline, sending the reader after the wrong cause. Discord
  enforces the window on its own clock, so only the log line changes.
- **The missed-ack warning omitted its likeliest cause.** It named event-loop
  congestion and slow pre-callback work, both of which point at the caller's
  own code. discord.py sleeps on an exhausted rate-limit bucket inside
  `HTTPClient.request`, so an earlier HTTP call can hold the interaction with
  nothing at the call site to show it. The message names that third cause now.

---

## [3.7.0] - 2026-07-21

### Added

- **`ack_first` view flag.** Opt-in per-view acknowledgement before the access
  checks and the callback run, for views whose callbacks do synchronous work
  that can stall the event loop. Trades the one-request acting-view refresh path
  for a guaranteed early ack.
- **`auto_defer_delay` is now a tunable on `Modal` and `DynamicPersistentButton`.**
  Both carry the ack-backstop delay views already have (default 2.5s), and both
  validate it at definition time, so a subclass setting it to a non-positive or
  non-numeric value fails when the class is defined.
- **`respond()` helpers for `DynamicPersistentButton` and `Modal`, plus a public
  `respond_safe`.** These auto-defer-armed surfaces now expose an `is_done()`-aware
  responder, so a reply sent from an `on_click`, a `Modal.on_submit` override, or a
  roles event hook (`on_role_assigned` and its siblings, via `respond_safe`) falls
  back to a followup instead of raising `InteractionResponded` when the ack timer
  has already fired.
- **`respond()` warns when it is handed a stateful view.** Passing a CascadeUI
  view as a raw `view=` kwarg skips that view's own `send()`, leaving it live but
  unregistered: invisible to the inspector, instance limits, and state cleanup,
  and its timeout later fires a destroy for a view that was never created. The
  warning names the `send()` call that replaces it.
- **`avatar_backfill` for section-mode leaderboards.** Opt-in flag that renders
  Discord default avatars immediately, then resolves the real avatars off the
  render path and reloads when they arrive, avoiding both a wrong-avatar first
  render and a per-row fetch storm on large guilds. Pairs with an overridable
  `resolve_avatar_urls` hook.
- **`LeaderboardLayoutView.bot` read-only property.** Exposes the client passed
  via `bot=` (or injected by `on_bind`) so a `resolve_avatar_urls` /
  `get_avatar_url` override resolves avatars through `self.bot` instead of
  reaching into a private attribute.
- **`FormView` and `FormLayoutView` expose `text_edit_modal_auto_defer_delay`.**
  The grouped text-edit modal these patterns build internally now takes its ack
  backstop from this class attribute (default 2.5s), so a form subclass with a
  slow async field validator can raise it without reconstructing the modal.
- **Public error setters on `FormView` / `FormLayoutView`.** `set_form_error(msg)`
  and `set_field_error(field_id, *msgs)` write the error state and re-render the
  form so it shows. They replace the previously documented approach of setting the
  private `_form_error` / `_field_errors` and calling `refresh()`, which set the
  state but never rendered it. Called from an `on_submit` override, they reject a
  cross-field rule and keep the form open (a per-field validator sees only its own
  field, so a constraint spanning fields belongs in `on_submit`).
- **`set_page(n)` on `PaginatedView` / `PaginatedLayoutView`.** Jumps to a
  zero-based page, clamps to range, fires `on_page_changed`, and re-renders.
  Setting `current_page` directly does not re-render; `set_page` is the supported
  cursor move.
- **`refresh_content()` on the tab and wizard patterns.** A public in-place
  re-render for a subclass callback that mutated data and needs the active tab or
  step redrawn without a re-fetch. On V1 it rebuilds the embed; on V2 it recomposes
  the tree.
- **`allow_reselect` on `choice_row`.** Single-select disables the active option
  by default (re-picking is a no-op); set `allow_reselect=True` to keep it
  clickable so a re-pick fires `on_select` again, for a control whose callback has
  a side effect beyond selection (re-opening the active option's editor). Ignored
  in multi-select, where active options already toggle.
- **Five public names now import from the package root.** `RetryConfig` (the
  config object for the `with_retry` decorator), `Transaction` (the raw-SQL
  transaction protocol a backend's `transaction()` returns), and `ColorScheme`,
  `FormatTemplate`, `JSONFormatter` (the `setup_logging` customization types)
  were reachable only via deep module paths; each is now exported from
  `cascadeui`.

### Changed

- **A component-less display view tears down without a wasted edit.** A static
  `DisplayLayoutView` (a card of text and images, no interactive components)
  previously shipped one no-op PATCH per teardown: `on_timeout` and
  `exit(delete_message=False)` froze the tree and re-sent an identical message.
  The freeze now reports how many components it disabled, and the cosmetic edit is
  skipped when nothing froze, so a posted-and-forgotten notice self-cleans for
  free. Views with interactive components are unaffected (the disable edit still
  ships). `exit()` is documented as the teardown seam: bare `stop()` cancels the
  timeout but leaves the view in the active-view registry.
- **Library logging is now non-blocking.** `setup_logging` routes console and
  file output through a `QueueHandler`/`QueueListener`, so a log call drops the
  record on an in-memory queue and a background thread performs the file/console
  I/O -- the event loop no longer blocks on logging, which matters most at DEBUG.
  For the built-in console and file formatters the output is byte-identical;
  levels, colours, file rotation, and the action stream are unchanged. A repeat
  `setup_logging()` call now reconfigures cleanly instead of double-logging, and
  `setup_logging(handler=...)` now also installs the action stream (the
  custom-handler path previously skipped it).

### Removed

- **The unused `AsyncLogger` class.** Its async-queue mechanism now lives in
  `setup_logging` (see Changed). `AsyncLogger` was never exported from the
  package root or `cascadeui.utils`; code that reached it via
  `cascadeui.utils.logging.AsyncLogger` should call `setup_logging()` instead.

### Fixed

- **Component clicks with slow access checks no longer expire under load.** When
  a view's `interaction_check` override does I/O (an uncached member or role
  lookup on a busy bot), the interaction could cross Discord's 3-second
  acknowledgement deadline before the click was acked, surfacing as an
  interaction-failed (10062) storm on later, unrelated buttons. The auto-defer
  safety net now arms before the access checks run, so a slow check keeps its
  ack backstop; a rejected check still shows its message via followup.
- **Modal submissions no longer expire under a slow access check, a slow
  validator, or a raising handler.** A modal acknowledged only inside
  `on_submit`, so a slow `interaction_check` override (which Discord runs before
  `on_submit`) or a slow validator could cross the 3-second deadline, and a
  validator or callback that raised left the interaction unacknowledged
  entirely. The modal now arms its ack backstop across the whole submission
  dispatch, and a validation-error message routes through the followup when the
  ack has already landed.
- **Role panels and other dynamic buttons acknowledge before their handler
  runs.** A `DynamicPersistentButton` click (role-reaction panels included)
  ran its handler, the role add/remove requests and all, with no acknowledgement
  timer, so a slow mutation on a busy panel dropped the click. Dynamic buttons
  now arm an ack timer before the handler.
- **Views sent from a slash command survive a slow preload.** A view's `on_load`
  and setup ran on the interaction clock during `send()`, so a slow preload
  could expire the interaction before the send acked it. A send-scoped ack timer
  now covers the pre-send work.
- **Owner-only DevTools commands defer before bulk work.** The `/cascadeui`
  commands that do bulk or I/O work (`exit`, `exitall`, `flush`, `purge`,
  `reset`) could exceed the acknowledgement deadline on a busy store; they now
  defer up front.
- **Missed acknowledgements are diagnosable.** When the auto-defer timer's own
  ack fails because the interaction already expired, it logs at warning with the
  elapsed time since the interaction was created (the sign of a congested event
  loop) instead of a silent debug line. A view whose `on_state_changed` override
  overruns the ack budget while an interaction is in flight now warns once per
  class, matching the existing
  `on_load` warning, and the persistent-view restart repaint logs its duration.
- **`auto_refresh_ephemeral` handoff survives push/pop navigation.** A
  long-lived ephemeral view lost its refresh handoff at the first `push()` or
  `pop()`, so a multi-step flow crossed the 900s webhook-token cliff with no
  "Continue Session" button and errored on a later, unrelated click. The arming
  deadline now rides the navigation chain and re-arms on the destination once
  the edit confirms.
- **Reopening an ephemeral view keeps its navigation identity and session.** A
  reopened view rebuilt from constructor kwargs alone, so it lost its
  post-construction selection, navigation stack, reopen factory, and undo/redo
  timeline, and its `Back` button degenerated. It now carries all of these,
  rejoins its original session so `shared_data` survives the swap, and no longer
  deletes its own attached child views while reopening.
- **A non-ephemeral `send()` clears a stale ephemeral flag.** A view instance
  reused for a public send after an ephemeral one kept its ephemeral marker,
  which routed its refreshes through a slower edit path and misread real
  permission errors as token expiry.
- **Attached child views survive navigation.** A parent view orphaned its
  attached children the moment it navigated, and a child that navigated escaped
  its parent's cleanup. Children now re-parent onto the destination across
  `push()` and `pop()`.
- **Pushing a pre-constructed view instance binds the acting interaction.** A
  later navigation from a `push(instance)` destination that fell back to the
  stored interaction took a no-edit path; the instance now binds the acting
  interaction the same way the class path does.
- **Late-imported `DynamicPersistentButton` subclasses recover via
  `reattach()`.** A dynamic button defined in a cog loaded after
  `setup_middleware` was never wired into dispatch, so its clicks silently never
  routed. `reattach()` now re-drives dynamic-item registration, matching the
  recovery already available to late-imported persistent views.
- **Reattached persistent views restore their original session.** Restart
  re-derived a suffix-free session key, so two panels of the same class opened by
  the same user collided on one session. The `session_id` captured at
  registration is now restored, keeping each view's original session identity.
- **`reload()` re-renders on the V1 tab, wizard, form, and paginated patterns.**
  It shipped an edit with no `embed`, so a V1 `reload()` re-fetched via `on_load`
  but never updated the visible embed. Each V1 pattern now routes `reload()`'s
  render step through its embed-carrying rebuild.
- **Form controls route through the stateful callback wrapper.** Every select,
  boolean button, text-edit button, and submit button on `FormView` and
  `FormLayoutView` had its callback replaced after construction, which dropped
  the component-interaction dispatch (form clicks never reached the action
  history or the inspector) and left the acting interaction unbound, so each
  field change paid two Discord round-trips instead of one.
- **`choice_row` in dropdown form honors its no-op re-pick contract.** Its
  docstring states that re-picking the active option is a no-op, and the button
  form disables the active option to enforce that; the dropdown form (6 or more
  options) fired `on_select` on a re-pick anyway, so the control's behavior
  flipped at `button_threshold`. The dropdown now swallows a single-select
  re-pick of the active value unless `allow_reselect=True` (see Added).

---

## [3.6.0] - 2026-07-17

### Breaking

- **`refresh_cooldown_ms` no longer throttles interaction-driven edits.** It
  paces the re-renders the library starts (a panel reloading on its own) and
  exempts edits made in direct answer to a click on the view's own message. It
  was never a spam guard: it is view-wide, so it slowed every viewer of a shared
  panel for one person's clicking, and on a panel that also reloads out of band
  the window stayed armed, taxing page turns by up to its full length.
  Migration: to throttle one control per clicker, wrap it with `with_cooldown`,
  which now survives the component being rebuilt. `reload()` is unchanged and
  still defers its `on_load()` fetch inside the window: that gate protects
  your data source, not Discord. The two throttles are now documented together,
  keyed on the question being asked, and `v2_battleship` demonstrates the
  button-level guard.
- **`TabLayoutView`, `WizardLayoutView`, and `PaginatedLayoutView` build their
  content in `on_load()`.** Their `send()` overrides are gone. A subclass that
  overrides `on_load()` must now call `super().on_load()`, or a tab or wizard
  renders its nav row above nothing and a cursor-mode paginator never leaves
  "Loading...". Subclasses that override neither are unaffected.

### Added

- **`get_nav_state()` / `restore_nav_state()`.** A view can name the state that
  should survive a `pop()`. Reconstruction replays constructor kwargs and
  re-runs `on_load()`, so data returns fresh, but anything selected *since*
  construction was never a kwarg and reverted to its default, silently, with the
  next write landing on a row the user never opened. `restore_nav_state()` runs
  before `on_load()`, so a preload reads the restored selection rather than
  fetching twice to correct it. The mapping rides the navigation stack and is
  never serialized, so it may hold live objects. Defaults to `{}`. The built-in
  patterns name their own cursors, so their fix below is automatic; the V2
  composites do not, by design: a `PaginatedRegion`'s page and a
  `Collapsible`'s expanded state are the host's to name. The guide now states
  the reconstruction boundary: what returns fresh, what reverts, and which of
  `get_nav_state()`, `shared_data`, or `rebuild=` carries which.
- **`nav_rebuild`.** The rebuild a view supplies for its own navigation
  edits, used whenever the caller passes none. A V2 view is its component tree
  and needs nothing here. A V1 view's content is its embed, and `pop()` has no
  `rebuild=` to pass, so a V1 view names its own with
  `nav_rebuild = staticmethod(lambda v: {"embed": v.build_embed()})`. An
  explicit `rebuild=` still wins.
- **`with_cooldown(key=...)`.** Names the deadline a component reads, for
  controls a name cannot tell apart. The default reads the `custom_id` when you
  set one and the wrapped callback's qualified name otherwise; buttons built in
  a loop share their factory's name, so those take a `custom_id=` or a `key=`.
  A rejected click checks whether the controls sharing that name call
  different callbacks, and logs one warning per view class when they do, so a
  loop that needed a name says so instead of throttling silently. `seconds` is
  typed `float`: sub-second guards always worked, and the rejection notice
  already reported the remainder to one decimal, but the annotation said
  otherwise.
- **`is_snowflake(value)`.** Reports whether an integer is shaped like a real
  Discord ID, by decoding the creation time Discord packs into it. A database
  row ID or a match number leaves those bits near zero and decodes to Discord's
  epoch day, so it fails. Sits beside `coerce_snowflake_id`, which accepts any
  integer by design.
- **Six names that were public but unreachable now import from the package
  root.** `is_snowflake`, `coerce_snowflake_id`, `coerce_snowflake_id_set`, and
  `coerce_snowflake_match` (coercion was the one utility module the root never
  re-exported), plus `Action`, which pairs with the already-exported `StateData`
  in every reducer signature, and `StatefulComponent`. Each was already public
  in its own subpackage, so the subpackage paths keep working.
- **`secret` form field option.** A field marked `secret=True` masks its value
  in the form display as fixed dots, for a password or token. Discord modals
  cannot mask the input itself, so this covers the form's own display; the
  entered value still reaches `on_submit` and validators normally.

### Changed

- **A rate-limited edit is retried instead of dropped.** Every 429 seam stamped
  the backoff window and returned, leaving nothing to ship the edit; views
  recovered only on their next state change, and an armed ephemeral view (which
  drops notifications by design) never recovered at all. The backoff now
  queues the edit to ship at the window boundary, through the same single task
  the cooldown uses, and a retry that is rate-limited again schedules its own
  successor.

### Fixed

- **A throttled refresh was dropped, not delayed, on views without `build_ui`.**
  The deferred render delegated to `on_state_changed`, whose default did nothing
  when there was no `build_ui` to run, so tab switches and wizard steps on
  `TabView` / `TabLayoutView` / `WizardView` / `WizardLayoutView` were swallowed
  outright. Not opt-in: the 429 backoff engages this path on any view.
- **The 429 backoff always waited exactly one second.** It read a `retry_after`
  attribute that `discord.HTTPException` does not carry, so every rate-limit fell
  through to the fallback. The delay now comes from the `Retry-After` header, and
  a header-less 429 (which discord.py raises only for a Cloudflare ban) backs
  off minutes rather than knocking once a second on an IP block.
- **`discord.RateLimited` escaped every edit seam.** It subclasses
  `DiscordException`, not `HTTPException`, so `except discord.HTTPException`
  never caught it, and it is the only rate-limit type that carries a real
  `retry_after`. Now handled wherever a 429 is.
- **`refresh_cooldown_ms` could defeat the ephemeral refresh handoff.** The
  arming edit queued behind the cooldown window and the deferred render then
  rebuilt over the refresh button, leaving a frozen panel with no way back and
  no notification able to repair it.
- **A refresh was lost when its backoff window grew mid-flight.** The waiting
  task woke, found the window still active, saw itself registered as the pending
  retry, declined to schedule a replacement, and cleared the last reference to
  itself on the way out.
- **A deferred render answered to the click that spawned it.** Task creation
  copies the caller's context, so a background render read a spent interaction
  and edited through a response slot whose window had closed.
- **A theme change did not reach views whose selector tracks their own
  content.** `state_selector` decides whether a view re-renders, and a theme
  is a render input that appears in no view's data, so a panel watching its
  own toggles went deaf to a theme switch and kept painting the old accent,
  while the panel that made the switch repainted. A theme resolved through
  `get_theme()` now rides the selector. A fixed theme yields a constant and
  notifies no more often than before; a view that sets its own colors rebuilds
  once and stops at the render hash, shipping no edit. `settings_menu` looked
  its theme up privately rather than returning it from `get_theme()`, so it
  now uses the override the theming guide teaches and `v2_settings` already
  used.
- **A V1 pattern's first message shipped no content.** `TabView`, `WizardView`,
  and `FormView` sent their controls over an empty body: V1 content is an embed,
  and only `PaginatedView` computed one in `send()`. The embed each pattern
  already builds for a `pop()` now renders the first message too, and `MenuView`
  supplies its `build_embed()` without the caller passing it. An explicit
  `embed=` or `content=` still wins.
- **A V1 wizard step with no builder froze the display.** The step advanced and
  the nav row relabelled, but the edit was skipped whenever the step contributed
  no embed, leaving the previous step on the message. The edit now ships either
  way, as it already did on V2.
- **A popped view lost its place.** `PaginatedView` / `PaginatedLayoutView` came
  back on page one, `TabView` / `TabLayoutView` on the first tab, `WizardView` /
  `WizardLayoutView` on step one (now snapped to a visible step), and
  `FormView` / `FormLayoutView` with the typed values gone.
- **A popped form came back with no fields at all.** `FormView` and
  `FormLayoutView` take their `__init__` from a plain mixin, and the kwargs
  capture only installed on classes declaring their own, so a form
  reconstructed by `pop()` had no fields, no title, and no schema.
- **A popped V1 view kept the child's embed.** The navigation edit shipped
  components alone unless the caller passed `rebuild=`, and `pop()` has nothing
  to pass: the back button is library code. V1 content lives in the embed, so
  the parent's buttons rendered over the child's content. Every V1 pattern
  (`PaginatedView`, `TabView`, `WizardView`, `FormView`, `MenuView`) now names
  its own edit through the new `nav_rebuild` class attribute, and any V1
  view can do the same.
- **A popped V1 tab or wizard kept the first tab's buttons.** The tab row and
  the wizard's Back button are built during `__init__` against the first
  tab/step, so restoring the cursor left the third tab's content under a row
  still highlighting the first, and step three's under a Back button disabled
  as though the user had never left step one.
- **Clearing an optional form select crashed the interaction.** An optional
  select lets Discord deliver an empty selection, and the callback read the
  first element without checking, so clearing a choice raised `IndexError` and
  the user saw "This interaction failed". An empty selection now clears the
  field to "not set".
- **A form revealed its errors one submit at a time.** A single field that
  failed to parse (an age outside its range, a malformed date), or a blank
  required field, short-circuited the submit, so validator errors on other
  fields (a bad email, a short password) stayed hidden until the first was
  fixed and the form resubmitted. Parse errors, blank-required errors, and
  validator errors now all surface together, with validators still skipping
  any field whose raw value never parsed and any field already flagged as
  required.
- **Changing one form field cleared every field's validation message.** A
  form showing errors wiped all of them the moment the user touched any other
  field, so picking a select or toggling a boolean erased still-accurate
  messages on unrelated fields. A field change now clears only that field's
  own error; the rest stay until they change or the next submit re-checks
  them.
- **A V1 form silently dropped fields past its row budget.** A form with more
  select and boolean fields than Discord's five action rows hold discarded the
  overflow with no error, leaving a required field unfillable. Construction now
  raises, naming the field that does not fit and pointing at `FormLayoutView`.
- **Two form fields with the same label crashed on the Edit click.** Modal input
  ids derive from the label, so two fields labelled alike collided when the
  modal opened. The clash is now rejected at construction, naming both fields.
- **A form field's `default` was ignored outside the modal.** A declared
  `default` reached only the modal prefill, so a required field with a default
  still displayed "not set" and blocked submission, and select defaults never
  rendered. Defaults now seed the form's values at construction.
- **A form's integer field could submit the raw text you typed.** When a typed
  field failed to parse, the raw string was written into the form's values, and
  any later field change cleared the error that blocked submission, so an
  integer field could submit `"twenty"`. Unparsed input is now held apart from
  the values; the modal still prefills what you typed, and the values carry only
  parsed results.
- **A rejected modal value was written back before validation.** `Modal`
  populated each input wrapper's `.value` before running validators, so a caller
  reading it after a failed submit saw the rejected input, contradicting the
  documented "populated after validation passes" contract. The write-back now
  follows validation.
- **A validator on an optional form field made it secretly required.** Most
  validators reject a blank value, and the runner ran them over every field, so
  an optional field left empty failed its own validator. Blank optional fields
  now skip validation; the required-check still owns the blank-required case,
  and `0` and `False` count as values, not blanks.
- **A validator that returned the wrong shape crashed far from the mistake.** A
  validator that forgot to return a `ValidationResult` raised a bare
  `AttributeError`, and an object with an async `__call__` (a database-backed
  uniqueness check, the documented awaitable shape) was called but never
  awaited. The runner now awaits any awaitable result and raises a directed
  `TypeError` naming the validator and field on a wrong return.
- **A form field written as a dict with no `type` rendered no control.** Typed
  `FormField` entries default to `text`, but a hand-written dict passed through
  untouched, so a field with no `type` had no input and, if required, left the
  form unsubmittable. A missing `type` now fills to `text` to match.
- **A numeric form field accepted `nan` and `inf`.** `float()` parses both, and
  a NaN compares False to every bound, so a field limited to 0-100 accepted a
  value that was neither, displayed "nan", and would have written invalid JSON
  on persistence. Float parsing and the `min_value` / `max_value` validators
  now reject non-finite numbers.
- **A menu category pointing at a pattern view crashed on click.** Both menus
  forced their own render shape onto every push: `MenuView` called
  `build_embed()` and `MenuLayoutView` called `build_ui()` on destinations
  that define neither, raising `AttributeError`. Every pattern was affected in
  both versions, so eight of the ten menu-to-pattern paths were dead. A
  destination that names its own `nav_rebuild` now keeps it, one that renders
  through another seam is left alone, and a plain view still takes the menu's.
- **A menu category naming a cross-version view failed on click.** A message
  carries its component version one way, so a V1 menu can never reach a V2
  destination. The category list names every destination at construction, but
  the mismatch waited for the push and reached the user as a dead button. Both
  menus now reject it where the category is declared.
- **Wrapping a component twice stacked the wrapper.** `with_loading_state`,
  `with_confirmation`, and `with_cooldown` each wrapped the previous callback, so
  a build method that wraps on every render nested without bound: three renders
  meant three confirmations for one click.
- **A `functools.partial` callback crashed every V2 render.** Stabilizing a
  component's `custom_id` reads the callback's qualified name, which a partial
  does not carry, so a button wired to one raised `AttributeError` inside
  `build_ui` unless it also passed an explicit `custom_id`.
- **`with_cooldown` did nothing on a rebuilt component.** Deadlines lived in the
  wrap call, and an accepted click triggers the rebuild that discards them, so
  the cooldown never fired. They now live on the owning view, named after the
  callback you wrote: each wrapper records that callback before burying it, so
  stacking `with_confirmation` or `with_loading_state` underneath does not
  collapse a view's controls onto one deadline.
- **A view that names an owner other than the clicker warned on every run.** The
  notice claimed an explicit `user_id` is re-derived from the interaction on
  push/pop and diverges after navigation; navigation carries the value forward
  unchanged, and naming an owner who is not the clicker is a supported shape the
  game examples are built on. The check ran before the subclass assigned
  `allowed_users`, so it could not tell that shape from an application id passed
  under a reserved name. It now runs at send and fires only when the id is not a
  Discord ID *and* the author it locks out is the one who built the view, so
  handing ownership to a real user (a game the challenger owns, an admin
  posting a panel for someone else) is silent.
- **The Inspector showed two clocks.** Actions stamped their time from the
  host's local zone while the Inspector's own stamps read UTC, so the History
  tab mixed both and a daylight-saving change rewound the action log for an
  hour. Every action now stamps UTC.
- **The 40-component error contradicted itself.** An oversized card added to an
  empty view reported that the limit was exceeded "already at 0 components". It
  now reports what the view holds, what the item adds, and the sum.
- **An oversized `TextDisplay` failed at Discord instead of pre-flight.** The V2
  placement validator enforced the MediaGallery item cap but skipped the
  `TextDisplay` 4000-character limit, so a body over that length passed
  validation and returned an opaque HTTP 400 at send, far from where it was
  built. The validator now rejects it at send, refresh, and navigation with a
  directed error naming the component.
- **Oversized Button labels, Select placeholders, and SelectOption text failed
  at Discord instead of pre-flight.** The same validator gap that skipped the
  `TextDisplay` cap also skipped these string limits: a Button `label` over 80
  characters, a Select `placeholder` over 150, or a SelectOption `label`,
  `value`, or `description` over 100 passed validation and returned an opaque
  HTTP 400 at send. The validator now rejects each at send, refresh, and
  navigation with a directed error naming the component.
- **The component wrapper reference had wrong signatures.** No wrapper has
  keyword-only parameters, all three accept any component (not just buttons),
  and `with_cooldown`'s `"user_guild"` scope was missing.
- **The `migrators=` bulk-registration form was missing from the persistence
  guide,** which documented only the decorator path.
- **The `@cascade_component` reference documented a registry that does not
  exist.** The decorator dispatches `COMPONENT_INTERACTION` from a view method
  rather than storing callbacks, so the `get_component()` pairing it showed
  returned `None` and its module-level snippet could not run. `register_component()`
  / `get_component()` now have their own entry as the V1 composition registry.
- **Reference gaps.** The landing page omitted the PostgreSQL backend, the
  examples index omitted `v2_attachments.py`, and the built-in theme objects had
  no entry naming them apart from the theme names they register under.

---

## [3.5.0] - 2026-07-11

### Breaking

- **`PersistenceManager.install_middleware()` removed.** The legacy
  manager-driven middleware install path is gone. Wire persistence through
  `setup_middleware(PersistenceMiddleware(...))` (the canonical path since
  v3.3.0). No consumer used the manager-driven topology in practice, but it was
  public API, so its removal is a breaking change.
- **`LeaderboardLayoutView.build_summary` removed.** Aggregate stats are now
  composed content rather than a dedicated hook: read the new `ranked_entries`
  property inside `build_header` and return a `stats_card(...)` (or any `card`).
  This collapses the overlapping "content above the rankings" hooks into one and
  drops the `dict`-vs-`Container` return-type ambiguity. Migration: a
  `build_summary(entries)` returning a stats dict or card becomes
  `build_header(page)` reading `self.ranked_entries` and returning a
  `stats_card`, gated on `page == 0` for a page-1-only Overview.

### Added

- **`PersistenceManager.reattach()`.** Re-drive persistent-view reattach after
  the initial pass, so a view class imported *after* `setup_middleware` (a cog
  loaded later) attaches without a restart: import order stops mattering.
  Idempotent: already-restored panels are skipped (no re-fetch, no double
  registration); transiently unreachable rows are retried. Call it once every
  cog has loaded.
- **Construction-time bound validation for selects and modal inputs.**
  `StatefulSelect` / `Dropdown` reject more than 25 options, and `CheckboxGroup`
  / `FileUpload` / `TextInput` reject out-of-range `min`/`max` bounds, with a
  directed `ValueError` at construction instead of a Discord HTTP 400 when the
  component ships or the modal opens.
- **`nav_divider` for in-card paginators.** With `nav_inside_container` on,
  `nav_divider = True` renders a divider between the page content and the
  in-card navigation row. Defaults to `False` (the flush look).
- **Built-in avatar resolution for Section-mode leaderboards.**
  `LeaderboardLayoutView` accepts a `bot=` kwarg and ships a default
  `get_avatar_url` that resolves each entry's avatar from the bot's user cache
  (a Discord default avatar on a cache miss), so section-mode boards no longer
  need a hand-rolled avatar hook. Without a `bot`, the default returns `None`
  and the two-line `TextDisplay` fallback is unchanged. The persistent variant
  receives the bot through `on_bind`.

### Changed

- **`reload()` respects the refresh throttle.** A `reload()` inside an active
  `refresh_cooldown_ms` window (or a 429 backoff) now defers the whole reload,
  `on_load`'s fetch included, and a burst collapses to one fetch + edit at the
  window boundary with fresh data. Previously only the edit was throttled, so an
  out-of-band-driven panel ran a full `on_load` fetch per trigger. A coalesced
  reload replays its keyword arguments at the boundary, so a subclass reload
  keyword (e.g. `force`) survives the defer when forwarded to
  `super().reload(**kwargs)`.
- **`LeaderboardLayoutView.build_footer` placement is now return-type-driven.**
  A raw component (a caption, link row, or image) folds inside the rankings card
  below the entries and stays attached to them, while a `Container` (`card(...)`)
  renders as its own standalone card below the rankings. Previously the default
  sibling layout floated a raw footer as an orphaned line between the card and
  the nav row.

### Removed

- Dead internals: the unused `_add_page_content` shim on `PaginatedLayoutView`
  and the unused `ErrorBoundary` utility class. Internal-only, no public surface.

### Fixed

- **Persistence flush no longer drops a re-registered view on a retry.** A
  re-register racing a failed unregister flush could leave the key queued for
  both write and delete. The next flush then deleted it, so a re-registered
  persistent view failed to reattach after restart. The retry re-enqueue now
  preserves the single-buffer-per-key routing invariant.
- **`APPLICATION_SLOTS_PRUNED` payload is consistent.** The TTL sweep and manual
  prune dispatched disagreeing keys (`reason` vs `cutoff`); both now emit a
  consistent `{deleted, cutoff}` shape via `ActionCreators`.
- **Paginated nav buttons no longer go stale on an out-of-band reload.** A view
  reloaded while parked past the first page redrew its Prev/Next/First/Last
  buttons in page-one states over later-page content; button state now derives
  from the current page in every render path.
- **Paginated jump buttons rebuild on a data refresh that crosses
  `jump_threshold`.** `refresh_data` / `refresh_pages` changing the page count
  across the threshold now add or remove the first/last/go-to buttons, not just
  re-sync their disabled state (V1 and V2).
- **In-card paginators and leaderboard footers coexist.** With
  `nav_inside_container` on, a `build_footer` frame renders inside the card
  above the pager instead of forcing the nav row out.
- **A raising `on_page_changed` / `on_tab_switched` override no longer desyncs
  the view.** These post-navigation hooks are now fire-and-forget (logged,
  swallowed) like their wizard/form siblings, so the page turn / tab switch and
  its refresh always complete.
- **Placement validation runs after `seed_initial_state`.** A view that builds
  its component tree inside `seed_initial_state` (rather than `__init__` /
  `build_ui`) no longer fails to send with a "no top-level components" error:
  the pre-flight placement check now validates the tree the hook produced, not
  the empty pre-seed tree.
- **`on_timeout` frees the instance-limit slot before its cosmetic edit,**
  mirroring `exit()`: a stalled timeout edit no longer holds the slot for up
  to `edit_timeout`.
- **`on_unauthorized` routes through `respond()`,** covering the
  already-acknowledged interaction case its sibling response hooks already
  handled.
- **`with_confirmation`'s prompt disables its buttons on timeout** instead of
  leaving live-looking buttons that error on a late click; `with_loading_state`
  logs its swallowed edit failures at debug.

---

## [3.4.1] - 2026-07-08

### Added

- **Leaderboard page-frame hooks.** `build_header(page)` and
  `build_footer(page)` on the leaderboard pattern place optional components
  above the summary card and below the rankings card (above the navigation
  row): the seam for a banner image or an attribution footer. Both default to
  `None`, accept a single component or a list, and receive the zero-based page
  index so a frame can target only the first page. `v2_leaderboard`
  demonstrates both.
- **Menu header/footer hooks are now public.** `MenuLayoutView.build_header()`
  and `build_footer()` replace the underscore-prefixed pair, matching the
  leaderboard's page-frame hook naming.
- **Structured modal forms guide.** The components guide teaches the modal
  input types and the edit-in-place defaults pattern, with `v2_wizard` as the
  live reference.
- **Option-count validation on modal groups.** `CheckboxGroup` enforces
  Discord's 1-10 option bound and `RadioGroup` its 2-10 bound with a directed
  `ValueError` at construction, replacing a silent acceptance that failed as
  an HTTP 400 when the modal opened.
- **Leaderboard card masthead.** A `banner` attribute (class attribute or
  `banner=` kwarg; URL string, `discord.File`, or anything with a string
  `.url` such as `guild.icon`) renders a full-width image at the top of the
  rankings card, above the title heading when both are set. `title=None` (or
  an empty string) now renders no text heading instead of a stray `## `, so
  banner-only and heading-free cards are declarative -- with an empty
  masthead the title divider is skipped. A `build_title(page)` hook replaces
  the whole masthead for dynamic composition, same shape as
  `build_header` / `build_footer`.

### Changed

- **`v2_wizard` example redesigned.** Every step now renders a live
  character-sheet preview (with a name-seeded portrait) and folds its controls
  into cards via `choice_row` and `action_section` instead of stacked bare
  selects. Also resolves a bug in the prior version where the Background step
  could not be advanced. The name and background modals now demonstrate the
  structured modal inputs: a `FileUpload` portrait that replaces the preview
  image, plus `RadioGroup`, `CheckboxGroup`, and `Checkbox` in one form.
- **`v2_lobby` example redesigned.** The lobby renders as one status-colored
  card: a host-avatar header, slot-fill progress bar, a roster that shows its
  open slots, and the control row inside the card.
- **Game-example challenge prompts enriched.** The `v2_tictactoe` and
  `v2_battleship` challenge cards gain an opponent-avatar header, the game
  config as a `key_value` block, and a caption naming who can respond and
  when the prompt expires. The tictactoe board legend now also shows the win
  condition, which was invisible during play on custom-size boards.
- The 40-component-limit error now also suggests trimming content nodes, not
  only compacting the pager row, since page content is the more common cause.
- **`v2_settings` example teaches the dynamic-theme idiom.** The hub and
  appearance page override `get_theme()` to read the user's selected theme
  from scoped state, replacing per-card color plumbing -- every card follows
  a theme switch automatically.

### Deprecated

- **`MenuLayoutView._build_header()` / `_build_footer()`.** Superseded by the
  public `build_header()` / `build_footer()`. Existing overrides keep
  rendering (the public hooks delegate to them) and a `DeprecationWarning`
  fires at class definition; migrate by dropping the underscore.

### Fixed

- **Empty V2 views now fail with a directed error.** `validate_placement`
  rejects a `LayoutView` with no top-level components at send, refresh, and
  navigation, replacing Discord's terse HTTP 400 for an empty components-v2
  message with a build-time `ValueError` that names the fix.
- **Empty modals now fail with a directed error.** `open_modal` rejects a modal
  with no components before it is sent, replacing a Discord HTTP 400.
- **Media-only rebuilds now re-render.** The render digest hashes media URLs
  (`MediaGallery` items, `Thumbnail`, `File`), so a rebuild that changes only
  an image (a regenerated banner, a new avatar) ships the edit instead of
  being skipped as an unchanged tree.
- **`nav_inside_container` no longer rejects mixed pages.** A page combining a
  `Container` with other top-level items (a rankings card plus a standalone
  summary card or frame components) previously raised a nested-Container
  placement error. Such pages now fall back to the sibling layout with the
  navigation row as a separate row; pages that can wrap still do.
- **README Hello World example runs as copied.** It was missing the initial
  `build_ui()` call in `__init__`, so a verbatim copy failed on first send.
  The quickstart now also states the first-render rule explicitly.
- **Stale modal-input limitation removed.** The known-limitations page claimed
  structured modal inputs (`Checkbox`, `CheckboxGroup`, `RadioGroup`) fail
  with a Discord 400, contradicting the component docs that teach them. The
  full serialize-and-submit loop verifies against discord.py 2.7.0 and 2.7.1,
  so the section is gone.
- **Theme accents apply everywhere, not only inside `build_ui()`.** Pattern
  render paths (leaderboard page builds, wizard steps, tab builders, paginated
  formatters, `on_load`) now run inside the view's theme context, and cards
  built without an explicit color re-resolve against the view's live theme at
  every render seam -- a card renders themed no matter where it was built, and
  a runtime theme switch restyles managed cards on the next refresh. The
  theme's `separator_spacing` style, documented but previously unread, now
  drives `divider()` / `gap()` spacing the same way. Explicit colors and
  spacing always win.

---

## [3.4.0] - 2026-07-01

### Added

- **`PaginatedRegion` composable pager.** A stateful composite component that
  pages one slice of a `StatefulLayoutView`'s tree while the host renders the
  rest -- the V2 sibling of the V1 `PaginationControls`. Set `items`, render
  `page_items`, and add `controls(self)` for the nav row inside the host's
  `build_ui()` or `on_load()`. Each instance holds its own page index, so two
  regions can share one view (give them distinct `key` values). Carries the
  `PaginatedLayoutView` navigation surface -- first/last jump buttons and a
  go-to-page modal past `jump_threshold` -- with the same class-attribute
  customization and `on_page_changed` hook. Fills the gap between a one-shot
  builder and a full paginated view. `control_buttons(self, compact=...)`
  returns the nav buttons unwrapped so a node-tight host fuses them into a row
  it owns alongside its own Back/Exit buttons; `compact=True` (also on
  `controls`) yields a three-button prev/go-to/next set that packs with Back +
  Exit inside one five-button row -- the shape a `per_page=1` carousel near the
  40-component cap needs.
- **`Collapsible` inline disclosure/expander.** A stateful composite where a
  trigger button toggles an inline region of revealed content -- a `choice_row`,
  a select, more buttons. The trigger relabels/restyles between collapsed and
  expanded states; `render(view)` returns the trigger collapsed or the trigger
  plus the revealed content expanded. Each instance holds its own state, so two
  collapsibles in one view stay independent. The host supplies a `reveal`
  callable and owns the collapse policy (`collapse()` after an action, or leave
  it open); an `on_toggle` hook fires on every open/close. Absorbs the
  hand-rolled "boolean flag + toggle callback + conditional render" disclosure
  idiom. An optional `summary` callable fuses the trigger into a Section beside
  a line of summary text (an `action_section`) instead of a bare button row --
  the shape a card-based disclosure wants; it falls back to the bare button when
  the callable yields nothing.
- **`choice_row` "pick one/any" control + `Choice` type.** A builder for a
  segmented choose-one control -- a row of buttons where the active option is
  highlighted and (single-select) disabled, all sharing one `on_select` -- that
  switches to a dropdown automatically when the option count outgrows a button
  row (6-25 options) and raises past 25. `multi=True` makes it a multi-select
  (toggle buttons, or a `max_values` dropdown). Accepts a `{label: value}` dict
  or a sequence of `Choice` (which adds per-option emoji and dropdown
  descriptions). The builder handles the string round-trip Discord forces on
  select option values, so `on_select` always receives the real Python value.
  `disabled=True` greys out the whole control (every button, or the dropdown)
  for a read-only state such as a locked or closed choice. Replaces the
  hand-rolled "style-if-active + disabled-if-current + per-option callback
  factory" idiom.
- **`action_section` / `toggle_section` accept `disabled=`.** Render a
  card-embedded action button greyed out and non-interactive instead of
  accepting the click and rejecting it inside the callback. Mirrors the raw
  `StatefulButton` API; default `False` keeps existing callers unchanged.
- **`MAX_SELECT_OPTIONS` constant** exported from the package root (Discord's
  25-option select cap). `choice_row` references it instead of the bare integer.
- **`on_load()` async preload hook + `reload()`.** Override `async def
  on_load(self)` to fetch from a database or other async source and build the
  view's tree against the result. The library calls it automatically before the
  initial send and before every push/pop edit, so navigating to a child or back
  to a parent re-reads its source (reload-on-render). `reload()` is the
  out-of-band convenience (`on_load` then `refresh`) for re-fetching inside a
  callback. Replaces the hand-rolled "sync `__init__` + async `load_and_build` +
  `rebuild=lambda v: v.load_and_build()`" idiom with one hook.
- **`on_pre_send(interaction)` hook** -- a pre-send veto gate on every view.
  Override it to run a permission or data check before the send; returning
  `False` aborts cleanly (no message, no state registered). It runs with the
  interaction response slot still open, so an override can respond to the user
  before the send without a forced `defer`. Demonstrated in the `v2_lobby` example.
- **`on_bind(bot)` hook on persistent views.** Inject non-serializable runtime
  dependencies -- a database pool, the bot, a service client -- that cannot ride
  the constructor through the persistence round-trip. The library calls it
  automatically during `send()` (when the bot is derivable from the context) and
  during restore, before `on_restore`. A non-serializable constructor kwarg now
  declines the registry write with a directed error naming the offending kwarg
  and pointing to `on_bind`, instead of a near-silent failure that dropped the
  view on the next restart. Demonstrated in the `v2_persistence` example.
- **`on_message_gone()` view hook.** Fires when an edit observes the message was deleted
  (a 404 from `refresh()`), so a consumer tracking the message in its own store can
  reconcile that reference. The edit-path counterpart to the gateway-driven
  `on_message_delete`.
- **`make_back_button()` and `make_nav_row()` navigation helpers.**
  `make_back_button()` returns an unattached Back button that pops the
  navigation stack (mirrors `make_exit_button()`). `make_nav_row()` (V2)
  returns one `ActionRow` combining Back and/or Exit, collapsing the common
  pushed-sub-view footer into a single call. With `on_load()` defined, Back
  reloads the restored parent automatically, so no rebuild callback is needed.
  `back_label` / `back_style` / `back_emoji` (and the `exit_` equivalents)
  customize each button, so a relabeled Back needs no manual composition.
- **Leaderboard out-of-band refresh is now a public call.** `LeaderboardLayoutView`
  builds its pages through `on_load()`, so the inherited `reload()` re-fetches
  entries and re-renders the message in one public call -- previously an
  out-of-band refresh needed `rebuild_pages()` plus reaching into the render
  internals. A pushed leaderboard also reloads its data automatically on
  navigation. Both short-circuits stay intact: an unchanged entry set skips the
  avatar re-fetch, and an unchanged tree skips the message edit.
- **`reload(force=True)` and `rebuild_pages(force=True)` on leaderboard views.** Force a
  page rebuild even when the entry signature is unchanged: for a filter or a select's
  highlighted option that changed without touching the row data. `reload(force=True)` is
  the out-of-band path (re-fetch, re-render, re-store); `rebuild_pages(force=True)` is the
  lower-level rebuild.
- **`emoji()` validator + `is_emoji()` utility.** Discord has no native emoji
  input, so `emoji()` (a sibling of `regex()` / `choices()`) gates a `Modal`
  `TextInput` on whether the value is a unicode emoji or a custom `<:name:id>`
  token; `is_emoji(text)` is the bare predicate, exported from
  `cascadeui.utils`. An emoji "picker" is then composition -- a `choice_row`
  of your emoji options plus a Custom option that opens a modal with the
  validator (recipe in the validation guide) -- not a dedicated widget.
- **Slow-`on_load` diagnostic.** When a view's `on_load()` preload overruns
  `auto_defer_delay` (default 2.5s), the library logs a one-per-class warning
  naming the view and the elapsed time. Exposes a render-path HTTP footgun
  (serial fetches competing with the interaction ack) that otherwise only shows
  up as a sluggish panel.
- **Construction warns when an explicit `user_id` diverges from the
  interaction user.** `user_id` is a framework-managed identity kwarg
  (re-derived from the interaction on push/pop), so passing a custom value
  silently diverges after navigation. Construction now logs a warning, once
  per view class, naming the fix: use a non-reserved kwarg name for
  application ids.
- **`DevToolsCog.is_owner_tool` marker.** The dev-tools cog now carries a
  readable `is_owner_tool = True` class attribute so a bot that routes
  owner-only cogs differently (for example, to a control guild) can detect it
  via `getattr(cog, "is_owner_tool", False)` instead of matching the class name.
  Any owner-gated cog can carry the same marker.
- **New example `v2_db_navigation.py`.** A repo-backed task list demonstrating
  the database-navigation pattern: a stable repo handle passed as a kwarg, rows
  loaded in `on_load()`, and reload-on-render through push/pop with `make_nav_row()`
  and no `rebuild=` callbacks. The row list is paged with a `PaginatedRegion`
  fed from `on_load()`.
- **Composition cookbook** in the components guide. A positive "what nests in
  what" reference for V2 layouts -- the nesting tree, what goes where, and
  worked examples for placing selects and collapsible regions inside cards.

### Changed

- **Persistence flush batches all dirty rows into one backend round-trip.** The
  middleware previously called `row_upsert` once per dirty row; it now calls the
  new `PersistenceBackend.row_upsert_many`, so the SQL backends collapse a flush
  into a single transaction (`executemany` + one commit) instead of N connection
  acquires and N commits. A custom backend that does not implement
  `row_upsert_many` keeps working -- the flush falls back to per-row
  `row_upsert`.
- **Persistent-view restore fetches concurrently at startup.** Reattaching
  persistent views on `setup_hook` previously fetched each view's channel and
  message one at a time, so a deployment with many panels paid an O(N) serial
  Discord round-trip cost before the bot reached READY. The fetch phase now runs
  concurrently under a bounded semaphore (`restore_concurrency` on
  `PersistenceMiddleware(...)`, default 8); view registration stays serial and in
  order. Many-panel bots reach READY substantially faster.
- **`REGISTRY_PRUNED` carries the pruned `persistence_key`s, and `last_reattach_summary`
  exposes them after startup.** The action payload gains a `keys` list alongside its
  `deleted` count. Because the action dispatches only once and code running after
  `setup_hook` completes (such as `on_ready` handlers) cannot subscribe in time to observe
  it, the manager also stashes the reattach result on
  `store.persistence_manager.last_reattach_summary` (`restored`/`skipped`/`failed`/`removed`/`unreachable`
  key lists), so a consumer can reconcile its own records after startup regardless of
  subscription timing.
- **The V2 40-component message cap raises a directed error.** When a
  `StatefulLayoutView` tree would exceed Discord's 40-component limit, `add_item`
  re-raises discord.py's terse "maximum number of children exceeded" with the
  running component count and a fix pointing at
  `PaginatedRegion.control_buttons(view, compact=True)`. discord.py still owns
  the enforcement; the library owns the message.
- **Expected ephemeral-lifecycle conditions no longer log at WARNING.** An
  ephemeral view that outlives its 15-minute interaction token logs its
  unavoidable edit-on-timeout failure (and the T+810s refresh-button arming
  failure) at DEBUG instead of WARNING -- the condition is expected and not
  operator-actionable. Non-ephemeral edit failures still warn. A persistent-view
  registry write that races the view's own exit also drops to DEBUG.
- **`DevToolsCog` slash commands are hidden from non-admins.** The `/cascadeui`
  group now sets `default_member_permissions` to none, so Discord no longer
  shows it in a regular member's command picker -- the `is_owner()` check
  already blocked execution; this removes the clutter for bots that sync the
  cog globally. The prefix form (`!cascadeui ...`) is unaffected.
- **DevTools inspector buttons are more responsive.** The Refresh, Clear History,
  and performance-tab buttons in `/cascadeui inspect` now edit and acknowledge in
  one round-trip instead of deferring first, removing a per-click pause. The
  genuinely-slow buttons (Flush, Exit All, Clear Session) still acknowledge first.
- **`v2_dashboard.py` enriched into a full V2 builder showcase** covering `choice_row`,
  `Collapsible`, `tab_nav`, `cycle_button`, and `link_section` across three tabs, plus a
  "tabs vs navigation vs `tab_nav`" decision note in the patterns guide.
- **Modal input `custom_id`s now derive through the shared `slugify` rule**, so a punctuated
  field label (`"A/B Test"`) yields a clean `input_a_b_test` id; letter-and-space labels are unchanged.

### Fixed

- **A failed interaction edit during navigation no longer bricks the view.** When a
  Discord edit failed mid-`push()`/`pop()` (a missed ack, an expired token, a transient
  5xx), the message kept showing the previous view while that view was already torn
  down, so every later click was silently dropped. Navigation is now atomic: the source
  view's teardown is deferred until the destination edit confirms, so a failed edit
  rolls back to a live, re-clickable source instead of a dead message. The edit is
  attempted across the interaction fast path, the deferred response, and the channel
  endpoint before any rollback.
- **Fast navigation no longer surfaces an occasional "This interaction failed".** When a
  view's first render took roughly `auto_defer_delay` seconds, the auto-defer timer
  could acknowledge the interaction in the same instant the fast-path edit ran, and
  `edit_message` raised `InteractionResponded` -- a sibling of `HTTPException` the fast
  path did not catch. Both the navigation and in-place-refresh fast paths now recognize
  the race and fall through to the deferred edit.
- **Tab views keep their Back button when used as a navigation rebuild target.**
  A `TabLayoutView` that is pushed and rebuilt through `send()` could lose the auto
  Back button on the rebuild; it is now restored afterward like the other navigation
  patterns.
- **`with_confirmation` no longer drops the confirmed action when the prompt edit
  fails.** If editing the confirm/cancel prompt failed (a deleted message, a transient
  error), the wrapped callback was skipped and the error propagated. The prompt edit is
  now contained so the confirmed action (or `on_cancel`) still runs.
- **Modal submissions no longer error when the trailing acknowledgement races.** A modal
  whose callback left the interaction unanswered acked it afterward; a dead or
  already-acked interaction made that defer raise into `Modal.on_error` even though the
  submission had processed. The trailing ack now absorbs the dead-interaction and
  already-acked cases, matching the view callback's post-callback defer.
- **Persistent views no longer render with a cold cache on restart.** `on_restore` ran on
  the `setup_hook` critical path, before the gateway cache was populated, so a render that
  read `bot.get_user`/members/channels painted defaults (e.g. default avatars) that never
  refreshed. `on_restore` now runs after `bot.wait_until_ready()` on a background task, so
  its render resolves real values; interaction routing still registers immediately during
  reattach.
- **Persistent leaderboard panels route clicks immediately after a restart.**
  `PersistentLeaderboardLayoutView` builds its ranking controls in `on_load`, so reattach
  registered only the `"No pages."` placeholder tree and the real selects and nav buttons
  dropped clicks until the next state change. `on_restore` now re-renders via `reload()`,
  whose message edit re-stores the real components in discord.py's view store.
- **Reattach no longer prunes a persistent view's row on a transient fetch failure.** A
  `Forbidden` or `HTTPException` during the startup mass-fetch (a momentary permission change
  or a 5xx) previously deleted the registry row alongside a real `NotFound`, so a
  still-existing panel was orphaned and never reattached on a later restart. Only a definitive
  `NotFound` now prunes; transient failures land in a new `unreachable` reattach-summary bucket
  and leave the row on disk for the next restart.
- **Persistent-view reattach logs one aggregate summary, not a line per view.** Startup
  logged an INFO line per reattached view (a flood at hundreds of views) plus an aggregate
  dumping every key list. Reattach now logs a single INFO summary with counts; the per-view
  detail moved to DEBUG.
- **`PersistenceMiddleware(migrators=...)` now registers the migrators.** The
  `migrators=` keyword was accepted but silently ignored -- the value was stashed
  and never wired into the migration registries. It now takes a dict with optional
  `"schema"` and `"kwargs"` keys (each mapping `(name, from_version)` to an async
  migrator) and registers them during `initialize()`, before schema migration and
  rehydrate run. Registration is idempotent, so re-construction does not raise.
- **`subscribed_actions` declared on a base class is now inherited.** The `__init__`
  defaulting checked the leaf class's `__dict__` instead of the MRO, so a value factored
  onto a base class was overwritten with an empty set and the view silently subscribed to
  nothing. It now resolves through the MRO and copies into a fresh per-instance set.
- **`TaskManager` no longer leaks a coroutine when a task is cancelled before it
  starts.** A task cancelled in the same tick it was created left its coroutine
  unawaited, emitting a "coroutine was never awaited" warning. Tasks now schedule their
  coroutine directly and clean up through a done-callback, so a cancel-before-first-step
  unwinds cleanly.
- **`LoggingMiddleware` honors its configured `level` for emission.** The action stream
  was hardcoded to `INFO`; it now emits at the configured level and no longer pins the
  logger threshold, so `level="DEBUG"` (or `setup_logging(actions="DEBUG")`) keeps action
  traffic out of INFO logs. `level="WARNING"` no longer suppresses the stream -- use
  `actions=False` for that.
- **`setup_logging` no longer prints literal ANSI escapes on a plain console.** The
  console handler emitted color unconditionally, so a Windows console without virtual-terminal
  processing showed `^[[31m` codes while the file handler stayed clean. `setup_logging` now
  detects color support (delegating to discord.py's detector, honoring `NO_COLOR` / `FORCE_COLOR`)
  and falls back to plain output; a new `color=True` / `color=False` argument forces it either way.
- **`Modal` validation errors now name the field label, not its custom_id slug.**
  A failed validator rendered the field's derived `custom_id` (e.g. `input_emoji`
  for a field labelled "Emoji") in the user-facing error line. It now prefixes the
  field's label instead.
- **Duplicate component `custom_id`s are caught at build time with a directed error.**
  Two components sharing a `custom_id` previously failed with an opaque Discord HTTP 400 at
  render, and two modal inputs deriving the same id from their label (e.g. two fields both
  labelled "Name") silently overwrote each other, dropping one field's submitted value. The
  pre-send validator now walks the component tree (V1 and V2) and `Modal` checks its inputs,
  each raising a `ValueError` that names the repeated id and the fix.
- **Link and premium buttons no longer break a stateful view.** A link (`url`) or premium
  (`sku_id`) button carries no `custom_id`, but the custom_id stabilizer treated it as an auto-id
  button and assigned one, producing a Discord 400 (code 50035, "custom id and url cannot both be
  specified") whenever such a button sat in a `build_ui()` tree. The stabilizer now skips both, and
  a V1 `PersistentView` no longer rejects a link button for lacking a custom_id.

---

## [3.3.4] - 2026-06-19

### Fixed

- **Select selection changes now re-render.** A `StatefulSelect` rebuilt with
  only its selected option changed (via `set_selected()` or `default=`) was
  treated as unchanged and the message edit was skipped, so the client kept
  showing the old selection and the next interaction submitted stale values.
  The render check now accounts for which options are selected.
- **In-callback navigation no longer edits the wrong message.** Calling
  `push()` or `pop()` from inside a `with_confirmation`-wrapped callback edited
  the ephemeral confirmation prompt instead of the view's own message, leaving
  the original message stale. Navigation now edits the view's message when the
  acting interaction belongs to a different one.

---

## [3.3.3] - 2026-06-18

### Added

- **`/cascadeui inspect` guild scoping.** The inspector takes an optional
  `scope` argument: pass a guild id to target one guild, or `global` (or `all`)
  to show every guild. View and session state rows now carry a `guild_id` field
  so the inspector can filter by guild.
- **Convenience components importable from the top level.** `PrimaryButton`,
  `ToggleButton`, `Dropdown`, and the other button/select subclasses now import
  directly from `cascadeui` (previously only from `cascadeui.components`).

### Changed

- **DevTools default to the current guild.** `/cascadeui inspect`, `views`,
  `sessions`, and `exitall` previously spanned every guild; they now scope to
  the guild where the command runs, and `exitall` no longer reaches other
  guilds' views. Pass `/cascadeui inspect global` for the all-guilds view.
- **Ephemeral views refresh more responsively.** A self-refreshing ephemeral
  message no longer shows a brief pause after each interaction. The edit now
  ships immediately and the interaction is acknowledged in the background,
  removing a per-interaction round-trip.
- **Navigation is more responsive.** `push()` and `pop()` (and the auto Back
  button) now edit the message and acknowledge the interaction in one round-trip
  instead of deferring first, removing a per-step pause on menu and wizard
  navigation.

### Fixed

- **Ghost views no longer accumulate in the inspector.** Finite-timeout and
  dismissed views could leak permanently into `/cascadeui inspect` (present in
  state, absent from the live registry) when a teardown step failed partway
  through. Teardown is now atomic: the state entry is removed before the
  registry entry, so a failed or cancelled teardown leaves both intact and
  self-heals instead of stranding a ghost.

---

## [3.3.2] - 2026-06-10

### Added

- **`edit_timeout` ceiling on every view.** Bounds each Discord edit the
  library issues after the initial send (refresh, navigation, teardown)
  with `asyncio.wait_for`, so a stalled connection is cancelled instead of
  pinning the view -- discord.py issues edits with no total HTTP timeout.
  Default `60.0` seconds; set `None` to restore unbounded awaits.

### Fixed

- **Ephemeral views could freeze under rapid clicking.** An ephemeral
  `StatefulLayoutView` that refreshed itself in component callbacks could
  intermittently show "This interaction failed" and go stale under latency.
  Ephemeral acting refreshes now acknowledge first and edit through the
  webhook, decoupling the ack from the slower ephemeral edit.

---

## [3.3.1] - 2026-06-09

### Fixed

- **Typed select `default_values` broke serialization on discord.py 2.7.**
  `RoleSelect`, `UserSelect`, `ChannelSelect`, and `MentionableSelect`
  raised `AttributeError` when `default_values` were supplied; fixed by
  coercing the type argument to `SelectDefaultValueType` at construction.

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
