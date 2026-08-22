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

## [3.12.0] - 2026-08-21

### Breaking

- `SQLiteBackend` and `PostgresBackend` reject a `table_prefix` that is not
  lower-case letters, digits, and underscores, starting with a letter or
  underscore, with `ValueError` at construction; a non-`str` prefix raises
  `TypeError`. The prefix reaches SQL through two paths that do not agree
  on quoting: the DDL and the migrator interpolate it as raw text, while a
  backend's row and key-value paths quote what they build. PostgreSQL
  folds an unquoted identifier to lower case and preserves a quoted one,
  so a prefix carrying a capital created one table and addressed another,
  splitting a deployment across two that both looked right on their own.
  **SQLite matches identifiers case-insensitively, so a prefix like
  `Bot_` worked there in 3.11.0 and now refuses to construct.** Lower-case
  the prefix to upgrade: SQLite resolves the new spelling to the existing
  table, so the data is found in place and nothing needs migrating.

### Added

- `MAX_MESSAGE_COMPONENTS`, `count_components(item)`, and
  `StatefulLayoutView.total_components` expose the per-message component
  budget the library already enforced. `add_item` raises when a tree would
  cross the cap, but the number and the recursive counter behind it were
  both private, so a consumer wanting to check a budget before composing
  had to transcribe the literal and rewrite the walk -- and a hand-rolled
  walk that counts the view itself disagrees with Discord's own count by
  one. A caller's check and the library's enforcement now read the same
  two numbers.
- `count_characters(item)` measures a subtree's display text the way
  `count_components(item)` measures its nodes, so both budgets are read
  from the same place while composing. Without it a caller holding items
  had to add them to a throwaway view to reach `content_length()`, which
  borrows that view's component limit: a subtree over the component
  budget failed a character measurement with an error about components.
- `MAX_MESSAGE_CHARACTERS` names the other budget a V2 message has, and a
  view over it now warns at every seam that ships a tree. The per-node
  4000-character cap on one `TextDisplay` was pre-flighted and the message
  total was not, so ten short text nodes passed every check and were
  refused at send with no component named -- on a persistent panel, hours
  later, in a channel nobody is watching. It warns rather than raises:
  Discord documents the cap while discord.py counts it without enforcing
  it, so a hard refusal could reject a message Discord would have taken.
  `LayoutView.content_length()` supplies the running total for a budget
  check while composing, and reaches `TextDisplay` content only: text in
  a button label or a select placeholder counts zero against it, so a
  control-heavy screen can approach the cap without the warning firing.
- `Modal.submit(interaction, values)` drives a submission offline through
  the pipeline a real one takes, returning whether the validators accepted
  it. A modal was the one interactive surface with no offline drive, so a
  test reached past it to the stored callback, which runs neither the
  validators, nor the write-back onto the input wrappers, nor the
  `MODAL_SUBMITTED` dispatch. Such a test succeeds against input the modal
  would have rejected, reporting coverage of a seam it never crossed.
  Values key by an input's label or its derived `custom_id`, resolved
  against the same map the modal itself uses, so a raw escape-hatch input
  is reachable too. A custom_id is an input's identity and always wins a
  contested name; a key naming no input raises. It assigns the values and
  calls `on_submit` itself, so there is no second implementation to
  drift. The verdict is what `on_submit` records as it runs, so an
  override must call up to it; one that does not is told so rather than
  having its submissions reported as rejected.
- `cascadeui.testing.stub_interaction()` returns the double
  `Modal.submit` asks for, recording what is said instead of sending it.
  A rejection is then assertable without a connection, including the part
  measurement cannot reach: whether the response slot or the followup
  carried the message depends on whether the ack backstop had already
  fired, so both record into one place.
- `cascadeui.testing.stub_client()` returns a `discord.Client` that never
  connects, for measuring a view with no Discord connection. It matters
  where composition depends on a client: a section-mode leaderboard row
  resolves an avatar into a four-node `image_section` with one bound and
  renders a one-node `TextDisplay` without, so a five-row page differs by
  fifteen components between the tree a test builds and the tree that
  ships. The stub reports the empty user cache a live bot reports for an
  unseen member, so the composed tree matches.
- Component callbacks are checked against the arguments the control will
  call them with, at construction rather than on the first click. A button
  callback declaring `(interaction, value)` was invoked with one argument
  and raised from inside the library wrapper, surfacing to the operator as
  "This interaction failed" with a traceback pointing at library code
  rather than at the wiring. The refusal names the callback's own
  signature and the shape the control wants. The mirror is closed too:
  `choice_row`, `cycle_button`, `toggle_button`, `ToggleGroup`,
  `PaginationControls`, and `Modal` report a value to their callback every
  time, so one that cannot receive it is refused where it is supplied.
  `PaginatedView.from_cursor` refuses a `fetch_fn` that cannot take
  `(offset, limit)` and a `formatter` that cannot take `(chunk)`. Cursor
  mode stores both and first calls them from inside the page loader, one
  line apart, so either arity error surfaced there rather than at the
  constructor that accepted it.
  `Collapsible` refuses a `reveal` or `summary` that requires an argument:
  a wrong-arity `reveal` constructed and rendered cleanly, then failed on
  the first expand click with the state already flipped and the screen
  still showing the collapsed body. A wizard step's `condition` is refused
  in both declaration forms, the raw dict and `WizardStep`, because it is
  the one step callable whose arity error is never seen: the visibility
  check calls it inside a guard that treats a raising predicate as
  visible, so a predicate that could not be called rendered the step it
  meant to hide and logged a warning as the only trace. The other step
  callables are left as they are, since each already fails at its own
  call with the callable named.
- `toggle_section` and `ToggleButton` pass the new toggle state to a
  callback that declares a second parameter, matching `toggle_button` and
  `cycle_button`. A one-parameter callback is called exactly as before.
  The library's own toggle-shaped controls delivered their value and these
  two did not, which is the inconsistency behind the wrong-arity callbacks
  the entry above refuses.
- Empty and whitespace-only media references are rejected where they are
  written. `image_section`, `gallery`, and `file_attachment` raise at
  construction naming the builder and the parameter, and the pre-flight
  validator rejects an empty media URL on a raw `Thumbnail`,
  `MediaGalleryItem`, or `File`, including one mutated to empty after
  construction. An empty reference previously shipped and failed the whole
  message with a 400 that named no component: the same mistake an empty
  text node has raised for since 3.8.0, one field over.
  `LeaderboardLayoutView` keeps its degrade paths, so a blank `banner=`
  still renders no banner and a whitespace `get_avatar_url` return still
  takes the stacked-text fallback.
- The pre-flight validator enforces two more documented Discord caps:
  `Thumbnail` and `MediaGalleryItem` `description` at 1024 characters, and
  link-button `url` at 512. The docs claimed 256 for the alt text and now
  name the real limit. An empty link-button `url` is rejected on the same
  reasoning as an empty media reference: a URL is machine-consumed, so a
  blank one cannot resolve and ships to be refused as a form error naming
  no component.
- `send()` warns when a send names an `attachment://` reference it carries
  no matching file for, across a V2 component tree and a V1 embed's image,
  thumbnail, author, and footer alike. Discord resolves the two
  by name and renders an unresolved placeholder when it cannot, raising
  nothing, so the initial send is the only seam where both halves are in
  scope.
- `indicator_button_format` on `PaginatedView`, `PaginatedLayoutView`, and
  `PaginatedRegion` templates the middle navigation button instead of
  replacing it. `indicator_button_label` freezes one literal across every
  page, which is worse than the default for a board that wants `1/12`
  rather than `Page 1/12` on a narrow row. Placeholders are `{page}` and
  `{total}`, one-based; the literal still wins when both are set, and a
  template that cannot render is refused at class definition rather than
  raising from inside a click. The roles panel's five `*_message`
  templates are checked the same way: a typo'd placeholder there raised a
  bare `KeyError` on the click that used it, naming neither the attribute
  nor the placeholders it accepts. `with_cooldown(message=)` joins them,
  and is the one that hid best: its template renders only when a click is
  refused inside the cooldown window, so a typo survived construction and
  the first click before raising. It is rendered once where it is
  supplied, with the value the refusal really passes -- already rounded to
  one decimal, so a spec like `{remaining:.0f}` is refused there too
  rather than failing on a str at click time.

### Changed

- A toggle callback whose second parameter has a default now receives the
  toggle state where it previously received that default. `toggle_section`
  and `ToggleButton` decide by whether a second positional parameter is
  declared, and a defaulted one is declared, so a callback written
  `(interaction, extra=None)` starts seeing the state in `extra`. Selects
  have read a defaulted second parameter the same way in every release, so
  nothing changes for them. Callbacks taking exactly one parameter are
  called as before.

### Fixed

- Several wire-visible fields did not reach the render digest, so `refresh()`
  reported `SKIPPED` over a tree that differed from the one on screen and
  shipped no edit. An entity select's `default_values` (the equivalent of
  the `opt.default` fixed for string selects in 3.3.4), a `Separator`
  appearing, disappearing, or changing spacing or visibility, a select
  option's label, description, or emoji, `min_values` and `max_values`,
  `channel_types`, and a premium button's `sku_id`. The relabel case is
  the everyday one: a count badge moving from `Inbox` to `Inbox (3)` keeps
  the option's value and so hashed identically. `SKIPPED` states that the
  screen already shows this state, which is what made each of these a
  wrong answer rather than a missed optimization. A test now derives the
  expected field set from `to_component_dict` itself, so a field discord.py
  adds later fails there instead of silently skipping a render.
