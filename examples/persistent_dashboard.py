
# // ========================================( Modules )======================================== // #


import discord
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import PersistentView, StatefulButton

import logging

logger = logging.getLogger(__name__)


# // ========================================( Views )======================================== // #


class RoleSelectorView(PersistentView):
    """A role selector panel that stays interactive across bot restarts.

    Post it once with /setup_roles and it works forever -- no need
    for users to re-run a command after the bot restarts.

    Requires setup_persistence(bot) to be called in the bot's setup_hook.
    """

    # Role ID to assign (replace with your own)
    ROLE_ID = 123456789012345678

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulButton(
            label="Get Role",
            style=discord.ButtonStyle.primary,
            custom_id="role_selector:get_role",
            callback=self.toggle_role,
        ))

        self.add_item(StatefulButton(
            label="View Info",
            style=discord.ButtonStyle.secondary,
            custom_id="role_selector:info",
            callback=self.show_info,
        ))

    async def toggle_role(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return

        role = interaction.guild.get_role(self.ROLE_ID)
        if role is None:
            await interaction.response.send_message(
                "Role not found. An admin needs to update the ROLE_ID.", ephemeral=True
            )
            return

        member = interaction.user
        if role in member.roles:
            await member.remove_roles(role)
            await interaction.response.send_message(
                f"Removed **{role.name}**.", ephemeral=True
            )
        else:
            await member.add_roles(role)
            await interaction.response.send_message(
                f"Gave you **{role.name}**!", ephemeral=True
            )

    async def show_info(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Click **Get Role** to toggle the role on or off.", ephemeral=True
        )

    async def on_restore(self, bot):
        """Called after the view is re-attached on bot restart."""
        logger.info(f"Role selector restored for state_key={self.state_key}")


# // ========================================( Cog )======================================== // #


class PersistentDashboardExample(commands.Cog, name="persistent_dashboard_example"):
    """Demonstrates persistent views that survive bot restarts.

    This cog defines the view and commands. Persistence setup is handled
    by the bot's setup_hook via setup_persistence(bot), not here.
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="setup_roles",
        description="Post a role selector panel (admin only, runs once)."
    )
    @commands.has_permissions(manage_roles=True)
    async def setup_roles(self, context: Context) -> None:
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
    await bot.add_cog(PersistentDashboardExample(bot=bot))
