"""
V2 Settings Menu -- CascadeUI V2 Feature Showcase
==================================================

The V2 counterpart of ``settings_menu.py``. A mock server settings panel
demonstrating most of CascadeUI's core features using V2 components:

    - ``MenuLayoutView``  (category-based hub with auto-wired push callbacks)
    - Session limiting    (one settings panel per user per guild)
    - Scoped state        (user-level and user_guild-level preferences)
    - Navigation stack    (hub -> sub-pages with push/pop)
    - Theming             (live theme switching with accent colors)
    - Undo/Redo           (revert notification preference changes)
    - State selectors     (views only re-render when their slice changes)
    - Batched dispatch    (multiple state updates in one notification)
    - ``with_confirmation`` wrapper on the Reset All danger button
    - Cross-view reactivity between V2 settings and V1 settings via shared
      ``SETTINGS_UPDATED`` action
    - V2 helpers          (card, key_value, toggle_section, divider)

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
    MenuLayoutView,
    StatefulButton,
    StatefulLayoutView,
    StatefulSelect,
    StateStore,
    card,
    cascade_reducer,
    divider,
    get_theme,
    key_value,
    toggle_section,
    with_confirmation,
)

logger = logging.getLogger(__name__)


# // ========================================( Reducer )======================================== // #


# An identical reducer exists in settings_menu.py so each cog remains
# copy-pasteable on its own.  Both registrations target the same action
# type; the second load overwrites the first with identical logic.
#
# Dispatching ``SETTINGS_UPDATED`` from either the V1 or V2 cog updates
# both panels live if both are open simultaneously -- they share the same
# scope bucket via ``"user_guild"``.
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

# Per-server, per-user preferences -- isolated to each (user, guild) pair.
GUILD_DEFAULTS = {
    "show_nickname": True,
    "highlight_events": False,
    "compact_mode": False,
}


# // ========================================( Helpers )======================================== // #


def _read_settings(scoped_state):
    """Merge DEFAULT_SETTINGS with a scoped state slot."""
    return {**DEFAULT_SETTINGS, **scoped_state.get("settings", {})}


def _read_guild_settings(scoped_state):
    """Merge GUILD_DEFAULTS with a user_guild scoped state slot."""
    return {**GUILD_DEFAULTS, **scoped_state.get("settings", {})}


def _bool_icon(value):
    """Return a checkmark for True, a cross for False."""
    return "\N{WHITE HEAVY CHECK MARK}" if value else "\N{CROSS MARK}"


# // ========================================( Settings Hub )======================================== // #


class V2SettingsHubView(MenuLayoutView):
    """Main settings dashboard -- the root of the navigation stack.

    ``MenuLayoutView`` handles the category-to-subview push wiring
    automatically. Declaring ``categories=`` in ``__init__`` replaces the
    four manual ``go_*`` callbacks and ``action_section()`` calls that a
    raw ``StatefulLayoutView`` hub would need.

    Features demonstrated:
        - ``MenuLayoutView`` (category hub with auto-wired push)
        - ``_build_header()`` override (live settings summary card)
        - ``_build_footer()`` override (``with_confirmation`` on Reset All)
        - instance_limit=1 with user_guild scope
        - State selector (only re-renders when this user's settings change)
        - Batched dispatch (multiple state updates in one notification)
    """

    owner_only = True
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "disable"
    auto_defer = True
    timeout = 600.0
    # The hub reads multiple scopes but does not write directly -- setting
    # state_scope = None keeps dispatch_scoped() unavailable (intentional;
    # writes go through the SETTINGS_UPDATED reducer for cross-view reactivity).
    state_scope = None
    # Named-action subscription is the cross-view reactivity gate: any
    # sub-page dispatching SETTINGS_UPDATED rebuilds the hub in place,
    # regardless of which scope the sub-page wrote to.
    subscribed_actions = {"SETTINGS_UPDATED"}
    # Exit button is placed manually in _build_footer alongside Reset All.
    auto_exit_button = False

    def __init__(self, *args, **kwargs):
        # Each category dict wires a push callback automatically.
        # ``description`` renders as the action_section text in V2.
        # ``rebuild=lambda v: v.build_ui()`` is the default for V2
        # and does not need to be specified explicitly.
        categories = [
            {
                "label": "Appearance",
                "emoji": "\N{ARTIST PALETTE}",
                "description": "Customize theme and accent colors",
                "view": V2AppearanceView,
            },
            {
                "label": "Notifications",
                "emoji": "\N{BELL}",
                "description": "Configure DM, mention, and event alerts",
                "view": V2NotificationsView,
            },
            {
                "label": "Locale",
                "emoji": "\N{GLOBE WITH MERIDIANS}",
                "description": "Set language and timezone preferences",
                "view": V2LocaleView,
            },
            {
                "label": "Server",
                "emoji": "\N{HOUSE BUILDING}",
                "description": "Per-server display and layout options",
                "view": V2GuildPrefsView,
            },
        ]
        super().__init__(*args, categories=categories, **kwargs)

    # // ----( Menu hooks )---- // #

    def _resolve_accent(self):
        """Resolve the accent color from the user-selected theme."""
        user_s = _read_settings(self.user_scoped_state())
        theme = get_theme(user_s["theme"]) or get_theme("default")
        return theme.get_style("primary_color") or discord.Color.blurple()

    def _build_category_card(self, items):
        """Match the category card accent to the user-selected theme."""
        return card(*items, color=self._resolve_accent())

    def _build_header(self):
        """Summary card showing current values from all sub-pages.

        ``_build_header()`` is called inside ``build_ui()``, which runs
        both at init and on every ``on_state_changed()`` cycle. The
        summary card therefore reflects live state without extra wiring.
        """
        user_s = _read_settings(self.user_scoped_state())
        guild_s = _read_guild_settings(self.user_guild_scoped_state())
        accent = self._resolve_accent()

        return [
            card(
                "## \N{GEAR}\N{VARIATION SELECTOR-16} Server Settings",
                divider(),
                key_value(
                    {
                        "\N{ARTIST PALETTE} Theme": user_s["theme"].title(),
                        "\N{BELL} Notifications": (
                            f"DMs {_bool_icon(user_s['notifications_dm'])}  "
                            f"Mentions {_bool_icon(user_s['notifications_mentions'])}  "
                            f"Events {_bool_icon(user_s['notifications_events'])}"
                        ),
                        "\N{GLOBE WITH MERIDIANS} Locale": (
                            f"{user_s['language']} / {user_s['timezone']}"
                        ),
                        "\N{HOUSE BUILDING} Server": (
                            f"Nickname {_bool_icon(guild_s['show_nickname'])}  "
                            f"Events {_bool_icon(guild_s['highlight_events'])}  "
                            f"Compact {_bool_icon(guild_s['compact_mode'])}"
                        ),
                    }
                ),
                color=accent,
            ),
        ]

    def _build_footer(self):
        """Footer with session note and Reset All + Exit buttons.

        ``with_confirmation`` wraps the Reset All button in an ephemeral
        confirmation prompt. The wrapped callback is only invoked after
        the user confirms.

        The session-limit note is wrapped in a ``card()`` with the same
        theme accent as the header and categories so it reads as a final
        panel of the view rather than floating unstyled below the cards.
        """
        reset_button = StatefulButton(
            label="Reset All",
            style=discord.ButtonStyle.danger,
            emoji="\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}",
            callback=self.reset_all,
        )
        return [
            card(
                TextDisplay("-# Session limited: only one settings panel per user per guild."),
                color=self._resolve_accent(),
            ),
            ActionRow(
                with_confirmation(
                    reset_button,
                    title="Reset all settings?",
                    message="This will restore every preference to its default value.",
                    confirm_label="Reset",
                    cancel_label="Keep",
                    confirmed_message="Settings reset to defaults.",
                    cancelled_message="Reset cancelled.",
                ),
                self.make_exit_button(),
            ),
        ]

    # // ----( State )---- // #

    def state_selector(self, state):
        """Only re-render when this user's scoped settings change.

        Selectors must read from the ``state`` argument, never from the live
        store -- the dispatcher passes the post-reduce snapshot here so the
        equality check sees the same view of the world the subscriber will.
        """
        user_s = StateStore.get_scoped_from(state, "user", user_id=self.user_id).get("settings")
        guild_s = StateStore.get_scoped_from(
            state, "user_guild", user_id=self.user_id, guild_id=self.guild_id
        ).get("settings")
        return (
            tuple(sorted(user_s.items())) if user_s else None,
            tuple(sorted(guild_s.items())) if guild_s else None,
        )

    async def reset_all(self, interaction):
        # ``with_confirmation`` already consumed ``interaction.response``
        # -- no reply is sent here; dispatching is enough.
        # ``self.batch()`` groups all dispatches into a single subscriber
        # notification -- reducers still run for each action, but subscribers
        # are only notified once at the end.
        async with self.batch():
            for key, value in DEFAULT_SETTINGS.items():
                await self.dispatch_scoped_as("SETTINGS_UPDATED", {key: value}, scope="user")
            for key, value in GUILD_DEFAULTS.items():
                await self.dispatch_scoped_as("SETTINGS_UPDATED", {key: value}, scope="user_guild")


# // ========================================( Appearance )======================================== // #


class V2AppearanceView(StatefulLayoutView):
    """Theme selection sub-page.

    Features demonstrated:
        - Push/pop navigation (back button returns to hub)
        - Live theme switching via scoped state dispatch
        - Per-view accent color (card color changes on theme switch)
        - ``default=True`` on ``SelectOption`` for V2 persistent selection
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
        self.build_ui()

    def state_selector(self, state):
        """Only re-render when the theme value changes."""
        return (
            StateStore.get_scoped_from(state, "user", user_id=self.user_id)
            .get("settings", {})
            .get("theme")
        )

    def build_ui(self):
        self.clear_items()

        s = _read_settings(self.scoped_state)
        theme = get_theme(s["theme"]) or get_theme("default")
        accent = theme.get_style("primary_color") or discord.Color.blurple()

        self.add_item(
            card(
                "## \N{ARTIST PALETTE} Appearance",
                TextDisplay(
                    f"Current theme: **{s['theme'].title()}**\n\n"
                    "Select a theme below. The card's accent color\n"
                    "updates instantly via the shared state subscription."
                ),
                color=accent,
            )
        )

        # ``default=True`` on the matching SelectOption preserves the visual
        # selection highlight across V2 immediate-mode rebuilds.
        self.add_item(
            ActionRow(
                StatefulSelect(
                    placeholder="Choose a theme...",
                    options=[
                        discord.SelectOption(
                            label=label,
                            value=value,
                            emoji=emoji,
                            default=(s["theme"] == value),
                        )
                        for label, value, emoji in self._THEME_OPTIONS
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
                    emoji="\N{LEFTWARDS ARROW WITH HOOK}",
                    callback=self.go_back,
                )
            )
        )

    async def on_theme_select(self, interaction, values):
        # auto_defer handles acknowledgment.
        await self.dispatch_scoped_as("SETTINGS_UPDATED", {"theme": values[0]}, scope="user")

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: v.build_ui())


# // ========================================( Notifications )======================================== // #


class V2NotificationsView(StatefulLayoutView):
    """Notification preferences sub-page.

    Features demonstrated:
        - Undo/redo with stack depth display
        - ``toggle_section()`` for green/red toggle buttons
        - State selector narrowed to notification keys only
        - Batched dispatch from Reset All propagation
    """

    owner_only = True
    auto_defer = True
    state_scope = "user"
    exit_policy = "disable"
    enable_undo = True
    undo_limit = 10
    subscribed_actions = {"SETTINGS_UPDATED", "UNDO", "REDO"}

    _TOGGLES = [
        ("notifications_dm", "Direct Messages", "Get notified about new DMs"),
        ("notifications_mentions", "Mentions", "Alerts when you're mentioned"),
        ("notifications_events", "Server Events", "Joins, leaves, and milestones"),
        ("notifications_updates", "Bot Updates", "New features and changelog notices"),
        ("notifications_tips", "Tips & Tricks", "Periodic usage tips and shortcuts"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_ui()

    def state_selector(self, state):
        """Only re-render when notification toggle values change."""
        s = StateStore.get_scoped_from(state, "user", user_id=self.user_id).get("settings", {})
        return tuple(s.get(k) for k, _, _ in self._TOGGLES)

    def build_ui(self):
        self.clear_items()

        s = _read_settings(self.scoped_state)

        undo_depth = self.undo_depth
        redo_depth = self.redo_depth

        # Build toggle sections from the config list
        toggles = []
        for key, label, description in self._TOGGLES:
            toggles.append(
                toggle_section(
                    f"**{label}**\n{description}",
                    active=s[key],
                    callback=self._make_toggle(key),
                )
            )

        self.add_item(
            card(
                "## \N{BELL} Notifications",
                TextDisplay(
                    "Toggle each notification type below.\n" "Use **Undo/Redo** to revert changes."
                ),
                divider(),
                *toggles,
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
                    emoji="\N{LEFTWARDS ARROW WITH HOOK}",
                    callback=self.do_undo,
                ),
                StatefulButton(
                    label="Redo",
                    style=discord.ButtonStyle.secondary,
                    emoji="\N{RIGHTWARDS ARROW WITH HOOK}",
                    callback=self.do_redo,
                ),
                StatefulButton(
                    label="Back",
                    style=discord.ButtonStyle.secondary,
                    emoji="\N{BLACK LEFT-POINTING TRIANGLE}",
                    callback=self.go_back,
                ),
            )
        )

    def _make_toggle(self, key):
        """Factory for a toggle callback bound to ``key``.

        Captures ``key`` in a closure so a single definition can produce
        distinct callbacks per toggle -- otherwise a plain ``for`` loop
        over ``_TOGGLES`` would alias every callback to the last key.
        Mirrors ``V2GuildPrefsView._make_toggle`` (user_guild scope) and
        ``NotificationsView._make_toggle`` in ``settings_menu.py`` (V1).
        """

        async def callback(interaction):
            s = _read_settings(self.scoped_state)
            await self.dispatch_scoped_as("SETTINGS_UPDATED", {key: not s[key]}, scope="user")

        return callback

    async def do_undo(self, interaction):
        # auto_defer handles acknowledgment; undo()/redo() do not consume
        # the interaction.
        await self.undo()

    async def do_redo(self, interaction):
        await self.redo()

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: v.build_ui())


# // ========================================( Locale )======================================== // #


class V2LocaleView(StatefulLayoutView):
    """Language and timezone selection sub-page.

    Features demonstrated:
        - Multiple select menus on one view
        - ``default=True`` for persistent V2 selection highlight
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
        self.build_ui()

    def state_selector(self, state):
        """Only re-render when language or timezone changes."""
        s = StateStore.get_scoped_from(state, "user", user_id=self.user_id).get("settings", {})
        return (s.get("language"), s.get("timezone"))

    def build_ui(self):
        self.clear_items()

        s = _read_settings(self.scoped_state)

        self.add_item(
            card(
                "## \N{GLOBE WITH MERIDIANS} Language & Region",
                key_value(
                    {
                        "Language": s["language"],
                        "Timezone": s["timezone"],
                    }
                ),
                color=discord.Color.teal(),
            )
        )

        self.add_item(
            ActionRow(
                StatefulSelect(
                    placeholder="Language...",
                    options=[
                        discord.SelectOption(
                            label=lang, value=lang, default=(lang == s["language"])
                        )
                        for lang in self._LANGUAGES
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
                        discord.SelectOption(
                            label=label, value=value, default=(value == s["timezone"])
                        )
                        for label, value in self._TIMEZONES
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
                    emoji="\N{LEFTWARDS ARROW WITH HOOK}",
                    callback=self.go_back,
                )
            )
        )

    async def on_language(self, interaction, values):
        # auto_defer handles acknowledgment.
        await self.dispatch_scoped_as("SETTINGS_UPDATED", {"language": values[0]}, scope="user")

    async def on_timezone(self, interaction, values):
        await self.dispatch_scoped_as("SETTINGS_UPDATED", {"timezone": values[0]}, scope="user")

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: v.build_ui())


# // ========================================( Server Preferences )======================================== // #


class V2GuildPrefsView(StatefulLayoutView):
    """Per-server display preferences isolated to each (user, guild) pair.

    Features demonstrated:
        - ``state_scope = "user_guild"`` -- flipping a toggle in Server A
          leaves the same user's preferences in Server B untouched
        - ``toggle_section()`` for green/red toggle buttons
        - ``user_guild`` scope key format in the SETTINGS_UPDATED payload
    """

    owner_only = True
    auto_defer = True
    state_scope = "user_guild"
    exit_policy = "disable"
    subscribed_actions = {"SETTINGS_UPDATED"}

    _TOGGLES = [
        ("show_nickname", "Show Nickname", "Display your guild nickname in leaderboards"),
        ("highlight_events", "Highlight Events", "Visual emphasis on event notifications"),
        ("compact_mode", "Compact Mode", "Reduce spacing and use smaller cards"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_ui()

    def state_selector(self, state):
        """Only re-render when guild-scoped toggle values change."""
        s = StateStore.get_scoped_from(
            state, "user_guild", user_id=self.user_id, guild_id=self.guild_id
        ).get("settings", {})
        return tuple(s.get(k) for k, _, _ in self._TOGGLES)

    def build_ui(self):
        self.clear_items()

        s = _read_guild_settings(self.scoped_state)

        toggles = []
        for key, label, description in self._TOGGLES:
            toggles.append(
                toggle_section(
                    f"**{label}**\n{description}",
                    active=s[key],
                    callback=self._make_toggle(key),
                )
            )

        self.add_item(
            card(
                "## \N{HOUSE BUILDING} Server Preferences",
                TextDisplay(
                    "These preferences are scoped to this server.\n"
                    "Changing them here leaves other servers untouched."
                ),
                divider(),
                *toggles,
                color=discord.Color.dark_teal(),
            )
        )

        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Back",
                    style=discord.ButtonStyle.secondary,
                    emoji="\N{LEFTWARDS ARROW WITH HOOK}",
                    callback=self.go_back,
                )
            )
        )

    def _make_toggle(self, key):
        """Factory for a guild-scoped toggle callback bound to ``key``.

        Captures ``key`` in a closure so a single definition can produce
        distinct callbacks per toggle -- otherwise a plain ``for`` loop
        over ``_TOGGLES`` would alias every callback to the last key.
        Mirrors ``V2NotificationsView._make_toggle`` (user scope) -- the
        only differences are the scope and the settings-reader helper.
        """

        async def callback(interaction):
            s = _read_guild_settings(self.scoped_state)
            await self.dispatch_scoped_as("SETTINGS_UPDATED", {key: not s[key]}, scope="user_guild")

        return callback

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: v.build_ui())


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

        view = V2SettingsHubView(context=context)
        await view.send()


async def setup(bot) -> None:
    # UndoMiddleware is required for this cog's undo/redo buttons.
    # Install it from your bot's setup_hook before loading this cog::
    #
    #     from cascadeui import UndoMiddleware, setup_middleware
    #     await setup_middleware(UndoMiddleware())
    #
    # Cogs should not install middleware themselves -- that's the bot
    # author's decision, not the cog's.
    await bot.add_cog(V2SettingsMenuExample(bot=bot))