- `LinkButton` accepted an empty `url`. The pre-flight validator refuses
  one in a V2 tree, and a V1 view reaches no pre-flight, so the same
  button shipped to a form error naming no component depending only on
  which view held it. It is refused at construction now, which covers
  both.
- A select inside a modal never delivered its value. `Modal.on_submit`
  collects submitted values by walking its children and matching types,
  and the select family was absent from that list, so a user's choice was
  dropped before the callback, before `values_by_input`, and before the
  `MODAL_SUBMITTED` dispatch. Reading it raised `KeyError`, or read `None`
  from a `.get()`, on every submission. Reachable through the documented
  raw-item escape hatch, which is the only way to put one in a modal.
- `Modal` discarded every keyword it did not recognize. `on_submit=` is
  the name discord.py subclasses override and the natural guess for
  `callback=`, and passing it left the handler unset: the modal
  acknowledged each submission, ran nothing, logged nothing, and saved no
  input. A mistyped `view_id` dropped the `MODAL_SUBMITTED` dispatch the
  same way, and `custom_id` (a real `discord.ui.Modal` parameter) was
  replaced by a generated one. The signature is closed now, so an
  unrecognized keyword raises naming it, and `custom_id` reaches
  discord.py.
- Pruning a stored registration left the key in the store's own registry
  mirror. `prune_registry` deletes the row on disk and reports the prune,
  but `state["persistent_views"]` kept the entry, so re-registering that
  key afterwards found a stale record, matched no live view, and took the
  orphan-cleanup branch against the message the prune existed to leave
  standing. The prune now drops the keys it deleted, and still reaches
  every subscriber and hook, since reducers run ahead of both notification
  passes. `APPLICATION_SLOTS_PRUNED` stays reducer-less on purpose: the
  slot it removes is still live in memory and re-upserts on the next
  write.
- A correct prune logged a warning. The store's missing-reducer check read
  "something subscribes to this" as the signal for a mistyped action, and
  subscriber filters default to an empty set, so `REGISTRY_PRUNED` and
  `APPLICATION_SLOTS_PRUNED` warned on every prune: once per row dropped
  at boot, and once per daily TTL sweep with no consumer involved at all.
  Actions the library declares dispatch-only now log at debug, and an
  unknown type with no reducer and no listener still warns.
- On PostgreSQL the 3.11.0 table rename left each renamed table's
  primary-key constraint and its backing index under the legacy name, so
  an upgraded database and a fresh install diverged in the catalog
  permanently and only an operator reading `pg_indexes` would see it.
  Boot renames the constraint to match its table, reaching databases
  already upgraded under 3.11.0 as well as those still on 3.10.0. A name
  already taken by another relation warns and boots. SQLite is unaffected,
  since its autoindex follows the table through a rename.
- A leaderboard that outgrew the component budget reported it in the
  vocabulary of a hand-composed view, suggesting folded text nodes and a
  compacted pager. A board's size is set by how many entries a page
  carries, and in section mode by whether a client is bound: the same
  class fits with none and overflows with one, because each row grows from
  one component to four. The message now names `leaderboard_per_page`,
  and what the page it just built actually cost per row -- counted rather
  than inferred from the client, since a
  `get_avatar_url` override returning nothing degrades every row to a
  stacked text node with a client still bound.
- `python -m cascadeui` reported the installed distribution's version
  rather than the version being imported. An editable install serves the
  number it recorded at install time, so a checkout several releases ahead
  still printed the old one, and the bug report form requires this block:
  the wrong version arrived in the issue stated as fact, with nothing to
  contradict it. It now reports the running version, and names the
  installed one only when the two disagree, since that disagreement is
  worth seeing. The backend probe also listed a driver for a backend the
  library does not ship while omitting `asyncpg`, so no report a
  `PostgresBackend` user filed named the one backend with a network
  surface.
- The persistence reference credited `SQLiteBackend` and `PostgresBackend`
  with every capability flag, which stopped being true when `OPEN_ROWS`
  was added in 3.10.0. Both store rows as fixed columns and declare five
  of the six; a backend author reading the page could conclude an unknown
  column would round-trip and skip the migrator that makes it.
- The documented offline-testing recipe could not measure a section-mode
  leaderboard's real budget. It builds the view with no client, which is
  the branch that renders one component per row, so a board reported 25
  components against a cap of 40 and shipped 40. The recipe binds a client
  now and reads `total_components`.

---

## [3.11.0] - 2026-08-13

### Breaking

- **The two unprefixed library tables are renamed.** `persistent_views` is now
  `cascadeui_persistent_views`, and `application_slots` is now
  `cascadeui_application_slots` (`cascadeui_schema` and `cascadeui_kv` are
  unchanged). The old names made an operator scope on `cascadeui_%` (a
  filtered backup, a grant, an audit query) silently miss the registry, and
  let `CREATE TABLE IF NOT EXISTS` adopt a consumer's same-named table. The
  SQL backends migrate an existing database in place on the first boot:
  the old table is renamed -- indexes and schema-version record included --
  in one transaction, only after its columns confirm it is the library's. A
  same-named consumer table without those columns is never touched, and when
  both names exist with rows, nothing is renamed, the library uses the new
  name, and a WARNING names both tables. On PostgreSQL the rename needs table
  ownership, which the usual DML grants do not confer: a role without it gets
  `PersistenceSchemaError` naming both tables and the privilege, the database
  is left unchanged, and the rename retries once the privilege is there. A role
  that cannot see the old table at all skips it silently. What needs a hand:
  external scripts, grants, and backup filters naming the old tables move to
  the new names; a schema migrator keyed on an old name is refused at
  registration with a `ValueError` naming the current one (keyed on the old
  string, it would never be looked up); a custom non-SQL backend that persists
  rows keyed by namespace string moves its `persistent_views` /
  `application_slots` keys itself; and every process sharing one database
  upgrades together, since an older library will not find the renamed tables.

### Added

- `LeaderboardLayoutView.get_entries()` accepts an `async def` override, so a
  board whose rows live in a database reads them through the documented seam
  instead of caching into a private field. The patterns guide covers both
  shapes and when to prefer each.
- `EntryList` type alias, exported from the package root beside `EmojiInput`
  and `MediaInput`, for annotating a `get_entries()` override.
- `render_progress()` returns a progress bar as a string, for inlining into
  text a caller is already building: a leaderboard row's secondary line, a
  `key_value` cell, a `stats_card` field. `progress_bar()` composes its
  `TextDisplay` from it, so an inlined bar and a standalone one cannot
  render differently. Both clamp `value` and hold the bar to `width` cells.
- `view.parent` reads the view a child is attached to, or `None` for a root.
  Available from construction when the child was built with `parent=`, so a
  child panel reaching its parent no longer stores the same view twice.
- The auto-defer backstop logs at INFO when it fires, naming the surface and
  the elapsed time. Firing means a handler used its whole `auto_defer_delay`
  budget and the interaction was rescued rather than lost: the leading
  indicator for a surface that will eventually miss the deadline. It covers
  all four acked
  surfaces (view callbacks, the send pipeline, `DynamicPersistentButton`, and
  `Modal.on_submit`), and the window it measures starts before the access
  check and any `serialize_interactions` lock wait, so it prices work no
  wrapper around a callback can reach. The cancelled path stays silent and
  free: nothing is timed unless the ack actually lands late.
- `table_prefix=` on `SQLiteBackend` and `PostgresBackend`, applied to every
  table and index they create and query, carried on PostgreSQL's invalidation
  channel so two prefixed deployments do not read each other's notifications,
  and `physical_table(backend, name)` for a migrator resolving a table name
  through it. The prefix keeps two CascadeUI deployments sharing one database
  apart. Empty by default, so existing databases are unaffected. The
  persistence guide now names the full table set.
- `docs/guide/persistence.md` gains a "Class identity" section: moving or
  renaming a persistent view class leaves its stored rows holding the old
  name, and `session_class_key` is what keeps them resolving.
- Screenshots and short recordings throughout the documentation site and the
  README, covering every V2 builder, composite, and view pattern. The landing
  page now opens with a working counter beside the view it renders.

### Fixed

- A view that set `session_class_key` could not be popped back to. Navigation
  entries recorded the session key while the class registry is keyed on the
  import path, so the lookup missed and Back died as a logged warning and a
  `None`. Entries now record the class path. The pin is the sanctioned answer
  to a persistence hazard, so following that advice on a navigating view had
  traded one bug for another.
- Two persistent view classes sharing one `session_class_key` silently
  displaced each other in the class registry, and every stored row under that
  key reattached as whichever class was defined last. The collision now raises
  at class definition, before either registry is written.
- `session_class_key` accepted any truthy value. A non-string reached the
  backend as the `view_class` column, where SQLite stored its digits and the
  lookup then missed forever. An empty string was ignored outright, so a
  class that declared one was never pinned and would orphan its rows on the
  first move. Both are refused at class definition. Several refusals below
  fire there too, so a class that imported on 3.10.0 can now fail at import
  rather than at first render; each entry names the seam it guards.
