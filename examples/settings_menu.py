"""
Advanced Settings Menu -- CascadeUI Feature Showcase
====================================================

A mock server settings menu that demonstrates most of CascadeUI's core
features in a single, cohesive example:

    - ``MenuView``        (category-based hub with auto-wired push callbacks)
    - Session limiting    (one settings panel per user per guild)
    - Scoped state        (user-level state shared across all views)
    - Navigation stack    (hub -> sub-pages with push/pop)
    - Theming             (live theme switching with per-view themes)
    - Undo/Redo           (revert notification preference changes)
    - State selectors     (views only rebuild when their slice changes)
    - Batched dispatch    (multiple state updates in one notification)
    - ``with_confirmation`` wrapper on the Reset All danger button
    - Exit button         (clean teardown with component removal)

Usage:
    Load this cog in your bot, then run ``/settings`` in any server.
    Invoking ``/settings`` a second time while the first panel is still
    open will automatically close the old one (instance_limit=1).

    Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import discord
from discord.ext import commands
from discord.ext.commands import Context

import logging

from cascadeui import (
    MenuView,
    StatefulButton,
    StatefulSelect,
    StatefulView,
    StateStore,
    cascade_reducer,
    get_theme,
    with_confirmation,
)

logger = logging.getLogger(__name__)


# // ========================================( Reducer )======================================== // #


# An identical reducer exists in v2_settings.py so each cog remains
# copy-pasteable on its own.  Both registrations target the same action
# type; the second load overwrites the first with identical logic.
@cascade_reducer("SETTINGS_UPDATED")
async def settings_reducer(action, state):
    """Merge settings changes into the user's scoped state.

    Decodes the canonical ``{"scope", "identifiers", "data"}`` payload
    shape emitted by ``view.dispatch_scoped_as(...)`` and merges ``data``
    under the ``"settings"`` subkey of the scope bucket.
    """
    payload = action["payload"]
    return StateStore.merge_scoped(
        state,
        payload.get("scope"),
        payload.get("data", {}),
        subkey="settings",
        **payload.get("identifiers", {}),
    )


# // ========================================( Defaults )======================================== // #


DEFAULT_SETTINGS = {
    "theme": "default",
    "notifications_dm": True,
    "notifications_mentions": True,
    "notifications_events": False,
    "notifications_updates": True,
    "notifications_tips": False,
    "language": "English",
    "timezone": "UTC",
}


# // ========================================( Helpers )======================================== // #


def _read_user_settings(scoped_state):
    """Merge DEFAULT_SETTINGS with a user's scoped settings slot.

    Centralizes the default/scoped merge so each view class reads the
    same shape. ``scoped_state`` is whatever ``self.scoped_state``
    returned -- a dict with an optional ``"settings"`` key.
    """
    return {**DEFAULT_SETTINGS, **scoped_state.get("settings", {})}


def _bool_icon(value):
    """Return a checkmark for True, a cross for False."""
    return "\N{WHITE HEAVY CHECK MARK}" if value else "\N{CROSS MARK}"


# // ========================================( Settings Hub )======================================== // #


class SettingsHubView(MenuView):
    """Main settings dashboard -- the root of the navigation stack.

    ``MenuView`` handles the category-to-subview push wiring automatically.
    Declaring ``categories=`` in ``__init__`` replaces the three manual
    ``go_*`` callbacks and ``StatefulButton`` additions that a raw
    ``StatefulView`` hub would need.

    Features demonstrated:
        - ``MenuView`` (category hub with auto-wired push)
        - ``build_embed()`` override (live settings summary)
        - ``_build_extra_items()`` override (``with_confirmation`` on Reset All)
        - instance_limit=1 with user_guild scope
        - State selector (only rebuilds when this user's settings change)
        - Batched dispatch (multiple state updates in one notification)
    """

    # Full policy surface declared explicitly per CLAUDE.md examples grammar.
    owner_only = True
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "disable"
    auto_defer = True
    timeout = 600.0
    state_scope = "user"
    subscribed_actions = {"SETTINGS_UPDATED"}
    # Exit button is placed manually in _build_extra_items alongside Reset All.
    auto_exit_button = False

    def __init__(self, *args, **kwargs):
        # Each category dict wires a push callback automatically.
        # ``rebuild=lambda v: {"embed": v.build_embed()}`` is the default
        # for V1 and does not need to be specified explicitly.
        categories = [
            {
                "label": "Appearance",
                "emoji": "\N{ARTIST PALETTE}",
                "view": AppearanceView,
            },
            {
                "label": "Notifications",
                "emoji": "\N{BELL}",
                "view": NotificationsView,
            },
            {
                "label": "Language & Region",
                "emoji": "\N{GLOBE WITH MERIDIANS}",
                "view": LocaleView,
            },
        ]
        super().__init__(*args, categories=categories, **kwargs)

    def _build_extra_items(self):
        """Add Reset All and Exit buttons below category buttons.

        ``with_confirmation`` wraps the Reset All button in an ephemeral
        confirmation prompt. The wrapped callback is only invoked after the
        user confirms; the wrapper consumes ``interaction.response`` to show
        the prompt, so ``reset_all`` must use ``self.respond()`` for
        any replies it issues (handles response/followup routing).
        """
        reset_button = StatefulButton(
            label="Reset All",
            style=discord.ButtonStyle.danger,
            emoji="\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}",
            row=1,
            callback=self.reset_all,
        )
        self.add_item(
            with_confirmation(
                reset_button,
                title="Reset all settings?",
                message="This will restore every preference to its default value.",
                confirm_label="Reset",
                cancel_label="Keep",
                confirmed_message="Settings reset to defaults.",
                cancelled_message="Reset cancelled.",
            )
        )
        self.add_exit_button(row=1)

    def build_embed(self):
        s = _read_user_settings(self.scoped_state)
        theme = get_theme(s["theme"]) or get_theme("default")

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
                f"DMs: {_bool_icon(s['notifications_dm'])}  "
                f"Mentions: {_bool_icon(s['notifications_mentions'])}  "
                f"Events: {_bool_icon(s['notifications_events'])}"
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
        """Only rebuild when this user's scoped settings change.

        Selectors must read from the ``state`` argument so the equality
        check sees the post-reduce snapshot. Reading ``self.scoped_state``
        here would race the dispatcher.
        """
        return StateStore.get_scoped_from(state, "user", user_id=self.user_id).get("settings")

    async def reset_all(self, interaction):
        # ``with_confirmation`` already consumed ``interaction.response`` to
        # edit the ephemeral prompt with ``confirmed_message``. This callback
        # only needs to dispatch the state changes; the subscription pipeline
        # rebuilds the hub card via ``on_state_changed``.
        #
        # ``self.batch()`` groups multiple dispatches into a single subscriber
        # notification -- reducers still run for each action, but subscribers
        # are only notified once at the end. Six dispatches, one rebuild.
        async with self.batch():
            for key, value in DEFAULT_SETTINGS.items():
                await self.dispatch_scoped_as("SETTINGS_UPDATED", {key: value}, scope="user")


# // ========================================( Appearance )======================================== // #


class AppearanceView(StatefulView):
    """Theme selection sub-page.

    Features demonstrated:
        - Push/pop navigation (back button returns to hub)
        - Live theme switching via scoped state dispatch
        - Per-view theming (embed color changes immediately)
        - ``set_selected()`` on a persistent select instance -- the
          current theme stays highlighted without rebuilding the
          component on every state change.
    """

    owner_only = True
    auto_defer = True
    state_scope = "user"
    exit_policy = "disable"
    subscribed_actions = {"SETTINGS_UPDATED"}

    _THEME_OPTIONS = [
        ("Default", "default", "\N{LARGE BLUE CIRCLE}"),
        ("Dark", "dark", "\N{CRESCENT MOON}"),
        ("Light", "light", "\N{SUN WITH FACE}"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._theme_select = StatefulSelect(
            placeholder="Choose a theme...",
            options=[
                discord.SelectOption(label=label, value=value, emoji=emoji)
                for label, value, emoji in self._THEME_OPTIONS
            ],
            callback=self.on_theme_select,
        )
        self.add_item(self._theme_select)
        self.add_item(
            StatefulButton(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="\N{LEFTWARDS ARROW WITH HOOK}",
                row=1,
                callback=self.go_back,
            )
        )

    def build_embed(self):
        s = _read_user_settings(self.scoped_state)
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
        return (
            StateStore.get_scoped_from(state, "user", user_id=self.user_id)
            .get("settings", {})
            .get("theme")
        )

    def build_ui(self):
        current_theme = _read_user_settings(self.scoped_state)["theme"]
        self._theme_select.set_selected(current_theme)
        return {"embed": self.build_embed()}

    async def on_theme_select(self, interaction, values):
        # auto_defer handles acknowledgment.
        await self.dispatch_scoped_as("SETTINGS_UPDATED", {"theme": values[0]}, scope="user")

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: {"embed": v.build_embed()})


# // ========================================( Notifications )======================================== // #


class NotificationsView(StatefulView):
    """Notification preferences sub-page.

    Features demonstrated:
        - Undo/redo (revert notification toggles)
        - Batched dispatch (multiple toggles in one state update)
        - Toggle buttons that reflect current state
    """

    owner_only = True
    auto_defer = True
    state_scope = "user"
    exit_policy = "disable"
    enable_undo = True
    undo_limit = 10
    subscribed_actions = {"SETTINGS_UPDATED", "UNDO", "REDO"}

    # Declarative toggle list -- mirrors V2NotificationsView's _TOGGLES so
    # both variants of the example read the same way. Each entry is a
    # (setting_key, button_label) pair.
    _TOGGLES = [
        ("notifications_dm", "DMs"),
        ("notifications_mentions", "Mentions"),
        ("notifications_events", "Events"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._build_buttons()

    def _make_toggle(self, key):
        """Factory for a toggle callback bound to ``key``.

        Captures ``key`` in a closure so a single definition can produce
        distinct callbacks per toggle -- otherwise a plain ``for`` loop
        over ``_TOGGLES`` would alias every callback to the last key.
        Mirrors the V2 factories in ``v2_settings.py`` so the three
        settings views read the same way.
        """
        async def toggle(interaction):
            s = _read_user_settings(self.scoped_state)
            # auto_defer handles acknowledgment; on_state_changed handles
            # _build_buttons + message.edit via the SETTINGS_UPDATED subscription.
            await self.dispatch_scoped_as(
                "SETTINGS_UPDATED", {key: not s[key]}, scope="user"
            )
        return toggle

    def _build_buttons(self):
        """Add toggle buttons and undo/redo controls."""
        self.clear_items()

        s = _read_user_settings(self.scoped_state)

        for key, label in self._TOGGLES:
            self.add_item(
                StatefulButton(
                    label=f"{label}: {'ON' if s[key] else 'OFF'}",
                    style=(
                        discord.ButtonStyle.success
                        if s[key]
                        else discord.ButtonStyle.secondary
                    ),
                    callback=self._make_toggle(key),
                )
            )

        # Undo / Redo controls
        self.add_item(
            StatefulButton(
                label="Undo",
                style=discord.ButtonStyle.secondary,
                emoji="\N{LEFTWARDS ARROW WITH HOOK}",
                row=1,
                callback=self.do_undo,
            )
        )
        self.add_item(
            StatefulButton(
                label="Redo",
                style=discord.ButtonStyle.secondary,
                emoji="\N{RIGHTWARDS ARROW WITH HOOK}",
                row=1,
                callback=self.do_redo,
            )
        )
        self.add_item(
            StatefulButton(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="\N{BLACK LEFT-POINTING TRIANGLE}",
                row=1,
                callback=self.go_back,
            )
        )

    def build_embed(self):
        s = _read_user_settings(self.scoped_state)
        theme = get_theme(s["theme"]) or get_theme("default")

        undo_depth = self.undo_depth
        redo_depth = self.redo_depth

        embed = discord.Embed(
            title="\N{BELL} Notification Preferences",
            description=(
                f"DMs: {_bool_icon(s['notifications_dm'])}  "
                f"Mentions: {_bool_icon(s['notifications_mentions'])}  "
                f"Events: {_bool_icon(s['notifications_events'])}\n\n"
                "Toggle each notification type. Use **Undo/Redo** to\n"
                "revert changes -- the state snapshots are per-view."
            ),
        )
        theme.apply_to_embed(embed)
        embed.set_footer(text=f"Undo: {undo_depth} | Redo: {redo_depth}")

        return embed

    async def do_undo(self, interaction):
        # auto_defer handles acknowledgment; undo()/redo() take no arguments.
        await self.undo()

    async def do_redo(self, interaction):
        await self.redo()

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: {"embed": v.build_embed()})

    def state_selector(self, state):
        settings = StateStore.get_scoped_from(state, "user", user_id=self.user_id).get(
            "settings", {}
        )
        return (
            settings.get("notifications_dm"),
            settings.get("notifications_mentions"),
            settings.get("notifications_events"),
        )

    def build_ui(self):
        """Rebuild buttons + embed when state changes (e.g. from undo/redo)."""
        self._build_buttons()
        return {"embed": self.build_embed()}


# // ========================================( Locale )======================================== // #


class LocaleView(StatefulView):
    """Language and timezone selection sub-page.

    Features demonstrated:
        - Multiple select menus on one view
        - Batched dispatch (language + timezone in one update)
        - Push/pop navigation
    """

    owner_only = True
    auto_defer = True
    state_scope = "user"
    exit_policy = "disable"
    subscribed_actions = {"SETTINGS_UPDATED"}

    _LANGUAGES = ["English", "Spanish", "French", "German", "Japanese"]
    _TIMEZONES = [
        ("UTC", "UTC"),
        ("US Eastern", "US/Eastern"),
        ("US Pacific", "US/Pacific"),
        ("Europe/London", "Europe/London"),
        ("Asia/Tokyo", "Asia/Tokyo"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._language_select = StatefulSelect(
            placeholder="Language...",
            options=[discord.SelectOption(label=lang, value=lang) for lang in self._LANGUAGES],
            row=0,
            callback=self.on_language,
        )
        self._timezone_select = StatefulSelect(
            placeholder="Timezone...",
            options=[
                discord.SelectOption(label=label, value=value) for label, value in self._TIMEZONES
            ],
            row=1,
            callback=self.on_timezone,
        )
        self.add_item(self._language_select)
        self.add_item(self._timezone_select)
        self.add_item(
            StatefulButton(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="\N{LEFTWARDS ARROW WITH HOOK}",
                row=2,
                callback=self.go_back,
            )
        )

    def build_embed(self):
        s = _read_user_settings(self.scoped_state)
        theme = get_theme(s["theme"]) or get_theme("default")

        embed = discord.Embed(
            title="\N{GLOBE WITH MERIDIANS} Language & Region",
            description=(
                f"Language: **{s['language']}**\n"
                f"Timezone: **{s['timezone']}**\n\n"
                "Changes are saved to your user-scoped state and\n"
                "update the hub live via dispatch."
            ),
        )
        theme.apply_to_embed(embed)
        return embed

    def state_selector(self, state):
        settings = StateStore.get_scoped_from(state, "user", user_id=self.user_id).get(
            "settings", {}
        )
        return (settings.get("language"), settings.get("timezone"))

    def build_ui(self):
        s = _read_user_settings(self.scoped_state)
        self._language_select.set_selected(s["language"])
        self._timezone_select.set_selected(s["timezone"])
        return {"embed": self.build_embed()}

    async def on_language(self, interaction, values):
        # auto_defer handles acknowledgment.
        await self.dispatch_scoped_as(
            "SETTINGS_UPDATED", {"language": values[0]}, scope="user"
        )

    async def on_timezone(self, interaction, values):
        await self.dispatch_scoped_as(
            "SETTINGS_UPDATED", {"timezone": values[0]}, scope="user"
        )

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: {"embed": v.build_embed()})


# // ========================================( Cog )======================================== // #


class SettingsMenuExample(commands.Cog, name="settings_menu_example"):
    """Advanced settings menu showcasing CascadeUI's core features."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(name="settings", description="Open the server settings panel.")
    async def settings(self, context: Context) -> None:
        """Open an interactive settings panel.

        Only one panel can be open per user per guild. Invoking this
        command again will automatically close the previous panel.
        """
        # instance_policy = "replace" exits the old panel automatically.
        # Flipping it to "reject" routes rejection through on_instance_limit,
        # which sends an ephemeral default message without a try/except.
        view = SettingsHubView(context=context)
        # send() returns None when blocked by session limiting. No-op here
        # under replace policy, but the guard is the canonical pattern.
        if await view.send(embed=view.build_embed()) is None:
            return


async def setup(bot) -> None:
    # UndoMiddleware is required for this cog's undo/redo buttons.
    # Install it from your bot's setup_hook before loading this cog::
    #
    #     from cascadeui import UndoMiddleware, setup_middleware
    #     await setup_middleware(UndoMiddleware())
    #
    # Cogs should not install middleware themselves -- that's the bot
    # author's decision, not the cog's.
    await bot.add_cog(SettingsMenuExample(bot=bot))
