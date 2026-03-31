"""
V2 Settings Menu — CascadeUI V2 Feature Showcase
==================================================

The V2 equivalent of settings_menu.py. A mock server settings panel
demonstrating most of CascadeUI's core features using V2 components:

    - Session limiting    (one settings panel per user per guild)
    - Scoped state        (user-level state shared across all views)
    - Navigation stack    (hub -> sub-pages with push/pop)
    - Theming             (live theme switching with accent colors)
    - Undo/Redo           (revert notification preference changes)
    - State selectors     (views only re-render when their slice changes)
    - V2 convenience helpers (card, key_value, toggle_section, action_section, divider)

In V1, each sub-page renders an embed. In V2, each sub-page builds a
card with accent colors, sections with inline action buttons, and
toggle sections with green/red feedback — all in the same message.

Commands:
    /v2settings   Open the settings panel

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
    SessionLimitError,
    StatefulButton,
    StatefulLayoutView,
    StatefulSelect,
    UndoMiddleware,
    action_section,
    card,
    cascade_reducer,
    divider,
    get_store,
    get_theme,
    key_value,
    toggle_section,
)

logger = logging.getLogger(__name__)


# // ========================================( Reducer )======================================== // #


@cascade_reducer("SETTINGS_UPDATED")
async def settings_reducer(action, state):
    """Merge settings changes into the user's scoped state."""
    new_state = state
    new_state.setdefault("application", {}).setdefault("_scoped", {})

    scope_key = action["payload"].get("scope_key")
    changes = action["payload"].get("changes", {})

    if scope_key:
        scoped = new_state["application"]["_scoped"].setdefault(scope_key, {})
        scoped.setdefault("settings", {}).update(changes)

    return new_state


# // ========================================( Defaults )======================================== // #


DEFAULT_SETTINGS = {
    "theme": "default",
    "notifications_dm": True,
    "notifications_mentions": True,
    "notifications_events": False,
    "language": "English",
    "timezone": "UTC",
}


# // ========================================( Settings Hub )======================================== // #


class V2SettingsHubView(StatefulLayoutView):
    """Main settings dashboard — the root of the navigation stack.

    Demonstrates session_limit, user-scoped state, push navigation,
    state selectors, card() with key_value(), and action_section()
    for inline navigation buttons with descriptions.
    """

    session_limit = 1
    session_scope = "user_guild"
    session_policy = "replace"

    scope = "user"

    subscribed_actions = {"SETTINGS_UPDATED"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_ui()

    def _get_settings(self):
        scoped = self.scoped_state
        return {**DEFAULT_SETTINGS, **scoped.get("settings", {})}

    def _build_ui(self):
        self.clear_items()

        s = self._get_settings()
        theme = get_theme(s["theme"]) or get_theme("default")
        accent = theme.get_style("primary_color") or discord.Color.blurple()

        bool_icon = lambda v: "\u2705" if v else "\u274c"

        self.add_item(
            card(
                "## \u2699\ufe0f Server Settings",
                key_value(
                    {
                        "\U0001f3a8 Theme": s["theme"].title(),
                        "\U0001f514 Notifications": (
                            f"DMs {bool_icon(s['notifications_dm'])}  "
                            f"Mentions {bool_icon(s['notifications_mentions'])}  "
                            f"Events {bool_icon(s['notifications_events'])}"
                        ),
                        "\U0001f310 Locale": f"{s['language']} / {s['timezone']}",
                    }
                ),
                divider(),
                action_section(
                    "Customize theme and accent colors",
                    label="Appearance",
                    emoji="\U0001f3a8",
                    callback=self.go_appearance,
                    style=discord.ButtonStyle.primary,
                ),
                action_section(
                    "Configure DM, mention, and event alerts",
                    label="Notifications",
                    emoji="\U0001f514",
                    callback=self.go_notifications,
                    style=discord.ButtonStyle.primary,
                ),
                action_section(
                    "Set language and timezone preferences",
                    label="Locale",
                    emoji="\U0001f310",
                    callback=self.go_locale,
                    style=discord.ButtonStyle.primary,
                ),
                divider(),
                TextDisplay("-# Session limited: only one settings panel per user per guild."),
                color=accent,
            )
        )

        async def _exit(interaction):
            await interaction.response.defer()
            await self.exit()

        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Reset All",
                    style=discord.ButtonStyle.danger,
                    emoji="\U0001f504",
                    callback=self.reset_all,
                ),
                StatefulButton(
                    label="Exit",
                    style=discord.ButtonStyle.secondary,
                    emoji="\u274c",
                    callback=_exit,
                ),
            )
        )

    def state_selector(self, state):
        scoped = state.get("application", {}).get("_scoped", {})
        key = f"user:{self.user_id}"
        return scoped.get(key, {}).get("settings")

    async def update_from_state(self, state):
        self._build_ui()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass

    async def go_appearance(self, interaction):
        await self.push(V2AppearanceView, interaction, rebuild=lambda v: v._build_ui())

    async def go_notifications(self, interaction):
        await self.push(V2NotificationsView, interaction, rebuild=lambda v: v._build_ui())

    async def go_locale(self, interaction):
        await self.push(V2LocaleView, interaction, rebuild=lambda v: v._build_ui())

    async def reset_all(self, interaction):
        await interaction.response.defer()
        await self.dispatch(
            "SETTINGS_UPDATED",
            {
                "scope_key": f"user:{self.user_id}",
                "changes": DEFAULT_SETTINGS,
            },
        )