- The reattach warning attributed every skipped row to a missing class, though
  rows turned away by a kwargs migrator land in the same bucket. It now counts
  only unresolved classes and names each stored string with its row count, the
  name an operator registers a class under or pins `session_class_key` to.
- The five `on_role_*` hooks, `get_avatar_url`, and `resolve_avatar_urls` were
  awaited unconditionally, so an override that needed no `await` and was
  written `def` raised a `TypeError` from inside the library. On a role button
  it escaped the handler entirely and surfaced as "This interaction failed".
- The leaderboard's frame and row hooks (`build_title`, `build_header`,
  `build_footer`, `format_entry`, `format_primary`, `format_secondary`,
  `on_leaderboard_empty`) accept `async def` overrides; an async one
  previously leaked its coroutine into the page build.
- `format_rank`, `format_name`, `format_stats`, `format_accessory`, the roles
  panel's five compose hooks, and the wizard's `step_indicator_label` resolve
  where nothing can await them, so an async override rendered as a coroutine
  repr in the board, on the button, or in the role card with nothing raised.
  They are refused at class definition, naming the seam.
- Every seam that refuses an async override missed one supplied as a
  `staticmethod` or `classmethod`, which is how a class body declares a
  plain-function hook, and missed an `async def` carrying a stray `yield`.
  `state_selector` and `@computed` selectors accepted both shapes silently,
  leaving the view deaf to state updates or the value never resolved.
- The seams that take their hook as an argument rather than reading it off a
  class body kept the narrower check: `Collapsible(reveal=)` and `summary=`,
  `StateStore.subscribe(selector=)`, `restore_on_dropped_render(rebuild=)`,
  and a wizard step's `condition`. All refuse the same set now.
- A leaderboard rebuild read `get_entries()` twice, once for the change
  signature and again for the page build, and stamped the signature from the
  first read while rendering the second.
- `PaginatedRegion.set_page()` resolved a negative index against the page
  count it had at the time of the call, but `show_page()` seeks and then
  re-renders, and the host loads fresh items during that render. A list that
  grew across a `per_page` boundary therefore left `show_page(-1)` on the old
  last page, so the row that prompted the jump rendered off-screen under a
  cursor reporting a later page. The index now resolves against the items that
  arrive; an explicit move drops a pending one.
- A state notification queued before a view's teardown could arrive after it,
  and the torn-down view still rebuilt. Cross-view fan-out is fire-and-forget,
  so the gap is ordinary rather than rare: the rebuild rendered into a view
  that could no longer be interacted with, and read attributes the teardown
  had already cleared, which surfaced as an ERROR and a traceback from inside
  the subscriber wrapper rather than as anything the user could act on.
  Finished views now skip the rebuild.
- The `v2_leaderboard` example sorted only its real guild members, so a board
  padded with demo rows rendered out of order beneath a footer stating
  "Rankings sorted by MMR". The assembled board is now sorted.

---

## [3.10.0] - 2026-08-10

### Breaking

- **A custom backend serving the registry namespace declares
  `Capability.OPEN_ROWS` or `Capability.RAW_SQL`.** This release ships the
  library's first schema migration, so the migration runner needs to know
  whether a backend can alter its own table or has nothing to alter. Declare
  `OPEN_ROWS` if rows are open mappings (unknown columns round-trip through
  `row_upsert` / `row_select` and a missing one reads as `None`), or `RAW_SQL`
  with the raw-SQL surface implemented so the migrator can run the `ALTER`.
  A backend declaring neither is rejected at `PersistenceMiddleware.initialize`
  with `PersistenceConfigError` naming both remedies, rather than accepting the
  version bump and then failing on the first registry write carrying a column
  its table lacks. Every built-in backend already qualifies: `SQLiteBackend` and
  `PostgresBackend` declare `RAW_SQL`, `InMemoryBackend` declares `OPEN_ROWS`.
  A backend with no migration pending is never asked for either flag, so a
  fresh install is unaffected whatever it declares.

### Added

- **Registry rows that stay unreachable now carry an age, and an operator can
  act on it.** Keeping an unreachable row is what stops a permission change
  during startup from deleting a live panel, and the cost is that a channel the
  bot will never see again is re-fetched at every boot forever. The first failed
  fetch stamps `first_unreachable_at`; the stamp clears the moment a fetch
  succeeds, and records the first failure rather than the latest so the age
  grows across boots instead of resetting.
  `PersistenceManager.unreachable_since` reads the backlog and
  `prune_unreachable(older_than_days=)` acts on it,
  re-checking every candidate against Discord first: a row that answers is kept
  and cleared whatever its age, a definitive 404 goes regardless of age, and the
  rest are deleted only past the cutoff. A stamp alone is one observation, and a
  host that has not restarted for a month carries a month-old stamp from a
  single failure. `/cascadeui unreachable` lists the backlog and takes an
  optional cutoff to run the same prune. The `persistent_views` schema migrates
  to v2 at startup; a migration that fails raises `PersistenceSchemaError`
  naming the table, the version step, and the underlying error, and leaves the
  on-disk version alone so the next start retries. PostgreSQL needs table
  ownership for `ALTER TABLE`, which the documented grants do not confer.
- **`prune_registry` takes `reason=`,** labeling the `REGISTRY_PRUNED` dispatch
  so a subscriber can tell an operator giving up on a row from a boot finding a
  404. Unset, it keeps the value the method already computed.
- **`refresh()` and `reload()` say what happened to the edit.** Both returned
  `None`, so a caller that had to know its render reached Discord inferred it
  from the absence of an exception, and a `reload()` that returned at the
  throttle gate without attempting anything was indistinguishable from one that
  landed. They now return a `RenderOutcome`: rendered, skipped as unchanged,
  deferred to the throttle boundary, dropped in transit, or no message left to
  edit. Members compare equal to their string values, and a caller ignoring the
  return is unaffected.
- **`PaginatedRegion.host` and `Collapsible.host`.** Both composites document
  hooks whose stated purpose needs the host they render into, and the only way
  there was the private `_view`. The read-only accessor mirrors
  `LeaderboardLayoutView.bot`, which exists for the same reason. It reads
  `None` until the host's first render captures it.
- **A note on Section accessories** in the components guide: a Section carries
  one accessory, so a row cannot hold both a thumbnail and its own button, and
  the two layouts that get closest are named.

### Changed

- **An empty leaderboard keeps its masthead.** The empty-state page returned
  before the masthead composed, so a board rendered its `banner` and `title`
  while it had entries and dropped both while it had none, and the banner
  reappeared on its own once the first entry landed. The masthead now composes
  above the empty-state page, and above it rather than inside it so an
  override of `on_leaderboard_empty` inherits the board's identity instead of
  having to know it was lost. `build_title` returning `[]` renders no masthead
  on that page, as on any other. `title` defaults to `"Leaderboard"`, so a
  board that never set one gains an `## Leaderboard` heading while it is
  empty. `title=None` renders no heading on any page; to bare only the empty
  page, return `[]` from `build_title` while `ranked_entries` is empty. An
  override that composes the banner or title itself now renders them twice;
  drop that composition, or return `[]` from `build_title`.
- **The leaderboard's page frames run on the empty page.** `build_header` and
  `build_footer` were skipped there, so an override that needed a frame on an
  empty board had to know it was lost and recompose the library's own
  return-type placement rule to get it back. Header content renders above the
  masthead and footer content below the empty-state card, each in the order
  returned. Two consequences worth checking on upgrade: an override returning
  frame content unconditionally now renders it on an empty board, and one that
  computes aggregates from `ranked_entries` without guarding an empty slice
  raises during the empty-page build, where before it never ran.

### Fixed

- **Pruning re-reads a row before deleting it.** Every reachability verdict
  costs a Discord fetch, so on a real backlog the snapshot behind one can be
  minutes old by the time it is acted on. A panel re-posted under its stable
  key in that window writes a fresh row the verdict knows nothing about, and
  deleting by key alone removed the live panel because the message it replaced
  answered 404. Both destructive paths now confirm the row still points at the
  message that was judged, and report a rewritten one as kept rather than
  pruned. A backend that raises while that confirmation is read no longer
  takes the whole reattach pass down with it: the key is reported rather than
  acted on, never deleted unconfirmed, and the pass still schedules its warm
  repaint. A prune that raises at the same seam is contained the same way,
  where before it left restored panels unrepainted and let a later
  `reattach()` re-register rows it had already restored.
- **A pruned registry row leaves this process's copy too.** `prune_registry`
  deleted from disk and left the row in memory, so a later `reattach()` walked
  rows whose messages were already gone and spent one fetch per dead row per
  pass. That is the cost the unreachable stamp exists to stop paying. The
  mirror is kept honest in the two other cases that move a row underneath a
  pass: one re-posted under its key is refreshed to its new coordinates rather
  than left pointing at the message it replaced, and one deleted out from
  under the pass is dropped. Neither is stamped unreachable, since one is live
  and the other no longer exists.
- **Panels sharing a channel repaint one at a time.** Discord buckets message
  edits per channel, and message writes carry sub-limits that response headers
  do not report, so the concurrent post-ready `on_restore` pass opened several
  edits into one channel's bucket at once and collected 429s no header-paced
  client can pre-empt. Panels that share a channel now repaint serially, in
  registry row order; panels in distinct channels keep the concurrent repaint
  under `restore_concurrency`, so a deployment spread across channels boots as
  before. The reattach fetches stay concurrent: read buckets report their
  limits in headers, so the HTTP layer paces those itself.
