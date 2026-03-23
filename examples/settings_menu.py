"""
Advanced Settings Menu — CascadeUI Feature Showcase
====================================================

A mock server settings menu that demonstrates most of CascadeUI's core
features in a single, cohesive example:

    - Session limiting    (one settings panel per user per guild)
    - Scoped state        (user-level state shared across all views)
    - Navigation stack    (hub -> sub-pages with push/pop)
    - Theming             (live theme switching with per-view themes)
    - Undo/Redo           (revert notification preference changes)
    - State selectors     (views only re-render when their slice changes)
    - Batched dispatch    (multiple state updates in one notification)
    - Exit button         (clean teardown with component removal)

Usage:
    Load this cog in your bot, then run ``/settings`` in any server.
    Invoking ``/settings`` a second time while the first panel is still
    open will automatically close the old one (session_limit=1).

    Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import copy
import discord
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import (
    StatefulView,
    StatefulButton,
    StatefulSelect,
    SessionLimitError,
    UndoMiddleware,
    Theme,
    cascade_reducer,
    get_store,
    get_theme,
    set_default_theme,
)

import logging

logger = logging.getLogger(__name__)


# // ========================================( Reducer )======================================== // #


@cascade_reducer("SETTINGS_UPDATED")
async def settings_reducer(action, state):
    """Merge settings changes into the user's scoped state."""
    new_state = copy.deepcopy(state)
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


# // ========================================( Helpers )======================================== // #


async def navigate_back(view, interaction):
    """Pop the navigation stack and update the message."""
    await interaction.response.defer()
    prev_view = await view.pop(interaction)
    if prev_view:
        embed = prev_view.build_embed()
        msg = await interaction.edit_original_response(embed=embed, view=prev_view)
        prev_view._message = msg


async def navigate_to(view, target_cls, interaction):
    """Push a new view and update the message."""
    await interaction.response.defer()
    new_view = await view.push(target_cls, interaction)
    embed = new_view.build_embed()
    msg = await interaction.edit_original_response(embed=embed, view=new_view)
    new_view._message = msg


# // ========================================( Settings Hub )======================================== // #


class SettingsHubView(StatefulView):
    """Main settings dashboard — the root of the navigation stack.

    Features demonstrated:
        - session_limit=1 with user_guild scope (only one open at a time)
        - User-scoped state for persistent preferences
        - Push navigation to sub-pages
        - State selector (only re-renders when this user's settings change)
    """

    session_limit = 1
    session_scope = "user_guild"
    session_policy = "replace"

    scope = "user"

    subscribed_actions = {"SETTINGS_UPDATED"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulButton(
            label="Appearance",
            style=discord.ButtonStyle.primary,
            emoji="\N{ARTIST PALETTE}",
            callback=self.go_appearance,
        ))
        self.add_item(StatefulButton(
            label="Notifications",
            style=discord.ButtonStyle.primary,
            emoji="\N{BELL}",
            callback=self.go_notifications,
        ))
        self.add_item(StatefulButton(
            label="Language & Region",
            style=discord.ButtonStyle.primary,
            emoji="\N{GLOBE WITH MERIDIANS}",
            callback=self.go_locale,
        ))
        self.add_item(StatefulButton(
            label="Reset All",
            style=discord.ButtonStyle.danger,
            emoji="\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}",
            row=1,
            callback=self.reset_all,
        ))
        self.add_exit_button(row=1)

    def _get_settings(self):
        """Read the current user's settings from scoped state."""
        scoped = self.scoped_state
        return {**DEFAULT_SETTINGS, **scoped.get("settings", {})}

    def build_embed(self):
        s = self._get_settings()
        theme = get_theme(s["theme"]) or get_theme("default")

        bool_icon = lambda v: "\N{WHITE HEAVY CHECK MARK}" if v else "\N{CROSS MARK}"

        embed = discord.Embed(
            title="\N{GEAR} Server Settings",
            description="Choose a category to configure.",
        )
        theme.apply_to_embed(embed)

        embed.add_field(
            name="\N{ARTIST PALETTE} Appearance",
            value=f"Theme: **{s['theme'].title()}**",
            inline=True,
        )
        embed.add_field(
            name="\N{BELL} Notifications",
            value=(
                f"DMs: {bool_icon(s['notifications_dm'])}  "
                f"Mentions: {bool_icon(s['notifications_mentions'])}  "
                f"Events: {bool_icon(s['notifications_events'])}"
            ),
            inline=True,
        )
        embed.add_field(
            name="\N{GLOBE WITH MERIDIANS} Locale",
            value=f"{s['language']} / {s['timezone']}",
            inline=True,
        )
        embed.set_footer(text="Session limited: only one settings panel per user per guild.")

        return embed

    def state_selector(self, state):
        """Only re-render when this user's scoped settings change."""
        scoped = state.get("application", {}).get("_scoped", {})
        key = f"user:{self.user_id}"
        return scoped.get(key, {}).get("settings")

    async def update_from_state(self, state):
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.NotFound:
                pass

    async def go_appearance(self, interaction):
        await navigate_to(self, AppearanceView, interaction)

    async def go_notifications(self, interaction):
        await navigate_to(self, NotificationsView, interaction)

    async def go_locale(self, interaction):
        await navigate_to(self, LocaleView, interaction)

    async def reset_all(self, interaction):
        await interaction.response.defer()
        await self.dispatch_scoped({"settings": {}})
        if self.message:
            await self.message.edit(embed=self.build_embed(), view=self)


