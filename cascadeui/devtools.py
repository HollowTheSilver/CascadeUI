
# // ========================================( Modules )======================================== // #


import json
from datetime import datetime
from typing import List, Optional

import discord
from discord.ext import commands

from .state.singleton import get_store


# // ========================================( Inspector )======================================== // #


class StateInspector:
    """Generates inspection pages for the current state store.

    Produces a list of discord.Embed pages showing state tree overview,
    active views, sessions, action history, and store configuration.
    """

    def __init__(self, store=None):
        self.store = store or get_store()

    def build_pages(self) -> List[discord.Embed]:
        """Build all inspection pages."""
        pages = [
            self._overview_page(),
            self._views_page(),
            self._sessions_page(),
            self._history_page(),
            self._config_page(),
        ]
        # Add page numbers
        for i, page in enumerate(pages):
            page.set_footer(text=f"Page {i + 1}/{len(pages)}")
        return pages

    def _overview_page(self) -> discord.Embed:
        """State tree overview with top-level key counts."""
        state = self.store.state
        embed = discord.Embed(
            title="State Inspector",
            description="Current state tree overview",
            color=discord.Color.blurple(),
            timestamp=datetime.now(),
        )

        embed.add_field(
            name="Views",
            value=f"`{len(state.get('views', {}))}` active",
            inline=True,
        )
        embed.add_field(
            name="Sessions",
            value=f"`{len(state.get('sessions', {}))}` active",
            inline=True,
        )
        embed.add_field(
            name="Components",
            value=f"`{len(state.get('components', {}))}` tracked",
            inline=True,
        )

        app_state = state.get("application", {})
        app_keys = list(app_state.keys()) if app_state else ["(empty)"]
        embed.add_field(
            name="Application Keys",
            value="`" + "`, `".join(app_keys[:10]) + "`",
            inline=False,
        )

        modals = state.get("modals", {})
        if modals:
            embed.add_field(
                name="Modals",
                value=f"`{len(modals)}` with submissions",
                inline=True,
            )

        # State size estimate
        state_json = json.dumps(state, default=str)
        size_kb = len(state_json.encode()) / 1024
        embed.add_field(
            name="State Size",
            value=f"`{size_kb:.1f} KB`",
            inline=True,
        )

        return embed

    def _views_page(self) -> discord.Embed:
        """Active views listing."""
        views = self.store.state.get("views", {})
        embed = discord.Embed(
            title="Active Views",
            description=f"{len(views)} view(s) registered",
            color=discord.Color.green(),
        )

        if not views:
            embed.description = "No active views"
            return embed

        for view_id, view_data in list(views.items())[:10]:
            short_id = view_id[:8] + "..."
            view_type = view_data.get("type", "Unknown")
            msg_id = view_data.get("message_id", "N/A")
            channel_id = view_data.get("channel_id", "N/A")
            user_id = view_data.get("user_id", "N/A")

            embed.add_field(
                name=f"{view_type} ({short_id})",
                value=(
                    f"Message: `{msg_id}`\n"
                    f"Channel: `{channel_id}`\n"
                    f"User: `{user_id}`"
                ),
                inline=True,
            )

        if len(views) > 10:
            embed.description += f" (showing first 10 of {len(views)})"

        return embed

    def _sessions_page(self) -> discord.Embed:
        """Active sessions listing."""
        sessions = self.store.state.get("sessions", {})
        embed = discord.Embed(
            title="Active Sessions",
            description=f"{len(sessions)} session(s)",
            color=discord.Color.gold(),
        )

        if not sessions:
            embed.description = "No active sessions"
            return embed

        for session_id, session_data in list(sessions.items())[:10]:
            user_id = session_data.get("user_id", "N/A")
            view_count = len(session_data.get("views", []))
            history_count = len(session_data.get("history", []))
            created = session_data.get("created_at", "N/A")

            # Truncate timestamp for readability
            if created != "N/A" and len(created) > 19:
                created = created[:19]

            embed.add_field(
                name=f"User {user_id}",
                value=(
                    f"Views: `{view_count}`\n"
                    f"Nav History: `{history_count}`\n"
                    f"Created: `{created}`"
                ),
                inline=True,
            )

        return embed

    def _history_page(self) -> discord.Embed:
        """Recent action history."""
        history = self.store.history
        embed = discord.Embed(
            title="Action History",
            description=f"{len(history)} action(s) in buffer (limit: {self.store.history_limit})",
            color=discord.Color.orange(),
        )

        if not history:
            embed.description = "No actions dispatched yet"
            return embed

        # Show last 15 actions in reverse order
        recent = list(reversed(history[-15:]))
        lines = []
        for action in recent:
            timestamp = action.get("timestamp", "")
            if len(timestamp) > 19:
                timestamp = timestamp[11:19]  # Just HH:MM:SS
            action_type = action["type"]
            source = action.get("source", "N/A")
            if source and len(source) > 8:
                source = source[:8] + "..."
            lines.append(f"`{timestamp}` **{action_type}** from `{source}`")

        embed.description += "\n\n" + "\n".join(lines)

        if len(history) > 15:
            embed.description += f"\n\n*...and {len(history) - 15} older actions*"

        return embed

    def _config_page(self) -> discord.Embed:
        """Store configuration and internals."""
        embed = discord.Embed(
            title="Store Configuration",
            color=discord.Color.purple(),
        )

        # Reducers
        core = list(self.store._core_reducers.keys()) if self.store._core_reducers else []
        custom = list(self.store._custom_reducers.keys())
        embed.add_field(
            name=f"Core Reducers ({len(core)})",
            value="`" + "`, `".join(core) + "`" if core else "Not loaded yet",
            inline=False,
        )
        embed.add_field(
            name=f"Custom Reducers ({len(custom)})",
            value="`" + "`, `".join(custom) + "`" if custom else "None",
            inline=False,
        )

        # Subscribers
        sub_count = len(self.store.subscribers)
        embed.add_field(
            name=f"Subscribers ({sub_count})",
            value=", ".join(
                f"`{sid[:8]}...`" for sid in list(self.store.subscribers.keys())[:10]
            ) or "None",
            inline=False,
        )

        # Middleware
        mw_count = len(self.store._middleware)
        embed.add_field(
            name=f"Middleware ({mw_count})",
            value=", ".join(
                f"`{mw.__class__.__name__ if hasattr(mw, '__class__') and mw.__class__.__name__ != 'function' else mw.__name__ if hasattr(mw, '__name__') else 'anonymous'}`"
                for mw in self.store._middleware
            ) or "None",
            inline=False,
        )

        # Persistence
        if self.store.persistence_enabled:
            backend = self.store.persistence_backend
            backend_name = backend.__class__.__name__ if backend else "None"
            embed.add_field(
                name="Persistence",
                value=f"Enabled ({backend_name})",
                inline=True,
            )
        else:
            embed.add_field(
                name="Persistence",
                value="Disabled",
                inline=True,
            )

        return embed


# // ========================================( View )======================================== // #


class InspectorView(discord.ui.View):
    """Paginated view for browsing inspector pages.

    Uses plain discord.ui.View (not StatefulView) to avoid polluting
    the state store with inspector metadata.
    """

    def __init__(self, pages: List[discord.Embed], timeout: float = 120):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.current_page = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.pages) - 1

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(len(self.pages) - 1, self.current_page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        inspector = StateInspector()
        self.pages = inspector.build_pages()
        self.current_page = min(self.current_page, len(self.pages) - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)


# // ========================================( Cog )======================================== // #


class DevToolsCog(commands.Cog, name="cascadeui_devtools"):
    """Optional cog that adds a state inspection command.

    Usage:
        from cascadeui.devtools import DevToolsCog
        await bot.add_cog(DevToolsCog(bot))
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="inspect",
        description="Inspect the CascadeUI state store."
    )
    @commands.is_owner()
    async def inspect(self, ctx: commands.Context):
        inspector = StateInspector()
        pages = inspector.build_pages()
        view = InspectorView(pages)
        await ctx.send(embed=pages[0], view=view)