- **A registry table whose version record is missing migrates instead of being
  recorded as current.** No version row was read as a fresh install, which was
  safe while the table had only ever had one shape. Now that a migration
  exists, a store whose version record was lost (a partial restore, a
  hand-edited schema table) would be stamped at the current version while still
  shaped as the old one, and every write to a column that version claims would
  fail from then on. Rows are the discriminator: a table with rows predates the
  record and migrates, an empty one is a genuine fresh install. The check runs
  only on the boot that writes the record.
- **A migrator passed to `migrators=` that collides with one already registered
  now says so.** The decorator path raises on a duplicate; this path skipped
  silently, so a consumer's migrator for a table and version the library had
  claimed was discarded without a word.
- **`InMemoryBackend` said it declared every capability flag.** It has never
  declared `RAW_SQL`, so the docstring sent anyone reading it in an editor
  tooltip to `execute()` and `transaction()`, which raise on that backend.
- **A leaderboard that empties stops reporting the entries it used to hold.**
  `ranked_entries` was assigned after the empty-board short-circuit, so a board
  going from populated to empty kept the previous slice. A `build_header`
  reading it for aggregate stats rendered totals for entries that no longer
  exist, above a card saying there are none. It is cleared before the empty
  page composes.
- **A leaderboard build that raises retries on the next rebuild.** The entry
  signature was stamped before the pages were built, so a hook that raised
  once (an avatar resolver, a frame hook) left the board on its previous
  pages, and the short-circuit then skipped every rebuild until the entry data
  changed again. The signature now stamps only after the build returns, so a
  transient failure recovers on the next rebuild without `force=True`.
- **A second ephemeral send no longer lets the first one's timer take the new
  panel early.** Both sends stamp their own token clock, but the first send's
  arm timer was already asleep against the older one, so it woke as much as
  thirteen minutes before the live token needed anything and swapped the
  working panel's children for a Continue Session button, freezing it against
  further state changes. A timer that wakes to find the deadline re-stamped
  stands down, and the second send's own timer arms on the new schedule.
- **A view re-sent publicly no longer offers to reopen the public message.**
  Re-sending the same instance without `ephemeral=True` cancelled nothing and
  cleared nothing, so the first send's arm timer woke against a message that
  was no longer ephemeral and swapped the view's children for a Continue
  Session button. It stands down on waking now, when the view no longer
  manages an ephemeral message. Ephemeral navigation chains and reopens still
  arm.
- **`auto_refresh_ephemeral = False` set after send is honored.** The handoff
  timer read the flag on the way in and slept through most of the token's life
  without re-reading it, so a view pinning the handoff off mid-session was
  armed anyway.
- **`auto_refresh_ephemeral` is honored on a view reached by navigation, in
  both directions.** The arming deadline records when the message's webhook
  token expires, but it was written only when the sending view wanted the
  handoff, so the navigation gates read its presence as the policy. Presence
  cannot tell "derive this" from "explicitly off", nor "explicitly on" from
  "never written": a view pinning the handoff off was armed anyway the moment
  a push reached it and offered a Continue Session button around thirteen
  minutes later, and a view pinning it on stayed silent when the original
  send had declined. Every ephemeral send now stamps the deadline, since the
  clock belongs to the message rather than to any view on it, and each
  destination is judged on its own flag: `True` arms against the original
  send's window, `False` stays off, and the `None` default inherits the
  effective policy of the view it was pushed from. A pop hands the restored
  view back the resolution it held when it was pushed away from, so a
  departing child's declaration never travels up the chain. A rolled-back
  navigation re-arms only a source whose handoff had engaged. A control that
  arms on one press and executes on the next is the shape the off case
  reaches: a reopen rebuilds it from constructor kwargs, which is not where
  the record of an already-completed action lives.
- **`auto_refresh_ephemeral` reads back what its author declared.** Resolving
  the `None` default wrote the derived answer onto the attribute itself, so a
  class declaring nothing reported `True` or `False` after its first send, and
  nothing could tell an author's explicit choice from the library's computed
  one. The frozen answer could not re-derive either: a second ephemeral send,
  or a `timeout` changed between sends, carried the first one. The declaration
  is now input the library never writes, and the resolution rides privately
  beside the arming deadline, re-derived at every ephemeral send, inherited
  down a push chain, and handed back on pop. Nothing else about when the
  handoff engages changed.
- **The ephemeral handoff no longer claims to preserve all state.** It
  reconstructs from constructor kwargs plus `get_nav_state()`, so an attribute
  assigned after `__init__` is lost unless that hook names it, and the loss is
  invisible until a reopen fires. The guide said "preserves all state" and
  never pointed at the hook.
- **Overlapping `reload()` calls on one view run one at a time.** Two `on_load`
  bodies could interleave at any await inside them and corrupt each other's
  half-built render state: a forced leaderboard rebuild parked at its avatar
  fetch could resume into entries a transient empty reload had already cleared,
  drop its masthead on a populated page, and then stamp a signature that made
  every later unforced reload short-circuit onto it. The library reaches this
  itself, from the avatar backfill task a caller cannot serialize from outside,
  while the two sibling entry points into the same state were already guarded.
  A reload arriving mid-fetch now waits and re-fetches, so the last to run
  renders the freshest data. A `reload()` called from inside its own `on_load`
  raises instead of recursing, a `force=True` folded into a throttle window is
  no longer dropped by a later unforced reload, and a reload gated while the
  deferred render task was mid-flight is no longer lost.
- **`card()` rejects a Container child at the call.** A Container is never a
  legal child of a Container, and the composition was accepted silently and
  rejected later by the placement validator, naming component indexes rather
  than the call that built it. On a pushed screen that surfaced as a failed
  navigation. The error names the child's position and points at placing it as
  a sibling. `alert()` and `stats_card()` compose their own children and admit
  no caller Container, so this is `card()` alone.

---

## [3.9.1] - 2026-08-03

### Added

- **`refresh_degraded` reports a dropped render.** Read-only `bool` on every
  view, `True` when the last `refresh()` was dropped by a transport failure.
  Read it when the caller changed something before the render that should
  not stand if the render never landed.
- **`restore_on_dropped_render(*attributes)` rolls back a dropped render.**
  Context manager that snapshots view attributes and rebinds them when the
  render inside it was dropped. A control armed on one press and executed on
  the next used to collapse into a single press when the arming render
  failed: the button on screen still looked unarmed, so pressing it again
  executed rather than armed. That has been true of the in-place
  arm-then-confirm shape for as long as it has existed; the library now
  offers the rollback rather than leaving each caller to remember it. The
  snapshot holds each attribute's value, so flags and cursors come back and a
  collection edited in place does not. Pass `rebuild=` to recompose the tree
  from the restored values: a V2 tree is the content, so rebinding the
  attribute alone leaves the screen describing a value that was rolled back,
  and the next refresh that does not rebuild first ships it.

### Changed

- **An aborted batch announces what it committed instead of discarding it.**
  Reducers run inline, so the dispatches before a raise have already changed
  state; dropping the queue reported nothing while state had moved, and left
  that change with no undo entry able to revert it. The committed prefix now
  fires one `BATCH_COMPLETE` and lands on the undo stack, and the exception
  still propagates. `batch()` remains a notification gate rather than a
  transaction: a block that must not leave anything behind rolls back itself,
  which the library's own pipelines now do.
- **A `BatchContext` cannot be entered twice.** Re-entering one after it
  closed collected onto a buffer that had already flushed, where the entries
  were never announced. `store.batch()` returns a fresh context per use;
  reusing a stored one now raises `RuntimeError`.
- **Restored persistent panels repaint concurrently.** The post-ready
  `on_restore` pass ran one panel at a time because batch state was shared,
  giving a repaint tail linear in panel count on views that were already
  interactive. It now runs under the existing `restore_concurrency` ceiling
  (default 8), which bounds both restore phases.
- **Dispatch cost no longer tracks how much data the store holds.** Two
  comparisons walked structures they could have skipped by identity. The
  built-in reducers shallow-spread, so a slot an action did not write is the
  same object on both sides of the undo diff, yet dict equality has no identity
  shortcut and compared it entry by entry: every undo-tracked action paid for
  the whole application namespace, measured at 493 microseconds against one
  untouched 20,000-entry slot and now 2.3. A subscriber whose selector
  returns a whole bucket was compared the same way on every dispatch, 20.8
  milliseconds at 200 subscribers over 5,000 scoped keys and now 0.15. Views
  never paid the second one, since their selector is wrapped in a tuple and
  tuple comparison does shortcut identical elements; it fell on direct
  `store.subscribe` callers. A batch commit also rebuilt its set of batched
  action types once per subscriber instead of once. `InMemoryBackend`'s
  batched upsert rescanned every stored row for each incoming one, so a
  200-row flush against 2,000 stored rows cost 167 milliseconds where the
  same batch into an empty namespace cost 8; it indexes the batch once and
  now costs 1.3. That backend is the reference implementation, so the shape
  it models is the one a new backend inherits.
