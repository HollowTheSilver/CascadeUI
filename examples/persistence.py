
# // ========================================( Modules )======================================== // #


import discord
from datetime import datetime
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import (
    StatefulView, StatefulButton, PersistentView,
    get_store, cascade_reducer, setup_persistence,
)
from cascadeui.persistence import SQLiteBackend

import logging

logger = logging.getLogger(__name__)


# // ========================================( Reducer )======================================== // #


@cascade_reducer("PERSISTENT_COUNTER_UPDATED")
async def counter_reducer(action, state):
    """Handle counter updates in the state."""
    new_state = state

    new_state.setdefault("application", {})
    new_state["application"].setdefault("counters", {})

    # Key by state_key (stable) for persistence, fall back to view_id
    key = action["payload"].get("state_key") or action["payload"].get("view_id")
    counter_value = action["payload"].get("counter")

    if key:
        new_state["application"]["counters"][key] = counter_value

    return new_state


# // ========================================( Views )======================================== // #


class PersistentCounterView(StatefulView):
    """A counter that persists its state to SQLite.

    Uses state_key to scope data by user ID, so each user's counter
    persists independently across view timeouts and bot restarts.

    Requires setup_persistence() to be called in the bot's setup_hook.
    """

    session_limit = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Restore counter from persisted state
        store = get_store()
        counters = store.state.get("application", {}).get("counters", {})
        self.counter = counters.get(self.state_key, 0)

        self.add_item(StatefulButton(
            label="Increment", style=discord.ButtonStyle.primary,
            callback=self.increment,
        ))
        self.add_item(StatefulButton(
            label="Decrement", style=discord.ButtonStyle.danger,
            callback=self.decrement,
        ))
        self.add_item(StatefulButton(
            label="Reset", style=discord.ButtonStyle.secondary,
            callback=self.reset,
        ))
        self.add_exit_button()

    async def increment(self, interaction):
        await interaction.response.defer()
        self.counter += 1
        await self._sync_state()
        await self.update_ui()

    async def decrement(self, interaction):
        await interaction.response.defer()
        self.counter -= 1
        await self._sync_state()
        await self.update_ui()

    async def reset(self, interaction):
        await interaction.response.defer()
        self.counter = 0
        await self._sync_state()
        await self.update_ui()

    async def _sync_state(self):
        """Push the current counter value into the state store."""
        await self.dispatch("PERSISTENT_COUNTER_UPDATED", {
            "state_key": self.state_key,
            "counter": self.counter,
        })

    async def update_ui(self):
        embed = discord.Embed(
            title="Persistent Counter",
            description=f"Current value: {self.counter}",
            color=discord.Color.blue() if self.counter >= 0 else discord.Color.red(),
        )
        embed.set_footer(text=f"SQLite-backed | Last updated: {datetime.now().strftime('%H:%M:%S')}")

        if self.message:
            await self.message.edit(embed=embed, view=self)

    async def update_from_state(self, state):
        pass


class RoleSelectorView(PersistentView):
    """A role selector panel that stays interactive across bot restarts.

    Post it once with /setup_roles and it works forever -- no need
    for users to re-run a command after the bot restarts.

    Requires setup_persistence(bot) to be called in the bot's setup_hook.
    """

    session_limit = 1
    session_scope = "guild"

    # Replace with your own role ID
    ROLE_ID = 123456789012345678

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulButton(
            label="Get Role", style=discord.ButtonStyle.primary,
            custom_id="role_selector:get_role",
            callback=self.toggle_role,
        ))
        self.add_item(StatefulButton(
            label="View Info", style=discord.ButtonStyle.secondary,
            custom_id="role_selector:info",
            callback=self.show_info,
        ))

    async def toggle_role(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used in a server.", ephemeral=True,
            )
            return

        role = interaction.guild.get_role(self.ROLE_ID)
        if role is None:
            await interaction.response.send_message(
                "Role not found. An admin needs to update the ROLE_ID.", ephemeral=True,
            )
            return

        member = interaction.user
        if role in member.roles:
            await member.remove_roles(role)
            await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
        else:
            await member.add_roles(role)
            await interaction.response.send_message(f"Gave you **{role.name}**!", ephemeral=True)

    async def show_info(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Click **Get Role** to toggle the role on or off.", ephemeral=True,
        )

    async def on_restore(self, bot):
        """Called after the view is re-attached on bot restart."""
        logger.info(f"Role selector restored for state_key={self.state_key}")


# // ========================================( Cog )======================================== // #


class PersistenceExample(commands.Cog, name="persistence_example"):
    """Demonstrates SQLite-backed state persistence and persistent views.

    Requires setup_persistence() in the bot's setup_hook::

        from cascadeui import setup_persistence
        from cascadeui.persistence import SQLiteBackend

        async def setup_hook():
            await setup_persistence(bot, backend=SQLiteBackend("cascadeui.db"))
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="pcounter",
        description="Display a persistent counter backed by SQLite."
    )
    async def pcounter(self, context: Context) -> None:
        # state_key scopes data by user -- each user gets their own counter
        view = PersistentCounterView(
            context=context,
            state_key=f"counter:{context.author.id}",
        )

        embed = discord.Embed(
            title="Persistent Counter",
            description=f"Current value: {view.counter}",
            color=discord.Color.blue() if view.counter >= 0 else discord.Color.red(),
        )

        if view.counter != 0:
            embed.set_footer(text="Restored from saved state")

        await view.send(embed=embed)

    @commands.hybrid_command(
        name="setup_roles",
        description="Post a role selector panel (admin only, runs once)."
    )
    @commands.has_permissions(manage_roles=True)
    async def setup_roles(self, context: Context) -> None:
        # Running this again with the same state_key automatically cleans
        # up the previous panel (exits the old view and removes its components).
        view = RoleSelectorView(
            context=context,
            state_key="role_selector:main",
        )

        embed = discord.Embed(
            title="Role Selector",
            description="Click the button below to get or remove the role.",
            color=discord.Color.blurple(),
        )

        await view.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(PersistenceExample(bot=bot))
