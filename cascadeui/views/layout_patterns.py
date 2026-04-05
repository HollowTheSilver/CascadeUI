# // ========================================( Modules )======================================== // #


from typing import Any, Callable, Dict, List, Optional

import discord
from discord import Interaction
from discord.ui import ActionRow, Button, Container, TextDisplay

from ..components.base import StatefulButton
from .layout import StatefulLayoutView

# // ========================================( TabLayoutView )======================================== // #


class TabLayoutView(StatefulLayoutView):
    """Tabbed interface with button-based tab switching for V2 layouts.

    The V2 equivalent of ``TabView``. Each tab is defined by a name and a
    builder function that returns a list of V2 components (Container,
    TextDisplay, etc.) for that tab's content.

    Usage:
        class MyTabs(TabLayoutView):
            def __init__(self, *args, **kwargs):
                tabs = {
                    "Overview": self.build_overview,
                    "Settings": self.build_settings,
                }
                super().__init__(*args, tabs=tabs, **kwargs)

            async def build_overview(self):
                return [Container(TextDisplay("Overview content..."))]

            async def build_settings(self):
                return [Container(TextDisplay("Settings content..."))]
    """

    def __init__(self, *args, tabs: Optional[Dict[str, Callable]] = None, **kwargs):
        super().__init__(*args, **kwargs)

        self._tabs: Dict[str, Callable] = tabs or {}
        self._tab_names: List[str] = list(self._tabs.keys())
        self._active_tab: int = 0

        self._build_tab_buttons()

    def _build_tab_buttons(self):
        """Create a button for each tab in an ActionRow."""
        buttons = []
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
                callback=make_callback(i),
            )
            buttons.append(button)

        if buttons:
            self.add_item(ActionRow(*buttons))

    async def _refresh_tabs(self):
        """Update button styles and rebuild content for the active tab."""
        self.clear_items()

        # Rebuild tab buttons with updated styles
        buttons = []
        for i, name in enumerate(self._tab_names):
            style = (
                discord.ButtonStyle.primary
                if i == self._active_tab
                else discord.ButtonStyle.secondary
            )

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
                callback=make_callback(i),
            )
            buttons.append(button)

        if buttons:
            self.add_item(ActionRow(*buttons))

        # Get content from the active tab builder
        tab_name = self._tab_names[self._active_tab]
        builder = self._tabs[tab_name]
        content = await builder()

        if isinstance(content, list):
            for item in content:
                self.add_item(item)
        else:
            self.add_item(content)

        if self.message:
            await self.message.edit(view=self)

    async def send(self, **kwargs):
        """Build initial tab content before sending.

        Tab builders are async and cannot run in ``__init__``, so the
        first tab's content is built here before the message is sent.
        """
        if self._tab_names:
            builder = self._tabs[self._tab_names[self._active_tab]]
            content = await builder()
            if isinstance(content, list):
                for item in content:
                    self.add_item(item)
            else:
                self.add_item(content)

        return await super().send(**kwargs)

    @property
    def active_tab(self) -> str:
        """Name of the currently active tab."""
        return self._tab_names[self._active_tab]

    async def switch_tab(self, name: str):
        """Switch to a tab by name and refresh the view.

        Raises ``ValueError`` if the tab name is not found.
        """
        try:
            index = self._tab_names.index(name)
        except ValueError:
            raise ValueError(f"Tab '{name}' not found. Available: {self._tab_names}")
        self._active_tab = index
        await self._refresh_tabs()


# // ========================================( WizardLayoutView )======================================== // #


