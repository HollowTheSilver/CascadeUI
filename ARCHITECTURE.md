# Architecture

CascadeUI is a state-management and UI framework built on top of
discord.py. It replaces ad-hoc view logic -- scattered attribute mutation,
manual message edits, hand-rolled navigation stacks -- with a centralized
store, dispatched actions, and reducer-driven updates. The architecture
borrows from the Redux family (unidirectional data flow, pure reducers,
middleware pipeline) and adapts the pattern to Discord's component model.

This document describes the load-bearing choices behind the library and
explains how the pieces fit together. It is written for developers
evaluating the library for their own bot and for contributors who want
the architectural "why" before reading the code.

For day-to-day usage, start with the [Quick Start](docs/guide/quickstart.md)
and the [Core Concepts](docs/guide/concepts.md) guide. Familiarity with
discord.py's `View`, `LayoutView`, `Interaction`, and `WebhookMessage`
primitives is assumed.

---

## Unidirectional Data Flow

Every state change in a CascadeUI application follows the same path:

```
Component callback
       │
       ▼
dispatch(action)
       │
       ▼
Middleware pipeline  (logging -> persistence -> undo -> custom)
       │
       ▼
Reducer (pure function)
       │
       ▼
Subscriber notifications  (filtered by action + selector)
       │
       ▼
on_state_changed() -> build_ui() -> refresh()
       │
       ▼
Discord message edit
```

The flow is strict in both directions. Callbacks dispatch actions; they do
not mutate state. Reducers transform state; they do not perform I/O.
Subscribers react to state; they do not dispatch during their own
notification frame. These rules collapse a large class of concurrency
bugs (racing message edits, stale reads, notification storms) into
deterministic, testable code paths.

See [Core Concepts](docs/guide/concepts.md) for the pipeline in full
detail.

---

## The Five Pillars

Every view-layer feature belongs to exactly one of five domains:

| Pillar | Question answered | Surface |
|--------|-------------------|---------|
| **Access Control** | Who can interact? | `owner_only`, `allowed_users`, `on_unauthorized` |
| **Instance Constraints** | How many exist? What happens at the limit? | `instance_limit`, `instance_scope`, `instance_policy`, `replace_policy`, `protect_attached`, `participant_limit` |
| **View Lifecycle** | What happens on timeout, error, or exit? | `timeout`, `exit_policy`, `on_timeout`, `on_error`, `auto_defer`, `enable_undo` |
| **Session Membership** | Which views share data? | `session_id`, `shared_data`, `attach_child`, `session_continuity` |
| **Navigation** | What view came before? | `push`, `pop`, `replace`, `auto_back_button` |

The pillars are independently tunable. A view with `owner_only = True` can
still have `instance_limit = None`. A view with `instance_limit = 1` can
still allow multiple users through `allowed_users`. No attribute straddles
two pillars, so reading the class body produces a stable mental model of
which data lives where.

See [The Five Pillar Model](docs/guide/five-pillars.md) for the full
attribute tables and orthogonality examples.

---

## The discord.py Boundary

CascadeUI wraps discord.py rather than replacing it. The library adds a
state layer, a lifecycle layer, and a pattern layer; everything below the
component interface remains discord.py.

**Inherited from discord.py (unchanged):**

- Component classes (`Button`, `Select`, `Modal`, `TextInput`)
- `View` and `LayoutView` base classes
- `Interaction`, `WebhookMessage`, and the REST adapter
- Gateway events, intents, and command registration
- `Client` / `Bot` / `commands.Cog` infrastructure

**Added by CascadeUI:**

- Centralized `StateStore` with pub/sub subscriptions and a middleware
  pipeline
- Per-view state integration (`session_id`, `nav_stack`, `shared_data`,
  `enable_undo`)
- Class-attribute policy surface for access control, instance
  constraints, lifecycle, and capacity
- `_send_pipeline` wrapping the discord.py send/edit cycle with
  three-tier rollback on failure
- Seven pre-built pattern classes (Menu, Form, Wizard, Tab, Paginated,
  Leaderboard, Roles), each with V1 and V2 variants where applicable
- `PersistenceBackend` Protocol with capability flags and opt-in per-slot
  routing