# // ========================================( Appearance )======================================== // #


class AppearanceView(StatefulView):
    """Theme selection sub-page.

    Features demonstrated:
        - Push/pop navigation (back button returns to hub)
        - Live theme switching via scoped state dispatch
        - Per-view theming (embed color changes immediately)
    """

    scope = "user"
    subscribed_actions = {"SETTINGS_UPDATED"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulSelect(
            placeholder="Choose a theme...",
            options=[
                discord.SelectOption(label="Default", value="default", emoji="\N{LARGE BLUE CIRCLE}"),
                discord.SelectOption(label="Dark", value="dark", emoji="\N{CRESCENT MOON}"),
                discord.SelectOption(label="Light", value="light", emoji="\N{SUN WITH FACE}"),
            ],
            callback=self.on_theme_select,
        ))
        self.add_item(StatefulButton(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="\N{LEFTWARDS ARROW WITH HOOK}",
            row=1,
            callback=self.go_back,
        ))

    def _get_settings(self):
        scoped = self.scoped_state
        return {**DEFAULT_SETTINGS, **scoped.get("settings", {})}

    def build_embed(self):
        s = self._get_settings()
        theme = get_theme(s["theme"]) or get_theme("default")

        embed = discord.Embed(
            title="\N{ARTIST PALETTE} Appearance",
            description=(
                f"Current theme: **{s['theme'].title()}**\n\n"
                "Select a theme below. The embed color updates instantly\n"
                "because this view uses scoped state and a state selector."
            ),
        )
        theme.apply_to_embed(embed)
        return embed

    def state_selector(self, state):
        scoped = state.get("application", {}).get("_scoped", {})
        key = f"user:{self.user_id}"
        return scoped.get(key, {}).get("settings", {}).get("theme")

    async def update_from_state(self, state):
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.NotFound:
                pass

    async def on_theme_select(self, interaction):
        selected = interaction.data["values"][0]
        await interaction.response.defer()
        await self.dispatch_scoped({"settings": {**self._get_settings(), "theme": selected}})
        # dispatch_scoped fires SCOPED_UPDATE, which isn't in subscribed_actions,
        # so update_from_state won't fire — manual edit needed.
        if self.message:
            await self.message.edit(embed=self.build_embed(), view=self)

    async def go_back(self, interaction):
        await navigate_back(self, interaction)


# // ========================================( Notifications )======================================== // #


