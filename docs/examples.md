# Examples

Working examples are in the [`examples/`](https://github.com/HollowTheSilver/CascadeUI/tree/main/examples) directory. Each is a discord.py cog that can be loaded into any bot.

---

## V2 Examples

These use the V2 component system (`StatefulLayoutView`, Containers, Sections, TextDisplay).

### v2_hello_world.py

The smallest working CascadeUI view: a per-user counter using `state_scope = "user"`, `subscribed_actions = {"SCOPED_UPDATE"}`, a one-line `state_selector` reading the count via `StateStore.get_scoped_from`, and `dispatch_scoped({"count": N})` for writes. Mirrors the [Quick Start](guide/quickstart.md) tutorial as a runnable cog. Open with `/hello`; the same user sees the same counter across every server that shares the bot.

**Command:** `/hello`

### v2_dashboard.py

Full V2 builder showcase using `TabLayoutView`. A Controls tab demonstrates `tab_nav` inner navigation, `button_row`, `toggle_button`, `cycle_button`, and `choice_row`; the Modules tab shows a `Collapsible` reset confirm; the About tab uses `link_section` links. Also covers `action_section`, `toggle_section`, `key_value`, `alert`, and session limiting.

**Command:** `/v2dashboard`

### v2_settings.py

Full settings menu showcasing most CascadeUI features with V2 components:

- **Instance limiting** (`instance_limit=1`, `instance_scope="user_guild"`) to prevent duplicate panels
- **Scoped state** (`state_scope="user"`) for per-user settings and (`state_scope="user_guild"`) for per-server display preferences
- **Navigation stack** (push/pop with `rebuild=` callback) for hub-and-spoke layout
- **Theming** with a live theme switcher
- **Undo/redo** for reverting notification preference toggles
- **Custom reducer** (`SETTINGS_UPDATED`) for domain-specific state management
- **`with_confirmation`** wrapper on the Reset All danger button
- **Cross-view reactivity** between V2 settings and V1 settings via shared state keys

**Command:** `/v2settings`

### v2_form.py

Registration form using `FormLayoutView` with native `text` fields, a dropdown select, and the full set of built-in validators (`min_length`, `max_length`, `regex`, `min_value`, `max_value`, `choices`) alongside an async "username already taken" check. Text fields are grouped behind a single "Edit Text Fields" button that opens a `Modal`.

**Command:** `/v2form`

### v2_pagination.py

Paginated inventory viewer using `PaginatedLayoutView` with `from_data()`. Container-based page content with jump controls (first/last, go-to-page modal). Demonstrates `_build_extra_items()` for an exit button below navigation.

**Command:** `/v2pages`

### v2_library.py

Pagination + drill-down navigation. A category hub (`StatefulLayoutView`) pushes a paginated `CategoryListView` per category using `await PaginatedLayoutView.from_data(...)` and `push(instance)`. `nav_inside_container = True` wraps page content + nav row in a single Container per page; `auto_back_button = True` adds a Back button that pops the nav stack and survives page turns. The closest mapping for users coming from [@Soheab](https://github.com/Soheab)'s [CV2 paginator gist](https://gist.github.com/Soheab/891c39d7294b1bdbadc7ecf35ce51cc5).

**Command:** `/v2library`

### v2_db_navigation.py

Database-backed navigation via `on_load()`. Views hold a repo handle as a
non-reserved constructor kwarg and define `on_load()` to fetch rows before
each render. Push and pop re-fetch the destination's source automatically
(reload-on-render), so no `rebuild=` callback is needed; `make_nav_row()`
builds the Back plus Exit footer. The row list is paged by a
`PaginatedRegion` fed from `on_load()`, so a page turn re-slices against the
current repo data.

**Command:** `/tasks`

### v2_computed.py

Quick poll demonstrating `@computed` for global memoized values that multiple views share without recalculating. Two `@computed` values derive vote totals and the current leader; the poll view reads both in `build_ui()` and displays them alongside the raw vote buttons. Contrast with `state_selector` (per-view change detection): computed values are global and shared across all views.

**Command:** `/v2poll`

### v2_wizard.py

D&D character creator using `WizardLayoutView`. Five steps (Name & Race, Class & Subclass, Ability Scores, Background, Confirmation) with per-step validation, cross-step state (class list filters by race, subclass list filters by class, ability points draw from a shared pool), and the `on_finish` method hook. Demonstrates navigation button customization (`back_button_label`, `finish_button_label`, `finish_button_emoji`), inline selects for structured choices (alignment, languages with race-based defaults), a toggle button in a `card()` for a boolean flag (heroic destiny), and a modal for free-form backstory text.

**Command:** `/v2wizard`

### v2_persistence.py

A `PersistentLayoutView`-based role selector panel that survives bot restarts. Categories are rendered as accent-colored containers, with exclusive-mode support (selecting one role in a category auto-removes the others). Running `/v2roles` again automatically cleans up the previous panel.

**Command:** `/v2roles`

### v2_tictactoe.py

Two-player TicTacToe demonstrating multi-user interaction patterns. Features a challenge acceptance flow (opponent must accept before the game starts), dynamic board size (3x3 to 5x5), configurable win length (e.g. 3-in-a-row on a 5x5 board), Discord mentions, mutual rematch agreement (both players must confirm), forfeit tracking, and participant-aware session limiting via `register_participant()`. Uses `allowed_users` for restricting interaction to the two players and a custom reducer for tracking game statistics.

**Command:** `/tictactoe @user [size] [win]`

### v2_lobby.py

Open-join multi-user lobby demonstrating `participant_limit` with V2 components. Up to 8 players join via a button -- no pre-set `allowed_users`. Features host-vs-participant authority (only the host can Start Game or Disband), live participant card refresh, `register_participant` bool-return contract, `unregister_participant` for the Leave path, and a custom `on_participant_limit` override with personalized rejection messages.

**Command:** `/lobby`

### v2_grids.py

Display-only showcase for the grid helpers (`emoji_grid()` and `button_grid()`). Demonstrates grid construction from slash-command arguments, axis label presets, cell mutation, and `fill_rect()` -- all without surrounding game logic. For interactive grid usage, see `v2_battleship.py` and `v2_tictactoe.py`.

**Commands:** `/emoji_grid`, `/button_grid`

### v2_battleship.py

Two-player 10x10 Battleship with a standard fleet, text-rendered emoji grids, a fleet setup phase with re-roll consensus, ephemeral private fleet panels via `attach_child()`, turn-based select targeting, and automatic cleanup via `_cleanup_attached_children()` on game end. Demonstrates `attach_child()`, `register_participant()`, dispatch-then-cleanup ordering, the ephemeral interaction-bound constraint, and live cross-view reactivity (Re-Roll dispatches update both the ephemeral and the public ready card via the standard subscriber pipeline).

**Command:** `/battleship @user`

### v2_leaderboard.py

Server leaderboard built on `LeaderboardLayoutView`. Overrides `format_entry` for MMR display with medal emojis and `build_summary` for aggregate stats. `leaderboard_top_n=25` with `leaderboard_per_page=5` produces five-page navigation. The cog inspects `bot.intents.members` and the guild cache: real members fill the top of the board when available, and synthetic Demo Player rows pad any remaining slots so the display always renders exactly 25 entries regardless of guild size or intent configuration. Stats are derived deterministically from member ID via `random.Random(member_id)`, so repeat invocations return a stable ranking without a persistence layer. Contrast with `v2_battleship.py`, which feeds the same view class from live computed state -- the tuple shape fed into the pattern is identical, the data source differs.

**Command:** `/leaderboard`

---

## V1 Examples (Classic)

These use the V1 component system (`StatefulView`, embeds, row-based layout).

### settings_menu.py

Advanced settings menu retained as the legacy V1 demonstration. Showcases session limiting, scoped state, push/pop navigation, theming, undo/redo, state selectors, batched dispatch, and a custom reducer. The V1 counterpart to `v2_settings.py` -- both share the same state keys for cross-view reactivity.

**Command:** `/settings`

### navigation.py

Navigation stack with push/pop between multi-level views and session data sharing. A dark mode toggle in the settings view writes to `shared_data`, which the nested view reads without any constructor kwargs. Demonstrates `update_session()`, `shared_data`, and `SESSION_UPDATED` subscriptions alongside multi-level push/pop.

**Command:** `/navtest`

!!! note "V1 primitive coverage"
    `FormView`, `Modal` with validators, `PaginatedView.from_data`, and
    `with_loading_state` remain fully supported in V1 but no longer ship
    with a dedicated example file. See the [API reference](api/components.md)
    for usage. Their V2 equivalents (`FormLayoutView`, V2 wizard modal
    pattern, `PaginatedLayoutView`) are demonstrated in the V2 examples above.

---

## Running the Examples

1. Create a bot on the [Discord Developer Portal](https://discord.com/developers/applications)
2. Install CascadeUI: `pip install pycascadeui[sqlite]`
3. Load the example cogs in your bot:

```python
from cascadeui import setup_middleware
from cascadeui.state.middleware import PersistenceMiddleware, UndoMiddleware
from cascadeui.persistence import SQLiteBackend

class MyBot(commands.Bot):
    async def setup_hook(self):
        # V2 examples
        await self.load_extension("examples.v2_hello_world")
        await self.load_extension("examples.v2_dashboard")
        await self.load_extension("examples.v2_settings")
        await self.load_extension("examples.v2_form")
        await self.load_extension("examples.v2_pagination")
        await self.load_extension("examples.v2_library")
        await self.load_extension("examples.v2_db_navigation")
        await self.load_extension("examples.v2_wizard")
        await self.load_extension("examples.v2_persistence")
        await self.load_extension("examples.v2_computed")
        await self.load_extension("examples.v2_tictactoe")
        await self.load_extension("examples.v2_battleship")
        await self.load_extension("examples.v2_leaderboard")
        await self.load_extension("examples.v2_lobby")
        await self.load_extension("examples.v2_grids")

        # V1 examples (classic API)
        await self.load_extension("examples.settings_menu")
        await self.load_extension("examples.navigation")

        # Install middleware after loading cogs so PersistentView subclasses
        # have registered themselves via __init_subclass__.
        await setup_middleware(
            PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=self),
            UndoMiddleware(),  # required by the settings examples
        )
```

4. Run your bot and use the slash commands

---

## Built something with CascadeUI?

The shipped examples are teaching material. For application-level work - real bots, real features, real constraints - check the **showcase forum** on the official [support Discord server](https://discord.com/invite/9Xj68BpKRb). It's where community members post what they built with the library and trade patterns you won't find in the reference examples.