- Component convenience wrappers (`StatefulButton`, `StatefulSelect`),
  V2 builders (`card`, `action_section`, `key_value`, `alert`), and
  decorators (`with_loading_state`, `with_confirmation`, `with_cooldown`)
- Auto-defer safety net, interaction serialization, and refresh
  throttling

Everything CascadeUI adds can be incrementally adopted. A bot can
install the library, apply `StatefulView` to a single panel, and leave
the rest of its codebase on raw discord.py.

---

## Decision Log

The load-bearing architectural choices behind the library, with the
alternatives considered and the reasons for the final decision.

### 1. Redux-inspired state flow over hand-rolled observers

**Decision:** Centralized store, pure reducers, middleware pipeline.

**Alternatives considered:** Per-view observer callbacks; event emitters;
direct attribute mutation with explicit `refresh()` calls.

**Reason:** Observer patterns scale poorly once multiple views share
state; they produce notification storms and ordering ambiguity. A store
with middleware serializes mutations through a single path, which makes
debugging (logging middleware), persistence (persistence middleware),
and undo (undo middleware) composable rather than cross-cutting. Pure
reducers make unit testing state transitions mechanical and eliminate
an entire class of race conditions where two callbacks mutate the same
attribute concurrently.

### 2. Opt-in persistence per slot

**Decision:** Persistence is off by default. Slots participate by
declaring `persistent_slots = ("slot_name",)` on the view class or by
calling `state_slot(..., persistent=True)`.

**Alternatives considered:** Opt-out (persist everything by default);
all-or-nothing (middleware-wide flag).

**Reason:** Opt-out is the wrong polarity for a library. Ephemeral UI
state (hover, focus, transient selections) should never hit disk, and
making users remember to mark data non-persistent leaks state by
default. All-or-nothing middleware flags force a choice between fully
ephemeral and fully persisted, which is too coarse for real
applications that mix both.

### 3. `PersistenceBackend` as Protocol with capability flags

**Decision:** Backends implement a Python `Protocol` and declare their
capabilities via a `Capability` flag enum (`KV`, `RELATIONAL`,
`SCHEMA_META`).

**Alternatives considered:** Abstract base class forcing a SQL-shaped
interface; hard-coded SQLite integration.

**Reason:** A Protocol does not force inheritance, which makes adapting
existing storage libraries (Redis, Postgres, DynamoDB) a matter of
implementing the methods. Capability flags prevent the middleware from
assuming SQL semantics on a KV-only backend, or vice versa.
`InMemoryBackend` ships as the reference implementation and the
default for tests; it exercises every Protocol method so downstream
backend authors have a working contract to match.

### 4. V1 and V2 component systems are peers, not a migration

**Decision:** Every pattern ships V1 and V2 variants (except
Leaderboard, which is V2-only). V1 is not deprecated in favor of V2.

**Alternatives considered:** V2-only with a compatibility shim for V1;
V1-only with a slow V2 port.

**Reason:** V1 (`View` + embeds) and V2 (`LayoutView` + Containers) are
both first-class Discord component models, not successive versions of
one system. V1 is denser for embed-heavy UIs (leaderboards shown in a
single embed, dashboards with many fields). V2 is better for
component-first UIs (interactive cards, sectioned layouts, media
galleries). Forcing migration would make the library useful only for
one shape of bot.

### 5. Single `_StatefulMixin` instead of duplicated V1 and V2 bases

**Decision:** All view-agnostic logic lives in `_StatefulMixin`
(~1960 lines). `StatefulView` and `StatefulLayoutView` are thin
subclasses that supply their version-specific `send()` signature and
component layout overrides.

**Alternatives considered:** Separate `StatefulView` and
`StatefulLayoutView` hierarchies with independent copies of access
control, lifecycle, session logic, and navigation.

**Reason:** Duplication would guarantee drift between the two versions
as the library evolves, and it would push responsibility for feature
parity onto every future change. The mixin centralizes every policy
attribute, every hook, and every lifecycle seam, so adding a new
feature happens in one place and both versions inherit it
automatically.

### 6. Acting-view fast path for interaction refresh

**Decision:** The subscriber whose view owns the current interaction
edits the message through `interaction.response.edit_message`,
collapsing the interaction ack and the UI edit into a single HTTP
request.

