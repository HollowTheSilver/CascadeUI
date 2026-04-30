"""
V2 Persistence -- Both Persistence Pillars
================================================================

CascadeUI has two orthogonal persistence pillars. This file demonstrates
both so the distinction is visible side-by-side.

    1. View persistence  -- the view object re-attaches to its message
       on bot restart, so users can keep clicking a panel that was
       posted days earlier. Demonstrated via ``PersistentRolesLayoutView``
       (role selector panel) -- the pattern wraps a
       ``PersistentLayoutView`` around cardinality-aware role buttons
       so the category and mode metadata survive restarts alongside
       the message.
       Showcased by: /v2roles (role selector panel).

    2. Data persistence  -- state-store writes survive restart via
       ``PersistenceMiddleware``. Persistence is opt-in per slot:
       list the slot name in the view's ``persistent_slots`` class
       attribute (or call ``access_slot(..., persistent=True)`` once
       from a reducer) and every future write to that slot is flushed
       to disk. Showcased by: /v2visits (personal visit counter).

Features demonstrated:

    - PersistentRolesLayoutView (role panel absorbing cardinality
                                 + click routing + response messages)
    - RoleCategory              (typed schema with exclusive / required
                                 cardinality flags)
    - DynamicPersistentButton   (under the hood; used by the roles
                                 pattern for click routing without
                                 per-button instance tracking)
    - PersistenceMiddleware     (installed via setup_middleware in
                                 setup_hook)
    - persistent_slots          (declarative slot opt-in on the
                                 ``PersonalVisitsView`` class)
    - @cascade_reducer           (user actions mutate the slot)
    - slot_property             (declarative attribute reads from
                                 the slot)

Commands:
    /v2roles      Post the role selector panel (requires Manage Roles)
    /v2visits     Open a personal visit counter (per-user, persisted)

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py

    Before loading, configure ROLE_CATEGORIES below with your server's
    actual role IDs. The example IDs are placeholders.

    Both demos require PersistenceMiddleware to be installed from the bot's
    setup_hook -- see the cog docstring below for the exact snippet.
"""

# // ========================================( Modules )======================================== // #


import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow

from cascadeui import (
    PersistentRolesLayoutView,
    RoleCategory,
    StatefulButton,
    StatefulLayoutView,
    access_slot,
    card,
    cascade_reducer,
    divider,
    key_value,
    slot_property,
)

logger = logging.getLogger(__name__)


# // ========================================( Config )======================================== // #


# Replace these role IDs with your server's actual role IDs. Each
# category carries its own accent color and cardinality flags:
#
#   exclusive=True  one active at a time in this category; selecting
#                   another swaps off the previous one. Useful for
#                   color roles, pronouns, team affiliation.
#   required=True   at least one role in this category must stay
#                   active. Removing the last one is blocked. Useful
#                   for pronoun / region / team categories where
#                   "no selection" is not a meaningful state.
#
# The two axes are orthogonal: ``exclusive=True, required=True`` gives
# radio-button behavior with a mandatory choice.
ROLE_CATEGORIES = [
    RoleCategory(
        name="Color Roles",
        color=discord.Color.red(),
        exclusive=True,
        roles={
            "Red": 123456789012345001,
            "Blue": 123456789012345002,
            "Green": 123456789012345003,
            "Purple": 123456789012345004,
        },
    ),
    RoleCategory(
        name="Gaming Roles",
        color=discord.Color.dark_teal(),
        # ``icon`` is a string rendered into the category heading as
        # markdown, so any string Discord renders inline works: a
        # unicode glyph, a custom guild emoji as
        # ``"<:name:1234567890123456789>"``, or an animated one as
        # ``"<a:name:1234567890123456789>"``. ``discord.Emoji`` and
        # ``discord.PartialEmoji`` instances are NOT accepted here --
        # this slot routes through markdown, not the
        # ``discord.ui.Button(emoji=...)`` parameter. Custom emoji
        # only render where the bot can see them (shared guild, or an
        # application-owned emoji created via the discord.py API).
        icon="🎮",
        roles={
            "Minecraft": 123456789012345005,
            "Valorant": 123456789012345006,
            "League": 123456789012345007,
        },
    ),
    RoleCategory(
        name="Pronoun Roles",
        color=discord.Color.blurple(),
        exclusive=True,
        required=True,
        roles={
            "He/Him": 123456789012345008,
            "She/Her": 123456789012345009,
            "They/Them": 123456789012345010,
            "Neopronoun": 123456789012345011,
        },
    ),
]


