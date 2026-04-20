# // ========================================( Modules )======================================== // #


from typing import Callable, List, Optional

import discord
from discord import ButtonStyle, Interaction

from ..base import StatefulButton, StatefulComponent
from ..v1_composition import CompositeComponent, register_component

# // ========================================( Classes )======================================== // #


class ConfirmationButtons(CompositeComponent):
    """A yes/no button pair for confirmations."""

    def __init__(
        self,
        on_confirm: Optional[Callable] = None,
        on_cancel: Optional[Callable] = None,
        confirm_label: str = "Yes",
        cancel_label: str = "No",
        confirm_style: ButtonStyle = ButtonStyle.success,
        cancel_style: ButtonStyle = ButtonStyle.danger,
    ) -> None:
        super().__init__()

        self.confirm_button = StatefulButton(
            label=confirm_label, style=confirm_style, callback=on_confirm
        )

        self.cancel_button = StatefulButton(
            label=cancel_label, style=cancel_style, callback=on_cancel
        )

        self.add_component(self.confirm_button)
        self.add_component(self.cancel_button)


# Register the component
register_component("confirmation_buttons", ConfirmationButtons)


class PaginationControls(CompositeComponent):
    """Navigation controls for paginated content."""

    def __init__(
        self, page_count: int, current_page: int = 0, on_page_change: Optional[Callable] = None
    ) -> None:
        super().__init__()
        self.page_count = max(1, page_count)
        self.current_page = min(max(0, current_page), self.page_count - 1)
        self.on_page_change = on_page_change

        # Create buttons
        self.prev_button = StatefulButton(
            label="Previous",
            style=ButtonStyle.secondary,
            disabled=self.current_page <= 0,
            callback=self._on_prev,
        )

        self.indicator = discord.ui.Button(
            label=f"Page {self.current_page + 1}/{self.page_count}",
            style=ButtonStyle.secondary,
            disabled=True,
        )

        self.next_button = StatefulButton(
            label="Next",
            style=ButtonStyle.secondary,
            disabled=self.current_page >= self.page_count - 1,
            callback=self._on_next,
        )

        # Add buttons to composite
        self.add_component(self.prev_button)
        self.add_component(self.indicator)
        self.add_component(self.next_button)

    async def _on_prev(self, interaction: Interaction) -> None:
        """Handle previous button click."""
        # Bare defer is the intentional fallback when on_page_change is None
        # or the click hits the boundary -- the click still needs ack-ing,
        # but no rebuild is owed. The is_done() guard keeps it safe under
        # auto-defer + serialize_interactions queueing.
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()

            if self.on_page_change:
                await self.on_page_change(interaction, self.current_page)
            elif not interaction.response.is_done():
                await interaction.response.defer()
        elif not interaction.response.is_done():
            await interaction.response.defer()

    async def _on_next(self, interaction: Interaction) -> None:
        """Handle next button click."""
        if self.current_page < self.page_count - 1:
            self.current_page += 1
            self._update_buttons()

            if self.on_page_change:
                await self.on_page_change(interaction, self.current_page)
            elif not interaction.response.is_done():
                await interaction.response.defer()
        elif not interaction.response.is_done():
            await interaction.response.defer()

    def _update_buttons(self) -> None:
        """Update button states based on current page."""
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == self.page_count - 1
        self.indicator.label = f"Page {self.current_page + 1}/{self.page_count}"


# Register the component
register_component("pagination_controls", PaginationControls)


class ToggleGroup(CompositeComponent):
    """Radio-button-like group where only one option can be active at a time.

    When a button is clicked, all others reset to secondary style and the
    selected one becomes primary. The on_select callback receives the
    selected value.

    Usage:
        group = ToggleGroup(
            options=["Easy", "Medium", "Hard"],
            on_select=my_handler,
            default="Medium",
        )
        group.add_to_view(my_view)
    """

    def __init__(
        self,
        options: List[str],
        on_select: Optional[Callable] = None,
        default: Optional[str] = None,
        row: Optional[int] = None,
    ):
        super().__init__()
        self.options = options
        self.on_select = on_select
        self.selected = default or options[0] if options else None
        # Captured by add_to_view so callbacks can refresh the host view
        # after mutating button styles in place.
        self._view = None
        self._buttons: List[StatefulButton] = []

        for option in options:
            is_active = option == self.selected
            style = ButtonStyle.primary if is_active else ButtonStyle.secondary

            def make_callback(opt=option):
                async def callback(interaction: Interaction):
                    self.selected = opt
                    for btn in self._buttons:
                        btn.style = (
                            ButtonStyle.primary
                            if btn.label == opt
                            else ButtonStyle.secondary
                        )

                    if self._view is not None and not self._view.is_finished():
                        await self._view.refresh()

                    if self.on_select:
                        await self.on_select(interaction, opt)
                    elif not interaction.response.is_done():
                        await interaction.response.defer()

                return callback

            button = StatefulButton(
                label=option,
                style=style,
                custom_id=f"toggle_{option.lower().replace(' ', '_')}",
                row=row,
                callback=make_callback(option),
            )
            self._buttons.append(button)
            self.add_component(button)

    def add_to_view(self, view, row=None):
        """Capture the host view so click callbacks can refresh it."""
        self._view = view
        return super().add_to_view(view, row=row)


register_component("toggle_group", ToggleGroup)


class ProgressBar:
    """Visual progress indicator rendered as a text-based bar for embeds.

    Not a discord component. Generates a string representation of progress
    that can be placed in embed fields or descriptions.

    Usage:
        bar = ProgressBar(total=100, fill_char="█", empty_char="░", width=20)
        embed.add_field(name="Progress", value=bar.render(current=65))
        # Output: █████████████░░░░░░░ 65%
    """

    def __init__(
        self,
        total: int = 100,
        width: int = 20,
        fill_char: str = "\u2588",
        empty_char: str = "\u2591",
    ):
        self.total = total
        self.width = width
        self.fill_char = fill_char
        self.empty_char = empty_char

    def render(self, current: int) -> str:
        """Render the progress bar for the given value."""
        clamped = max(0, min(current, self.total))
        ratio = clamped / self.total if self.total > 0 else 0
        filled = int(self.width * ratio)
        empty = self.width - filled
        percent = int(ratio * 100)
        return f"{self.fill_char * filled}{self.empty_char * empty} {percent}%"
