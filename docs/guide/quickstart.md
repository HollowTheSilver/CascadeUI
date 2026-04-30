# Quick Start

Build a working stateful counter in 5 minutes. This tutorial introduces the
core concepts one at a time -- by the end, the full data flow pattern is clear.

## Prerequisites

- Python 3.10+ with discord.py 2.7+ installed
- CascadeUI [installed](installation.md)
- A Discord bot token and a test server

---

## Step 1: Build the View

A **view** is a single UI screen backed by the state store. V2 views
(`StatefulLayoutView`) use Discord's container-based component system -- the
component tree IS the message content:

```python
import discord
from discord.ui import ActionRow

from cascadeui import (
    StatefulButton,
    StatefulLayoutView,
    StateStore,
    card,
    key_value,
)


class CounterView(StatefulLayoutView):
    # Access control: only the user who opened this counter can click.
    owner_only = True

    # Instance control: one counter per user; opening a second replaces
    # the first instead of stacking duplicates.
    instance_limit = 1
    instance_scope = "user"
    instance_policy = "replace"

    # State scope: the count is stored per user under
    # ``state["application"]["scoped"]["user:<id>"]``. Same user gets
    # the same counter across every server that shares this bot.
    state_scope = "user"

    # Reactivity: subscribe to SCOPED_UPDATE so the view notices its own
    # writes, and return the count from state_selector so the store only
    # rebuilds when the number actually changes.
    subscribed_actions = {"SCOPED_UPDATE"}

    def state_selector(self, state):
        # ``state`` is the post-reduce snapshot the store compares
        # against; ``self.scoped_state`` would be stale here.
        return StateStore.get_scoped_from(
            state, "user", user_id=self.user_id
        ).get("count", 0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_ui()

    def build_ui(self):
        """Rebuild the component tree from current state."""
        count = self.scoped_state.get("count", 0)

        self.clear_items()
        self.add_item(card(
            "## Counter",
            key_value({"Value": str(count)}),
            color=discord.Color.blurple(),
        ))
        self.add_item(ActionRow(
            StatefulButton(
                label="+1", style=discord.ButtonStyle.primary,
                callback=self.increment,
            ),
            StatefulButton(
                label="-1", style=discord.ButtonStyle.danger,
                callback=self.decrement,
            ),
        ))
        self.add_exit_button()

    async def increment(self, interaction):
        count = self.scoped_state.get("count", 0)
        await self.dispatch_scoped({"count": count + 1})

    async def decrement(self, interaction):
        count = self.scoped_state.get("count", 0)
        await self.dispatch_scoped({"count": count - 1})
```

Key points:

- **`state_scope = "user"`** stores the count under a per-user slot in
  the state tree. CascadeUI provides built-in scopes (`"user"`,
  `"guild"`, `"user_guild"`, `"global"`); writes via `dispatch_scoped`
  land in the right slot automatically.
- **`subscribed_actions`** declares which action types this view
  receives on the state-change pub/sub. The default is an empty set
  (opt-in posture for performance), so a view that omits this attribute
  receives **no** notifications and its message never edits. Subscribe
  to `SCOPED_UPDATE` to react to scoped writes, or set to `None` to
  receive every action.
- **`state_selector`** narrows the view's reactivity to one slice
  (here, the count). The store only fires `on_state_changed()` when
  the selector's return value changes between dispatches, so the
  view does not rebuild on unrelated state churn.
- **`dispatch_scoped({"count": N})`** is the convenience layer: it
  writes into `state["application"]["scoped"]["user:<id>"]` for the
  current scope without a custom reducer.
- **`build_ui()`** rebuilds the component tree from scratch. The
  default `on_state_changed()` calls `build_ui()` followed by
  `refresh()` whenever the selector's value changes -- no manual
  callback wiring needed.

---

## Step 2: Wire It Up

Register the view as a slash command:

```python
from discord.ext import commands

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.hybrid_command()
async def counter(ctx):
    view = CounterView(context=ctx)
    await view.send()
```

`view.send()` handles message creation, state registration, session tracking,
and message reference capture in one call.

---

## The Data Flow

Here is what happens each time a button is clicked:

```
Click "+1"  →  increment()  →  dispatch_scoped({"count": N})
                                        │
                               SCOPED_UPDATE reducer writes
                               state["application"]["scoped"]["user:<id>"]
                                        │
                               subscribed_actions filter:
                               SCOPED_UPDATE in this view's set?
                                        │
                               state_selector compares old vs new count
                                        │
                               on_state_changed() fires
                                        │
                               build_ui() → refresh()
                                        │
                               Discord message edited ✓
```

This is the **unidirectional data flow** pattern -- every state change follows
the same path. See [Core Concepts](concepts.md#data-flow) for the full
diagram.

!!! warning "If the message never updates, check `subscribed_actions`"
    The default for `subscribed_actions` is an empty set, which filters
    every action out before notification. The most common quickstart
    bug is dispatching an action whose type is not listed in the view's
    `subscribed_actions` -- the reducer runs (state updates) but
    `on_state_changed` never fires (message stays stale). Either add
    the action type to the set, or set `subscribed_actions = None` to
    receive every notification.

---

## Next Steps

- **[Core Concepts](concepts.md)** -- the mental models that make everything click
- **[Views](views.md)** -- lifecycle, navigation, sessions, policies
- **[Components](components.md)** -- selects, modals, V2 builders, grid helpers
- **[State Management](state.md)** -- custom reducers, scoped slots, undo/redo, batching. Reach for `@cascade_reducer` when your state shape outgrows the slot model (cross-view aggregations, complex transitions, derived data).
- **[View Patterns](patterns.md)** -- pre-built forms, wizards, tabs, pagination