# // ========================================( Roles View )======================================== // #


class RoleSelectorPanel(PersistentRolesLayoutView):
    """Multi-category role selector that survives bot restarts.

    Ships as a subclass of ``PersistentRolesLayoutView`` so the
    pattern handles panel rendering, button custom-id encoding,
    cardinality enforcement (exclusive / required), response messages,
    and restart re-attachment. Users only declare ``categories``; the
    rest is the pattern's responsibility.

    The panel uses a stable ``persistence_key`` so re-running /v2roles
    automatically cleans up the previous panel.
    """

    categories = ROLE_CATEGORIES
    title = "Server Roles"

    # Role panels are server-wide; anyone in the channel toggles their
    # own roles. Matches the PersistentRolesLayoutView default.
    owner_only = False

    # ``instance_limit`` is deliberately not set on a
    # PersistentRolesLayoutView. Persistent views already get
    # deterministic single-panel-per-key enforcement from the built-in
    # persistence_key dedup path, which actively cleans up old messages
    # (even externally-deleted ones, via a NotFound-swallowing
    # fetch_message call). Adding instance_limit on top is redundant
    # protection with a worse failure mode -- if a panel message is
    # deleted in Discord while the view is still live in
    # _active_views, ``instance_limit = 1`` with ``"reject"`` would
    # lock the guild out of re-posting until the next bot restart.
    # persistence_key dedup handles this edge case; instance_limit
    # does not.

    # ``exit_policy = "disable"`` is the PersistentRolesLayoutView
    # default and the correct choice here -- a role panel is a product
    # surface, not a session; when it times out the buttons should
    # freeze in place rather than deleting the panel message.
    exit_policy = "disable"

    async def on_restore(self, bot):
        """Called after the panel is re-attached on bot restart."""
        logger.info(f"Role selector panel restored (persistence_key={self.persistence_key})")


# // ========================================( Reducers )======================================== // #


# The ``visits`` slot is declared persistent on the view class via
# ``persistent_slots = ("visits",)``; every write from these reducers
# to the same slot name inherits the persistence contract and is
# flushed to disk by ``PersistenceMiddleware``. The reducers
# themselves are namespace-agnostic -- they just mutate the slot.
@cascade_reducer("VISIT_RECORDED")
async def reduce_visit_recorded(action, state):
    payload = action["payload"]
    user_data = access_slot(state, "visits", payload["user_id"])
    user_data["count"] = user_data.get("count", 0) + 1
    user_data["last_visit"] = payload["now"]
    return state


@cascade_reducer("VISIT_RESET")
async def reduce_visit_reset(action, state):
    user_data = access_slot(state, "visits", action["payload"]["user_id"])
    user_data["count"] = 0
    user_data["last_visit"] = None
    return state


# // ========================================( Visits View )======================================== // #


