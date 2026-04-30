# Views

Views are the primary UI containers in CascadeUI. They integrate discord.py's
view system with a centralized state store, lifecycle management, and task
tracking.

CascadeUI supports two component systems:

- **V2 (recommended)** -- `StatefulLayoutView` wraps discord.py's `LayoutView`.
  Content and controls live together in containers with accent colors. The view
  IS the message content.
- **V1 (classic)** -- `StatefulView` wraps discord.py's `View`. Embeds sit on
  top, buttons float below. Content and controls are visually separated.

Both share the same state integration, navigation stack, instance limiting,
undo/redo, and all other framework features through a shared `_StatefulMixin`.

For the full policy attribute reference, see [Core Concepts -- Policy Surface](concepts.md#policy-surface).

---

## V2 Views (LayoutView)

The base class for V2 views. Unlike V1, there are no `content` or `embed`
parameters on `send()` -- the component tree IS the message:

```python
from cascadeui import StatefulLayoutView, StatefulButton, card, divider
from discord.ui import ActionRow, TextDisplay
import discord

class MyView(StatefulLayoutView):
    instance_limit = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_ui()

    def build_ui(self):
        self.clear_items()
        self.add_item(card(
            "## My Dashboard",
            TextDisplay("Welcome to the V2 interface."),
            divider(),
            ActionRow(
                StatefulButton(
                    label="Click Me",
                    style=discord.ButtonStyle.primary,
                    callback=self.on_click,
                ),
            ),
            color=discord.Color.blurple(),
        ))
        self.add_exit_button()

    async def on_click(self, interaction):
        self.build_ui()
        await self.refresh()
```

### Sending

```python
view = MyView(context=ctx)
await view.send()  # No content/embed params -- the component tree is the content
```

### Key Differences from V1

| | V2 (`StatefulLayoutView`) | V1 (`StatefulView`) |
|---|---|---|
| Content | Component tree (Containers, TextDisplay) | Embeds + content string |
| `send()` | No content/embed params | Accepts content, embed, embeds |
| Interactive items | Must be wrapped in `ActionRow` | Can be added directly |
| Exit behavior | Freezes components in place | Strips view, keeps embed |
| Accent colors | Per-container via `card(color=...)` | One embed color |
| Components per message | Up to 40 | Up to 25 (5 rows × 5) |

!!! warning "ActionRow wrapping"
    Buttons and selects cannot be top-level children of a `LayoutView`. Always
    wrap them in `ActionRow` before calling `add_item()`. The V2 builder
    functions (`card`, `action_section`, `toggle_section`) handle this
    automatically when buttons are part of a container.

!!! warning "V2 exit behavior"
    Calling `message.edit(view=None)` on a V2 message produces an empty message
    (Discord error 50006) because the view IS the content. CascadeUI handles
    this automatically -- `exit()` freezes all components with
    `_freeze_components()` and edits with the frozen view, preserving visual
    content.

### DisplayLayoutView -- One-Shot V2 Sends

`DisplayLayoutView` is a parameterized `StatefulLayoutView` for cases where
the goal is to send a pre-built V2 container without authoring a full view
subclass. Pass the container as a `container=` kwarg and call `send()`:

```python
from cascadeui import DisplayLayoutView, card, key_value

body = card(
    "## Session Stats",
    key_value({"Games": 5, "Wins": 3}),
)
await DisplayLayoutView(context=ctx, container=body).send(ephemeral=True)
```

Interactive items inside the container still route through the normal
dispatch pipeline -- `DisplayLayoutView` trades per-instance state (no
`build_ui` override, no custom hooks) for the ability to instantiate
directly. Defaults differ from `StatefulLayoutView` to match the common
one-shot use case:

| Attribute | Default | Reason |
|---|---|---|
| `owner_only` | `False` | Display cards are typically public |
| `state_scope` | `None` | No per-scope state slice needed |

Use it for ephemeral confirmations, stats readouts, and error panels that
don't warrant a dedicated class.

---

## V1 Views (Classic)

The V1 base class wraps discord.py's `View` with embed-based content:

```python
from cascadeui import StatefulView, StatefulButton
import discord

class MyView(StatefulView):
    instance_limit = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(StatefulButton(label="Click Me", callback=self.on_click))
        self.add_exit_button()

    def build_ui(self):
        return {"embed": discord.Embed(title="My View", description="Hello!")}

    async def on_click(self, interaction):
        await self.respond(interaction, "Clicked!", ephemeral=True)

view = MyView(context=ctx)
await view.send(**view.build_ui())
```

V1 views use embeds for content with buttons below. `build_ui()` returns a
dict splatted into `refresh()` by the default `on_state_changed()`.

For pre-built patterns (Menu, Form, Wizard, Tab, Paginated, Leaderboard,
Roles) in V1 and V2 where applicable, see [View Patterns](patterns.md).

---

## Lifecycle

Every view follows the same lifecycle:

1. **Init** -- view created, components added, subscribed to state store
2. **Send** -- message sent, state registered (`VIEW_CREATED`, `SESSION_CREATED`)
3. **Interact** -- user clicks buttons/selects, callbacks fire
4. **Exit/Timeout** -- components disabled, state cleaned up (`VIEW_DESTROYED`)

### Timeout

Views timeout after 180 seconds by default. On timeout, all components are
disabled, the message is edited, and the view unsubscribes from the store.

```python
super().__init__(*args, timeout=300, **kwargs)   # 5 minutes
super().__init__(*args, timeout=None, **kwargs)  # Never timeout
```

!!! note "Ephemeral timeout derivation"
    `send(ephemeral=True)` adjusts `timeout` based on `auto_refresh_ephemeral`:

    - When `auto_refresh_ephemeral = True` (or left at the `None` default that
      auto-engages on long timeouts), a timeout below `86400s` (24 hours) is
      bumped up to `86400s`. The refresh handoff re-opens the view on a fresh
      interaction token before the webhook's 900-second cliff, so the state
      store lifetime must extend past it.
    - When `auto_refresh_ephemeral = False`, a missing or larger timeout is
      clamped down to `900s` to match the interaction token's 15-minute
      cliff, so the view does not linger after its webhook token expires.

    The derivation is centralized in `_send_pipeline` -- every view that
    routes through `send()` (patterns included) inherits the policy.

!!! tip "Long-lived non-ephemeral views"
    After `send()`, both view classes re-fetch the message as a plain `Message`
    via `channel.fetch_message()`. This replaces the `InteractionMessage` whose
    `edit()` expires with the 15-minute interaction token. The plain
    `Message.edit()` uses the channel REST endpoint with no token expiry, so
    `refresh()` and `on_timeout()` work indefinitely. Ephemeral messages skip
    the re-fetch (not fetchable via channel).

---

## Reacting to State Changes

If a subclass defines `build_ui()`, the default `on_state_changed()` calls
it and then `refresh()` automatically. The minimal stateful view only needs
`build_ui()` and a `subscribed_actions` set.

**V2 views** mutate the component tree inside `build_ui()` and return `None`:

```python
class MyView(StatefulLayoutView):
    subscribed_actions = {"MY_ACTION"}

    def build_ui(self):
        self.clear_items()
        # ... build the component tree from current state
```

**V1 views** return a dict splatted into `refresh()`:

```python
class MyView(StatefulView):
    subscribed_actions = {"MY_ACTION"}

    def build_ui(self):
        return {"embed": discord.Embed(title="Counter", description=f"Value: {self.value}")}
```

Override `on_state_changed()` only when custom logic beyond rebuild + refresh
is needed:

```python
class CustomView(StatefulLayoutView):
    subscribed_actions = {"GAME_FINISHED"}

    async def on_state_changed(self, state):
        winner = state["application"]["last_winner"]
        if winner == self.user_id:
            await self._play_victory_sound()
        self.build_ui()
        await self.refresh()
```

!!! tip "`subscribed_actions` is opt-in"
    Views receive no notifications by default. Set `subscribed_actions` to the
    action types the view needs to react to:
    ```python
    subscribed_actions = {"GAME_UPDATED", "GAME_FINISHED"}
    ```
    Every matching dispatch fires `on_state_changed()` (which calls `build_ui()`
    + `refresh()` by default), so subscribe only to actions the view reads.
    Set `subscribed_actions = None` to receive all actions (not recommended).

---

## `send()` and Rollback

`send()` handles message creation, state registration, session tracking, and
message reference capture in one call.

**Return value:** the sent `discord.Message` on success, or `None` when the
view was blocked. Two conditions produce `None`:

1. **Instance limit rejection** -- `instance_policy = "reject"` and the user has
   hit `instance_limit`. The `on_instance_limit` hook fires automatically.
2. **Participant registration failure** -- `auto_register_participants = True`
   and a user in `allowed_users` already occupies an instance.

In both cases, the library handles cleanup completely -- no message, no state
tree entry, no registry slot.

```python
view = ExpensiveView(context=ctx)
if await view.send() is None:
    return  # Block was handled by on_instance_limit or on_participant_limit
```

**Overriding `send()`.** Post-send work (starting timers, spawning children)
must be guarded behind `result is not None`:

```python
async def send(self, *, ephemeral: bool = False):
    result = await super().send(ephemeral=ephemeral)
    if result is not None:
        self._start_countdown()
    return result
```

### `refresh(**kwargs)`

Edits the view's message with `view=self` plus any extra kwargs. Does NOT
rebuild components -- call `build_ui()` first. Handles `discord.NotFound`
silently. V2 callers pass no args; V1 callers pass `embed=` or `content=`.

### `set_class_attribute(name, value)`

Override a class attribute for this instance only. Useful when a policy needs
to differ per-invocation without subclassing:

```python
view = MyView(context=ctx)
view.set_class_attribute("instance_limit", 5)
```

---

## Navigation Stack

Push views onto a stack and pop them to go back:

=== "V2"

    ```python
    class HubView(StatefulLayoutView):
        async def go_settings(self, interaction):
            await self.push(SettingsView, interaction,
                            rebuild=lambda v: v.build_ui())

    class SettingsView(StatefulLayoutView):
        async def go_back(self, interaction):
            await self.pop(interaction,
                           rebuild=lambda v: v.build_ui())
    ```

=== "V1"

    ```python
    class HubView(StatefulView):
        async def go_settings(self, interaction):
            await self.push(SettingsView, interaction,
                            rebuild=lambda v: {"embed": v.build_embed()})

    class SettingsView(StatefulView):
        async def go_back(self, interaction):
            await self.pop(interaction,
                           rebuild=lambda v: {"embed": v.build_embed()})
    ```

### The `rebuild` Callback

The Discord message edit fires on every push and pop. `rebuild=` is an
optional pre-edit hook for views that need post-construction setup:

1. The interaction is auto-deferred
2. The optional `rebuild` callback runs against the new view
3. The message is edited with the new view (plus any kwargs the
   callback returned)

V2 `rebuild` typically calls `build_ui()` to populate views that
construct empty. V1 `rebuild` returns a dict of edit kwargs
(e.g., `{"embed": v.build_embed()}`). Sync or async both work. Views
built by async classmethods like `PaginatedLayoutView.from_data` come
fully populated -- omit `rebuild` entirely.

### How It Works

- `push()` stops the current view, stacks it, and creates a new view instance
- `pop()` stops the current view and reconstructs the previous one
- Constructor kwargs are preserved automatically for faithful reconstruction
- The new view inherits `session_id`, keeping navigation within one session

### Pushing Pre-Constructed Instances

`push()` and `replace()` accept either a view class (the default form
shown above) or a pre-constructed view instance. The instance form
pairs with async classmethod constructors -- `PaginatedLayoutView.from_data`
and `from_cursor` -- where the view is built before the navigation call.

```python
class HubView(StatefulLayoutView):
    async def go_inventory(self, interaction):
        # from_data is async; build the view first, then push it.
        child = await InventoryView.from_data(
            items=ITEMS,
            per_page=10,
            formatter=format_inventory_page,
            interaction=interaction,
        )
        # No rebuild -- from_data returns a fully-built paginator and
        # push() edits the message on its own.
        await self.push(child, interaction)
```

Passing extra kwargs alongside an instance raises `TypeError` -- the
instance is already built.

!!! tip "Coming from a paginator gist?"
    If you're migrating from [@Soheab](https://github.com/Soheab)'s
    [CV2 paginator gist](https://gist.github.com/Soheab/891c39d7294b1bdbadc7ecf35ce51cc5)
    or [classic paginator gist](https://gist.github.com/Soheab/f226fc06a3468af01ea3168c95b30af8),
    see the [migration map in the patterns guide](patterns.md#coming-from-a-paginator-gist)
    for the full mapping of gist concepts to CascadeUI's grammar.

### Auto Back Button

```python
class SettingsView(StatefulLayoutView):
    auto_back_button = True  # Added automatically when pushed
```

The auto-added back button survives pattern rebuilds. Paginated page
turns, tab switches, form re-layout, menu refresh, role panel rebuild,
and wizard step advance all call `clear_items()` and recompose the
component tree from scratch. The library re-adds the back button after
each recomposition via `_restore_navigation_artifacts`, so a view that
combines `auto_back_button = True` with a pattern's interactive
controls keeps both reachable across every state-driven rebuild.

### Push vs. Replace

| | `push()` | `replace()` |
|---|----------|-------------|
| Stack | Adds entry, supports back | No stack, one-way |
| Session | Shared | Shared |
| V1/V2 mixing | Blocked | Allowed |
| Use case | Menu hierarchy | Replacing the view entirely |

!!! warning "Push/pop between V1 and V2"
    `push()` and `pop()` between V1 and V2 views raises `TypeError`. Discord's
    `IS_COMPONENTS_V2` flag is a one-way switch per message. Use `replace()` for
    cross-version transitions. See
    [Known Limitations](known-limitations.md#v1-and-v2-views-cannot-pushpop-between-each-other).

---

## Instance Management

Most Discord bots track active views manually -- a dict mapping user IDs to
view instances, checked at the top of every command:

```python
# The manual approach (no library support)
active_games = {}

@bot.command()
async def game(ctx):
    if ctx.author.id in active_games:
        await ctx.send("You already have an active game.", ephemeral=True)
        return
    view = GameView()
    active_games[ctx.author.id] = view
    await ctx.send(view=view)
    # ... and you need to remember to clean up on timeout, exit, error, etc.
```

CascadeUI replaces that entire pattern with three class attributes. The library
tracks instances in the state store, enforces limits on `send()`, handles
cleanup on exit/timeout/error, and counts participants (not just owners):

```python
class SettingsView(StatefulLayoutView):
    instance_limit = 1              # Max active instances (None = unlimited)
    instance_scope = "user_guild"  # Scope for counting instances
    instance_policy = "replace"    # What to do when the limit is reached
```

These attributes belong to [Pillar 2 -- Instance Constraints](five-pillars.md#pillar-2-instance-constraints).

### Instance Scope

| Scope | Groups by | Use case |
|-------|-----------|----------|
| `"user"` | User ID | Per-user across all guilds |
| `"guild"` | Guild ID | Per-server, shared by all users |
| `"user_guild"` *(default)* | User + Guild ID | Per-user within each guild |
| `"global"` | Nothing | One instance across the entire bot |

### Instance Policy

| Policy | Behavior |
|--------|----------|
| `"replace"` *(default)* | Exits the oldest view(s) to make room |
| `"reject"` | Blocks `send()`, fires `on_instance_limit`, returns `None` |

### Replace Behavior: `replace_policy`

When replacing, the old message is either deleted or frozen:

```python
class SettingsView(StatefulLayoutView):
    replace_policy = "delete"   # default -- old message is deleted
```

Set `replace_policy = "disable"` to keep the old view visible as a frozen
snapshot (useful for audit trails).

### Attachment Protection: `protect_attached`

By default, views with active participants or attached children from other
users cannot be silently replaced:

```python
class GameView(StatefulLayoutView):
    instance_limit = 1
    instance_policy = "replace"
    protect_attached = True       # default
    participant_limit = 2
    auto_register_participants = True
```

When the owner tries to start a new game while their current game has a
participant or an attached child belonging to another user, the replacement
is blocked and `on_instance_limit` fires on the new view instead. Same-user
attachments do not trigger protection -- the owner can always replace their
own views.

Set `protect_attached = False` to allow silent replacement of views with
active attachments (e.g. spectator panels where replacement is expected).

### Replacement Notification: `on_replaced`

When replacement proceeds (either `protect_attached = False` or the view
has no cross-user attachments), the old view's `on_replaced()` hook fires
before `exit()`. The view is fully intact at this point -- message,
participants, and channel access are all live.

**Zero config** -- silent replacement:

```python
class MyView(StatefulLayoutView):
    replaced_message = None  # default
```

**Static message** -- notify the channel:

```python
replaced_message = "This game has been cancelled."
```

**Dynamic override** -- full control:

```python
async def on_replaced(self):
    if self._participants and self._message:
        mentions = " ".join(f"<@{p}>" for p in self._participants)
        await self._message.channel.send(
            f"{mentions} game cancelled - the host started a new one."
        )
```

Errors in `on_replaced` are logged but never block the new view's `send()`.

### Bare Exit Behavior: `exit_policy`

Controls what `exit()` does when called without an explicit `delete_message`:

```python
class MyView(StatefulLayoutView):
    exit_policy = "disable"  # default -- freeze the view
```

Set `exit_policy = "delete"` for close buttons and `on_timeout` to delete the
message.

Both policies follow the
[three-tier precedence model](concepts.md#the-three-tier-precedence-model):
class attribute → method override → explicit argument.

### Handling Rejection: `on_instance_limit`

Under reject policy, the library handles the block automatically:

**Zero config** -- sends an ephemeral with singular/plural phrasing:

```python
class ExpensiveView(StatefulLayoutView):
    instance_limit = 1
    instance_policy = "reject"
```

**Static message** -- override the text:

```python
instance_limit_message = "You already have a report running."
```

**Dynamic override** -- full control:

```python
async def on_instance_limit(self, error: InstanceLimitError) -> None:
    if self.interaction is not None:
        await self.interaction.followup.send(
            f"<@{self.user_id}> is already in another game.",
            ephemeral=True,
        )
```

### InstanceLimitError

The error object passed to `on_instance_limit`:

| Attribute | Type | Meaning |
|---|---|---|
| `view_type` | `str` | Class name of the blocked view |
| `limit` | `int` | The instance limit |
| `blocked_user_id` | `int \| None` | User blocked (`None` for owner rejections) |
| `default_message` | `str` *(property)* | Singular/plural-aware fallback text |

### PersistentView Protection

Persistent views cannot be replaced by non-persistent views. If a regular view
tries to replace a persistent one, `InstanceLimitError` is raised instead.

### Pre-Checking Availability (Command-Level Guard)

The declarative system handles enforcement automatically on `send()`, but
sometimes you want to check *before* constructing the view -- the same place
most bots do their manual `if user_id in active_games` check today:

```python
@app_commands.command()
async def game(self, interaction: discord.Interaction):
    if not GameView.check_instance_available(
        user_id=interaction.user.id,
        guild_id=interaction.guild.id,
    ):
        await interaction.response.send_message(
            "You already have an active game.", ephemeral=True
        )
        return

    view = GameView(user_id=interaction.user.id, guild_id=interaction.guild.id)
    await view.send(interaction)
```

This is useful when `__init__` is expensive (e.g. fetching data from a database)
and you want to bail early. The check counts both owners and participants, so a
user who joined someone else's game counts against their limit. Returns `True`
when no `instance_limit` is set or when scope can't be determined (missing IDs).

### Class Naming and Session Keys

CascadeUI identifies view classes by `f"{cls.__module__}.{cls.__qualname__}"`.
Two classes sharing a short name in different modules are treated as distinct.
Override with `session_class_key` for stability across renames:

```python
class HubView(StatefulLayoutView):
    session_class_key = "myapp.HubView"
```

---

## Interaction Ownership

By default, only the view owner can interact:

```python
class MyView(StatefulLayoutView):
    owner_only = True                                        # Default
    unauthorized_message = "You cannot interact with this."  # Default
```

`PersistentView` and `PersistentLayoutView` default to `owner_only = False`.

### Multi-User Access Control

For views shared between specific users:

```python
class GameView(StatefulLayoutView):
    unauthorized_message = "You're not part of this game."

    def __init__(self, *args, opponent_id: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_users = {self.user_id, opponent_id}
```

The setter coerces both `int` IDs and snowflake-shaped objects (`Member`,
`User`, `Object`).

### Custom Access Control

Override `interaction_check()` for role-based or advanced logic:

```python
class AdminView(StatefulLayoutView):
    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return False
        return True
```

---

## Participants and Multi-User Views

For multi-user views (games, polls, lobbies), `register_participant` adds
non-owner users to the session index:

```python
joined = await view.register_participant(opponent.id, interaction=interaction)
if not joined:
    return  # Library already responded ephemerally
```

`register_participant` is `async`, returns `bool`, accepts `int` or snowflake.
Pass `interaction` so rejection hooks respond on the right interaction.

### Participant Capacity: `participant_limit`

Caps total occupants (owner + participants):

```python
class LobbyView(StatefulLayoutView):
    participant_limit = 8
    participant_limit_message = "This lobby is full."
```

The trio follows the standard policy grammar: `participant_limit` (cap),
`participant_limit_message` (static text), `on_participant_limit()` (dynamic
override).

### Auto-Registration: `auto_register_participants`

For fixed-roster views where the player set is known at construction:

```python
class BattleshipView(StatefulLayoutView):
    participant_limit = 2
    auto_register_participants = True

    def __init__(self, *args, opponent_id: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_users = {self.user_id, opponent_id}
```

Rollback is all-or-nothing and runs before the Discord send.

### Combining `allowed_users` and `participant_limit`

| `allowed_users` | `participant_limit` | Pattern |
|---|---|---|
| **set** | `None` | Fixed roster, unlimited capacity |
| **set** | **int** | Fixed roster, capped (pedagogical redundancy) |
| **empty** | `None` | Open interaction, unlimited |
| **empty** | **int** | Open join, capped (lobby pattern) |

---

## Interaction Handling

### Auto-Defer Safety Net

CascadeUI eliminates manual `interaction.response.defer()` calls for most
callbacks. Three mechanisms work together so the interaction is always
acknowledged, regardless of callback speed or response pattern:

1. **Post-callback defer** -- after every callback, CascadeUI checks whether the
   interaction was acknowledged. If not, it defers automatically. Callbacks that
   use the `dispatch() -> build_ui() -> refresh()` pattern edit the message via
   the channel REST endpoint (not the interaction response), so the interaction
   goes unacknowledged by the callback itself. The post-callback defer catches
   this and acknowledges it instantly.

2. **Timed defer** -- if a callback takes longer than `auto_defer_delay` (default
   2.5s) without responding, a background timer defers proactively. This covers
   slow operations like database queries or API calls.

3. **Interaction serialization** -- when `serialize_interactions = True` (default),
   rapid button clicks are processed sequentially via `asyncio.Lock`. The timed
   defer runs outside the lock, so queued interactions are deferred before
   Discord's 3-second timeout.

```python
class MyView(StatefulLayoutView):
    auto_defer = True        # Default -- enables all three mechanisms
    auto_defer_delay = 2.5   # Seconds before the timed defer fires
    serialize_interactions = True  # Default -- sequential callback processing
```

This means most callbacks need no interaction handling at all:

```python
async def _on_toggle(self, interaction):
    self._enabled = not self._enabled
    self.build_ui()
    await self.refresh()
    # No defer() needed -- CascadeUI handles it after the callback returns
```

Manual `defer()` is not needed in CascadeUI callbacks. For sending messages,
use `self.respond()` (see [below](#sending-ephemeral-feedback-from-callbacks)).
For opening modals, use `self.open_modal()` -- it handles the case where
auto-defer already consumed the response slot:

```python
await self.open_modal(interaction, modal)
```

!!! danger "Never call `interaction.response.defer()` manually"
    Manual `defer()` is actively harmful in two ways. First, under rapid
    clicking with `serialize_interactions = True`, a queued interaction can
    wait longer than `auto_defer_delay` for the callback lock; the timed
    defer fires first, and the callback's own `defer()` then raises
    `InteractionResponded`, killing the rest of the callback (`build_ui()`
    never runs, `refresh()` never runs, the user sees a phantom click).
    Second, pre-deferring flips `interaction.response.is_done()` to `True`,
    which disqualifies the acting-view `edit_message` fast path in
    `refresh()` and forces the refresh through the slower two-call channel
    endpoint path. The auto-defer system already handles acknowledgement
    for every callback. The only places manual defer is appropriate are
    before `interaction.followup.send()` or outside CascadeUI's
    `_scheduled_task` scope (e.g. a raw `discord.ui.Modal.on_submit`).

!!! warning "Patterns deliberately do not pre-defer"
    Library patterns (`PaginatedView`, `TabView`, `WizardView`, `FormView`,
    and their V2 counterparts) do **not** call `_safe_defer()` inside their
    component callbacks, even though the helper is available. Pre-deferring
    inside a callback that rebuilds state and calls `refresh()` starves
    the acting-view fast path. The post-callback defer in
    `_scheduled_task` acks the interaction after the callback returns.
    When writing your own patterns, follow the same shape: no defer, just
    rebuild and `refresh()`. Use `_safe_defer()` only when you explicitly
    want the slow path (e.g. long async work before refreshing).

See [Opening Modals from Callbacks](#opening-modals-from-callbacks) for details.

All three mechanisms check `interaction.response.is_done()` before acting, so
manual `defer()`, `with_loading_state`, `with_confirmation`, and `with_cooldown`
are all safe to combine with auto-defer.

### Sending Ephemeral Feedback from Callbacks

Callbacks that need to send a message back to the user (turn enforcement,
validation errors, confirmations) should use `self.respond()` instead of
`interaction.response.send_message()`:

```python
async def my_callback(self, interaction):
    if not allowed:
        await self.respond(interaction, "Not your turn!", ephemeral=True)
        return
```

`respond()` checks `interaction.response.is_done()` and automatically routes
to `interaction.followup.send()` when the response slot has already been
consumed by auto-defer. This matters under `serialize_interactions` where
queued interactions may wait long enough for the auto-defer timer to fire.

The method accepts all keyword arguments that `send_message` and
`followup.send` accept (`embed=`, `view=`, `file=`, `ephemeral=`, etc.),
and works for both ephemeral and public responses.

### Opening Modals from Callbacks

Callbacks that open a modal should use `self.open_modal()` instead of
`interaction.response.send_modal()`:

```python
async def on_edit(self, interaction):
    modal = Modal(title="Edit Name", inputs=[name_input], callback=self.handle_edit)
    await self.open_modal(interaction, modal)
```

`send_modal()` must be the first response to an interaction -- it cannot
follow a `defer()`. Under `serialize_interactions`, a queued interaction
may be auto-deferred before the callback runs, making raw `send_modal()`
raise `InteractionResponded`. `open_modal()` checks `is_done()` and sends
an ephemeral "please try again" fallback instead of crashing.

The method returns `True` if the modal opened, `False` if the fallback
fired. Pass `fallback_message=` to customize the fallback text.

---

## Ephemeral Views

### Auto-Refresh for Long-Lived Ephemerals

Ephemeral views can survive Discord's 15-minute editability wall by engaging
the `auto_refresh_ephemeral` handoff. The default (`None`) derives the behavior
from `timeout`: short-lived ephemerals (`timeout <= 900`) decline the handoff
and expire naturally; longer timeouts (or `None`) engage it. Shortly before the
wall, the library replaces the view's children with a "Continue Session"
button. Clicking it uses a fresh interaction token to send a replacement
ephemeral with another full 15-minute window.

Pin the behavior explicitly on short display ephemerals if you want to skip
the derivation:

```python
class QuickInfo(StatefulLayoutView):
    auto_refresh_ephemeral = False
```

Customization knobs:

| Attribute | Default | Purpose |
|---|---|---|
| `refresh_warning_seconds` | `90` | How early to swap |
| `refresh_button_label` | `"Continue Session"` | Button text |
| `refresh_button_emoji` | `"🔄"` | Button emoji |
| `refresh_button_style` | `ButtonStyle.primary` | Button style |
| `reopen_failure_message` | `"Could not refresh..."` | Sent when the refresh fails |

Override `_build_refresh_button()` for deeper customization (custom
`custom_id`, row placement, additional callbacks).

### Handling Refresh Failures: `on_reopen_failure`

When the refresh button fails to construct a replacement view, the
`on_reopen_failure` hook fires. Two failure modes:

- **Factory raised** (`error` is an `Exception`): the default implementation
  sends `reopen_failure_message` as an ephemeral.
- **Factory returned `None`** (`error` is `None`): the session has ended. The
  default sends "This session has ended." and calls `exit()`.

```python
class MyView(StatefulLayoutView):
    reopen_failure_message = "Session expired. Run /start again."

    # Or override the hook for full control:
    async def on_reopen_failure(self, interaction, error=None):
        if error:
            await interaction.response.send_message(
                f"Refresh failed: {error}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Done! Thanks for playing.", ephemeral=True
            )
            await self.exit()
```

See [Known Limitations -- Ephemeral Editability](known-limitations.md#ephemeral-editability-expires-after-15-minutes) for the full rationale and ghost-panel behavior.

---

## State Scoping

Isolate state per user or per guild:

```python
class SettingsView(StatefulLayoutView):
    state_scope = "user"

    async def click(self, interaction):
        current = self.scoped_state.get("clicks", 0)
        await self.dispatch_scoped({"clicks": current + 1})
```

### Scope Values

| `state_scope` | Key | Use case |
|-------|-----|----------|
| `"user"` | User ID | Per-user preferences |
| `"guild"` | Guild ID | Per-server configuration |
| `"user_guild"` | User + Guild ID | Per-user-per-server isolation |
| `"global"` | (none) | Global namespace |
| `None` *(default)* | N/A | Shared state -- `dispatch_scoped` unavailable |

!!! note "`state_scope` vs `instance_scope`"
    Both accept the same string values but govern different subsystems.
    `state_scope` controls Redux scoped state (where data is stored).
    `instance_scope` controls instance limit indexing (how instances are counted).

### Reading Scoped State

```python
# Generic property (returns the view's own scope slice)
my_data = self.scoped_state

# Named accessors for hub views reading multiple scopes
user_prefs = self.user_scoped_state()
guild_config = self.guild_scoped_state()
per_server = self.user_guild_scoped_state()
global_settings = self.global_scoped_state()

# Named accessors accept overrides for reading other users' data
other_user = self.user_scoped_state(user_id=other_id)
```

### Writing Scoped State

```python
await self.dispatch_scoped({"clicks": 5, "name": "Alice"})
```

Scoped state persists through restarts when a persistence backend is
configured.

### Cross-View Reactivity

`dispatch_scoped()` fires `SCOPED_UPDATE`, which other views don't subscribe
to by default. For live cross-view updates, use `dispatch()` with a named
action and a [custom reducer](state.md#custom-reducers):

| Method | Other views react? | Creates undo snapshots? |
|--------|-------------------|------------------------|
| `dispatch_scoped()` | No | Yes |
| `dispatch("NAMED_ACTION")` | Yes | Yes |

When multiple dispatches converge on the same subscriber concurrently (e.g. two
players acting simultaneously), notifications are coalesced automatically. See
[Concurrent Updates](state.md#concurrent-updates) for details.

---

## Undo/Redo

Enable undo/redo on any view:

```python
from cascadeui import UndoMiddleware, get_store

store = get_store()
store.add_middleware(UndoMiddleware(store))

class EditableView(StatefulLayoutView):
    enable_undo = True
    undo_limit = 20  # Max snapshots (default)

    async def undo_action(self, interaction):
        await self.undo()

    async def redo_action(self, interaction):
        await self.redo()
```

Only `state["application"]` is snapshotted. Internal lifecycle actions are
excluded from undo tracking.

---

## Child Attachment

Parent views can register children for automatic cleanup. The `parent=`
kwarg is the recommended approach -- `send()` calls `attach_child`
automatically on success:

```python
class GameView(StatefulLayoutView):
    async def _show_panel(self, interaction):
        panel = PanelView(context=interaction, parent=self)
        await panel.send(ephemeral=True)
```

When the parent exits or times out, attached children are exited with
`delete_message=True`. `attach_child()` still works standalone for manual
use cases where the timing or conditional logic differs.

Three invariants are enforced on attachment: self-attachment raises
`ValueError`, circular chains raise `ValueError` (ancestor walk), and
re-parenting detaches from the old parent cleanly.

!!! warning "Dispatch before cleanup"
    When broadcasting a terminal action AND cleaning up children, dispatch
    first. Children need the final state update before exit:
    ```python
    await self.dispatch("GAME_FINISHED", {"winner": winner})
    await self._cleanup_attached_children()
    ```

---

## Message Deletion Cleanup

When a view's Discord message is deleted externally (admin delete, bulk purge,
channel delete), the library automatically cleans up the view's state, tasks,
and store registration. The `on_message_delete()` hook fires by default and
calls `exit(delete_message=False)`.

Override the hook for custom behavior:

```python
class MyView(StatefulLayoutView):
    async def on_message_delete(self):
        print(f"View {self.id} message was deleted")
        await self.exit(delete_message=False)
```

The cleanup listener is installed automatically on first `send()` or when
`PersistenceMiddleware(bot=self)` initializes. No manual setup is required.

---

## Exit Button

```python
self.add_exit_button()  # "Exit" button with ❌ emoji

# Customize:
self.add_exit_button(label="Close", emoji=None, delete_message=True)

# PersistentView requires custom_id:
self.add_exit_button(custom_id="my_view:exit")
```

---

## Error Handling

All views include a built-in `on_error` handler that shows a red ephemeral
embed when a callback raises an exception. The embed description uses the
`error_message` class attribute:

```python
class MyView(StatefulLayoutView):
    error_message = "Something broke. Please try again or contact support."
```

Override `on_error` for fully custom error handling (different embed layout,
DM the bot owner, conditional logging):

```python
async def on_error(self, interaction, error, item):
    logger.critical(f"View error: {error}")
    await self.respond(interaction, "Bug reported!", ephemeral=True)
```

---

## Persistent Views

Views that survive bot restarts. All interactive components must have an
explicit `custom_id`:

```python
from cascadeui import PersistentLayoutView, StatefulButton, card
from discord.ui import ActionRow

class RolePanel(PersistentLayoutView):
    instance_limit = 1
    instance_scope = "guild"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(card(
            "## Role Selector",
            ActionRow(StatefulButton(
                label="Get Role",
                custom_id="roles:get",
                callback=self.toggle_role,
            )),
            color=discord.Color.blurple(),
        ))

    async def toggle_role(self, interaction):
        ...
```

See [Persistence](persistence.md) for backend setup, `PersistenceMiddleware`,
and the full persistent view lifecycle.

---

## Defensive Input Handling

CascadeUI catches malformed input at two boundaries:

### Snowflake Coercion (Instance-Level)

`user_id`, `guild_id`, `allowed_users`, and `register_participant` accept
either `int` or any object with `.id: int`:

```python
view.allowed_users = {member, 12345, discord.Object(id=99999)}
await view.register_participant(opponent)  # discord.Member works
```

### Class-Attribute Validation (Definition Time)

String enums, positive numbers, and booleans are validated when a subclass is
defined via `__init_subclass__`:

```python
class BadView(StatefulLayoutView):
    instance_policy = "rejct"  # ValueError at module import
```

The traceback names the class, attribute, bad value, and valid options.
