"""
V2 Pagination — CascadeUI V2 Paginated Content
=================================================

Demonstrates PaginatedLayoutView, CascadeUI's V2 pagination pattern.
Pages are built as V2 component trees (Container, TextDisplay) instead
of embeds. Navigation controls, jump buttons, and go-to-page modal
all work identically to the V1 PaginatedView.

V2 advantages shown here:

    - Pages as accent-colored cards via card() helper
    - Multiple content blocks per page (not limited to one embed)
    - Inline page metadata alongside content
    - Jump buttons and go-to-page modal for large page counts
    - _build_extra_items() hook for adding an exit button
    - refresh_data() for live re-pagination

Commands:
    /v2pages   Browse a paginated item list

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import logging

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import PaginatedLayoutView, StatefulButton, card, divider

logger = logging.getLogger(__name__)


# // ========================================( Data )======================================== // #


# Sample inventory — enough items to trigger jump buttons (>5 pages at 4/page)
SAMPLE_ITEMS = [
    # Common (pages 1-2)
    {"name": "Iron Sword", "rarity": "Common", "value": 50},
    {"name": "Steel Shield", "rarity": "Common", "value": 75},
    {"name": "Health Potion", "rarity": "Common", "value": 25},
    {"name": "Mana Potion", "rarity": "Common", "value": 30},
    {"name": "Leather Armor", "rarity": "Common", "value": 60},
    {"name": "Wooden Bow", "rarity": "Common", "value": 45},
    {"name": "Cloth Robe", "rarity": "Common", "value": 35},
    {"name": "Bronze Ring", "rarity": "Common", "value": 20},
    # Uncommon (pages 3-4)
    {"name": "Fire Staff", "rarity": "Uncommon", "value": 200},
    {"name": "Ice Bow", "rarity": "Uncommon", "value": 225},
    {"name": "Thunder Gauntlets", "rarity": "Uncommon", "value": 250},
    {"name": "Shadow Cloak", "rarity": "Uncommon", "value": 275},
    {"name": "Wind Boots", "rarity": "Uncommon", "value": 300},
    {"name": "Stone Hammer", "rarity": "Uncommon", "value": 180},
    {"name": "Silver Amulet", "rarity": "Uncommon", "value": 220},
    {"name": "Enchanted Quiver", "rarity": "Uncommon", "value": 260},
    # Rare (pages 5-6)
    {"name": "Dragon Scale Mail", "rarity": "Rare", "value": 800},
    {"name": "Phoenix Feather Wand", "rarity": "Rare", "value": 900},
    {"name": "Void Dagger", "rarity": "Rare", "value": 850},
    {"name": "Crystal Orb", "rarity": "Rare", "value": 750},
    {"name": "Moonstone Tiara", "rarity": "Rare", "value": 820},
    {"name": "Abyssal Trident", "rarity": "Rare", "value": 880},
    {"name": "Runic Shield", "rarity": "Rare", "value": 770},
    {"name": "Starlight Bow", "rarity": "Rare", "value": 910},
    # Legendary (page 7)
    {"name": "Titan's Hammer", "rarity": "Legendary", "value": 2500},
    {"name": "Celestial Crown", "rarity": "Legendary", "value": 3000},
    {"name": "Excalibur", "rarity": "Legendary", "value": 5000},
    {"name": "Aegis of the Ancients", "rarity": "Legendary", "value": 4500},
]

RARITY_COLORS = {
    "Common": discord.Color.light_grey(),
    "Uncommon": discord.Color.green(),
    "Rare": discord.Color.blue(),
    "Legendary": discord.Color.gold(),
}

RARITY_ORDER = ["Common", "Uncommon", "Rare", "Legendary"]


# // ========================================( View )======================================== // #


class InventoryView(PaginatedLayoutView):
    """Paginated inventory with an exit button below navigation.

    Overrides ``_build_extra_items()`` to add a Close button after
    the pagination controls. This hook is called during init and
    on every page turn.
    """

    session_limit = 1

    def _build_extra_items(self):
        """Add an exit button below the navigation row."""
        self.add_exit_button()


# // ========================================( Formatter )======================================== // #


def format_page(items: list) -> list:
    """Format a page of items as V2 components.

    Each page gets an accent-colored card with item rows and a
    summary footer. The accent color matches the highest rarity
    item on the page.
    """
    lines = []
    best_rarity = "Common"

    for item in items:
        rarity = item["rarity"]
        if RARITY_ORDER.index(rarity) > RARITY_ORDER.index(best_rarity):
            best_rarity = rarity

        rarity_tag = f"` {rarity} `"
        lines.append(f"**{item['name']}** {rarity_tag} — {item['value']}g")

    color = RARITY_COLORS.get(best_rarity, discord.Color.light_grey())
    total_value = sum(item["value"] for item in items)

    return [
        card(
            "## Inventory",
            TextDisplay("\n".join(lines)),
            divider(),
            TextDisplay(f"-# {len(items)} items | Page value: {total_value:,}g"),
            color=color,
        )
    ]


# // ========================================( Cog )======================================== // #


class V2PaginationExample(commands.Cog, name="v2_pagination_example"):
    """V2 pagination demonstrating Container-based page content with jump controls."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2pages",
        description="Browse a paginated inventory using V2 components.",
    )
    async def v2pages(self, context: Context) -> None:
        """Browse a paginated item inventory.

        Pages are accent-colored cards. The accent shifts with the
        rarity tier: grey for Common, green for Uncommon, blue for
        Rare, gold for Legendary. With 7 pages, jump buttons and
        a go-to-page modal are shown automatically.
        """
        view = await InventoryView.from_data(
            items=SAMPLE_ITEMS,
            per_page=4,
            formatter=format_page,
            context=context,
        )
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2PaginationExample(bot=bot))