class PersonalVisitsView(StatefulLayoutView):
    """Per-user visit counter backed by opt-in slot persistence.

    This view is **not** a PersistentLayoutView -- the Discord message
    itself is ephemeral. What survives restart is the *data* the view
    reads and writes, stored in a named slot (``visits``) that the
    class opts into persistence with a single
    ``persistent_slots = ("visits",)`` declaration.
    ``PersistenceMiddleware`` flushes every write to disk, and the
    rehydrate pass on startup puts the data back where the reducers
    expect it.

    Partitioning and persistence are orthogonal axes:

        - *Partitioning* decides where a piece of state lives in the
          tree. Scoped state (``state_scope`` + ``dispatch_scoped``)
          partitions by user/guild; a keyed slot partitions by whatever
          key the reducer writes under. This view uses a keyed slot.
        - *Persistence* decides whether that state is flushed to disk.
          It's orthogonal to partitioning -- any named slot, keyed or
          not, can be persistent or not. This slot is persistent.
    """

    owner_only = True
    # Ephemeral by policy -- the view object is short-lived, only the
    # data outlives it. instance_limit=1 keeps a user from opening
    # multiple stacked counters against the same underlying slot.
    instance_limit = 1
    instance_scope = "user"
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "delete"
    auto_defer = True
    # Declarative opt-in to slot persistence. The middleware flushes
    # every ``access_slot`` write to a name listed here, regardless of
    # which reducer performed the write. Equivalent to seeding the slot
    # with ``persistent=True`` in a hook, minus the hook.
    persistent_slots = ("visits",)
    # The default `None` derives from ``timeout``: ephemeral views with
    # ``timeout > 900`` auto-engage the refresh handoff. Pinning ``True``
    # forces it on regardless of the configured timeout.
    auto_refresh_ephemeral = True
    # Opt in to the two named actions this view dispatches. Without
    # this declaration the default empty set gates every notification
    # out, and ``on_state_changed`` never fires on self-dispatch.
    subscribed_actions = {"VISIT_RECORDED", "VISIT_RESET"}

    # Declarative reads from the "visits" slot. ``self.count`` and
    # ``self.last_visit`` resolve at attribute access against the live
    # store state, falling back to the declared defaults when the slot
    # has no entry for this user yet.
    count = slot_property("count", slot="visits", key=lambda self: self.user_id, default=0)
    last_visit = slot_property(
        "last_visit", slot="visits", key=lambda self: self.user_id, default=None
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_ui()

    def build_ui(self) -> None:
        self.clear_items()

        last_display = self.last_visit if self.last_visit else "never"

        self.add_item(
            card(
                "## Personal Visit Counter",
                "-# Keyed by your user ID. Survives bot restarts.",
                divider(),
                key_value(
                    {
                        "Visits": str(self.count),
                        "Last visit": last_display,
                    }
                ),
                color=discord.Color.blurple(),
            )
        )

        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Record Visit",
                    style=discord.ButtonStyle.primary,
                    emoji="\U0001f4cd",  # round pushpin
                    callback=self._record,
                ),
                StatefulButton(
                    label="Reset",
                    style=discord.ButtonStyle.danger,
                    emoji="♻️",  # recycle
                    callback=self._reset,
                ),
                self.make_exit_button(),
            )
        )

    async def _record(self, interaction: discord.Interaction):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await self.dispatch("VISIT_RECORDED", {"user_id": self.user_id, "now": now})

    async def _reset(self, interaction: discord.Interaction):
        await self.dispatch("VISIT_RESET", {"user_id": self.user_id})


# // ========================================( Cog )======================================== // #


class V2PersistenceExample(commands.Cog, name="v2_persistence_example"):
    """V2 persistent role selector panel + per-user visit counter.

    Requires ``PersistenceMiddleware`` installed in the bot's setup_hook::

        from cascadeui import PersistenceMiddleware, SQLiteBackend, setup_middleware

        async def setup_hook(self):
            await setup_middleware(
                PersistenceMiddleware(backend=SQLiteBackend("cascadeui.db"), bot=self),
            )
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2roles",
        description="Post a V2 role selector panel (admin only, runs once).",
    )
    @commands.has_permissions(manage_roles=True)
    async def v2roles(self, context: Context) -> None:
        """Post a role selector panel using V2 components.

        The panel stays interactive across bot restarts. Running
        this command again replaces the previous panel automatically.

        Configure ROLE_CATEGORIES in the source with your server's
        actual role IDs before using.
        """
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        view = RoleSelectorPanel(
            context=context,
            persistence_key=f"roles:panel:{context.guild.id}",
        )
        await view.send()

    @commands.hybrid_command(
        name="v2visits",
        description="Open a personal visit counter (per-user, survives restarts).",
    )
    async def v2visits(self, context: Context) -> None:
        """Open the per-user visit counter.

        Demonstrates the *data persistence* pillar: the view object is
        ephemeral, but the count lives in a named slot (``visits``)
        opted into persistence via the view's ``persistent_slots``
        class attribute. Restart the bot, re-run the command, and the
        count is still there.
        """
        view = PersonalVisitsView(
            context=context,
            user_id=context.author.id,
            guild_id=context.guild.id if context.guild else None,
        )
        await view.send(ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(V2PersistenceExample(bot=bot))