class NotificationsView(StatefulView):
    """Notification preferences sub-page.

    Features demonstrated:
        - Undo/redo (revert notification toggles)
        - Batched dispatch (multiple toggles in one state update)
        - Toggle buttons that reflect current state
    """

    scope = "user"
    enable_undo = True
    undo_limit = 10
    subscribed_actions = {"SETTINGS_UPDATED", "UNDO", "REDO"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_buttons()

    def _get_settings(self):
        scoped = self.scoped_state
        return {**DEFAULT_SETTINGS, **scoped.get("settings", {})}

    def _build_buttons(self):
        """Add toggle buttons and undo/redo controls."""
        self.clear_items()

        s = self._get_settings()

        self.add_item(StatefulButton(
            label=f"DMs: {'ON' if s['notifications_dm'] else 'OFF'}",
            style=discord.ButtonStyle.success if s["notifications_dm"] else discord.ButtonStyle.secondary,
            callback=self.toggle_dm,
        ))
        self.add_item(StatefulButton(
            label=f"Mentions: {'ON' if s['notifications_mentions'] else 'OFF'}",
            style=discord.ButtonStyle.success if s["notifications_mentions"] else discord.ButtonStyle.secondary,
            callback=self.toggle_mentions,
        ))
        self.add_item(StatefulButton(
            label=f"Events: {'ON' if s['notifications_events'] else 'OFF'}",
            style=discord.ButtonStyle.success if s["notifications_events"] else discord.ButtonStyle.secondary,
            callback=self.toggle_events,
        ))

        # Undo / Redo controls
        self.add_item(StatefulButton(
            label="Undo",
            style=discord.ButtonStyle.secondary,
            emoji="\N{LEFTWARDS ARROW WITH HOOK}",
            row=1,
            callback=self.do_undo,
        ))
        self.add_item(StatefulButton(
            label="Redo",
            style=discord.ButtonStyle.secondary,
            emoji="\N{RIGHTWARDS ARROW WITH HOOK}",
            row=1,
            callback=self.do_redo,
        ))
        self.add_item(StatefulButton(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="\N{BLACK LEFT-POINTING TRIANGLE}",
            row=1,
            callback=self.go_back,
        ))

    def build_embed(self):
        s = self._get_settings()
        theme = get_theme(s["theme"]) or get_theme("default")

        bool_icon = lambda v: "\N{WHITE HEAVY CHECK MARK}" if v else "\N{CROSS MARK}"

        store = get_store()
        session = store.state.get("sessions", {}).get(self.session_id, {})
        undo_depth = len(session.get("undo_stack", []))
        redo_depth = len(session.get("redo_stack", []))

        embed = discord.Embed(
            title="\N{BELL} Notification Preferences",
            description=(
                f"DMs: {bool_icon(s['notifications_dm'])}  "
                f"Mentions: {bool_icon(s['notifications_mentions'])}  "
                f"Events: {bool_icon(s['notifications_events'])}\n\n"
                "Toggle each notification type. Use **Undo/Redo** to\n"
                "revert changes — the state snapshots are per-session."
            ),
        )
        theme.apply_to_embed(embed)
        embed.set_footer(text=f"Undo: {undo_depth} | Redo: {redo_depth}")

        return embed

    async def _toggle(self, interaction, key):
        s = self._get_settings()
        s[key] = not s[key]
        await interaction.response.defer()
        # update_from_state handles _build_buttons + message.edit via the
        # SETTINGS_UPDATED subscription, so no manual edit needed here.
        await self.dispatch("SETTINGS_UPDATED", {
            "scope_key": f"user:{self.user_id}",
            "changes": {key: s[key]},
        })

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
        await navigate_back(self, interaction)

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
        """Rebuild buttons when state changes (e.g. from undo/redo)."""
        self._build_buttons()
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.NotFound:
                pass


# // ========================================( Locale )======================================== // #


class LocaleView(StatefulView):
    """Language and timezone selection sub-page.

    Features demonstrated:
        - Multiple select menus on one view
        - Batched dispatch (language + timezone in one update)
        - Push/pop navigation
    """

    scope = "user"
    subscribed_actions = {"SETTINGS_UPDATED"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulSelect(
            placeholder="Language...",
            options=[
                discord.SelectOption(label="English", value="English"),
                discord.SelectOption(label="Spanish", value="Spanish"),
                discord.SelectOption(label="French", value="French"),
                discord.SelectOption(label="German", value="German"),
                discord.SelectOption(label="Japanese", value="Japanese"),
            ],
            row=0,
            callback=self.on_language,
        ))
        self.add_item(StatefulSelect(
            placeholder="Timezone...",
            options=[
                discord.SelectOption(label="UTC", value="UTC"),
                discord.SelectOption(label="US Eastern", value="US/Eastern"),
                discord.SelectOption(label="US Pacific", value="US/Pacific"),
                discord.SelectOption(label="Europe/London", value="Europe/London"),
                discord.SelectOption(label="Asia/Tokyo", value="Asia/Tokyo"),
            ],
            row=1,
            callback=self.on_timezone,
        ))
        self.add_item(StatefulButton(
            label="Back",
            style=discord.ButtonStyle.secondary,
            emoji="\N{LEFTWARDS ARROW WITH HOOK}",
            row=2,
            callback=self.go_back,
        ))

    def _get_settings(self):
        scoped = self.scoped_state
        return {**DEFAULT_SETTINGS, **scoped.get("settings", {})}

    def build_embed(self):
        s = self._get_settings()
        theme = get_theme(s["theme"]) or get_theme("default")

        embed = discord.Embed(
            title="\N{GLOBE WITH MERIDIANS} Language & Region",
            description=(
                f"Language: **{s['language']}**\n"
                f"Timezone: **{s['timezone']}**\n\n"
                "Changes are saved to your user-scoped state and\n"
                "persist across views via ``dispatch_scoped()``."
            ),
        )
        theme.apply_to_embed(embed)
        return embed

    def state_selector(self, state):
        scoped = state.get("application", {}).get("_scoped", {})
        key = f"user:{self.user_id}"
        settings = scoped.get(key, {}).get("settings", {})
        return (settings.get("language"), settings.get("timezone"))

    async def update_from_state(self, state):
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.NotFound:
                pass

    async def on_language(self, interaction):
        selected = interaction.data["values"][0]
        await interaction.response.defer()
        s = self._get_settings()
        s["language"] = selected
        await self.dispatch_scoped({"settings": s})
        if self.message:
            await self.message.edit(embed=self.build_embed(), view=self)

    async def on_timezone(self, interaction):
        selected = interaction.data["values"][0]
        await interaction.response.defer()
        s = self._get_settings()
        s["timezone"] = selected
        await self.dispatch_scoped({"settings": s})
        if self.message:
            await self.message.edit(embed=self.build_embed(), view=self)

    async def go_back(self, interaction):
        await navigate_back(self, interaction)


# // ========================================( Cog )======================================== // #


class SettingsMenuExample(commands.Cog, name="settings_menu_example"):
    """Advanced settings menu showcasing CascadeUI's core features."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="settings",
        description="Open the server settings panel."
    )
    async def settings(self, context: Context) -> None:
        """Open an interactive settings panel.

        Only one panel can be open per user per guild. Invoking this
        command again will automatically close the previous panel.
        """
        try:
            view = SettingsHubView(context=context)
            await view.send(embed=view.build_embed())
        except SessionLimitError:
            # This branch is only reachable if session_policy were "reject".
            # With "replace" (the default above), the old view is exited
            # automatically. Shown here for completeness.
            await context.send(
                "You already have a settings panel open in this server.",
                ephemeral=True,
            )


async def setup(bot) -> None:
    # Ensure UndoMiddleware is installed (idempotent check)
    store = get_store()
    if not any(isinstance(mw, UndoMiddleware) for mw in store._middleware):
        store.add_middleware(UndoMiddleware(store))

    await bot.add_cog(SettingsMenuExample(bot=bot))
