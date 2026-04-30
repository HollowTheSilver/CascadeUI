"""
V2 Library -- pagination + drill-down navigation in CascadeUI
==============================================================

A focused walkthrough for users coming from hand-rolled paginators
asking how to combine pagination with back buttons. The whole flow is
two view classes: a category hub and a paginated list. CascadeUI
absorbs the boilerplate.

What this example demonstrates:

    - PaginatedLayoutView.from_data(items, per_page, formatter)
        One async classmethod chunks the items, runs the formatter
        per page, and returns a fully-built paginator. CascadeUI
        handles page state, prev/next/jump button wiring, the goto-page
        modal, and the ActionRow wrapping.

    - nav_inside_container = True
        Wraps page content + nav row in a single Container so the
        paginator renders as one cohesive card. Default ``False`` keeps
        the original sibling layout.

    - auto_back_button = True
        CascadeUI generates a Back button on the pushed view. The
        button calls ``self.pop()`` automatically and the underlying
        Discord message is restored to the parent's content -- no
        manual back-button wiring, no message re-send.

    - await self.push(view, interaction)
        ``push`` accepts either a view class or a pre-constructed
        instance. The instance form pairs with async classmethod
        constructors like ``from_data``. CascadeUI transfers the message
        reference, unsubscribes the parent, sets up the nav stack on the
        child, and edits the same Discord message with the child's
        component tree. The ``rebuild`` kwarg is an optional pre-edit
        hook; views built by ``from_data`` need no rebuild.

Commands:
    /v2library   Browse a small library catalog (Books / Music / Movies)

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
    PaginatedLayoutView,
    StatefulButton,
    StatefulLayoutView,
    card,
    divider,
    key_value,
)

logger = logging.getLogger(__name__)


# // ========================================( Data )======================================== // #


# Three categories with enough items to span multiple pages at 3 per page.
CATALOG = {
    "Books": [
        {"name": "The Pragmatic Programmer", "by": "Hunt & Thomas", "year": 1999},
        {"name": "Code Complete", "by": "Steve McConnell", "year": 2004},
        {"name": "The Mythical Man-Month", "by": "Fred Brooks", "year": 1975},
        {"name": "Refactoring", "by": "Martin Fowler", "year": 1999},
        {"name": "Design Patterns", "by": "Gang of Four", "year": 1994},
        {"name": "Domain-Driven Design", "by": "Eric Evans", "year": 2003},
        {"name": "Clean Code", "by": "Robert Martin", "year": 2008},
        {"name": "Test-Driven Development", "by": "Kent Beck", "year": 2002},
    ],
    "Music": [
        {"name": "Kind of Blue", "by": "Miles Davis", "year": 1959},
        {"name": "Pet Sounds", "by": "The Beach Boys", "year": 1966},
        {"name": "Abbey Road", "by": "The Beatles", "year": 1969},
        {"name": "OK Computer", "by": "Radiohead", "year": 1997},
        {"name": "Discovery", "by": "Daft Punk", "year": 2001},
        {"name": "In Rainbows", "by": "Radiohead", "year": 2007},
    ],
    "Movies": [
        {"name": "The Godfather", "by": "Coppola", "year": 1972},
        {"name": "2001: A Space Odyssey", "by": "Kubrick", "year": 1968},
        {"name": "Seven Samurai", "by": "Kurosawa", "year": 1954},
        {"name": "Tokyo Story", "by": "Ozu", "year": 1953},
        {"name": "Vertigo", "by": "Hitchcock", "year": 1958},
        {"name": "Apocalypse Now", "by": "Coppola", "year": 1979},
        {"name": "Stalker", "by": "Tarkovsky", "year": 1979},
    ],
}


# Per-category accent color. Picked at hub-button-click time and threaded
# through to the paginated list so every page on the same category shares
# the same Container border tint.
CATEGORY_COLORS = {
    "Books": discord.Color.blurple(),
    "Music": discord.Color.green(),
    "Movies": discord.Color.dark_gold(),
}


# // ========================================( Page formatter factory )======================================== // #


def _make_formatter(name: str, accent: discord.Color):
    """Return a per-category formatter bound to the category name and accent.

    Each page is rendered as a ``card`` with the category accent so the
    color travels with the chunked items. The ``nav_inside_container``
    flag on the view wraps the card + nav row in another Container; the
    inner card keeps the per-category accent visible inside that wrapper.
    """

    def format_page(items: list) -> list:
        lines = [f"**{item['name']}** -- *{item['by']}* ({item['year']})" for item in items]
        return [
            card(
                f"## {name}",
                divider(),
                TextDisplay("\n".join(lines)),
                color=accent,
            )
        ]

    return format_page


# // ========================================( Level 2: paginated category list )======================================== // #


class CategoryListView(PaginatedLayoutView):
    """A paginated list of items in one category.

    Constructed via ``await CategoryListView.from_data(...)`` from the
    hub's category-button callback. ``auto_back_button`` adds the Back
    button; clicking it pops the nav stack and restores the hub view to
    the same Discord message. ``nav_inside_container`` wraps page
    content + nav row in a single Container.
    """

    owner_only = True
    auto_back_button = True
    nav_inside_container = True
    exit_policy = "disable"
    state_scope = None
    # No Redux reactivity -- pagination state is pattern-internal.
    subscribed_actions = set()


# // ========================================( Level 1: category hub )======================================== // #


class LibraryHubView(StatefulLayoutView):
    """Top-level hub with one button per category.

    Each button builds a paginated ``CategoryListView`` via
    ``from_data`` and pushes it onto the navigation stack. The
    paginator is constructed before the push because ``from_data`` is
    async; ``push()`` accepts the resulting instance directly.
    """

    owner_only = True
    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "replace"
    replace_policy = "delete"  # delete the previous hub on re-invoke
    exit_policy = "disable"
    # Hub dispatches nothing; all navigation state is pattern-internal.
    state_scope = None
    subscribed_actions = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_ui()

    def build_ui(self):
        self.clear_items()

        self.add_item(
            card(
                "## Library",
                "Pick a category to browse.",
                divider(),
                key_value({name: f"{len(items)} items" for name, items in CATALOG.items()}),
                color=discord.Color.greyple(),
            )
        )

        buttons = [
            StatefulButton(
                label=name,
                style=discord.ButtonStyle.primary,
                callback=self._make_category_callback(name),
            )
            for name in CATALOG
        ]
        self.add_item(ActionRow(*buttons, self.make_exit_button()))

    def _make_category_callback(self, name: str):
        items = CATALOG[name]
        accent = CATEGORY_COLORS[name]

        async def callback(interaction: discord.Interaction):
            child = await CategoryListView.from_data(
                items=items,
                per_page=3,
                formatter=_make_formatter(name, accent),
                interaction=interaction,
            )
            # ``from_data`` returns a fully-built paginator; no rebuild
            # hook is needed. ``push`` defers the interaction and edits
            # the message with the child view regardless.
            await self.push(child, interaction)

        return callback


# // ========================================( Cog )======================================== // #


class V2LibraryExample(commands.Cog, name="v2_library_example"):
    """V2 example: pagination + drill-down navigation with auto back buttons."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2library",
        description="Browse a small library catalog with paginated category lists.",
    )
    async def v2library(self, context: Context) -> None:
        """Open the library hub.

        Click a category to push a paginated list of its items. The
        list view's auto-generated Back button pops back to this hub.
        Same Discord message stays in place across every transition.
        """
        view = LibraryHubView(context=context)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2LibraryExample(bot=bot))