- **The copy a custom reducer is handed is about twice as cheap.** State is
  dicts, lists and scalars, because it round-trips through `json.dumps` on the
  persistence path, and `copy.deepcopy` cannot assume that: it consults
  `__reduce_ex__` and the copy dispatch table at every node. Walking the two
  container shapes directly and delegating anything else takes a dispatch
  against 2,000 scope keys from 5.8ms to 2.9ms. A memo is still carried, so a
  self-referential value terminates and a reference appearing twice in the
  tree stays one object on the other side. The contract is unchanged: the
  reducer receives its own copy and mutates it freely.
- **A reducer that forgets to return names itself.** The store assigns whatever
  comes back, so falling off the end of a reducer installed `None` as the
  entire state and the failure surfaced at whichever unrelated read came next.
  A non-dict return now raises where the mistake is, naming the reducer, the
  action, and the missing `return state`; the store's own reducer guard logs it
  and keeps the previous state, so one dispatch is lost instead of the session.
- **The circular-attachment error names which views.** It reported the two
  class names, and attaching two instances of one view class is the ordinary
  case, so it named the same type twice and said nothing. It now carries each
  view's id, the chain it walked, and what to do about it.

### Fixed

- **A dropped connection no longer surfaces as an error card.** A request
  that never reached Discord raises from aiohttp and carries none of the HTTP
  types, so it escaped every handler: a reset during a page turn unwound out
  of `refresh()` into `on_error` and rendered a failure card over content that
  was fine. `aiohttp.ClientError` joined the shared catch-tuple as a third
  sibling. Each surface degrades and logs at `WARNING` rather than raising:
  `refresh()` re-ships on the next state change, the post-send message
  re-fetch keeps the message it just sent, and `respond()` / `open_modal()`
  drop the undelivered notice instead of retrying it on the followup path,
  where a duplicate reply would be worse. Navigation tries the channel
  endpoint before giving up and otherwise rolls back, since a swap the user
  never saw must not tear the source view down. Every paging
  pattern rewinds its own cursor (page, wizard step, active tab, and both V2
  composites), so a dropped turn no longer leaves the reader a position
  behind and makes the next press skip past content. Role toggles route their
  failures through `on_role_error` rather than escaping a dynamic button that
  has no handler beneath it. aiohttp's connect and socket timeouts read as
  transport rather than as a stalled edit: they inherit `asyncio.TimeoutError`
  too, so the timeout clause saw them first, and with discord.py passing no
  session timeout they are the likeliest transport failure of all. The full
  per-surface table is in the known-limitations guide. (The umbrella is
  `aiohttp.ClientError` rather than `OSError`: `ServerDisconnectedError` is
  not an `OSError`, and `asyncio.TimeoutError` is one from 3.11 but not on
  3.10.)
- **An ephemeral view keeps trying to arm its refresh button.** The handoff
  swaps the view's children for a Refresh button at T+810s and then drops
  every state notification, so that one edit is the only thing that can put
  the button on screen. A rate-limited attempt re-queues itself, but a
  dropped one gave up after a single retry and left the panel frozen with no
  button and most of the 90-second window unspent. It now retries for as long
  as the token can still carry an edit.
- **`card(color=...)` and `stats_card(color=...)` accept the int form their
  signatures document.** discord.py coerces an int in `Embed.colour`'s setter
  but stores one verbatim on `Container.accent_colour`, so a hex literal that
  had always themed a V1 embed reached the render digest as a bare `int` and
  raised after the message was already sent. Both builders and every colour
  style on `Theme` coerce to `discord.Colour`, and the digest reads either
  form, so a Container built without a builder works too. A theme declaring
  `primary_color` as an int was the widest way in: `accent_colour` defaults
  to it, and every themed card takes its accent from there. An out-of-range
  int and `True` are rejected where they are written. One read-back
  consequence: `Theme.get_style()` returns the `discord.Colour` for a style
  written as an int, so code comparing that value against the literal it
  passed needs `.value`.
- **A failure after the message was sent no longer reports the send as
  failed.** `send()`'s rollback path ends at the Discord call, so anything
  raising past it left the view registered and the message live while telling
  the caller neither happened; a caller retrying on that answer posted a
  second copy, and the registration stayed behind permanently, holding an
  `instance_limit` slot no live view occupied. Post-send bookkeeping now
  degrades and logs what is inactive on the message, and the same contract
  covers a persistent view whose registry write fails. A circular `parent=`
  chain, the other way into that window, is rejected before the send where
  the rollback still applies.
- **A raising hook no longer strands the view it was building.** `send()`
  registers state before running `seed_initial_state`, and `push()` quiesces
  the source before constructing the destination; neither rolled back if that
  caller code raised. A raising seed hook left a registered view with no
  message, invisible but counted by the instance limit, which under
  `instance_policy="reject"` locked the owner out of the view class until the
  process restarted. A raising destination `__init__` left the source on
  screen with buttons that no longer rendered, because it had already
  unsubscribed. Both now run the rollback they already own and re-raise, and
  a constructor that raises after `super().__init__()` no longer leaves its
  store subscriber behind. `push()`'s rollback runs inside its batch, so an
  aborted navigation no longer paints the destination onto the live message
  before destroying it. Both seams catch `BaseException`, since a cancellation
  strands exactly what a raise does.
- **Undo history survives actions that change nothing.** Every dispatch from
  an undo-enabled view pushed an entry, including ones that wrote no
  application slot and left `shared_data` alone: a selection change, a
  view-local toggle, a re-render. `undo_limit` bounds the stack, so those
  entries evicted the ones a user was trying to reach, and four of them
  cleared a stack of three. A snapshot that can put nothing back is no longer
  pushed, on the per-dispatch path and at batch commit. The test is whether
  `shared_data` *changed*, not whether it holds anything, because a session
  gaining its first shared data records an empty pre-value that is precisely
  what an undo has to restore.
- **A batch belongs to the task that opened it.** `_batch_depth` and the
  queued-action list were plain attributes on the store, so two tasks
  batching at once were indistinguishable from one batch nested inside
  another: the second never flushed its own, and whichever exited last fired
  a single `BATCH_COMPLETE` for both under one `source_id`. Worse without any
  concurrency of the caller's own making, a dispatch from a background task
  landing while an unrelated view was sending or navigating was absorbed into
  that view's batch, reaching subscribers late and under the wrong action
  type; if that batch then aborted, the queued actions were dropped and the
  background action's notification went with them, so state had changed and
  no subscriber was ever told. Batch membership now follows the task, and
  nesting inside one task absorbs exactly as before. Undo diffs are captured
  per action and merged at commit rather than diffed against live state,
  which had pulled an overlapping batch's slot writes into another view's
  undo entry; a slot a batch writes and then restores leaves no entry at all,
  and a batch holding undo records but no actions of its own finalizes them
  rather than discarding a snapshot for a change that committed. Profiling
  frames were shared the same way: with `perf` on, overlapping dispatches
  took each other's edit frames and a batched
  reducer's time landed on whichever unbatched dispatch was in flight.
- **A closing card composed before teardown reaches the message.** The V2
  teardown edit shipped only when the freeze disabled something, which is
  right for a display view that has nothing to disable and wrong for a view
  that clears its tree and composes a farewell card first: that view has
  nothing left to freeze either, so `exit()` and `on_timeout()` dropped the
  card and left the original controls on screen looking live. The next press
  reached a stopped view and Discord reported it as failed. The edit now also
  ships when the tree no longer matches the last render, and the no-op PATCH
  the guard exists to prevent is still skipped. Both challenge prompts in the
  examples expired this way, and both now print their deadline as a live
  Discord countdown timestamp instead of a fixed second count that was wrong
  the moment it was sent.
- **Four examples now key shared state per guild.** `v2_battleship.py`,
  `v2_computed.py`, and `v2_dashboard.py` each wrote every guild's data into
  one slot, and the battleship one decides hit-or-miss from that state, so a
  player in two matches at once had one overwrite the other's fleet.
  `v2_pagination.py`'s two commands shared a view class and its
  `instance_limit` slot. Worth re-reading if one was used as a starting point.

---

## [3.9.0] - 2026-07-30

### Security

- **`ToggleButton` accepted `owner_only=True` and never enforced it.** The
  gate lives in the callback wrapper that `StatefulButton` installs, and
  `ToggleButton` replaced that wrapper with one of its own after
  construction, so the flag was stored and never read. Any user could
  operate a toggle meant for the host, and the view's `on_unauthorized`
  never fired. Present since the per-component gate shipped in 3.2.0.
  The toggle now runs inside the shared wrapper, which also restores the
  three other guarantees it had been missing: no dispatch once the view
  is finished, the acting interaction bound so the refresh takes the
  one-request fast path, and a synchronous callback accepted. Routing a
  self-mutating component through the shared wrapper also moved where its
  value is read: `COMPONENT_INTERACTION` now records the state the toggle
  landed on rather than the one it was clicked in. Selects and plain
  buttons are unaffected, since neither changes its value during its own
  callback. Anyone using `owner_only=` on a toggle should treat it as
  having been open to everyone.

### Added

- **`confirm_style` and `cancel_style` on `confirm_section()`.** The builder
  exposed labels, emoji, and `custom_id` but hardcoded `success` for confirm
  and `danger` for cancel, so a destructive prompt rendered a green Delete
  beside a red Keep, and its own docstring example paired
  `confirm_label="Delete"` with a red card while teaching exactly that
  inversion. Both default to the values they replaced, so nothing that
  worked changes colour. Every builder that takes a style now also rejects
  a value that is not a `discord.ButtonStyle`: a bare `"danger"` used to
  construct cleanly and fail at send with an attribute error naming neither
  the builder nor the parameter, which the class-attribute validator has
  always caught on the equivalent class attributes.
