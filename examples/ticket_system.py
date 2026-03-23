"""
Support Ticket System — CascadeUI Real-World Example
=====================================================

A guild-scoped support ticket system that demonstrates most of CascadeUI's
framework features working together in a production-style use case:

    - PersistentView      (ticket panel survives bot restarts)
    - Modal + Validation  (ticket creation with field-level error checking)
    - PaginatedView       (paginated ticket list via from_data factory)
    - refresh_data        (live-updating list when tickets change)
    - _build_extra_items  (select menu below nav buttons via subclass hook)
    - clear_row           (row-level component management)
    - Custom Reducers     (TICKET_CREATED, TICKET_CLOSED)
    - State Selectors     (panel re-renders only when open ticket count changes)
    - Session Limiting    (one ticket list per user per guild)
    - Theming             (custom "support" theme applied to all embeds)

Commands:
    /ticket_setup   Post a persistent ticket panel (admin only, once per channel)
    /my_tickets     Show your open tickets in a paginated list

Usage:
    Load this cog and call ``setup_persistence(bot, ...)`` in your setup_hook.
    Then run ``/ticket_setup`` in any channel to post the panel.

    Requires: pip install cascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import copy
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import (
    PaginatedView,
    PersistentView,
    SessionLimitError,
    StatefulButton,
    StatefulSelect,
    StatefulView,
    Modal,
    TextInput,
    Theme,
    cascade_reducer,
    choices,
    get_store,
    get_theme,
    min_length,
    register_theme,
)

import logging

logger = logging.getLogger(__name__)


# // ========================================( Theme )======================================== // #


support_theme = Theme("support", {
    "primary_color": discord.Color.from_rgb(88, 101, 242),
    "header_emoji": "\N{TICKET}",
    "footer_text": "Support Tickets",
})


# // ========================================( Reducers )======================================== // #


@cascade_reducer("TICKET_CREATED")
async def ticket_created_reducer(action, state):
    """Increment the guild counter and append the new ticket."""
    new_state = copy.deepcopy(state)
    app = new_state.setdefault("application", {})

    guild_id = str(action["payload"]["guild_id"])

    # Increment counter
    counters = app.setdefault("ticket_counter", {})
    counters[guild_id] = counters.get(guild_id, 0) + 1
    ticket_num = counters[guild_id]

    # Build ticket record
    ticket = {
        "id": f"T-{ticket_num:04d}",
        "subject": action["payload"]["subject"],
        "description": action["payload"]["description"],
        "priority": action["payload"]["priority"],
        "status": "open",
        "author_id": action["payload"]["author_id"],
        "guild_id": action["payload"]["guild_id"],
        "created_at": action["payload"].get("created_at", datetime.now(timezone.utc).isoformat()),
    }

    tickets = app.setdefault("tickets", {})
    tickets.setdefault(guild_id, []).append(ticket)

    return new_state


@cascade_reducer("TICKET_CLOSED")
async def ticket_closed_reducer(action, state):
    """Set the matching ticket's status to closed."""
    new_state = copy.deepcopy(state)
    app = new_state.get("application", {})

    guild_id = str(action["payload"]["guild_id"])
    ticket_id = action["payload"]["ticket_id"]

    for ticket in app.get("tickets", {}).get(guild_id, []):
        if ticket["id"] == ticket_id:
            ticket["status"] = "closed"
            break

    return new_state


# // ========================================( Helpers )======================================== // #


def _get_guild_tickets(guild_id):
    """Read all tickets for a guild from the store."""
    store = get_store()
    return store.state.get("application", {}).get("tickets", {}).get(str(guild_id), [])


def _get_user_open_tickets(guild_id, user_id):
    """Get open tickets authored by a specific user in a guild."""
    return [
        t for t in _get_guild_tickets(guild_id)
        if t["author_id"] == user_id and t["status"] == "open"
    ]


def _find_ticket(guild_id, ticket_id):
    """Find a specific ticket by ID in a guild."""
    for t in _get_guild_tickets(guild_id):
        if t["id"] == ticket_id:
            return t
    return None


