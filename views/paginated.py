"""Paginated view implementation for CascadeUI."""

# // ========================================( Modules )======================================== // #


import discord
from typing import List, TYPE_CHECKING

# Import the logger
from ..utils.logger import AsyncLogger
from ..view import CascadeView

# Create logger
logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a")


# // ========================================( Class )======================================== // #


class PaginatedCascadeView(CascadeView):
    """View that handles paginated embeds with navigation controls."""

    def __init__(self, embeds: List[discord.Embed], *args, **kwargs):
        # Extract embeds to avoid auto-assignment
        all_embeds = embeds
        if 'embeds' in kwargs:
            del kwargs['embeds']

        super().__init__(*args, **kwargs)
        self._all_embeds = all_embeds
        self.current_page = 0
        self.update_view()

    def update_view(self):
        """Update navigation buttons based on current page."""
        self.clear_items()

        # Previous page button
        prev_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.primary,
            disabled=self.current_page == 0,
            row=0
        )
        prev_button.callback = self.previous_page
        self.add_item(prev_button)

        # Page indicator
        page_indicator = discord.ui.Button(
            label=f"Page {self.current_page + 1}/{len(self._all_embeds)}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            row=0
        )
        self.add_item(page_indicator)

        # Next page button
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.primary,
            disabled=self.current_page == len(self._all_embeds) - 1,
            row=0
        )
        next_button.callback = self.next_page
        self.add_item(next_button)

        # Update the displayed embed
        self.embeds = [self._all_embeds[self.current_page]]

    async def previous_page(self, interaction: discord.Interaction):
        """Go to previous page."""
        self.current_page = max(0, self.current_page - 1)
        self.update_view()
        await interaction.response.edit_message(view=self, embeds=self.embeds)

    async def next_page(self, interaction: discord.Interaction):
        """Go to next page."""
        self.current_page = min(len(self._all_embeds) - 1, self.current_page + 1)
        self.update_view()
        await interaction.response.edit_message(view=self, embeds=self.embeds)