# // ========================================( Appearance )======================================== // #


class V2AppearanceView(StatefulLayoutView):
    """Theme selection sub-page.

    Demonstrates push/pop navigation, live theme switching via scoped
    state dispatch, and accent color changes. Uses card() for the
    themed container.
    """

    scope = "user"
    subscribed_actions = {"SETTINGS_UPDATED"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_ui()

    def _get_settings(self):
        scoped = self.scoped_state
        return {**DEFAULT_SETTINGS, **scoped.get("settings", {})}

    def _build_ui(self):
        self.clear_items()

        s = self._get_settings()
        theme = get_theme(s["theme"]) or get_theme("default")
        accent = theme.get_style("primary_color") or discord.Color.blurple()

        self.add_item(
            card(
                "## \U0001f3a8 Appearance",
                TextDisplay(
                    f"Current theme: **{s['theme'].title()}**\n\n"
                    "Select a theme below. The card's accent color\n"
                    "updates instantly via scoped state."
                ),
                color=accent,
            )
        )

        self.add_item(
            ActionRow(
                StatefulSelect(
                    placeholder="Choose a theme...",
                    options=[
                        discord.SelectOption(label="Default", value="default", emoji="\U0001f535"),
                        discord.SelectOption(label="Dark", value="dark", emoji="\U0001f319"),
                        discord.SelectOption(label="Light", value="light", emoji="\U0001f31e"),
                    ],
                    callback=self.on_theme_select,
                )
            )
        )

        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Back",
                    style=discord.ButtonStyle.secondary,
                    emoji="\u21a9\ufe0f",
                    callback=self.go_back,
                )
            )
        )

    def state_selector(self, state):
        scoped = state.get("application", {}).get("_scoped", {})
        key = f"user:{self.user_id}"
        return scoped.get(key, {}).get("settings", {}).get("theme")

    async def update_from_state(self, state):
        self._build_ui()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass

    async def on_theme_select(self, interaction):
        selected = interaction.data["values"][0]
        await interaction.response.defer()
        await self.dispatch(
            "SETTINGS_UPDATED",
            {
                "scope_key": f"user:{self.user_id}",
                "changes": {"theme": selected},
            },
        )

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: v._build_ui())


# // ========================================( Notifications )======================================== // #


class V2NotificationsView(StatefulLayoutView):
    """Notification preferences sub-page.

    Demonstrates undo/redo with stack depth display and toggle_section()
    for green/red toggle buttons with inline labels. Each toggle is a
    Section with a StatefulButton accessory — the V2 signature pattern.
    """

    scope = "user"
    enable_undo = True
    undo_limit = 10
    subscribed_actions = {"SETTINGS_UPDATED", "UNDO", "REDO"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_ui()

    def _get_settings(self):
        scoped = self.scoped_state
        return {**DEFAULT_SETTINGS, **scoped.get("settings", {})}

    def _build_ui(self):
        self.clear_items()

        s = self._get_settings()

        store = get_store()
        session = store.state.get("sessions", {}).get(self.session_id, {})
        undo_depth = len(session.get("undo_stack", []))
        redo_depth = len(session.get("redo_stack", []))

        self.add_item(
            card(
                "## \U0001f514 Notifications",
                TextDisplay(
                    "Toggle each notification type below.\n" "Use **Undo/Redo** to revert changes."
                ),
                divider(),
                toggle_section(
                    "**Direct Messages**\nGet notified about new DMs",
                    active=s["notifications_dm"],
                    callback=self.toggle_dm,
                ),
                toggle_section(
                    "**Mentions**\nAlerts when you're mentioned",
                    active=s["notifications_mentions"],
                    callback=self.toggle_mentions,
                ),
                toggle_section(
                    "**Server Events**\nJoins, leaves, and other events",
                    active=s["notifications_events"],
                    callback=self.toggle_events,
                ),
                divider(),
                TextDisplay(f"-# Undo: {undo_depth} | Redo: {redo_depth}"),
                color=discord.Color.og_blurple(),
            )
        )

        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Undo",
                    style=discord.ButtonStyle.secondary,
                    emoji="\u21a9\ufe0f",
                    callback=self.do_undo,
                ),
                StatefulButton(
                    label="Redo",
                    style=discord.ButtonStyle.secondary,
                    emoji="\u21aa\ufe0f",
                    callback=self.do_redo,
                ),
                StatefulButton(
                    label="Back",
                    style=discord.ButtonStyle.secondary,
                    emoji="\u25c0",
                    callback=self.go_back,
                ),
            )
        )

    async def _toggle(self, interaction, key):
        s = self._get_settings()
        s[key] = not s[key]
        await interaction.response.defer()
        await self.dispatch(
            "SETTINGS_UPDATED",
            {
                "scope_key": f"user:{self.user_id}",
                "changes": {key: s[key]},
            },
        )

    async def toggle_dm(self, interaction):
        await self._toggle(interaction, "notifications_dm")

    async def toggle_mentions(self, interaction):
        await self._toggle(interaction, "notifications_mentions")

    async def toggle_events(self, interaction):
        await self._toggle(interaction, "notifications_events")

    async def do_undo(self, interaction):
        await interaction.response.defer()
        await self.undo(interaction)

    async def do_redo(self, interaction):
        await interaction.response.defer()
        await self.redo(interaction)

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: v._build_ui())

    def state_selector(self, state):
        scoped = state.get("application", {}).get("_scoped", {})
        key = f"user:{self.user_id}"
        settings = scoped.get(key, {}).get("settings", {})
        return (
            settings.get("notifications_dm"),
            settings.get("notifications_mentions"),
            settings.get("notifications_events"),
        )

    async def update_from_state(self, state):
        self._build_ui()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass


