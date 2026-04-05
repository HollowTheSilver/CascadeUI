"""
V2 Persistence — CascadeUI V2 Persistent Counter & Role Panel
================================================================

Two persistence demos using V2 components:

1. **Persistent Counter** (``/v2pcounter``): A counter that saves its
   value to SQLite via the state store. Each user gets their own counter.
   Close the view, restart the bot — the count is still there. Uses
   card(), key_value(), and action_section() for the V2 presentation.

2. **Role Selector Panel** (``/v2roles``): A role panel that stays
   interactive across bot restarts. Post it once and it works forever.
   Uses PersistentLayoutView with accent-colored containers per category.

Persistence features demonstrated:

    - Data persistence      (counter value survives restarts via state_key)
    - View persistence      (role panel re-attaches to its message on restart)
    - PersistentLayoutView  (timeout=None, explicit custom_id on all items)
    - setup_persistence()   (single call in setup_hook enables both)
    - on_restore() hook     (post-restart logging)
    - Orphan cleanup        (re-running /v2roles replaces the old panel)

Commands:
    /v2pcounter   Display a persistent counter with V2 components
    /v2roles      Post the role selector panel (requires Manage Roles)

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py

    Before loading, configure ROLE_CATEGORIES below with your server's
    actual role IDs. The example IDs are placeholders.

    Your bot's setup_hook must call setup_persistence(bot=self) for
    both demos to work across restarts.
"""

# // ========================================( Modules )======================================== // #


import logging
from datetime import datetime

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    PersistentLayoutView,
    StatefulButton,
    StatefulLayoutView,
    card,
    cascade_reducer,
    divider,
    get_store,
    key_value,
    slugify,
)

logger = logging.getLogger(__name__)


# // ========================================( Reducer )======================================== // #


@cascade_reducer("PERSISTENT_COUNTER_UPDATED")
async def counter_reducer(action, state):
    """Track counter values keyed by state_key for persistence."""
    new_state = state
    app = new_state.setdefault("application", {})
    counters = app.setdefault("counters", {})

    key = action["payload"].get("state_key") or action["payload"].get("view_id")
    if key:
        counters[key] = action["payload"].get("counter", 0)

    return new_state


# // ========================================( Persistent Counter )======================================== // #


class V2PersistentCounterView(StatefulLayoutView):
    """A counter that persists its value across view timeouts and bot restarts.

    Uses state_key scoped by user ID so each user gets their own counter.
    The value is stored in the Redux state tree and saved to SQLite by the
    persistence backend. On re-invoke, the counter reads its last value
    from state and picks up where it left off.

    Demonstrates: card(), key_value(), data persistence via state_key,
    and the PERSISTENT_COUNTER_UPDATED custom reducer.
    """

    session_limit = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Restore counter from persisted state
        store = get_store()
        counters = store.state.get("application", {}).get("counters", {})
        self.counter = counters.get(self.state_key, 0)
        self._restored = self.counter != 0

        self._build_ui()

    def _build_ui(self):
        self.clear_items()

        if self.counter > 0:
            color = discord.Color.green()
        elif self.counter < 0:
            color = discord.Color.red()
        else:
            color = discord.Color.light_grey()

        now = datetime.now().strftime("%H:%M:%S")
        status = "Restored from saved state" if self._restored else "New session"

        self.add_item(
            card(
                "## Persistent Counter",
                key_value(
                    {
                        "Value": str(self.counter),
                        "Status": status,
                        "Last update": now,
                    }
                ),
                divider(),
                TextDisplay(
                    "-# SQLite-backed via state_key. Close the view,\n"
                    "-# restart the bot — your count is still here."
                ),
                ActionRow(
                    StatefulButton(
                        label="-1", style=discord.ButtonStyle.danger, callback=self._decrement
                    ),
                    StatefulButton(
                        label="Reset", style=discord.ButtonStyle.secondary, callback=self._reset
                    ),
                    StatefulButton(
                        label="+1", style=discord.ButtonStyle.success, callback=self._increment
                    ),
                ),
                color=color,
            )
        )

        self.add_exit_button()

    async def _increment(self, interaction):
        await interaction.response.defer()
        self.counter += 1
        self._restored = False
        await self._sync_and_update()

    async def _decrement(self, interaction):
        await interaction.response.defer()
        self.counter -= 1
        self._restored = False
        await self._sync_and_update()

    async def _reset(self, interaction):
        await interaction.response.defer()
        self.counter = 0
        self._restored = False
        await self._sync_and_update()

    async def _sync_and_update(self):
        """Push counter to state store and rebuild UI."""
        await self.dispatch(
            "PERSISTENT_COUNTER_UPDATED",
            {"state_key": self.state_key, "counter": self.counter},
        )
        self._build_ui()
        if self.message:
            await self.message.edit(view=self)


# // ========================================( Config )======================================== // #