**Alternatives considered:** Always defer then edit through the channel
endpoint (two HTTP calls); always edit through the webhook endpoint.

**Reason:** The two-call path adds a round-trip to every click, which
consumes rate-limit budget and widens the window where a second click
lands mid-edit. The fast path eliminates the extra call for the common
case (one click, one refresh, one edit) and falls back to the channel
endpoint for cross-view subscribers and rate-limited seams. The fast
path is scoped via `contextvars` so only the acting subscriber takes
it; background subscribers route through the channel endpoint as
before.

### 7. Scoped state as an organization pattern, not a persistence tier

**Decision:** Scoped data (`get_scoped`, `set_scoped`,
`dispatch_scoped`) lives under `state["application"]["scoped"]`.
Scoping is a key-routing convention, not a separate persistence
namespace.

**Alternatives considered:** Dedicated `scoped` top-level namespace
with its own persistence configuration.

**Reason:** A separate namespace would force a choice between "scoped"
and "application" when the distinction is about routing (per-user vs
per-guild vs global), not about durability. Keeping scoped under
application means the persistence opt-in (`persistent_slots =
("scoped",)` or a named scoped slot) uses the same mechanism as any
other slot.

### 8. Three-tier rollback in `_send_pipeline`

**Decision:** `send()` cleans up progressively more state as each
success stage is passed (instance-limit check, participant
registration, Discord HTTP). A failure at stage N reverses stages 1
through N-1.

**Alternatives considered:** Best-effort cleanup with logging; no
rollback (let user code handle partial failures).

**Reason:** Library code that registers state before talking to Discord
must reverse those registrations if Discord rejects the send; leaving
half-registered views in the state store corrupts downstream reads.
The three-tier model mirrors the acquisition order so every
partial-failure path has an exact counterpart. The test suite exercises
each tier against a mock Discord client.

### 9. Armed ephemeral freeze before the 15-minute cliff

**Decision:** Ephemeral views with `auto_refresh_ephemeral = True`
install a refresh button at T+810 seconds and suppress state-driven UI
rebuilds from that point until T+900. On refresh click, the view is
reconstructed against a fresh interaction token.

**Alternatives considered:** Let the view expire silently at T+900;
attempt webhook-token reuse (not supported by Discord).

**Reason:** Discord's webhook tokens expire 15 minutes after the
originating interaction. A long-running ephemeral view (a settings
panel, a multi-step wizard) hits the cliff with no fallback. Freezing
the UI at T+810 preserves the refresh button so the user can continue
without losing state; without the freeze, a state notification between
T+810 and T+900 would rebuild the component tree and discard the
refresh button before the user reached it.

### 10. Class-attribute validation at `__init_subclass__`

**Decision:** Policy enums, numeric ranges, and boolean attributes are
validated when the subclass is defined, not when the view runs.

**Alternatives considered:** Runtime validation at `__init__`; no
validation (trust user code).

**Reason:** A typo in `instance_policy = "rejct"` should fail at module
import, not forty clicks into a game when a user triggers the limit.
Validating at subclass-definition time produces clear errors at the
right call site with zero runtime cost.

### 11. Navigation atomicity via defer-teardown

**Decision:** `push()` and `pop()` defer the source view's teardown until
the destination edit confirms. The source is unsubscribed but not
destroyed until the edit succeeds; a failed edit rolls back to the live
source instead of a torn-down one.

**Alternatives considered:** Tear the source down before the edit (the
original order); best-effort teardown with logging on edit failure.

**Reason:** Tearing the source down first meant a failed edit (a missed
ack, an expired token, a transient 5xx) left the message showing a view
whose handlers were already gone, so every later click was silently
dropped. Deferring teardown past the fallible edit makes navigation
all-or-nothing: on success the source is committed-destroyed, on failure
it is re-subscribed and stays clickable on the message it still owns.
`replace()` keeps inline teardown because it is a one-way transition with
no source to recover to.

### 12. Duplicate custom_id detection over auto-uniqueness

**Decision:** Two components in one view, or two inputs in one modal, that
share a `custom_id` raise a directed error at build time. The library
never rewrites the id to make it unique.

