"""
V1 Navigation Stack -- Push/Pop with Session Data
==================================================

A three-level menu demonstrating how ``push()`` and ``pop()`` carry an
embed-based view hierarchy through Discord's component system, and how
``shared_data`` shares ephemeral state across the entire navigation chain.

Patterns demonstrated:
    - ``push(view_class, interaction, *, rebuild=...)`` for V1 embed transitions
    - ``pop(interaction, *, rebuild=...)`` for unwinding the stack
    - Deep nesting (Main -> Settings -> Nested) on a single message
    - ``update_session()`` to write ephemeral metadata shared across views
    - ``shared_data`` property to read that metadata from any view in the chain
    - ``subscribed_actions = {"SESSION_UPDATED"}`` for cross-view reactivity
    - ``state_selector`` narrowed to session data changes

All three views share a ``session_id`` (inherited automatically through
push/pop), so ``update_session(dark_mode=True)`` in SettingsView is
immediately readable as ``self.shared_data["dark_mode"]`` in NestedView.
Session data is ephemeral -- it lives for the duration of the navigation
session and is not persisted across restarts.

Commands:
    /navtest   Open the three-level navigation stack

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import discord
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import StatefulButton, StatefulView

# // ========================================( Views )======================================== // #


class MainMenuView(StatefulView):
    """Root navigation view -- the entry point of the nav stack.

    The hub itself does not subscribe to any actions because its content
    is static. Sub-views pushed from here inherit the same ``session_id``
    and can read/write shared session data.
    """

    # Access, instance, and lifecycle policies for the hub.
    owner_only = True
    instance_limit = 1
    instance_scope = "user"
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "disable"
    auto_defer = True
    # No Redux state -- cross-view preferences live in session data,
    # which is ephemeral and shared across the push/pop chain.
    state_scope = None
    timeout = 300.0
    # Manual back buttons on sub-views; the hub has no parent to pop to.
    auto_back_button = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(
            StatefulButton(
                label="Settings",
                style=discord.ButtonStyle.primary,
                emoji="\N{GEAR}",
                callback=self.go_settings,
            )
        )
        self.add_item(
            StatefulButton(
                label="About",
                style=discord.ButtonStyle.secondary,
                emoji="\N{INFORMATION SOURCE}",
                callback=self.go_about,
            )
        )
        self.add_exit_button()

    def build_embed(self):
        return discord.Embed(
            title="Main Menu",
            description="Choose a destination. The navigation stack tracks where you've been.",
            color=discord.Color.green(),
        )

    async def go_settings(self, interaction):
        await self.push(SettingsView, interaction, rebuild=lambda v: {"embed": v.build_embed()})

    async def go_about(self, interaction):
        await self.push(AboutView, interaction, rebuild=lambda v: {"embed": v.build_embed()})


class SettingsView(StatefulView):
    """Settings view with a dark mode toggle stored in session data.

    ``update_session(dark_mode=...)`` writes the preference into the
    session's shared ``data`` dict. Every view in the push/pop chain
    reads it via ``self.shared_data`` -- no constructor kwargs, no
    Redux state, no scoped state. The preference is ephemeral: it
    lasts for this navigation session and disappears on timeout or exit.

    Subscribing to ``SESSION_UPDATED`` triggers ``on_state_changed()``
    after each toggle, which calls ``build_ui()`` and ``refresh()``.
    The ``build_ui()`` dict-return pattern lets the default
    ``on_state_changed()`` splat the embed into ``refresh()``
    automatically -- no manual override needed.
    """

    owner_only = True
    auto_defer = True
    # No Redux state: this view uses session data, not scoped state.
    state_scope = None
    # Manual back button; auto_back_button doesn't call rebuild.
    auto_back_button = False
    # React to session data changes from update_session().
    subscribed_actions = {"SESSION_UPDATED"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(
            StatefulButton(
                label="Toggle Dark Mode",
                style=discord.ButtonStyle.primary,
                callback=self.toggle_dark,
            )
        )
        self.add_item(
            StatefulButton(
                label="Go Deeper",
                style=discord.ButtonStyle.secondary,
                emoji="\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE}",
                callback=self.go_nested,
            )
        )
        self.add_item(
            StatefulButton(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="\N{BLACK LEFT-POINTING TRIANGLE}",
                row=4,
                callback=self.go_back,
            )
        )

    def state_selector(self, state):
        """Only rebuild when dark_mode changes in the session data."""
        session = state.get("sessions", {}).get(self.session_id, {})
        return session.get("shared_data", {}).get("dark_mode")

    def build_embed(self):
        dark = self.shared_data.get("dark_mode", False)
        status = "ON" if dark else "OFF"
        return discord.Embed(
            title="Settings",
            description=(
                f"Dark Mode: **{status}**\n\n"
                "Toggle the preference, then push deeper -- the nested\n"
                "view reads the same session data without any kwargs."
            ),
            color=discord.Color.dark_theme() if dark else discord.Color.blue(),
        )

    def build_ui(self):
        """V1 dict-return: splatted into ``refresh()`` by the default
        ``on_state_changed()`` implementation."""
        return {"embed": self.build_embed()}

    async def toggle_dark(self, interaction):
        dark = self.shared_data.get("dark_mode", False)
        # update_session() dispatches SESSION_UPDATED, which triggers
        # on_state_changed() -> build_ui() -> refresh() automatically.
        await self.update_session(dark_mode=not dark)

    async def go_nested(self, interaction):
        await self.push(NestedView, interaction, rebuild=lambda v: {"embed": v.build_embed()})

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: {"embed": v.build_embed()})


class NestedView(StatefulView):
    """A deeply nested view that reads session data from the parent.

    This view never calls ``update_session()`` itself -- it only reads.
    The dark mode preference set in ``SettingsView`` is visible here
    via ``self.shared_data`` because both views share the same
    ``session_id`` (inherited through push).
    """

    owner_only = True
    auto_defer = True
    state_scope = None
    auto_back_button = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(
            StatefulButton(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="\N{BLACK LEFT-POINTING TRIANGLE}",
                row=4,
                callback=self.go_back,
            )
        )

    def build_embed(self):
        # Read session data written by SettingsView. No kwargs needed,
        # no Redux state -- just self.shared_data on the shared session.
        dark = self.shared_data.get("dark_mode", False)
        mode_label = "dark" if dark else "light"
        return discord.Embed(
            title="Nested View",
            description=(
                "Two levels deep in the navigation stack.\n\n"
                f"Dark mode is **{mode_label}** (read from session data).\n"
                "Hit **Back** to unwind."
            ),
            color=discord.Color.dark_theme() if dark else discord.Color.orange(),
        )

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: {"embed": v.build_embed()})


class AboutView(StatefulView):
    """Simple about page pushed from the main menu."""

    owner_only = True
    auto_defer = True
    state_scope = None
    auto_back_button = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(
            StatefulButton(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="\N{BLACK LEFT-POINTING TRIANGLE}",
                row=4,
                callback=self.go_back,
            )
        )

    def build_embed(self):
        return discord.Embed(
            title="About",
            description=(
                "**CascadeUI Navigation Demo**\n\n"
                "This view was pushed onto the navigation stack.\n"
                "Hit Back to pop it off and return to the main menu."
            ),
            color=discord.Color.greyple(),
        )

    async def go_back(self, interaction):
        await self.pop(interaction, rebuild=lambda v: {"embed": v.build_embed()})


# // ========================================( Cog )======================================== // #


class NavigationExample(commands.Cog, name="navigation_example"):
    """Navigation stack demo with push/pop and session data sharing."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(name="navtest", description="Navigation stack demo with push/pop.")
    async def navtest(self, context: Context) -> None:
        view = MainMenuView(context=context)
        await view.send(embed=view.build_embed())


async def setup(bot) -> None:
    await bot.add_cog(NavigationExample(bot=bot))
