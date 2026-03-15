
# // ========================================( Modules )======================================== // #


from typing import List, Dict, Any, Optional, Callable

import discord
from discord import Interaction

from .base import StatefulView
from ..components.base import StatefulButton, StatefulSelect


# // ========================================( TabView )======================================== // #


class TabView(StatefulView):
    """Tabbed interface with button-based tab switching.

    Each tab is defined by a name and a builder function that returns
    an embed (and optionally extra components) for that tab's content.

    Usage:
        class MyTabs(TabView):
            def __init__(self, *args, **kwargs):
                tabs = {
                    "Overview": self.build_overview,
                    "Settings": self.build_settings,
                    "Stats": self.build_stats,
                }
                super().__init__(*args, tabs=tabs, **kwargs)

            async def build_overview(self) -> discord.Embed:
                return discord.Embed(title="Overview", description="...")

            async def build_settings(self) -> discord.Embed:
                return discord.Embed(title="Settings", description="...")

            async def build_stats(self) -> discord.Embed:
                return discord.Embed(title="Stats", description="...")
    """

    def __init__(self, *args, tabs: Optional[Dict[str, Callable]] = None, **kwargs):
        super().__init__(*args, **kwargs)

        self._tabs: Dict[str, Callable] = tabs or {}
        self._tab_names: List[str] = list(self._tabs.keys())
        self._active_tab: int = 0

        self._build_tab_buttons()

    def _build_tab_buttons(self):
        """Create a button for each tab."""
        for i, name in enumerate(self._tab_names):
            style = discord.ButtonStyle.primary if i == 0 else discord.ButtonStyle.secondary

            def make_callback(index=i):
                async def callback(interaction: Interaction):
                    await interaction.response.defer()
                    self._active_tab = index
                    await self._refresh_tabs()
                return callback

            button = StatefulButton(
                label=name,
                style=style,
                custom_id=f"tab_{i}",
                row=0,
                callback=make_callback(i),
            )
            self.add_item(button)

    async def _refresh_tabs(self):
        """Update button styles and rebuild content for the active tab."""
        # Update button styles
        for item in self.children:
            cid = getattr(item, "custom_id", None)
            if cid and cid.startswith("tab_"):
                tab_index = int(cid.split("_")[1])
                item.style = (
                    discord.ButtonStyle.primary
                    if tab_index == self._active_tab
                    else discord.ButtonStyle.secondary
                )

        # Get content from the active tab builder
        tab_name = self._tab_names[self._active_tab]
        builder = self._tabs[tab_name]
        embed = await builder()

        if self.message:
            await self.message.edit(embed=embed, view=self)

    @property
    def active_tab(self) -> str:
        """Name of the currently active tab."""
        return self._tab_names[self._active_tab]

    async def update_from_state(self, state):
        pass


# // ========================================( WizardView )======================================== // #


class WizardView(StatefulView):
    """Multi-step form with back/next navigation and per-step validation.

    Each step is defined by a builder function that returns an embed.
    Steps can have optional validators that determine whether the user
    can proceed to the next step.

    Usage:
        class SetupWizard(WizardView):
            def __init__(self, *args, **kwargs):
                steps = [
                    {"name": "Welcome", "builder": self.build_welcome},
                    {"name": "Config", "builder": self.build_config,
                     "validator": self.validate_config},
                    {"name": "Confirm", "builder": self.build_confirm},
                ]
                super().__init__(*args, steps=steps, on_finish=self.finish, **kwargs)

            async def build_welcome(self) -> discord.Embed:
                return discord.Embed(title="Step 1: Welcome", description="...")

            async def build_config(self) -> discord.Embed:
                return discord.Embed(title="Step 2: Configuration")

            async def validate_config(self) -> tuple[bool, str]:
                # Return (valid, error_message)
                return True, ""

            async def build_confirm(self) -> discord.Embed:
                return discord.Embed(title="Step 3: Confirm")

            async def finish(self, interaction):
                await interaction.response.send_message("Setup complete!")
    """

    def __init__(self, *args, steps: Optional[List[Dict[str, Any]]] = None,
                 on_finish: Optional[Callable] = None, **kwargs):
        super().__init__(*args, **kwargs)

        self._steps: List[Dict[str, Any]] = steps or []
        self._current_step: int = 0
        self._on_finish = on_finish

        self._build_nav_buttons()

    def _build_nav_buttons(self):
        """Create back, next, and cancel navigation buttons."""
        self._back_btn = StatefulButton(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="wizard_back",
            row=4,
            disabled=True,
            callback=self._go_back,
        )
        self.add_item(self._back_btn)

        self._step_indicator = discord.ui.Button(
            label=self._step_label(),
            style=discord.ButtonStyle.gray,
            custom_id="wizard_indicator",
            row=4,
            disabled=True,
        )
        self.add_item(self._step_indicator)

        self._next_btn = StatefulButton(
            label="Next" if len(self._steps) > 1 else "Finish",
            style=discord.ButtonStyle.primary,
            custom_id="wizard_next",
            row=4,
            callback=self._go_next,
        )
        self.add_item(self._next_btn)

    def _step_label(self) -> str:
        total = max(len(self._steps), 1)
        return f"Step {self._current_step + 1}/{total}"

    async def _go_back(self, interaction: Interaction):
        await interaction.response.defer()
        if self._current_step > 0:
            self._current_step -= 1
            await self._refresh_wizard()

    async def _go_next(self, interaction: Interaction):
        # Validate current step if validator exists
        step = self._steps[self._current_step] if self._steps else None
        if step and "validator" in step:
            valid, error = await step["validator"]()
            if not valid:
                await interaction.response.send_message(error, ephemeral=True)
                return

        if self._current_step >= len(self._steps) - 1:
            # Last step: finish
            if self._on_finish:
                await self._on_finish(interaction)
            else:
                await interaction.response.defer()
                await self.exit()
        else:
            await interaction.response.defer()
            self._current_step += 1
            await self._refresh_wizard()

    async def _refresh_wizard(self):
        """Update navigation state and rebuild current step content."""
        self._back_btn.disabled = self._current_step == 0
        self._step_indicator.label = self._step_label()

        is_last = self._current_step >= len(self._steps) - 1
        self._next_btn.label = "Finish" if is_last else "Next"

        # Build step content
        if self._steps:
            step = self._steps[self._current_step]
            builder = step.get("builder")
            if builder:
                embed = await builder()
                if self.message:
                    await self.message.edit(embed=embed, view=self)

    @property
    def current_step(self) -> int:
        return self._current_step

    @property
    def step_count(self) -> int:
        return len(self._steps)

    async def update_from_state(self, state):
        pass