- **`id=` on the V2 builders.** Discord gives every component an optional
  32-bit `id`, unique per message and auto-assigned sequentially when
  omitted, and discord.py surfaces it on `Container`, `Section`,
  `Thumbnail`, `MediaGallery`, and `TextDisplay`. None of the builders
  forwarded it, so composing a view from them was the one path that could
  not set an id at all: reaching a documented upstream field meant dropping
  to raw `discord.ui` for that node. `id=` names the component a builder
  returns, so the nineteen that return exactly one take it and the two that
  return a list do not: `confirm_section` yields a text display and a row of
  buttons, `button_grid` a row per grid row, and neither has a single node
  for an id to mean. Assign `.id` on the returned components for those, or
  set it per button inside `button_grid`'s `cell_factory`, which already
  owns every other per-button attribute.
- **Component ids are checked before the message is sent.** An id must be a
  whole number from 1 to `MAX_COMPONENT_ID` (2147483647, exported from the
  package root), and no two components in one message may share one --
  Discord's rules, previously discoverable only as an HTTP 400 with a form
  body that named a numeric path rather than a component. The builders
  reject a bad value where it is written, naming the builder; the
  uniqueness walk runs at the same three seams as the placement check
  (initial send, refresh, navigation edit) and covers V1 and V2 alike,
  since both carry ids. It can only refuse what Discord already refuses, so
  a view that sends today still sends.
- **`nav_depth` reads how many views sit beneath this one.** Zero on a
  view opened directly, one on the first push. This release disables a
  Back button with nowhere to go, but a screen reachable both by a push
  and by its own command usually wants that button *absent* on the root
  entry rather than present and greyed, and deciding that needs the stack
  the library already holds: `make_nav_row(back=bool(self.nav_depth))`.
  Without a read of it the caller carries its own root flag, which
  duplicates the stack and lives on whoever constructs the view, so a
  class that builds its nav row in two branches has to remember the flag
  in both. Safe to read inside `on_load`, since the stack is assigned
  before the load hook runs. Named for the `undo_depth` / `redo_depth`
  pair it sits beside.

### Changed

- **`PaginatedRegion.set_page` takes a negative index.** `set_page(-1)` is
  the last page, counting from the end the way Python indexing does. It is
  the only way to name the last page without first knowing how many there
  are, which is what made "add a row, then show it" cost two reads: asking
  the region for its page count needs the data loaded, and the jump then
  re-renders and loads it again. `await pager.show_page(-1)` does the whole
  gesture in one read and one edit, since it seeks and re-renders together.
  `set_page` moves the cursor only, for the seams with no tree to re-render
  yet, and a seek stays invisible until something rebuilds.
- **The `{label: callback}` builders accept a sequence of pairs.**
  `button_row`, `tab_nav`, `key_value`, `choice_row`, and both tab views
  read their argument with `.items()` or `.keys()`, so the same data
  written as `[("Save", on_save), ("Cancel", on_cancel)]` failed on an
  `AttributeError` naming a method rather than a parameter. The pair form
  is the same information in the order it was written, so it is converted
  rather than refused, and anything that is neither a mapping nor a pair
  sequence now names the parameter it was passed to. Mappings other than
  `dict` were already accepted by most of these and now are by all of them.
- **Type hints that named less than the code accepts.** Several parameters
  advertised a narrower type than they have ever taken, which matters more
  than a documentation slip: the obvious guard to write against a
  `list` annotation is `isinstance(items, list)`, and that would have
  rejected the tuples, ranges, and strings `from_data` has always paged.
  `items` is `Iterable[Any]`, which is what it reads. `MediaInput` names
  `discord.UnfurledMediaItem`, which every media builder has always passed
  through. The builders that now take a sequence of pairs say so rather
  than naming `Dict`, and each says `Mapping`, since a `UserDict` has
  always worked.

### Fixed

- **A non-string `label`, `value`, `content`, or `placeholder` crashed the
  placement validator.** discord.py stores these fields unvalidated, so a
  caller can pass an int and it reaches the payload unchanged. The character
  and empty-string guards called `len()` on the raw value, so an int
  `SelectOption` value raised `TypeError: object of type 'int' has no len()`
  from inside a pre-flight check instead of the send it was meant to protect.
  All six guarded fields now measure only strings. Present since 3.6.0, when
  the length caps landed.
- **`from_data()` validated none of what `from_cursor()` validates.** The
  two constructors take the same page size and the same formatter, and the
  cursor one rejects a non-callable and a non-positive `per_page` with
  directed messages. Its sibling checked nothing, so `per_page=0` surfaced
  as `range() arg 3 must not be zero` and a string page size as an indexing
  `TypeError`, while `per_page=-1` raised nothing at all and built a view
  with zero pages. Both now reject the same mistakes in the same words. A
  generator of items is consumed rather than refused, and tuples, ranges,
  and strings keep working as they always have despite the annotation
  naming a list. One input changes meaning: `per_page=True` previously
  paged by one, since a bool is an int, and is now rejected the way its
  sibling has always rejected it.
- **The paginated views ignored an async formatter unless it was a plain
  coroutine function.** All three seams that format a page asked
  `inspect.iscoroutinefunction` before deciding to await, which answers
  `False` for an object whose `__call__` is async, so the formatter's
  coroutine was stored as the page and rendered as a repr. It affected the
  eager build, the cursor mode's page fetch, and `refresh_data` alike.
  `validate_field` documents that same object as a supported shape. All
  three now await the result rather than interrogating the callable, which
  resolves every shape any of them accepts.
- **A synchronous post-event hook logged as though it had failed.** The
  wrapper that runs `on_page_changed`, `on_tab_switched`, `on_step_entered`
  and their siblings awaited the hook unconditionally, so an override
  written `def` rather than `async def` ran correctly, returned `None`, and
  then failed on awaiting that `None` inside the wrapper. The failure was
  caught and logged as a raising override, which named the user's hook for
  a fault in the caller. Both shapes are accepted now.
- **A `Collapsible` whose `reveal` or `summary` was async slipped its own
  check.** The constructor rejects a coroutine function, but that predicate
  recognises only the object itself, so an instance with an async `__call__`
  passed it and returned a coroutine into a synchronous render. The two
  halves then failed differently: a revealed coroutine reached `add_item`
  and raised `expected Item not coroutine` on the caller's line, loud but
  unnamed, while a summary coroutine was truthy, became a `TextDisplay`
  body, cleared the placement validator, and surfaced only as a JSON
  serialization error at send, naming neither the parameter nor the class.
  Both are caught at the render call now with the message the constructor
  would have given. Relatedly, a wizard step condition written as an async
  generator, one stray `yield` away from a correct predicate, was neither
  awaitable nor falsy, so its step rendered with nothing reported; it warns.
- **`dispatch()` accepted an action where it wanted an action's type.** The
  Redux idiom most callers arrive with is `dispatch(action)`, and reducers
  here receive exactly that dict, so handing one to `dispatch` is the
  natural mistake. It reached the reducer lookup and failed on
  `cannot use 'dict' as a dict key`, which names neither the parameter nor
  the shape. A non-string type was quieter: nothing raised, and an action
  no reducer or subscriber could ever match was recorded against a type
  like `None` or `5`. Both are rejected at the entry point now, and the
  dict case says how to split it. `dispatch_scoped_as` and the view-level
  `dispatch` route through the same seam.
- **`dispatch("")` raised `TypeError` for what is a value problem.** An
  empty action type is the right type carrying a wrong value, which is
  where the two stdlib exceptions divide. It raises `ValueError` now; a
  non-string still raises `TypeError`.
- **A wizard step or form field written as a dict escaped the checks its
  typed form has always run.** `WizardStep` and `FormField` validate at
  construction; the raw-dict alternative both patterns document was passed
  through untouched, so the same mistake behaved differently depending on
  which spelling was used. Mostly it stayed quiet: a step whose builder was
  not callable crashed at render, but one whose condition was not callable
  was swallowed by the visibility guard and rendered anyway, and a field
  with an unrecognised `type` produced no control at all, so it simply went
  missing from the form. The dict form stays the looser of the two, which
  is its purpose. It reads only `builder`, `validator` and `condition`, and
  a step with none of them is a supported shape that renders its navigation
  alone. What it no longer accepts is a value under one of those keys that
  cannot be called, or a field type nothing knows how to draw.
- **A leaderboard's entries were read for their shape without being
  checked for it.** Both the `entries=` kwarg and the `get_entries()` hook
  feed a signature helper that unpacks each entry and reads the second item
  as a mapping, so `[(user_id, score)]` and `{user_id: stats}`, the two
  most natural guesses, failed with `'int' object has no attribute 'items'`
  and `cannot unpack non-iterable int object` from inside a generator
  expression that named neither the parameter nor the shape. The failure
  fires during `send()`, so nothing surfaced until the board was displayed.
  Entries are checked where they enter now, naming the source and the
  offending index. A mapping of id to stats and a one-shot iterable are the
  same data in another container and are converted; lists of lists and
  tuples of tuples keep working as they always have.