# Replace these with your server's actual role IDs.
# Each category gets its own accent-colored container.
# Set "exclusive": True on categories where selecting one role
# should automatically remove the others (e.g. pronouns, colors).
ROLE_CATEGORIES = {
    "Color Roles": {
        "color": discord.Color.red(),
        "exclusive": True,
        "roles": {
            "Red": 123456789012345001,
            "Blue": 123456789012345002,
            "Green": 123456789012345003,
            "Purple": 123456789012345004,
        },
    },
    "Gaming Roles": {
        "color": discord.Color.dark_teal(),
        "exclusive": False,
        "roles": {
            "Minecraft": 123456789012345005,
            "Valorant": 123456789012345006,
            "League": 123456789012345007,
        },
    },
    "Pronoun Roles": {
        "color": discord.Color.blurple(),
        "exclusive": True,
        "roles": {
            "He/Him": 123456789012345008,
            "She/Her": 123456789012345009,
            "They/Them": 123456789012345010,
        },
    },
}


# // ========================================( View )======================================== // #


class RoleSelectorPanel(PersistentLayoutView):
    """A multi-category role selector that survives bot restarts.

    Each role category is a Container with an accent color and
    toggle buttons for each role. Clicking a button gives or
    removes the role via an ephemeral confirmation.

    The panel uses a stable state_key so re-running /v2roles
    automatically cleans up the previous panel.
    """

    session_limit = 1
    session_scope = "guild"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_panel()

    def _build_panel(self):
        """Build the role selector UI from ROLE_CATEGORIES config."""
        self.clear_items()

        for category_name, category in ROLE_CATEGORIES.items():
            color = category["color"]
            roles = category["roles"]
            exclusive = category.get("exclusive", False)

            # Collect all role IDs in this category for exclusive mode
            category_role_ids = set(roles.values())

            # Build toggle buttons for this category
            buttons = []
            for role_name, role_id in roles.items():
                # custom_id must be explicit and unique for persistent views
                btn = StatefulButton(
                    label=role_name,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"roles:{slugify(category_name)}:{slugify(role_name)}",
                    callback=self._make_toggle(role_id, role_name, exclusive, category_role_ids),
                )
                buttons.append(btn)

            # Label shows selection mode
            mode_hint = " *(pick one)*" if exclusive else ""

            # Each category gets its own colored container
            self.add_item(
                card(
                    f"### {category_name}{mode_hint}",
                    divider(),
                    ActionRow(*buttons),
                    color=color,
                )
            )

    def _make_toggle(self, role_id, role_name, exclusive, category_role_ids):
        """Create a toggle callback for a specific role.

        When exclusive=True, selecting a role removes all other roles
        in the same category first (e.g. pronouns, color roles).
        """

        async def callback(interaction):
            if interaction.guild is None:
                await interaction.response.send_message(
                    "This can only be used in a server.",
                    ephemeral=True,
                )
                return

            role = interaction.guild.get_role(role_id)
            if role is None:
                await interaction.response.send_message(
                    f"Role **{role_name}** not found. An admin needs to update the role IDs.",
                    ephemeral=True,
                )
                return

            member = interaction.user
            if role in member.roles:
                await member.remove_roles(role)
                await interaction.response.send_message(
                    f"Removed **{role.name}**.",
                    ephemeral=True,
                )
            else:
                # In exclusive mode, remove other roles from this category first
                if exclusive:
                    to_remove = [
                        r for r in member.roles if r.id in category_role_ids and r.id != role_id
                    ]
                    if to_remove:
                        await member.remove_roles(*to_remove)

                await member.add_roles(role)

                if exclusive and to_remove:
                    removed_names = ", ".join(f"**{r.name}**" for r in to_remove)
                    await interaction.response.send_message(
                        f"Switched to **{role.name}** (removed {removed_names}).",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        f"Gave you **{role.name}**!",
                        ephemeral=True,
                    )

        return callback

    async def on_restore(self, bot):
        """Called after the panel is re-attached on bot restart."""
        logger.info(f"Role selector panel restored (state_key={self.state_key})")


# // ========================================( Cog )======================================== // #


class V2PersistenceExample(commands.Cog, name="v2_persistence_example"):
    """V2 persistent counter and role selector panel.

    Requires setup_persistence(bot=self) in the bot's setup_hook::

        from cascadeui import setup_persistence
        from cascadeui.persistence import SQLiteBackend

        async def setup_hook(self):
            await setup_persistence(bot=self, backend=SQLiteBackend("cascadeui.db"))
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2pcounter",
        description="Display a persistent V2 counter backed by SQLite.",
    )
    async def v2pcounter(self, context: Context) -> None:
        """Display a V2 counter that persists across restarts.

        Each user gets their own counter, scoped by state_key.
        Close the view, restart the bot, and invoke again — the
        count picks up where you left off.
        """
        view = V2PersistentCounterView(
            context=context,
            state_key=f"counter:{context.author.id}",
        )
        await view.send()

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
            state_key=f"roles:panel:{context.guild.id}",
        )
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2PersistenceExample(bot=bot))