**Alternatives considered:** Auto-uniquify by appending a per-instance
suffix; leave it to Discord's HTTP 400 at send.

**Reason:** Persistent view reattachment matches the exact `custom_id`
Discord stored on the original message, so a suffix minted fresh on
restart dead-ends the button. Raising instead surfaces the collision
at the call site rather than as a render-time 400 (or, for modal inputs, a
silent value overwrite). The ids the stabilizer assigns are already
collision-free by tree-position anchoring, so only caller-supplied ids
reach this check, where a collision is always a genuine mistake.

---

## When CascadeUI Fits

CascadeUI is designed for bots that ship stateful, interactive UI as a
first-class concern. The library earns its complexity when:

- Multiple views share state and react to each other (settings hubs with
  sub-pages, leaderboards with player-specific overlays, games with
  public boards and private fleet panels).
- Persistence is required (role panels, ticket systems, dashboards
  surviving restarts).
- Patterns repeat across views (forms with validation, wizards with
  per-step state, paginated lists, tabbed layouts).
- Interaction timing and rate-limit behavior matter (rapid clicking,
  concurrent players, long-running ephemeral panels).

## When CascadeUI Is Overkill

- **Single-view bots with no shared state.** A command that sends an
  embed and exits does not need a state store. Raw discord.py is the
  right choice.
- **Purely message-based bots.** CascadeUI is about components and
  views. Bots that send only text or embeds without interactive
  elements will not use most of the library.
- **Multi-tenant plugin isolation.** Reducers are not sandboxed;
  middleware has full state access. Applications that need per-plugin
  isolation build that layer above CascadeUI.

---

## Composition Model

The library composes as a stack of concerns. Each layer depends only
on the layers below it:

```
┌─────────────────────────────────────────────────────────┐
│  Patterns (Menu, Form, Wizard, Tab, Paginated,          │
│            Leaderboard, Roles -- V1 / V2 variants)      │
├─────────────────────────────────────────────────────────┤
│  StatefulView, StatefulLayoutView, PersistentView       │
│  (three-tier rollback, auto-defer, refresh throttling)  │
├─────────────────────────────────────────────────────────┤
│  _StatefulMixin (access, instance, lifecycle,           │
│                  session, navigation)                   │
├─────────────────────────────────────────────────────────┤
│  StateStore (pub/sub, batching, @computed, middleware   │
│              chain, active-view registry)               │
├─────────────────────────────────────────────────────────┤
│  PersistenceBackend Protocol (InMemory, SQLite, custom) │
├─────────────────────────────────────────────────────────┤
│  discord.py (View, LayoutView, Interaction, Webhook)    │
└─────────────────────────────────────────────────────────┘
```

Each layer is replaceable:

- Patterns can be subclassed or replaced with custom implementations.
- `StatefulView` and `StatefulLayoutView` can be extended with new
  mixins or bypassed entirely; raw discord.py views coexist with
  CascadeUI views in the same bot.
- `StateStore` is a singleton, but tests construct fresh instances;
  the module-level `@computed` registry re-seeds on every store
  construction.
- `PersistenceBackend` accepts any Protocol-conforming implementation.
  Swapping SQLite for Redis or Postgres requires no changes above the
  backend layer.

---

## Further Reading

- **[Core Concepts](docs/guide/concepts.md)** -- data flow, interaction
  lifecycle, state topology
- **[Five Pillar Model](docs/guide/five-pillars.md)** -- attribute
  tables, orthogonality examples, state tree
- **[View Patterns](docs/guide/patterns.md)** -- Menu, Form, Wizard,
  Tab, Paginated, Leaderboard reference
- **[State Management](docs/guide/state.md)** -- reducers, subscribers,
  scoped state, `@computed`
- **[Persistence](docs/guide/persistence.md)** -- backend Protocol,
  opt-in slots, schema migrators
- **[Performance](docs/guide/performance.md)** -- render-hash, fast
  path, refresh throttling, profiling
- **[CHANGELOG](CHANGELOG.md)** -- v3.0.0 feature catalog and
  subsequent diffs
- **[CONTRIBUTING](CONTRIBUTING.md)** -- design philosophy, API grammar
  rules, inclusion bar for new patterns
