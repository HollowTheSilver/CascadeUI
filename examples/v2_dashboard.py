"""
V2 Dashboard -- CascadeUI V2 Layout Showcase
=============================================

A multi-tab dashboard demonstrating V2 layout capabilities that are
impossible with V1 embeds:

    - Multiple containers with different accent colors in one message
    - Sections with button accessories (text + action on the same line)
    - Tabbed navigation via ``TabLayoutView`` with customized tab styles
    - ``on_tab_switched`` hook for analytics on every tab change
    - Separators for visual hierarchy between content blocks
    - State-driven module toggles with live visual feedback
    - Session limiting (one dashboard per user per guild)
    - V2 convenience helpers for concise component assembly
    - ``@computed`` aggregate (``dashboard_total_visits``) read from the store

A V1 view shows one embed (one color) with buttons separated below.
A V2 view gives every content block its own color, its own inline
buttons, and its own visual identity -- all in a single message.

Commands:
    /v2dashboard   Open the dashboard

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    TabLayoutView,
    action_section,
    card,
    cascade_reducer,
    computed,
    divider,
    gap,
    read_slot,
    key_value,
    access_slot,
    toggle_section,
)

import logging

logger = logging.getLogger(__name__)


# // ========================================( Reducer + @computed )======================================== // #


@cascade_reducer("DASHBOARD_TAB_VISITED")
async def _dashboard_tab_visited(action, state):
    """Track per-tab visit counts in application state.

    The visits dict lives at ``state["application"]["dashboard"]["visits"]``
    so the ``@computed`` aggregate below has a stable read path. Lifting
    visits out of instance state is what lets the Overview tab back its
    "Tab visits" line with a derived store value rather than a per-view
    dict that would not survive a session restart.
    """
    visits = access_slot(state, "dashboard", "visits", default_factory=dict)
    name = action["payload"]["tab_name"]
    visits[name] = visits.get(name, 0) + 1
    return state


@computed(selector=lambda state: read_slot(state, "dashboard", "visits", default={}))
def dashboard_total_visits(visits):
    """Total tab visits across every dashboard session.

    Selector returns the visits dict; the compute step sums values. The
    cache key is the dict itself, so the sum only runs when a tab visit
    actually mutates the slot -- subsequent renders reuse the cached
    integer.
    """
    return sum(visits.values())


# // ========================================( Dashboard )======================================== // #


class DashboardView(TabLayoutView):
    """Multi-tab dashboard showcasing V2 layout features.

    Three tabs demonstrate different V2 component patterns:

        Overview  -- Multiple themed containers with section accessories
        Modules   -- State-driven toggles with live visual feedback
        About     -- Rich text layout with separators and containers
    """

    # // ----( Policy surface )---- // #
    owner_only = True
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "delete"
    auto_defer = True
    # ``state_scope = None`` because module toggles and tab-visit counters
    # live on the instance, not the Redux tree. The dashboard is a layout
    # showcase rather than a state-management tutorial -- see ``v2_settings``
    # for the scoped-state pattern.
    state_scope = None
    # Non-ephemeral panel; the flag has no effect here.
    auto_refresh_ephemeral = False

    # // ----( Tab styling )---- // #
    # Success green marks the active tab so the dashboard reads like an
    # admin panel; inactive tabs stay on the muted secondary style.
    active_tab_style = discord.ButtonStyle.success
    inactive_tab_style = discord.ButtonStyle.secondary

    def __init__(self, *args, **kwargs):
        # Module toggle states live on the instance, not in the Redux
        # tree, because this example is a layout showcase rather than a
        # state-management tutorial. Visit counts, by contrast, live in
        # the store so the ``@computed`` aggregate above has a stable
        # read path -- see ``dashboard_total_visits``.
        self._modules = {
            "Moderation": True,
            "Logging": True,
            "Welcome Messages": False,
            "Auto-Role": False,
            "Leveling": True,
        }

        tabs = {
            "\U0001f4ca Overview": self.build_overview,
            "\U0001f9e9 Modules": self.build_modules,
            "\u2139\ufe0f About": self.build_about,
        }
        super().__init__(*args, tabs=tabs, **kwargs)

    # // ==================( Override hooks )================== // #

    async def on_tab_switched(self, index: int) -> None:
        """Dispatch a visit event for the newly active tab.

        ``TabLayoutView.on_tab_switched`` is a no-op by default; overriding
        it is the library-supported way to run analytics, audit logging,
        or async setup on every tab change without having to reimplement
        the tab-button wiring. Dispatching ``DASHBOARD_TAB_VISITED``
        routes through the reducer above and feeds the ``@computed``
        total that the Overview surfaces.
        """
        name = self._tab_names[index]
        await self.dispatch("DASHBOARD_TAB_VISITED", {"tab_name": name})

    # // ==================( Helpers )================== // #

    def _exit_row(self):
        """Build an exit button ActionRow for the bottom of each tab.

        ``make_exit_button`` returns an unattached ``StatefulButton`` whose
        callback honors ``exit_policy`` and any ``delete_message`` override,
        so tab builders can pack it into their own ``ActionRow`` without
        hand-rolling a close handler.
        """
        return ActionRow(self.make_exit_button(label="Close"))

    @property
    def _guild(self):
        """Resolve the guild from whichever context is available."""
        if self.interaction and self.interaction.guild:
            return self.interaction.guild
        if self.context and self.context.guild:
            return self.context.guild
        return None

    # // ==================( Overview Tab )================== // #

    async def build_overview(self):
        """Server stats with action buttons as section accessories.

        Demonstrates:
            - card() helper for titled containers
            - action_section() for text + button on one line
            - key_value() for formatted stats
            - gap() for invisible gaps between containers
            - @computed read via ``store.computed["dashboard_total_visits"]``
        """
        guild = self._guild
        name = guild.name if guild else "Unknown Server"
        members = guild.member_count if guild else "?"
        channels = len(guild.channels) if guild else "?"
        roles = len(guild.roles) if guild else "?"
        enabled = sum(1 for v in self._modules.values() if v)
        # ``store.computed[name]`` returns the cached value produced by
        # the ``@computed`` registration above. The selector recomputes
        # only when the visits dict changes; subsequent tab renders read
        # the cached integer.
        visits = read_slot(self.state_store.state, "dashboard", "visits", default={})
        total_visits = self.state_store.computed["dashboard_total_visits"]

        # Server stats card -- green accent
        stats = card(
            f"## {name}",
            action_section(
                f"**Members:** {members}",
                label="Refresh",
                callback=self._refresh_overview,
                emoji="\U0001f504",
            ),
            divider(),
            key_value(
                {
                    "Channels": channels,
                    "Roles": roles,
                    "Active Modules": f"{enabled}/{len(self._modules)}",
                    "Tab visits (total)": total_visits or "_none yet_",
                    "Tab visits (per tab)": (
                        ", ".join(
                            f"{tab.split(' ', 1)[-1]} {count}"
                            for tab, count in visits.items()
                        )
                        or "_none yet_"
                    ),
                }
            ),
            color=discord.Color.green(),
        )

        # Quick actions card -- blurple accent
        actions = card(
            "## Quick Actions",
            action_section(
                "View and manage active bot modules",
                label="Modules",
                callback=self._go_to_modules,
                style=discord.ButtonStyle.primary,
            ),
            color=discord.Color.blurple(),
        )

        return [stats, gap(), actions, self._exit_row()]

    async def _refresh_overview(self, interaction):
        """Refresh the overview tab to update live stats."""
        await self._refresh_tabs()

    async def _go_to_modules(self, interaction):
        """Switch to the Modules tab from the Overview quick action."""
        await self.switch_tab("\U0001f9e9 Modules")

    # // ==================( Modules Tab )================== // #

    async def build_modules(self):
        """Toggleable module list with visual on/off indicators.

        Demonstrates:
            - toggle_section() for enable/disable rows
            - Dynamic component tree rebuilt on each toggle
        """
        enabled = sum(1 for v in self._modules.values() if v)
        items: list = [TextDisplay(f"## Bot Modules\n{enabled} of {len(self._modules)} enabled")]

        for name, active in self._modules.items():
            emoji = "\u2705" if active else "\u274c"
            items.append(
                toggle_section(
                    f"{emoji} **{name}**",
                    active=active,
                    callback=self._make_toggle(name),
                )
            )

        return [card(*items, color=discord.Color.purple()), self._exit_row()]

    def _make_toggle(self, module_name):
        """Create a toggle callback for a specific module."""

        async def callback(interaction):
            self._modules[module_name] = not self._modules[module_name]
            await self._refresh_tabs()

        return callback

    # // ==================( About Tab )================== // #

    async def build_about(self):
        """Rich text layout demonstrating V2 visual features.

        Demonstrates:
            - card() with long-form markdown content
            - gap() for invisible gaps between containers
            - Subheading text via Discord's -# markdown
        """
        info = card(
            "## About This Dashboard",
            TextDisplay(
                "This dashboard demonstrates **V2 Components**, Discord's "
                "layout system for rich, structured messages.\n\n"
                "### What's different from V1?\n"
                "- **Multiple colored containers** in one message\n"
                "- **Sections** with action buttons on the same line as text\n"
                "- **Separators** for clean visual hierarchy\n"
                "- **Buttons inside containers**, not floating below"
            ),
            color=discord.Color.gold(),
        )

        tech = card(
            "## Built With",
            TextDisplay(
                "**CascadeUI** - Stateful Discord UI framework\n"
                "**discord.py 2.7+** - V2 component support\n\n"
                "-# TabLayoutView \u2022 Section \u2022 Container \u2022 StatefulButton"
            ),
            color=discord.Color.dark_grey(),
        )

        return [info, gap(), tech, self._exit_row()]


# // ========================================( Cog )======================================== // #


class V2DashboardExample(commands.Cog, name="v2_dashboard_example"):
    """V2 dashboard showcasing multi-container layouts and sections."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2dashboard",
        description="Open a V2 dashboard with tabs, sections, and themed containers.",
    )
    async def v2dashboard(self, context: Context) -> None:
        """Open a multi-tab V2 dashboard.

        Three tabs demonstrate layout patterns that are impossible in V1:
        multiple colored containers, sections with inline action buttons,
        and state-driven toggles -- all in one message.
        """
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        # send() auto-handles InstanceLimitError via on_instance_limit,
        # which sends an ephemeral default message. No try/except needed.
        view = DashboardView(context=context)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2DashboardExample(bot=bot))