def _count_open_tickets(guild_id):
    """Count open tickets in a guild."""
    return sum(1 for t in _get_guild_tickets(guild_id) if t["status"] == "open")


PRIORITY_EMOJI = {
    "low": "\N{LARGE GREEN CIRCLE}",
    "medium": "\N{LARGE YELLOW CIRCLE}",
    "high": "\N{LARGE RED CIRCLE}",
}

PRIORITY_LABEL = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
}

STATUS_EMOJI = {
    "open": "\N{CLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}",
    "closed": "\N{WHITE HEAVY CHECK MARK}",
}


# // ========================================( Create Ticket Modal )======================================== // #


class CreateTicketModal(Modal):
    """Modal for creating a new support ticket.

    Validates subject length, description length, and priority value
    before dispatching TICKET_CREATED.
    """

    def __init__(self, guild_id, user_id):
        self._guild_id = guild_id
        self._user_id = user_id

        super().__init__(
            title="Create Support Ticket",
            inputs=[
                TextInput(
                    label="Subject",
                    placeholder="Brief summary of your issue",
                    min_length=5,
                    max_length=100,
                    style=discord.TextStyle.short,
                ),
                TextInput(
                    label="Description",
                    placeholder="Describe the issue in detail...",
                    min_length=10,
                    max_length=1000,
                    style=discord.TextStyle.paragraph,
                ),
                TextInput(
                    label="Priority",
                    placeholder="low, medium, or high",
                    max_length=6,
                    style=discord.TextStyle.short,
                ),
            ],
            validators={
                "input_subject": [min_length(5)],
                "input_description": [min_length(10)],
                "input_priority": [choices(["low", "medium", "high"])],
            },
            callback=self._on_submit,
        )

    async def _on_submit(self, interaction, values):
        store = get_store()
        await store.dispatch("TICKET_CREATED", {
            "guild_id": self._guild_id,
            "author_id": self._user_id,
            "subject": values["input_subject"],
            "description": values["input_description"],
            "priority": values["input_priority"].lower(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

        # Find the ticket ID that was just created
        counter = store.state.get("application", {}).get("ticket_counter", {})
        ticket_num = counter.get(str(self._guild_id), 0)

        await interaction.response.send_message(
            f"\N{WHITE HEAVY CHECK MARK} Ticket **T-{ticket_num:04d}** created!", ephemeral=True
        )


# // ========================================( Ticket Detail View )======================================== // #


class TicketDetailView(StatefulView):
    """Detailed view of a single ticket.

    Shows full subject, description, priority, status, and creation time.
    Includes a Close button (if the ticket is still open) and a Dismiss
    button to remove the detail message.

    Subscribes to TICKET_CLOSED so the embed updates live if the ticket
    is closed from another view (e.g. the close flow on the panel).
    """

    subscribed_actions = {"TICKET_CLOSED"}

    def __init__(self, *args, ticket=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._ticket = ticket or {}

        self.add_item(StatefulButton(
            label="Close This Ticket",
            style=discord.ButtonStyle.danger,
            emoji="\N{CROSS MARK}",
            disabled=self._ticket.get("status") != "open",
            callback=self.close_this_ticket,
        ))
        self.add_exit_button(label="Dismiss")

    def _build_embed(self):
        t = self._ticket
        theme = get_theme("support") or get_theme("default")

        priority = t.get("priority", "medium")
        status = t.get("status", "open")
        created = t.get("created_at", "")

        # Parse and format the timestamp
        time_str = ""
        if created:
            try:
                dt = datetime.fromisoformat(created)
                time_str = discord.utils.format_dt(dt, style="f")
            except (ValueError, TypeError):
                time_str = created

        embed = discord.Embed(
            title=f"{STATUS_EMOJI.get(status, '')} {t.get('id', '?')} — {t.get('subject', '?')}",
            description=t.get("description", "No description."),
        )
        embed.add_field(
            name="Priority",
            value=f"{PRIORITY_EMOJI.get(priority, '')} {PRIORITY_LABEL.get(priority, priority)}",
            inline=True,
        )
        embed.add_field(
            name="Status",
            value=status.title(),
            inline=True,
        )
        embed.add_field(
            name="Created",
            value=time_str or "Unknown",
            inline=True,
        )
        theme.apply_to_embed(embed)
        return embed

    def state_selector(self, state):
        """Re-render if this ticket's status changes."""
        guild_id = str(self._ticket.get("guild_id", ""))
        ticket_id = self._ticket.get("id")
        for t in state.get("application", {}).get("tickets", {}).get(guild_id, []):
            if t["id"] == ticket_id:
                return t["status"]
        return None

    async def update_from_state(self, state):
        """Refresh the embed when the ticket is closed externally."""
        fresh = _find_ticket(
            self._ticket.get("guild_id"),
            self._ticket.get("id"),
        )
        if fresh:
            self._ticket = fresh

        # Disable the close button if the ticket is now closed
        for item in self.children:
            if isinstance(item, StatefulButton) and item.label == "Close This Ticket":
                item.disabled = self._ticket.get("status") != "open"

        if self.message:
            try:
                await self.message.edit(embed=self._build_embed(), view=self)
            except discord.HTTPException:
                pass

    async def close_this_ticket(self, interaction):
        if self._ticket.get("status") != "open":
            await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
            return

        await interaction.response.defer()
        store = get_store()
        await store.dispatch("TICKET_CLOSED", {
            "guild_id": self._ticket["guild_id"],
            "ticket_id": self._ticket["id"],
        })
        # update_from_state will handle the UI refresh via the subscriber


# // ========================================( Close Ticket View )======================================== // #


class CloseTicketView(StatefulView):
    """Ephemeral view for selecting and closing one of the user's open tickets.

    Shows a select menu populated with the user's open tickets, and a
    confirm button to close the selected one.
    """

    def __init__(self, *args, tickets=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._tickets = tickets or []
        self._selected_id = None

        options = [
            discord.SelectOption(
                label=f"{t['id']} — {t['subject'][:40]}",
                value=t["id"],
                description=f"Priority: {t['priority'].title()}",
                emoji=PRIORITY_EMOJI.get(t["priority"]) or None,
            )
            for t in self._tickets[:25]  # Select max 25 options
        ]

        self.add_item(StatefulSelect(
            placeholder="Select a ticket to close...",
            options=options,
            callback=self.on_select,
        ))
        self.add_item(StatefulButton(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            emoji="\N{CROSS MARK}",
            row=1,
            callback=self.on_close,
        ))
        self.add_item(StatefulButton(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=1,
            callback=self.on_cancel,
        ))

    async def on_select(self, interaction):
        self._selected_id = interaction.data["values"][0]
        await interaction.response.edit_message(view=self)

    async def on_close(self, interaction):
        if not self._selected_id:
            await interaction.response.send_message(
                "Select a ticket first.", ephemeral=True
            )
            return

        await interaction.response.defer()
        store = get_store()
        await store.dispatch("TICKET_CLOSED", {
            "guild_id": self.guild_id,
            "ticket_id": self._selected_id,
        })
        await interaction.followup.send(
            f"\N{WHITE HEAVY CHECK MARK} Ticket **{self._selected_id}** closed.", ephemeral=True
        )
        await self.exit()

    async def on_cancel(self, interaction):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        await self.exit()

    async def update_from_state(self, state):
        pass


# // ========================================( Ticket Panel )======================================== // #


class TicketPanelView(PersistentView):
    """Persistent ticket panel posted once per channel.

    Buttons:
        - Create Ticket: opens the CreateTicketModal
        - My Tickets: sends a paginated list of the user's open tickets
        - Close Ticket: sends an ephemeral select+confirm view

    Re-renders the open ticket count via state_selector whenever a
    ticket is created or closed.
    """

    subscribed_actions = {"TICKET_CREATED", "TICKET_CLOSED"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_item(StatefulButton(
            label="Create Ticket",
            style=discord.ButtonStyle.primary,
            emoji="\N{HEAVY PLUS SIGN}",
            custom_id="ticket_panel:create",
            callback=self.create_ticket,
        ))
        self.add_item(StatefulButton(
            label="My Tickets",
            style=discord.ButtonStyle.secondary,
            emoji="\N{OPEN BOOK}",
            custom_id="ticket_panel:list",
            callback=self.list_tickets,
        ))
        self.add_item(StatefulButton(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            emoji="\N{CROSS MARK}",
            custom_id="ticket_panel:close",
            callback=self.close_ticket,
        ))

    def _build_embed(self):
        guild_id = self.guild_id
        open_count = _count_open_tickets(guild_id) if guild_id else 0
        theme = get_theme("support") or get_theme("default")

        embed = discord.Embed(
            title="Support Tickets",
            description=(
                "Click a button below to create, view, or close tickets.\n\n"
                f"**Open tickets:** {open_count}"
            ),
        )
        theme.apply_to_embed(embed)  # Prepends header_emoji, sets color + footer
        return embed

    def state_selector(self, state):
        """Only re-render when the open ticket count changes."""
        guild_id = str(self.guild_id) if self.guild_id else None
        if not guild_id:
            return None
        tickets = state.get("application", {}).get("tickets", {}).get(guild_id, [])
        return sum(1 for t in tickets if t["status"] == "open")

    async def update_from_state(self, state):
        if self.message:
            try:
                await self.message.edit(embed=self._build_embed(), view=self)
            except discord.HTTPException:
                pass

    async def on_restore(self, bot):
        """Refresh the embed after bot restart so the open count is current."""
        if self.message:
            try:
                await self.message.edit(embed=self._build_embed(), view=self)
            except discord.HTTPException:
                pass

    async def create_ticket(self, interaction):
        modal = CreateTicketModal(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
        )
        await interaction.response.send_modal(modal)

    async def list_tickets(self, interaction):
        tickets = _get_user_open_tickets(interaction.guild_id, interaction.user.id)

        if not tickets:
            await interaction.response.send_message(
                "You have no open tickets.", ephemeral=True
            )
            return

        # Defer first so send() uses the followup path internally
        await interaction.response.defer(thinking=True, ephemeral=True)
        view = await _build_ticket_list(tickets, interaction)
        await view.send(ephemeral=True)

    async def close_ticket(self, interaction):
        tickets = _get_user_open_tickets(interaction.guild_id, interaction.user.id)

        if not tickets:
            await interaction.response.send_message(
                "You have no open tickets to close.", ephemeral=True
            )
            return

        # Defer first so send() uses the followup path internally
        await interaction.response.defer(thinking=True, ephemeral=True)
        view = CloseTicketView(interaction=interaction, tickets=tickets)
        await view.send("Select a ticket to close:", ephemeral=True)


# // ========================================( Ticket List )======================================== // #


class TicketListView(PaginatedView):
    """Paginated ticket list with session limiting and live updates.

    Only one list can be open per user per guild. Opening a second
    one automatically closes the first.

    Subscribes to TICKET_CREATED and TICKET_CLOSED so the list stays
    in sync when tickets are created or closed from the panel.
    Includes a select menu (row 1) for viewing ticket details.
    """

    session_limit = 1
    session_scope = "user_guild"
    session_policy = "replace"

    subscribed_actions = {"TICKET_CREATED", "TICKET_CLOSED"}

    def __init__(self, *args, ticket_data=None, **kwargs):
        # Must be set before super().__init__() because _build_extra_items()
        # is called during PaginatedView.__init__
        self._ticket_data = ticket_data or []
        super().__init__(*args, **kwargs)

    def _build_extra_items(self):
        """Build or rebuild the ticket select menu on row 1.

        Called automatically by PaginatedView after nav buttons are built,
        on every page turn, and after refresh_data().
        """
        self.clear_row(1)

        if not self._ticket_data:
            return

        options = [
            discord.SelectOption(
                label=f"{t['id']} — {t['subject'][:40]}",
                value=t["id"],
                description=f"{PRIORITY_LABEL.get(t['priority'], t['priority'])} priority",
                emoji=PRIORITY_EMOJI.get(t["priority"]) or None,
            )
            for t in self._ticket_data[:25]
        ]

        select = StatefulSelect(
            custom_id="ticket_list_select",
            placeholder="Select a ticket to view details...",
            options=options,
            row=1,
            callback=self._on_ticket_select,
        )
        self.add_item(select)

    async def _on_ticket_select(self, interaction):
        """Send an ephemeral detail view for the selected ticket."""
        ticket_id = interaction.data["values"][0]
        ticket = _find_ticket(self.guild_id, ticket_id)
        if not ticket:
            await interaction.response.send_message(
                f"Ticket {ticket_id} not found.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        detail_view = TicketDetailView(interaction=interaction, ticket=ticket)
        await detail_view.send(embed=detail_view._build_embed(), ephemeral=True)

    def state_selector(self, state):
        """Re-render when the user's open ticket list changes."""
        guild_id = str(self.guild_id) if self.guild_id else None
        if not guild_id:
            return None
        all_tickets = state.get("application", {}).get("tickets", {}).get(guild_id, [])
        # Return a tuple of open ticket IDs for this user
        return tuple(
            t["id"] for t in all_tickets
            if t["author_id"] == self.user_id and t["status"] == "open"
        )

    async def update_from_state(self, state):
        """Rebuild pages and select when tickets change.

        Uses refresh_data() which re-chunks, re-formats, clamps the page
        index, calls _build_extra_items(), and edits the message — all in
        one call.
        """
        fresh_tickets = _get_user_open_tickets(self.guild_id, self.user_id)
        self._ticket_data = fresh_tickets

        if not fresh_tickets:
            # All tickets closed — show empty state
            self.pages = [discord.Embed(
                title="\N{OPEN BOOK} Your Open Tickets",
                description="All tickets have been resolved!",
            )]
            self.current_page = 0
            await self._update_page()
        else:
            await self.refresh_data(fresh_tickets)



def _format_ticket_page(chunk):
    """Format a chunk of tickets into an embed page."""
    theme = get_theme("support") or get_theme("default")

    lines = []
    for t in chunk:
        emoji = PRIORITY_EMOJI.get(t["priority"], "")
        status = STATUS_EMOJI.get(t["status"], "")
        lines.append(f"{status} **{t['id']}** {emoji} {t['subject']}")

    embed = discord.Embed(
        title="\N{OPEN BOOK} Your Open Tickets",
        description="\n".join(lines) or "No tickets.",
    )
    theme.apply_to_embed(embed)
    return embed


async def _build_ticket_list(tickets, interaction):
    """Build a TicketListView from a list of ticket dicts."""
    return await TicketListView.from_data(
        items=tickets,
        per_page=5,
        formatter=_format_ticket_page,
        interaction=interaction,
        ticket_data=tickets,
    )


# // ========================================( Cog )======================================== // #


class TicketSystemExample(commands.Cog, name="ticket_system_example"):
    """Support ticket system showcasing CascadeUI's framework features."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="ticket_setup",
        description="Post a persistent ticket panel in this channel (admin only)."
    )
    @commands.has_permissions(manage_guild=True)
    async def ticket_setup(self, context: Context) -> None:
        """Post a ticket panel that persists across bot restarts.

        Running this again in the same channel replaces the old panel
        automatically (PersistentView duplicate state_key cleanup).
        """
        view = TicketPanelView(
            context=context,
            state_key=f"ticket_panel:{context.channel.id}",
        )
        await view.send(embed=view._build_embed())

    @commands.hybrid_command(
        name="my_tickets",
        description="View your open support tickets."
    )
    async def my_tickets(self, context: Context) -> None:
        """Show a paginated list of your open tickets in this guild."""
        if not context.guild:
            await context.send("This command can only be used in a server.", ephemeral=True)
            return

        tickets = _get_user_open_tickets(context.guild.id, context.author.id)

        if not tickets:
            await context.send("You have no open tickets.", ephemeral=True)
            return

        try:
            view = await TicketListView.from_data(
                items=tickets,
                per_page=5,
                formatter=_format_ticket_page,
                context=context,
                ticket_data=tickets,
            )
            await view.send()
        except SessionLimitError:
            await context.send("You already have a ticket list open.", ephemeral=True)


async def setup(bot) -> None:
    register_theme(support_theme)
    await bot.add_cog(TicketSystemExample(bot=bot))
