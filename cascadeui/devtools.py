# // ========================================( Modules )======================================== // #


import json
import logging

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from .components.base import StatefulButton
from .components.v2_patterns import action_section, alert, card, divider, gap, key_value
from .state.singleton import get_store
from .views.base import SessionLimitError
from .views.layout_patterns import TabLayoutView

logger = logging.getLogger(__name__)


# // ========================================( Inspector )======================================== // #


class InspectorView(TabLayoutView):
    """V2 state inspector for browsing the CascadeUI state store.

    Uses TabLayoutView to dogfood the library's own V2 component system.
    Self-filters its own view and session from displayed data to avoid
    observer-effect noise in the inspection output.

    Tabs:
        Overview  — State tree summary with key counts and size
        Views     — Active view instances and registry stats
        Sessions  — Active sessions with nav stack and data info
        History   — Recent action dispatch log
        Config    — Reducers, middleware, hooks, persistence status
    """

    session_limit = 1
    session_scope = "user_guild"
    session_policy = "replace"
    subscribed_actions = {"VIEW_CREATED", "VIEW_DESTROYED"}

    def state_selector(self, state):
        """Track filtered view/session counts to detect external changes."""
        views = {k for k in state.get("views", {}) if k != self.id}
        sessions = {k for k in state.get("sessions", {}) if k != self.session_id}
        return (len(views), len(sessions))

    def __init__(self, *args, **kwargs):
        tabs = {
            "\U0001f4ca Overview": self.build_overview,
            "\U0001f441\ufe0f Views": self.build_views,
            "\U0001f4c2 Sessions": self.build_sessions,
            "\U0001f4dc History": self.build_history,
            "\u2699\ufe0f Config": self.build_config,
        }
        super().__init__(*args, tabs=tabs, **kwargs)

    # // ==================( Filtering )================== // #

    def _filtered_views(self):
        """Return state views excluding the inspector's own entry."""
        views = self.state_store.state.get("views", {})
        return {k: v for k, v in views.items() if k != self.id}

    def _filtered_sessions(self):
        """Return state sessions excluding the inspector's own session."""
        sessions = self.state_store.state.get("sessions", {})
        return {k: v for k, v in sessions.items() if k != self.session_id}

    def _filtered_history(self):
        """Return action history excluding the inspector's own dispatches."""
        return [a for a in self.state_store.history if a.get("source") != self.id]

    def _filtered_active_views(self):
        """Return active view instances excluding the inspector itself."""
        return {k: v for k, v in self.state_store._active_views.items() if k != self.id}

    # // ==================( Helpers )================== // #

    def _exit_row(self):
        """Build an exit button ActionRow for the bottom of each tab."""
        btn = StatefulButton(
            label="Close",
            style=discord.ButtonStyle.secondary,
            emoji="\u274c",
            callback=self._close,
        )
        return ActionRow(btn)

    async def _close(self, interaction):
        await interaction.response.defer()
        await self.exit()

    async def _refresh(self, interaction):
        await interaction.response.defer()
        await self._refresh_tabs()

    def _truncate(self, items, max_len=200):
        """Join items as comma-separated string, truncating if too long."""
        if not items:
            return "None"
        text = ", ".join(str(i) for i in items)
        if len(text) <= max_len:
            return text
        # Find the last complete item that fits
        truncated = text[:max_len].rsplit(", ", 1)[0]
        shown = truncated.count(",") + 1
        remaining = len(items) - shown
        return f"{truncated}, ... +{remaining} more"

    def _format_timestamp(self, timestamp):
        """Extract HH:MM:SS from an ISO timestamp string."""
        if not timestamp or timestamp == "N/A":
            return "N/A"
        if len(timestamp) > 19:
            return timestamp[11:19]
        if len(timestamp) > 11:
            return timestamp[11:]
        return timestamp

    # // ==================( Overview Tab )================== // #

    async def build_overview(self):
        """State tree summary with key counts and estimated size."""
        state = self.state_store.state
        views = self._filtered_views()
        sessions = self._filtered_sessions()
        components = state.get("components", {})
        modals = state.get("modals", {})

        stats = {
            "Views": len(views),
            "Sessions": len(sessions),
            "Components": len(components),
        }
        if modals:
            stats["Modals"] = len(modals)

        overview = card(
            "## State Inspector",
            action_section(
                "Snapshot of the CascadeUI state store",
                label="Refresh",
                callback=self._refresh,
                emoji="\U0001f504",
            ),
            divider(),
            key_value(stats),
            color=discord.Color.blurple(),
        )

        # Application state summary
        app_state = state.get("application", {})
        app_keys = list(app_state.keys()) if app_state else []
        history = self._filtered_history()

        state_json = json.dumps(state, default=str)
        size_kb = len(state_json.encode()) / 1024

        app_info = {
            "App Keys": self._truncate(app_keys) if app_keys else "(empty)",
            "State Size": f"{size_kb:.1f} KB",
            "History Buffer": f"{len(history)}/{self.state_store.history_limit}",
        }

        app_card = card(
            "## Application State",
            key_value(app_info),
            color=discord.Color.dark_grey(),
        )

        return [overview, gap(), app_card, self._exit_row()]

    # // ==================( Views Tab )================== // #

    async def build_views(self):
        """Active view instances and registry statistics."""
        views = self._filtered_views()
        active = self._filtered_active_views()

        if not views:
            empty = alert("No active views", level="info")
            return [empty, self._exit_row()]

        # Build markdown block for view entries
        lines = []
        shown = list(views.items())[:8]
        for view_id, view_data in shown:
            short_id = view_id[:8] + "..."
            view_type = view_data.get("type", "Unknown")
            user_id = view_data.get("user_id", "N/A")
            channel_id = view_data.get("channel_id", "N/A")
            msg_id = view_data.get("message_id", "N/A")
            lines.append(f"**{view_type}** (`{short_id}`)")
            lines.append(f"-# User: {user_id} | Channel: {channel_id} | Msg: {msg_id}")

        if len(views) > 8:
            lines.append(f"\n-# ...showing first 8 of {len(views)}")

        views_card = card(
            "## Active Views",
            TextDisplay(f"{len(views)} view(s) registered"),
            divider(),
            TextDisplay("\n".join(lines)),
            color=discord.Color.green(),
        )

        # Registry stats
        sub_count = len(self.state_store.subscribers) - 1  # Exclude self
        session_index = self.state_store._session_index
        registry = card(
            "## View Registry",
            key_value(
                {
                    "Active Instances": len(active),
                    "Session Index Entries": len(session_index),
                    "Subscribers": sub_count,
                }
            ),
            color=discord.Color.dark_grey(),
        )

        return [views_card, gap(), registry, self._exit_row()]

    # // ==================( Sessions Tab )================== // #

    async def build_sessions(self):
        """Active sessions with view counts and nav stack info."""
        sessions = self._filtered_sessions()

        if not sessions:
            empty = alert("No active sessions", level="info")
            return [empty, self._exit_row()]

        lines = []
        shown = list(sessions.items())[:6]
        for session_id, session_data in shown:
            view_count = len(session_data.get("views", []))
            nav_depth = len(session_data.get("nav_stack", []))
            created = self._format_timestamp(session_data.get("created_at", "N/A"))
            data_keys = list(session_data.get("data", {}).keys())

            lines.append(f"**{session_id}**")
            detail = f"-# Views: {view_count} | Nav Stack: {nav_depth} | Created: {created}"
            lines.append(detail)
            if data_keys:
                lines.append(f"-# Data Keys: {', '.join(data_keys[:5])}")
            lines.append("")  # Blank line between entries

        if len(sessions) > 6:
            lines.append(f"-# ...showing first 6 of {len(sessions)}")

        sessions_card = card(
            "## Active Sessions",
            TextDisplay(f"{len(sessions)} session(s)"),
            divider(),
            TextDisplay("\n".join(lines).rstrip()),
            color=discord.Color.gold(),
        )

        return [sessions_card, self._exit_row()]

    # // ==================( History Tab )================== // #

    async def build_history(self):
        """Recent action dispatch log."""
        history = self._filtered_history()

        if not history:
            empty = alert("No actions dispatched yet", level="info")
            return [empty, self._exit_row()]

        recent = list(reversed(history[-20:]))
        lines = []
        for action in recent:
            timestamp = self._format_timestamp(action.get("timestamp", ""))
            action_type = action["type"]
            source = action.get("source", "N/A")
            if source and len(source) > 8:
                source = source[:8] + "..."
            lines.append(f"`{timestamp}` **{action_type}** from `{source}`")

        if len(history) > 20:
            lines.append(f"\n-# ...showing last 20 of {len(history)}")

        history_card = card(
            "## Action History",
            action_section(
                f"{len(history)} action(s) in buffer (limit: {self.state_store.history_limit})",
                label="Refresh",
                callback=self._refresh,
                emoji="\U0001f504",
            ),
            divider(),
            TextDisplay("\n".join(lines)),
            color=discord.Color.orange(),
        )

        return [history_card, self._exit_row()]

    # // ==================( Config Tab )================== // #

    async def build_config(self):
        """Reducers, middleware, hooks, and persistence status."""
        store = self.state_store

        # Reducers
        core = list(store._core_reducers.keys()) if store._core_reducers else []
        custom = list(store._custom_reducers.keys())

        reducers_card = card(
            "## Reducers",
            key_value(
                {
                    f"Core ({len(core)})": self._truncate(core),
                    f"Custom ({len(custom)})": self._truncate(custom),
                }
            ),
            color=discord.Color.purple(),
        )

        # Middleware and hooks
        mw_names = []
        for mw in store._middleware:
            if hasattr(mw, "__class__") and mw.__class__.__name__ != "function":
                mw_names.append(mw.__class__.__name__)
            elif hasattr(mw, "__name__"):
                mw_names.append(mw.__name__)
            else:
                mw_names.append("anonymous")

        hook_count = sum(len(cbs) for cbs in store._hooks.values())
        computed_count = len(store._computed)

        middleware_card = card(
            "## Middleware & Hooks",
            key_value(
                {
                    "Middleware": self._truncate(mw_names),
                    "Hooks": f"{hook_count} registered" if hook_count else "None",
                    "Computed Values": f"{computed_count} registered" if computed_count else "None",
                }
            ),
            color=discord.Color.dark_grey(),
        )

        # Persistence
        if store.persistence_enabled:
            backend = store.persistence_backend
            backend_name = backend.__class__.__name__ if backend else "None"
            persistent_views = store.state.get("persistent_views", {})
            persist_info = {
                "Status": f"Enabled ({backend_name})",
                "Persistent Views": f"{len(persistent_views)} registered",
            }
            persist_color = discord.Color.green()
        else:
            persist_info = {"Status": "Disabled"}
            persist_color = discord.Color.red()

        persistence_card = card(
            "## Persistence",
            key_value(persist_info),
            color=persist_color,
        )

        return [reducers_card, gap(), middleware_card, gap(), persistence_card, self._exit_row()]

    async def update_from_state(self, state):
        """Auto-refresh the active tab when external state changes."""
        if self.message:
            await self._refresh_tabs()


# Backwards-compatible alias
StateInspector = InspectorView


# // ========================================( Cog )======================================== // #


class DevToolsCog(commands.Cog, name="cascadeui_devtools"):
    """Optional cog that adds a V2 state inspection command.

    Usage:
        from cascadeui.devtools import DevToolsCog
        await bot.add_cog(DevToolsCog(bot))
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="inspect", description="Inspect the CascadeUI state store.")
    @commands.is_owner()
    async def inspect(self, ctx: Context) -> None:
        """Open the CascadeUI state inspector.

        A tabbed V2 dashboard showing the live state tree, active views,
        sessions, action history, and store configuration.
        """
        try:
            view = InspectorView(context=ctx)
            await view.send()
        except SessionLimitError:
            await ctx.send("Inspector already open.", ephemeral=True)