# // ========================================( Locale )======================================== // #


class V2LocaleView(StatefulLayoutView):
    """Language and timezone selection sub-page.

    Demonstrates multiple select menus, scoped state dispatch,
    and push/pop navigation. Uses card() with key_value() for
    the current settings display.
    """

    scope = "user"
    subscribed_actions = {"SETTINGS_UPDATED"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_ui()

    def _get_settings(self):
        scoped = self.scoped_state
        return {**DEFAULT_SETTINGS, **scoped.get("settings", {})}

    def _build_ui(self):
        self.clear_items()

        s = self._get_settings()

        self.add_item(
            card(
                "## \U0001f310 Language & Region",
                key_value(
                    {
                        "Language": s["language"],
                        "Timezone": s["timezone"],
                    }
                ),
                TextDisplay(
                    "\nChanges are saved to your user-scoped state\n"
                    "and update the hub live via dispatch."
                ),
                color=discord.Color.teal(),
            )
        )

        self.add_item(
            ActionRow(
                StatefulSelect(
                    placeholder="Language...",
                    options=[
                        discord.SelectOption(label="English", value="English"),
                        discord.SelectOption(label="Spanish", value="Spanish"),
                        discord.SelectOption(label="French", value="French"),
                        discord.SelectOption(label="German", value="German"),
                        discord.SelectOption(label="Japanese", value="Japanese"),
                    ],
                    callback=self.on_language,
                )
            )
        )

        self.add_item(
            ActionRow(
                StatefulSelect(
                    placeholder="Timezone...",
                    options=[
                        discord.SelectOption(label="UTC", value="UTC"),
                        discord.SelectOption(label="US Eastern", value="US/Eastern"),
                        discord.SelectOption(label="US Pacific", value="US/Pacific"),
                        discord.SelectOption(label="Europe/London", value="Europe/London"),
                        discord.SelectOption(label="Asia/Tokyo", value="Asia/Tokyo"),
                    ],
                    callback=self.on_timezone,
                )
            )
        )

        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Back",
                    style=discord.ButtonStyle.secondary,
                    emoji="\u21a9\ufe0f",
                    callback=self.go_back,
                )
            )
        )

    def state_selector(self, state):
        scoped = state.get("application", {}).get("_scoped", {})
        key = f"user:{self.user_id}"
        settings = scoped.get(key, {}).get("settings", {})
        return (settings.get("language"), settings.get("timezone"))

    async def update_from_state(self, state):
        self._build_ui()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass

    async def on_language(self, interaction):
        selected = interaction.data["values"][0]
        await interaction.response.defer()
        await self.dispatch(
            "SETTINGS_UPDATED",
            {
                "scope_key": f"user:{self.user_id}",
                "changes": {"language": selected},
            },
        )

    async def on_timezone(self, interaction):
        selected = interaction.data["values"][0]
        await interaction.response.defer()
        await self.dispatch(
            "SETTINGS_UPDATED",
            {
                "scope_key": f"user:{self.user_id}",
                "changes": {"timezone": selected},
            },
        )

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: v._build_ui())


# // ========================================( Cog )======================================== // #


class V2SettingsMenuExample(commands.Cog, name="v2_settings_menu_example"):
    """V2 settings menu showcasing CascadeUI's core features with V2 components."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2settings",
        description="Open the V2 server settings panel.",
    )
    async def v2settings(self, context: Context) -> None:
        """Open an interactive settings panel using V2 components.

        Only one panel can be open per user per guild. Invoking this
        command again will automatically close the previous panel.
        """
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        try:
            view = V2SettingsHubView(context=context)
            await view.send()
        except SessionLimitError:
            await context.send(
                "You already have a settings panel open in this server.",
                ephemeral=True,
            )


async def setup(bot) -> None:
    store = get_store()
    if not any(isinstance(mw, UndoMiddleware) for mw in store._middleware):
        store.add_middleware(UndoMiddleware(store))

    await bot.add_cog(V2SettingsMenuExample(bot=bot))