- **Toggle labels were read by index without being checked.** Both toggles
  take an on/off pair and read `labels[0]` and `labels[1]`, so a one-item
  sequence raised `IndexError` from inside the builder. A bare string was
  worse: it indexes without complaint, so `labels="Enabled"` rendered a
  button captioned `E` in one state and `n` in the other, with nothing
  raised anywhere. `cycle_button` had rejected a mismatched label list all
  along; the toggles now check their pair the same way.
- **A cell factory was type-checked on its first cell only.** `button_grid`
  validated the first button it received and appended the rest untouched,
  so a factory returning `None` for one position, the natural way to leave
  a gap, failed inside `ActionRow` on an attribute the caller never wrote.
  Every cell is checked now, and the error names the row and column.
- **`choice_row` rejected the values its own docstring promised.**
  `Choice.value` is documented as any Python object, but active values were
  held in a set, so a list or a dict raised `cannot use 'list' as a set
  element` on the containment test, even with nothing selected. Active
  values are a list now and compare by equality. Options cap at 25, so the
  scan costs nothing and unhashable values work as documented.
- **Numbers, separators and single-item arguments reached operations that
  assumed their type.** `progress_bar` compared and divided its arguments
  before checking them, so a string bound failed on the operator rather
  than the parameter. `EmojiGrid` checked `rows`, `cols` and `fill` but not
  `cell_sep` or `corner`, which reach `str.join` during construction.
  `Theme(styles=...)` read its argument by key, so a sequence of pairs
  failed on `list indices must be integers`, and now converts the way the
  builders do. A `Modal` given a single input rather than a list failed on
  `not iterable`; it now says which it wanted.
- **The media builders disagreed with each other and with `banner=`.**
  `gallery`, `image_section`, and `file_attachment` share one `MediaInput`
  union, and passing a `discord.Asset` to it behaved three ways: `gallery`
  refused it at construction through discord.py's own check, while the
  other two accepted it and crashed at send inside the payload build with
  an attribute error. `LeaderboardLayoutView`'s `banner=` had always
  coerced the same objects, so `member.display_avatar` worked there and
  nowhere else, despite being one attribute away from the `.url` form every
  docstring example shows. All four now resolve an Asset, and all four
  reject an unusable value at construction naming the builder and the
  argument that carried it, down to which `gallery` item was wrong. A
  `discord.File`, a URL string, and an `UnfurledMediaItem` are unchanged.
- **Option lists accepted entries that were not options.** `CheckboxGroup`,
  `RadioGroup`, and `Dropdown` converted dicts and appended everything else
  untouched, so a list of plain strings became a list of plain strings and
  reached Discord as options carrying neither a label nor a value. Passing
  a single option dict without its list was worse: iterating a mapping
  yields its keys, so `options={"label": "A", "value": "a"}` built two
  options named `label` and `value`, which the one-to-ten count check then
  counted and approved. Both surfaced only when Discord rendered the
  component, as an attribute error from inside discord.py and "This
  interaction failed" on screen. Entries are now checked where they are
  read, naming the index, and a mapping passed in place of a list says so.
  `StatefulSelect` is checked too. `Dropdown` is the seam that accepts dict
  shorthand, and its base class took the same shapes without converting
  them, so the three failures above reached Discord unaltered through it.
  It now names the offending index and points at the sibling that does the
  conversion.
- **Three guards failed on the inputs they existed to screen.** A modal
  title or label that was not a string either crashed inside the length
  check with `object of type 'int' has no len()` or, when it happened to
  have a length of its own, passed both tests and reached Discord as a list
  or a bytestring. A numeric bound that was not an int either crashed on
  the comparison or slipped through as a float or a bool, which Discord
  rejects at modal-open. And a menu category that was not a mapping died
  inside the category validator on `'tuple' object has no attribute 'get'`,
  while one missing `label` or `view` surfaced later as a bare `KeyError`
  from whichever builder read it first. All three now name the parameter,
  the type they got, and the shape they wanted. Twelve call sites share
  the two input helpers, and the category check runs once per entry with
  the index in the message. Mappings other than `dict` keep working.
- **A V1 form's dropdown showed the wrong selection on the edit that
  recorded the right one.** The option marks are stamped when the controls
  are built, and V1 answers a pick by shipping an embed edit rather than
  rebuilding its controls, so Discord re-rendered the select from a payload
  still carrying the seeded marks. The user chose B, the summary read B, and
  the dropdown beside it snapped back to A. A multi-select was worse: every
  tick cleared on the very edit that confirmed it. The same stamps went
  stale on a pop, where restored entries rendered over controls built before
  the values arrived. Both paths re-derive from the form's values now, using
  the `set_selected` helper the library already shipped for exactly this and
  had never called. V2 was unaffected, since it rebuilds its controls on
  every update, which is the parity gap that hid this.
- **A Back button with nowhere to go destroyed the panel it sat on.**
  `make_nav_row()` defaults to `back=True`, so a view that is sent rather
  than pushed rendered Back against an empty stack, and pressing it took
  the empty-stack path, which froze every component on a V2 view and
  stripped them on a V1 one. The only recovery was running the command
  again. Two things change. Back is disabled while the stack is empty, the
  way the paginated and wizard controls beside it already derive their
  state from a cursor, and the empty-stack path acknowledges the press
  without touching the message. The disabled state is resolved at the
  render seams rather than when the button is built, because a pushed view
  is constructed before its stack is assigned: reading it at build time
  would disable a working button on any view that composes its tree in
  `__init__`. Both paths share one implementation now; the V2 freeze
  override is gone. A view that wants Back to close the panel overrides
  `_clear_on_empty_back`, though `exit()` and the Exit button are the
  surfaces built for that.
- **Anything you write for the library had to be `async def`, or the
  library failed on your behalf.** Every seam that runs your code awaited
  its result unconditionally, so a synchronous function ran, returned a
  value, and then failed on awaiting that value. Fifty-nine seams were
  affected, and between them they covered most of the surface you write
  against: every `on_*` override on a view (`on_load`, `on_submit`,
  `on_state_changed`, `on_error`, `on_finish`, `on_pre_send`,
  `on_unauthorized`, `on_restore`, `on_message_delete` and the rest),
  the `seed_initial_state` preload hook,
  plus store subscribers and `store.on()` hooks, custom reducers,
  component callbacks, the `with_loading_state` / `with_confirmation` /
  `with_cooldown` wrappers, `with_retry` and `with_error_boundary`, wizard
  and tab builders and validators, `choice_row` and `cycle_button` and
  `toggle_button` handlers, and the paginated cursor's fetch function.
  What surfaced varied by seam and none of it named the cause: a wizard
  builder gave `'list' object can't be awaited` at render, a subscriber
  logged an error against the user's own callback, and a view whose
  `on_load` was missing its `async` failed to send at all. A custom
  reducer was the quietest of them: the store caught the failure and
  logged it, so the action completed and the state never changed. These now
  test the result with `inspect.isawaitable`, which is what the rest of
  the library already did, and take either shape. A
  wizard step's `condition` is the inverse
  case: it was never awaited, and a coroutine is always truthy, so an async
  predicate rendered its step regardless of the answer, with nothing raised
  and nothing logged. `WizardStep` now rejects a coroutine function at
  construction, matching how `Collapsible` treats `reveal`, and the
  visibility check recognises an awaitable answer, warns, and shows the
  step. Both are needed: a step declared as a raw dict never passes through
  `WizardStep`, and neither construction check sees an object whose
  `__call__` is async.
- **A `build_ui` that returned a coroutine had the library's own work done
  against a tree it had not built.** `__init_subclass__` wraps `build_ui`
  to set the ambient theme and stabilize `custom_id`s, choosing a sync or
  async wrapper by inspecting the function. Several shapes answer "not a
  coroutine function" and still return a coroutine (a callable instance
  with an async `__call__`, a `partial` around one, a plain function
  returning one), so they took the sync wrapper, which stabilized ids and
  tore the theme down before the body ran. The ids therefore kept the
  random hex discord.py assigns, which changes on every rebuild and churns
  the dispatch table that stabilization exists to keep still, and a `card()`
  built with no explicit colour missed the view's theme. The sync wrapper
  now checks what it got back and, holding a coroutine, returns one that
  re-enters the theme and stabilizes once the body has resolved. It stays
  synchronous otherwise, because three classes call `build_ui()` from
  `__init__`.
- **An async `build_ui` on a pattern that builds in `__init__` did
  nothing, quietly.** `DisplayLayoutView`, `MenuLayoutView` and
  `RolesLayoutView` compose their tree in the constructor, which cannot
  await. An `async def build_ui` there was called and discarded: the view
  constructed, its tree stayed empty, and the only signal was a "never
  awaited" warning most bots never surface. The mistake then arrived at
  `send()` as a placement error naming "no top-level components" -- the
  symptom, nowhere near the cause. All three now refuse it where it
  happens, naming the class and pointing at `on_load()`, the hook the
  library awaits before every render. The refusal covers the shapes an
  `iscoroutinefunction` check misses, since it reads what `build_ui`
  returned rather than what it looks like.
- **A wizard step validator's answer was unpacked without being read.** The
  documented return is `(valid, error)`, and the code took it apart on the
  spot. A bare `True`, the obvious near miss, raised `cannot unpack
  non-iterable bool object` and reached the user as "Something went wrong."
  Worse was the shape that did not raise: any two-element iterable unpacks,
  so a two-character string or a two-key dict handed back a truthy first
  element and the wizard advanced past the step the validator was
  rejecting. A bare bool is now read as the answer it plainly is, a pair is
  read as documented, and anything else is refused with a message naming
  the step and the shape it owes.
