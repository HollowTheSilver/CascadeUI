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
    - The V2 builder set on the Controls tab: ``tab_nav`` inner navigation,
      ``button_row``, ``toggle_button``, ``cycle_button``, and a
      ``choice_row`` dropdown placed inside a card
    - A ``Collapsible`` revealing a ``confirm_section`` for a reveal-on-click
      reset, re-rendering through the tab's own rebuild path when toggled
    - ``link_section`` for text paired with a link button (About tab)
    - Session limiting (one dashboard per user per guild)
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


import logging

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    Collapsible,
    TabLayoutView,
    access_slot,
    action_section,
    button_row,
    card,
    cascade_reducer,
    choice_row,
    computed,
    confirm_section,
    cycle_button,
    divider,
    gap,
    key_value,
    link_section,
    read_slot,
    tab_nav,
    toggle_button,
    toggle_section,
)

logger = logging.getLogger(__name__)


# Default module enablement, restored by the Controls > Reset action.
_MODULE_DEFAULTS = {
    "Moderation": True,
    "Logging": True,
    "Welcome Messages": False,
    "Auto-Role": False,
    "Leveling": True,
}

# Controls-tab option sets. The theme presets map a label to the accent
# color the Appearance card previews when that preset is picked.
_REFRESH_INTERVALS = ["15s", "30s", "60s"]
_NOTIF_TYPES = ["Mentions", "DMs", "Replies", "Reactions"]
_THEME_PRESETS = {
    "Default": discord.Color.blurple(),
    "Midnight": discord.Color.dark_blue(),
    "Ocean": discord.Color.teal(),
    "Forest": discord.Color.green(),
    "Sunset": discord.Color.orange(),
    "Rose": discord.Color.magenta(),
}


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

    Four tabs demonstrate different V2 component patterns:

        Overview  -- Multiple themed containers with section accessories
        Modules   -- State-driven toggles + a Collapsible reset confirm
        Controls  -- The interactive builder set: tab_nav inner navigation,
                     button_row, toggle_button, cycle_button, and a
                     choice_row dropdown inside a card
        About     -- Rich text layout with separators, containers, and links
    """

    # // ----( Policy surface )---- // #
    owner_only = True
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "delete"
    auto_defer = True
    # ``state_scope = None`` because module toggles live on the instance; tab
    # visits travel through application state via the custom reducer above and
    # are read in build_overview.
    state_scope = None
    # Non-ephemeral panel; the flag has no effect here.
    auto_refresh_ephemeral = False

    # // ----( Tab styling )---- // #
    # Active tab uses success green, inactive tabs use secondary.
    active_tab_style = discord.ButtonStyle.success
    inactive_tab_style = discord.ButtonStyle.secondary

    def __init__(self, *args, **kwargs):
        # Module toggles and Controls-tab preferences are instance state, not
        # Redux. Visit counts live in the store so the ``@computed`` aggregate
        # above has a stable read path (``dashboard_total_visits``).
        self._modules = dict(_MODULE_DEFAULTS)

        # Controls-tab state: which inner sub-view ``tab_nav`` shows, plus
        # the preferences the builders drive.
        self._controls_view = "Preferences"
        self._notifications = True
        self._notif_types = {"Mentions", "DMs"}
        self._refresh_interval = "30s"
        self._theme_preset = "Default"

        # The Modules reset confirm hides behind a ``Collapsible`` (see
        # ``build_modules``); the trigger reveals a ``confirm_section`` inline.
        self._reset_zone = Collapsible(
            label="Danger zone",
            expanded_label="Hide",
            style=discord.ButtonStyle.danger,
            reveal=self._reset_reveal,
            # ``summary`` flips the trigger into an in-card action_section: the
            # button sits beside this one-line summary instead of standing alone.
            summary=lambda: "Reset all modules to defaults?",
            key="reset",
        )

        tabs = {
            "\U0001f4ca Overview": self.build_overview,
            "\U0001f9e9 Modules": self.build_modules,
            "\u2699\ufe0f Controls": self.build_controls,
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
        name = self.active_tab
        await self.dispatch("DASHBOARD_TAB_VISITED", {"tab_name": name})

    # // ==================( Helpers )================== // #

    def _exit_row(self):
        """One ActionRow holding just a Close (Exit) button for each tab.

        ``make_nav_row(back=False)`` yields the standard Back+Exit footer
        with Back dropped -- an exit-only row -- so there is no ActionRow to
        hand-roll.
        """
        return self.make_nav_row(back=False, exit_label="Close")

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
                            f"{tab.split(' ', 1)[-1]} {count}" for tab, count in visits.items()
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
            - Collapsible wrapping confirm_section() for a reveal-on-click
              danger action
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

        # The reset action stays collapsed until the user opens it, so the
        # confirm/cancel row only spends component budget when needed.
        reset = card(
            "## Maintenance",
            *self._reset_zone.render(self),
            color=discord.Color.dark_red(),
        )
        return [card(*items, color=discord.Color.purple()), gap(), reset, self._exit_row()]

    def _make_toggle(self, module_name):
        """Create a toggle callback for a specific module."""

        async def callback(interaction):
            self._modules[module_name] = not self._modules[module_name]
            await self._refresh_tabs()

        return callback

    def _reset_reveal(self):
        """Build the confirm prompt the reset Collapsible reveals when open."""
        return confirm_section(
            "Reset every module to its default state?",
            on_confirm=self._on_reset_modules,
            on_cancel=self._on_cancel_reset,
            confirm_label="Reset",
            cancel_label="Keep",
        )

    async def _on_reset_modules(self, interaction):
        self._modules = dict(_MODULE_DEFAULTS)
        self._reset_zone.collapse()
        await self._refresh_tabs()

    async def _on_cancel_reset(self, interaction):
        self._reset_zone.collapse()
        await self._refresh_tabs()

    # // ==================( Controls Tab )================== // #

    async def build_controls(self):
        """Interactive controls demonstrating the builder set.

        Demonstrates:
            - tab_nav() for lightweight inner sub-navigation (Preferences /
              Appearance) without a second TabLayoutView
            - button_row(), toggle_button(), cycle_button(), and a
              choice_row(multi=True) toggle group on Preferences
            - choice_row() as a single-select dropdown inside a card on Appearance
        """
        # tab_nav is the lighter alternative to TabLayoutView: tab-styled
        # buttons the view switches in its own callback, with no async tab
        # builders or on_tab_switched lifecycle. Here it splits one tab into
        # two sub-views.
        nav = tab_nav(
            {"Preferences": self._show_prefs, "Appearance": self._show_appearance},
            active=self._controls_view,
        )
        body = (
            self._controls_prefs()
            if self._controls_view == "Preferences"
            else self._controls_appearance()
        )
        return [nav, *body, self._exit_row()]

    def _controls_prefs(self):
        return [
            card(
                "## Preferences",
                key_value(
                    {
                        "Notifications": "On" if self._notifications else "Off",
                        "Refresh interval": self._refresh_interval,
                    }
                ),
                # A standalone toggle and a value-cycler share one ActionRow.
                # Both are rebuilt each render, so start= re-seeds the cycler
                # from the stored value rather than resetting to the first.
                ActionRow(
                    toggle_button(
                        active=self._notifications,
                        on_toggle=self._on_notifications,
                        labels=("Notifications On", "Notifications Off"),
                    ),
                    cycle_button(
                        values=_REFRESH_INTERVALS,
                        start=_REFRESH_INTERVALS.index(self._refresh_interval),
                        on_change=self._on_interval,
                        emoji="\U0001f504",
                    ),
                ),
                # button_row turns a {label: callback} map into one ActionRow.
                button_row(
                    {"Save": self._on_save, "Reset": self._on_reset_prefs},
                    style=discord.ButtonStyle.primary,
                ),
                # choice_row(multi=True) renders one toggle button per type
                # (4 <= the 5-button threshold). on_select receives the full
                # selected list, which _on_notif_types stores as a set.
                TextDisplay("Notify me about:"),
                choice_row(
                    {t: t for t in _NOTIF_TYPES},
                    selected=self._notif_types,
                    on_select=self._on_notif_types,
                    multi=True,
                    custom_id="notif_types",
                ),
                color=discord.Color.blurple(),
            )
        ]

    def _controls_appearance(self):
        # Six theme presets render choice_row as a dropdown -- placed inside
        # the card, not stranded below it. Picking one recolors this card.
        # custom_id keeps this control distinct from any other choice_row.
        return [
            card(
                "## Appearance",
                TextDisplay("Theme preset:"),
                choice_row(
                    {name: name for name in _THEME_PRESETS},
                    selected=self._theme_preset,
                    on_select=self._on_theme,
                    custom_id="theme",
                ),
                color=_THEME_PRESETS[self._theme_preset],
            )
        ]

    async def _show_prefs(self, interaction):
        self._controls_view = "Preferences"
        await self._refresh_tabs()

    async def _show_appearance(self, interaction):
        self._controls_view = "Appearance"
        await self._refresh_tabs()

    async def _on_notifications(self, interaction, active):
        self._notifications = active
        await self._refresh_tabs()

    async def _on_notif_types(self, interaction, values):
        # multi=True hands on_select the full selected list, not one value.
        self._notif_types = set(values)
        await self._refresh_tabs()

    async def _on_interval(self, interaction, value):
        self._refresh_interval = value
        await self._refresh_tabs()

    async def _on_theme(self, interaction, value):
        self._theme_preset = value
        await self._refresh_tabs()

    async def _on_save(self, interaction):
        await self.respond(interaction, "Preferences saved.", ephemeral=True)

    async def _on_reset_prefs(self, interaction):
        self._notifications = True
        self._notif_types = {"Mentions", "DMs"}
        self._refresh_interval = "30s"
        self._theme_preset = "Default"
        await self._refresh_tabs()

    # // ==================( About Tab )================== // #

    async def build_about(self):
        """Rich text layout demonstrating V2 visual features.

        Demonstrates:
            - card() with long-form markdown content
            - link_section() for text paired with a link button
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

        # link_section pairs text with a link button (no callback -- the
        # platform handles navigation). Link buttons are Section
        # accessories, so they cost no ActionRow budget.
        links = card(
            "## Resources",
            link_section(
                "Browse the full guide and API reference.",
                label="Docs",
                url="https://hollowthesilver.github.io/CascadeUI/",
            ),
            link_section(
                "Read Discord's V2 component reference.",
                label="Components",
                url="https://discord.com/developers/docs/components/reference",
            ),
            color=discord.Color.blurple(),
        )

        return [info, gap(), tech, gap(), links, self._exit_row()]


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

        Four tabs demonstrate layout patterns that are impossible in V1:
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