class WizardLayoutView(StatefulLayoutView):
    """Multi-step form with back/next navigation for V2 layouts.

    The V2 equivalent of ``WizardView``. Each step is defined by a builder
    function that returns a list of V2 components. Steps can have optional
    validators that determine whether the user can proceed.

    Usage:
        class SetupWizard(WizardLayoutView):
            def __init__(self, *args, **kwargs):
                steps = [
                    {"name": "Welcome", "builder": self.build_welcome},
                    {"name": "Config", "builder": self.build_config,
                     "validator": self.validate_config},
                    {"name": "Confirm", "builder": self.build_confirm},
                ]
                super().__init__(*args, steps=steps, on_finish=self.finish, **kwargs)

            async def build_welcome(self):
                return [Container(TextDisplay("Welcome!"))]

            async def build_config(self):
                return [Container(TextDisplay("Configure settings..."))]

            async def validate_config(self):
                return True, ""

            async def build_confirm(self):
                return [Container(TextDisplay("Ready to finish?"))]

            async def finish(self, interaction):
                await interaction.response.send_message("Done!")
    """

    def __init__(
        self,
        *args,
        steps: Optional[List[Dict[str, Any]]] = None,
        on_finish: Optional[Callable] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._steps: List[Dict[str, Any]] = steps or []
        self._current_step: int = 0
        self._on_finish = on_finish

        self._build_nav_buttons()

    def _build_nav_buttons(self):
        """Create back, indicator, and next navigation buttons in an ActionRow."""
        self._back_btn = StatefulButton(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="wizard_back",
            disabled=True,
            callback=self._go_back,
        )

        self._step_indicator = Button(
            label=self._step_label(),
            style=discord.ButtonStyle.secondary,
            custom_id="wizard_indicator",
            disabled=True,
        )

        self._next_btn = StatefulButton(
            label="Next" if len(self._steps) > 1 else "Finish",
            style=discord.ButtonStyle.primary,
            custom_id="wizard_next",
            callback=self._go_next,
        )

        self.add_item(ActionRow(self._back_btn, self._step_indicator, self._next_btn))

    def _step_label(self) -> str:
        total = max(len(self._steps), 1)
        return f"Step {self._current_step + 1}/{total}"

    async def _go_back(self, interaction: Interaction):
        await interaction.response.defer()
        if self._current_step > 0:
            self._current_step -= 1
            await self._refresh_wizard()

    async def _go_next(self, interaction: Interaction):
        step = self._steps[self._current_step] if self._steps else None
        if step and "validator" in step:
            valid, error = await step["validator"]()
            if not valid:
                await interaction.response.send_message(error, ephemeral=True)
                return

        if self._current_step >= len(self._steps) - 1:
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
        self.clear_items()

        # Rebuild nav buttons with updated state
        self._back_btn = StatefulButton(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id="wizard_back",
            disabled=self._current_step == 0,
            callback=self._go_back,
        )

        self._step_indicator = Button(
            label=self._step_label(),
            style=discord.ButtonStyle.secondary,
            custom_id="wizard_indicator",
            disabled=True,
        )

        is_last = self._current_step >= len(self._steps) - 1
        self._next_btn = StatefulButton(
            label="Finish" if is_last else "Next",
            style=discord.ButtonStyle.primary,
            custom_id="wizard_next",
            callback=self._go_next,
        )

        # Build step content first, then add nav
        if self._steps:
            step = self._steps[self._current_step]
            builder = step.get("builder")
            if builder:
                content = await builder()
                if isinstance(content, list):
                    for item in content:
                        self.add_item(item)
                else:
                    self.add_item(content)

        self.add_item(ActionRow(self._back_btn, self._step_indicator, self._next_btn))

        if self.message:
            await self.message.edit(view=self)

    async def send(self, **kwargs):
        """Build initial step content before sending.

        Step builders are async and cannot run in ``__init__``, so the
        first step's content is built here before the message is sent.
        """
        if self._steps:
            step = self._steps[self._current_step]
            builder = step.get("builder")
            if builder:
                content = await builder()
                # Insert step content before the nav ActionRow
                nav_row = self.children[-1] if self.children else None
                self.clear_items()
                if isinstance(content, list):
                    for item in content:
                        self.add_item(item)
                else:
                    self.add_item(content)
                if nav_row is not None:
                    self.add_item(nav_row)

        return await super().send(**kwargs)

    @property
    def current_step(self) -> int:
        return self._current_step

    @property
    def step_count(self) -> int:
        return len(self._steps)