- **A component interaction recorded its value nested inside itself.**
  `ActionCreators.component_interaction` collected `value=` into a keyword
  bag and then stored the bag under `value`, so every click wrote
  `{"value": {"value": True}}` into `state["components"]`. `value` is now a
  named parameter and records what it was given; any other keyword lands
  beside it under its own name. Nothing in the library read the nested
  shape, but a subscriber that dug through the extra layer needs to stop.
- **A computed value could cache the wrong answer permanently.** The memo
  compared the selector's output against the previous output, but held
  that previous output by reference. A slice mutated in place carried the
  stored reference with it, so the check compared the slice against
  itself and kept returning the stale result. A later correct replacement
  did not recover it either: the aliased reference already matched the new
  value while the cached result predated it. `access_slot` mutates in
  place by design and `seed_initial_state` hands it live state, so a
  computed read before another view seeded its slot stayed wrong for the
  life of the store. The memo now keeps its own copy, and recomputes every
  time rather than trust an input it cannot copy.
- **Constructing a `Theme` edited the dict you constructed it from.** The
  six style defaults were seeded into the caller's own mapping, so passing
  a dict to `Theme` silently added keys to it, and two Themes built from
  one base dict shared a single live mapping where restyling either
  restyled both. The styles are copied before the defaults are applied.
- **A failed cosmetic edit could cancel the action it was decorating.**
  `with_loading_state` shows its loading state before running the wrapped
  callback. On a plain `discord.ui.View` that pre-edit caught only the
  already-answered case, so a deleted message or a transient 5xx
  propagated and the click did nothing -- while the branch three lines
  above, for stateful views, had always swallowed the same failure. The
  two branches agree now.
- **Three exception types Discord raises are siblings, and half the
  library caught only one of them.** `RateLimited` and
  `InteractionResponded` do not inherit from `HTTPException`, so
  `except discord.HTTPException` misses both. `RateLimited` reaches user
  code whenever the bot passes `max_ratelimit_timeout` to its client, and
  the escapes were not cosmetic: the ephemeral-reopen cleanup skipped its
  `exit()` and left the replaced view registered holding its instance-limit
  slot, a participant-limit rejection skipped the send rollback and leaked
  a registered view, `with_confirmation` skipped the confirmed action its
  own comment promised to run, and a rate-limited acknowledgement during
  navigation became a full rollback and an error embed. Every seam in the
  view base, navigation, the interaction helpers and the component
  wrappers now catches what it meant to, and says which of the three
  happened rather than printing two question marks where a `RateLimited`'s
  `retry_after` belongs.
- **A refresh past the ephemeral cliff reported an error nobody could act
  on.** `exit()` and `on_timeout()` both read the 401 an expired 15-minute
  webhook token returns as ordinary lifecycle and log it at debug.
  `refresh()` re-raised it instead, so every state dispatch reaching such a
  view surfaced an ERROR and a full traceback from the store's subscriber
  wrapper, for a condition the library treats as normal at every other edit
  seam. It is absorbed at the same level as its siblings now. Navigation
  inherits the change: an edit onto a message no token can reach is no
  longer a reason to roll a push or pop back to its source.
- **A `slot_property` declared wrong returned its default forever.** The
  `key=` callable was never checked at construction, and errors raised by
  the callable itself were swallowed by the same handler that absorbs a
  store that is not wired yet, so `key="user_id"` (a string, not a
  callable) and `key=lambda self: self.usr_id` (a typo) both read as a
  permanent default that looks correct in every test. A non-callable
  `key=` is now refused where it is declared, and one that raises is
  reported instead of being folded into the ordinary empty read -- once
  per class, since a descriptor is read on every attribute access and a
  line per read buries the one that matters.
- **`ToggleGroup` built colliding `custom_id`s.** Ids came from the option
  label alone, so two groups in one view that shared a label sent Discord
  a duplicate id and the message was rejected. A `key=` prefix
  disambiguates them, matching `choice_row`'s `custom_id=`. The id is
  otherwise built exactly as before, so a persistent panel already on
  screen keeps the ids its buttons are registered under.
- **`CompositeComponent.add_to_view` took a `row` and ignored it.** The
  parameter has always been in the signature and never read, so components
  landed wherever discord.py placed them. It now sets the row it was
  given.
- **`state["components"]` disappeared once a view was destroyed.** Two
  reducers removed the key entirely when the last entry was filtered out,
  even though it is one of the four keys the initial state establishes. A
  subscriber that indexed it directly worked until the first teardown and
  then raised. It is emptied now, not removed.
- **A reducer registered for `BATCH_COMPLETE` silently never ran.** The
  batch commit fires that action straight at subscribers and hooks,
  bypassing dispatch, so no reducer could ever see it -- and the collision
  guard did not list it, so registering one looked like it worked. It is
  reserved now, and `@cascade_reducer` says so at decoration time.
- **An async subscriber selector was accepted and did the opposite of its
  job.** The change check runs inline in dispatch and cannot await, so an
  `async def` selector handed back a fresh coroutine on every call. A
  coroutine never equals the previous one, so the subscriber was notified
  on every single action (precisely what passing a selector asks the
  store to avoid), and each unawaited coroutine printed a warning from
  the bot's own console. `subscribe` now refuses one where it is passed,
  including the two shapes a plain `iscoroutinefunction` check misses: an
  object with an async `__call__`, and a `functools.partial` around a
  coroutine function.
- **The selector guard reached one of the three places a selector is
  supplied.** `subscribe` refuses an async selector, but it only ever sees
  what it is handed, and a view's `state_selector` override arrives wrapped
  in a lambda that is never a coroutine function whatever it closes over.
  An `async def state_selector` therefore sailed past the check and the
  view went silently deaf: the store compared one fresh coroutine against
  the last, they never matched, and the notification was skipped on every
  dispatch. `@computed`'s selector was not checked at all, not even for
  being callable. Both are refused now, where they are declared, and both
  catch the shapes a plain `iscoroutinefunction` misses.
- **Restoring persistent views warned about the pattern it documents.**
  A view class not yet imported when `initialize()` runs is the state the
  two-pass design exists to absorb: the row lands in `skipped` and
  `reattach()` collects it once the cog has loaded, which is what makes
  import order stop mattering. Each such row was reported at warning level
  anyway, with advice to import the module before `initialize()` -- the one
  thing the sanctioned pattern deliberately does not do. A bot with a dozen
  panels logged a dozen wrong warnings on every boot, which teaches an
  operator to skim the level. That detail now sits at debug, where the summary's own
  comment already said per-view detail belonged, and the warning is saved for
  a class still missing after a `reattach()` pass, when it is a real problem.
  Unreachable channels got the same treatment: one warning naming the count
  rather than one per row, since the count is what an operator acts on.
- **A broken selector was silent forever.** A subscriber selector that
  raised was swallowed with no log at all, on every dispatch, degrading to
  notify-on-everything -- which is the safe answer, and indistinguishable
  from a subscriber that legitimately wants every action. It reports once
  per subscriber now. Three other silences got the same treatment: an
  attached child that failed to exit left no trace before the list was
  cleared, a gateway wait that failed skipped the post-ready re-render
  without saying so, and a failed ephemeral reopen blamed a "refresh
  factory" on the path where the caller never supplied one.
- **`SlotPolicy(persistent=True)` never persisted anything.** Declaring a
  slot persistent in `ApplicationPersistence.slots`, or through
  `register_slot_policy`, registered its retention policy and not the slot
  itself. The middleware scans an opt-in set that neither route wrote to, so
  it skipped the slot entirely and a setup copied from the persistence guide
  put nothing on disk and said nothing about it. Both routes now register
  the slot the way `persistent_slots` and `access_slot(persistent=True)` do.
  Slots declared this way begin persisting, and rehydrating on restart, as
  soon as you upgrade, which is what declaring them always claimed.
- **The missing-aiosqlite error named a package that does not exist.** The
  zero-config path told you to run `pip install 'cascadeui[sqlite]'`; the
  distribution is `pycascadeui`, which the two backend modules had right
  all along. The name is written by hand at each hint, so a test now walks
  the source and fails on any that stops matching the distribution.
- **A shutdown could discard the writes it was shutting down to save.**
  The flush drains its dirty rows into a local batch before handing them
  to the backend, and the retry that puts a failed batch back caught
  `Exception`, which `CancelledError` is not. So a cancel arriving while
  the write was in flight lost the batch outright: buffers empty, nothing
  committed, nothing retried. `flush_all` opened exactly that window on
  every shutdown, cancelling in-flight flushes before its own final
  drain, which is the one path `close` exists to guarantee. A cancelled
  batch is now returned to the buffers under the same both-sides guard
  the retry uses, so the drain that follows finds it.
- **The attachment example taught a one-image technique without its bound.**
  `refresh(attachments=[...])` replaces the message's whole attachment list,
  which is correct for the single-image swap the example demonstrates and
  wrong the moment a tree references two. Adapting it to paged content
  ships each page's reference with only that page's bytes attached,
  and every other page renders as a permanent loading placeholder. The
  example now states that the list must carry every file the tree
  references, and shows the multi-image shape.

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
  three-second deadline, sending the operator after the wrong cause. Discord
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
