"""
V2 Wizard -- D&D Character Creator
==================================

A five-step character creator that demonstrates ``WizardLayoutView``
handling the full range of wizard features in a single flow:

    - Text input via a modal opened from a step button
    - Inline selects and toggles for structured input (alignment,
      languages, heroic destiny) alongside a modal for free-form text
    - Cascading selects where later options depend on earlier choices
    - Point-pool allocation with live remaining-count feedback
    - Per-step validators that block progression with fail-loud errors
    - A final review card assembled from every prior step's state
    - Navigation-button customization via the
      ``back/next/finish_button_{label,emoji,style}`` triples
    - ``on_finish`` as a method hook that posts the finished sheet

The character progression mirrors a simplified D&D 5e flow: pick a race,
then a race-gated class, then a class-gated subclass, then allocate a
pool of ability points, then fill in background details (backstory,
alignment, languages, and an optional heroic-destiny flag). Every step
reads from and writes to instance state, and the review step composes
the full sheet from all accumulated values.

Commands:
    /v2wizard   Start the character creator

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
    DisplayLayoutView,
    Modal,
    StatefulButton,
    StatefulSelect,
    TextInput,
    WizardLayoutView,
    WizardStep,
    card,
    divider,
    key_value,
    toggle_section,
)

# // ========================================( Character data )======================================== // #


logger = logging.getLogger(__name__)


RACES = ["Human", "Elf", "Dwarf", "Halfling"]

# Class availability is gated by race. The gating is deliberately sparse
# so the cascade is visible at a glance: Halflings cannot be Paladins,
# Dwarves cannot be Wizards, and so on.
CLASSES_BY_RACE = {
    "Human": ["Fighter", "Wizard", "Rogue", "Cleric"],
    "Elf": ["Wizard", "Ranger", "Rogue"],
    "Dwarf": ["Fighter", "Cleric", "Paladin"],
    "Halfling": ["Rogue", "Bard"],
}

SUBCLASSES_BY_CLASS = {
    "Fighter": ["Champion", "Battle Master"],
    "Wizard": ["Evoker", "Illusionist"],
    "Rogue": ["Thief", "Assassin"],
    "Cleric": ["Life Domain", "War Domain"],
    "Ranger": ["Hunter", "Gloom Stalker"],
    "Paladin": ["Devotion", "Vengeance"],
    "Bard": ["Lore", "Valor"],
}

ALIGNMENTS = [
    "Lawful Good",
    "Neutral Good",
    "Chaotic Good",
    "Lawful Neutral",
    "True Neutral",
    "Chaotic Neutral",
    "Lawful Evil",
    "Neutral Evil",
    "Chaotic Evil",
]

# Common is always known; racial languages are pre-selected as defaults
# in the background step's language select based on the chosen race.
LANGUAGES = ["Common", "Elvish", "Dwarvish", "Halfling", "Draconic", "Infernal", "Celestial"]
RACIAL_LANGUAGES = {
    "Human": [],
    "Elf": ["Elvish"],
    "Dwarf": ["Dwarvish"],
    "Halfling": ["Halfling"],
}

ABILITIES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
ABILITY_NAMES = {
    "STR": "Strength",
    "DEX": "Dexterity",
    "CON": "Constitution",
    "INT": "Intelligence",
    "WIS": "Wisdom",
    "CHA": "Charisma",
}

# Every ability starts at 8 and the character has 6 points to spend.
# Each click of the ability-increment select adds 1 to the chosen score.
STARTING_SCORE = 8
POINT_POOL = 6
MAX_SCORE = 15


# // ========================================( Wizard )======================================== // #


class CharacterCreatorView(WizardLayoutView):
    """Five-step D&D character creator built on ``WizardLayoutView``.

    Step order:
        1. Identity       -- Name (modal) and race
        2. Class          -- Race-gated class and class-gated subclass
        3. Attributes     -- Point-pool allocation across six stats
        4. Background     -- Inline selects + toggle + backstory modal
        5. Review         -- Full character sheet summary
    """

    # // ----( Policy surface )---- // #
    owner_only = True
    auto_defer = True
    instance_limit = 1
    instance_scope = "user"  # One open creator per user, across guilds.
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "delete"
    # state_scope = None because character sheet state lives on instance
    # attributes (_name, _race, etc.), not the Redux tree.
    state_scope = None
    auto_refresh_ephemeral = False  # Non-ephemeral view; the refresh handoff never arms.
    instance_limit_message = (
        "You already have a character creator open. Finish or exit it before starting another."
    )

    # // ----( Progress header )---- // #
    # Setting ``show_progress_bar = True`` tells ``WizardLayoutView`` to
    # render a progress bar above every step. The header uses the
    # ``step_indicator_label`` text plus a proportional progress bar --
    # override ``_build_progress_header`` to customize the header card.
    show_progress_bar = True

    # // ----( Navigation-button customization )---- // #
    # Every navigation button on every wizard step is built from these
    # class attributes. The back, next, and finish triples together form
    # the full customization surface.
    back_button_label = "Previous"
    back_button_emoji = "\u2b05\ufe0f"  # ⬅️
    back_button_style = discord.ButtonStyle.secondary
    next_button_label = "Continue"
    next_button_emoji = "\u27a1\ufe0f"  # ➡️
    next_button_style = discord.ButtonStyle.primary
    finish_button_label = "Create Character"
    finish_button_emoji = "\U0001f3b2"  # 🎲
    finish_button_style = discord.ButtonStyle.success

    def __init__(self, *args, **kwargs):
        # Character sheet state. Every step reads from and writes to
        # these attributes; the review step composes them into a card.
        self._name: str = ""
        self._race: str = ""
        self._class: str = ""
        self._subclass: str = ""
        self._scores: dict[str, int] = {a: STARTING_SCORE for a in ABILITIES}
        self._backstory: str = ""
        self._alignment: str = ""
        self._languages: list[str] = []
        self._heroic_destiny: bool = False

        # Each step is a ``WizardStep`` dataclass. Builders are passed as
        # bound-method references (``self.build_identity``, not
        # ``self.build_identity()``) -- the wizard calls them each time a
        # step renders, reading whatever the instance attributes above hold
        # at that moment. ``WizardStep`` validates ``name`` non-empty and
        # ``builder``/``validator``/``condition`` callability at
        # construction, so an accidental trailing ``()`` raises
        # ``ValueError`` at class-load time rather than on the first click.
        # Review has no validator because the finish button runs on the
        # last step's accumulated state.
        steps = [
            WizardStep(
                name="Identity",
                builder=self.build_identity,
                validator=self.validate_identity,
            ),
            WizardStep(
                name="Class",
                builder=self.build_class,
                validator=self.validate_class,
            ),
            WizardStep(
                name="Abilities",
                builder=self.build_abilities,
                validator=self.validate_abilities,
            ),
            WizardStep(
                name="Background",
                builder=self.build_background,
                validator=self.validate_background,
            ),
            # The Destiny step is conditional: it only renders when the
            # Heroic Destiny toggle on the Background step is set.
            # ``condition`` receives the live view so reading
            # ``v._heroic_destiny`` reflects whatever the Background step
            # wrote last. Conditions are re-evaluated on every
            # navigation, so toggling the destiny flag back off hides the
            # step immediately and the step indicator re-flows.
            WizardStep(
                name="Destiny",
                builder=self.build_destiny,
                condition=lambda v: v._heroic_destiny,
            ),
            WizardStep(name="Review", builder=self.build_review),
        ]
        # Per-step analytics counters -- ``on_validation_failed`` records
        # validator rejections so post-hoc analysis can see which step
        # the user is repeatedly bouncing off.
        self._validation_failures: dict[int, int] = {}
        super().__init__(*args, steps=steps, **kwargs)

    # // ========================================( Lifecycle hooks )======================================== // #

    async def on_step_entered(self, step_index: int):
        """Fires after each step becomes active (initial send, next, back).

        Fire-and-forget -- exceptions raised here are logged but do not
        block navigation. Common uses: analytics, prefetch, per-step
        side effects that do not belong in the builder itself.
        """
        logger.info(
            "Wizard step entered: user=%s step=%s/%s",
            self.user_id,
            step_index + 1,
            self.step_count,
        )

    async def on_validation_failed(self, step_index: int, error: str, interaction):
        """Fires when the current step's validator returns ``(False, error)``.

        The third ``interaction`` argument is the raw Discord interaction
        that tripped the validator -- forward it to ``self.respond(...)``
        when the override needs to surface a custom error to the user.
        Useful for counting retry loops, surfacing stuck users to a
        moderator channel, or gating retries behind a cooldown.
        """
        self._validation_failures[step_index] = self._validation_failures.get(step_index, 0) + 1

    # // ========================================( Derived helpers )======================================== // #

    @property
    def _points_spent(self) -> int:
        return sum(self._scores.values()) - STARTING_SCORE * len(ABILITIES)

    @property
    def _points_remaining(self) -> int:
        return POINT_POOL - self._points_spent

    def _available_classes(self) -> list[str]:
        return CLASSES_BY_RACE.get(self._race, [])

    def _available_subclasses(self) -> list[str]:
        return SUBCLASSES_BY_CLASS.get(self._class, [])

    # // ========================================( Step 1 - Identity )======================================== // #

    async def build_identity(self):
        """Name button + race select.

        The name is captured through a modal so a paragraph of free text
        does not have to be squeezed into a select option. The race
        select rebuilds on each change so the chosen option is visible
        in the card heading below.
        """
        name_display = f"**{self._name}**" if self._name else "_not set_"
        race_display = f"**{self._race}**" if self._race else "_not set_"

        body = card(
            "## \U0001f3ad Identity",
            TextDisplay(
                "Every adventurer starts with a name and a lineage. Pick "
                "both here - the available classes on the next step depend "
                "on the race chosen."
            ),
            divider(),
            key_value({"Name": name_display, "Race": race_display}),
            color=discord.Color.blurple(),
        )

        name_btn = StatefulButton(
            label="Enter Name",
            style=discord.ButtonStyle.primary,
            emoji="\u270d\ufe0f",
            callback=self._open_name_modal,
        )

        race_select = StatefulSelect(
            placeholder="Choose a race...",
            options=[
                discord.SelectOption(label=race, value=race, default=(race == self._race))
                for race in RACES
            ],
            callback=self._on_race_selected,
        )

        return [body, ActionRow(name_btn), ActionRow(race_select)]

    async def _open_name_modal(self, interaction):
        """Open a modal that writes the submitted text back to ``self._name``."""
        name_input = TextInput(
            label="Character Name",
            placeholder="e.g. Kael Ironbeard",
            default=self._name or None,
            required=True,
            min_length=1,
            max_length=40,
        )

        async def on_submitted(modal_interaction, values):
            # The ``name_input`` reference is captured by this closure at
            # modal construction time; ``name_input.value`` holds the
            # submitted text after Discord delivers the modal payload.
            self._name = (name_input.value or "").strip()
            await self._refresh_wizard()

        modal = Modal(
            title="Name your character",
            inputs=[name_input],
            callback=on_submitted,
        )
        await self.open_modal(interaction, modal)

    async def _on_race_selected(self, interaction, values):
        new_race = values[0]
        if new_race != self._race:
            # Changing race invalidates any previous class and subclass
            # because the class pool is gated by race.
            self._race = new_race
            self._class = ""
            self._subclass = ""
        await self._refresh_wizard()

    async def validate_identity(self):
        if not self._name:
            return False, "Enter a character name before continuing."
        if not self._race:
            return False, "Choose a race before continuing."
        return True, ""

    # // ========================================( Step 2 - Class )======================================== // #

    async def build_class(self):
        """Class + subclass selects, gated by the prior step's race.

        The subclass select renders as a disabled placeholder until a
        class has been picked, so the dependency chain is obvious.
        """
        class_display = f"**{self._class}**" if self._class else "_not set_"
        subclass_display = f"**{self._subclass}**" if self._subclass else "_not set_"

        body = card(
            f"## \u2694\ufe0f Class - {self._race}",
            TextDisplay(
                f"A {self._race} can train as any of the classes below. "
                "Subclass options appear once a class is chosen."
            ),
            divider(),
            key_value({"Class": class_display, "Subclass": subclass_display}),
            color=discord.Color.dark_red(),
        )

        class_options = [
            discord.SelectOption(label=c, value=c, default=(c == self._class))
            for c in self._available_classes()
        ]
        class_select = StatefulSelect(
            placeholder="Choose a class...",
            options=class_options,
            callback=self._on_class_selected,
        )

        rows: list = [body, ActionRow(class_select)]

        if self._class:
            subclass_options = [
                discord.SelectOption(label=s, value=s, default=(s == self._subclass))
                for s in self._available_subclasses()
            ]
            subclass_select = StatefulSelect(
                placeholder="Choose a subclass...",
                options=subclass_options,
                callback=self._on_subclass_selected,
            )
            rows.append(ActionRow(subclass_select))

        return rows

    async def _on_class_selected(self, interaction, values):
        new_class = values[0]
        if new_class != self._class:
            # Subclass pool is gated by class, so changing class clears
            # any stale subclass choice.
            self._class = new_class
            self._subclass = ""
        await self._refresh_wizard()

    async def _on_subclass_selected(self, interaction, values):
        self._subclass = values[0]
        await self._refresh_wizard()

    async def validate_class(self):
        if not self._class:
            return False, "Choose a class before continuing."
        if not self._subclass:
            return False, "Choose a subclass before continuing."
        return True, ""

    # // ========================================( Step 3 - Abilities )======================================== // #

    async def build_abilities(self):
        """Point-pool allocation across six attributes.

        Every attribute starts at 8 and a pool of 6 points is available.
        The select increments the chosen ability by 1; the reset button
        zeroes the allocation so the user can start over.
        """
        lines = [f"{ABILITY_NAMES[a]}: **{self._scores[a]}**" for a in ABILITIES]
        pool_line = f"**Points remaining:** {self._points_remaining} / {POINT_POOL}"

        body = card(
            "## \U0001f4ca Attributes",
            TextDisplay(
                f"Every attribute starts at {STARTING_SCORE}. Spend all "
                f"{POINT_POOL} points by increasing the attributes of "
                "your choice. Values cap at "
                f"{MAX_SCORE}, and the Continue button stays locked "
                "until the pool is empty."
            ),
            divider(),
            TextDisplay("\n".join(lines)),
            divider(),
            TextDisplay(pool_line),
            color=discord.Color.gold(),
        )

        # Only abilities that are below the cap and that the pool can
        # still afford are offered as increment targets. When the filter
        # produces an empty list, ``StatefulSelect`` substitutes a
        # disabled placeholder automatically, so no fallback branch is
        # needed here -- the placeholder text alone communicates state.
        eligible = [
            a for a in ABILITIES if self._scores[a] < MAX_SCORE and self._points_remaining > 0
        ]
        if eligible:
            placeholder = "Spend a point on..."
        elif self._points_remaining == 0:
            placeholder = "Pool empty -- press Continue or Reset"
        else:
            placeholder = "Every score is at the cap -- press Reset"

        increment_select = StatefulSelect(
            placeholder=placeholder,
            options=[
                discord.SelectOption(
                    label=f"+1 {ABILITY_NAMES[a]} (now {self._scores[a]} → {self._scores[a] + 1})",
                    value=a,
                )
                for a in eligible
            ],
            callback=self._on_point_spent,
        )
        select_row = ActionRow(increment_select)

        reset_btn = StatefulButton(
            label="Reset",
            style=discord.ButtonStyle.secondary,
            emoji="\u21a9\ufe0f",
            callback=self._reset_scores,
        )

        return [body, select_row, ActionRow(reset_btn)]

    async def _on_point_spent(self, interaction, values):
        ability = values[0]
        if ability in self._scores and self._points_remaining > 0:
            if self._scores[ability] < MAX_SCORE:
                self._scores[ability] += 1
        await self._refresh_wizard()

    async def _reset_scores(self, interaction):
        self._scores = {a: STARTING_SCORE for a in ABILITIES}
        await self._refresh_wizard()

    async def validate_abilities(self):
        if self._points_remaining != 0:
            return (
                False,
                f"Allocate every point before continuing " f"({self._points_remaining} remaining).",
            )
        return True, ""

    # // ========================================( Step 4 - Background )======================================== // #

    async def build_background(self):
        """Backstory, alignment, languages, and heroic destiny.

        Structured choices (alignment, languages, destiny) are inline
        components on the step page. Free-form text (backstory) opens a
        modal -- modals are the right tool for paragraph-length input,
        while selects and toggles work better inline where the user can
        see every option at a glance.
        """
        # Backstory preview
        if self._backstory:
            preview = self._backstory
            if len(preview) > 300:
                preview = preview[:297] + "..."
            backstory_display = f"> {preview}"
        else:
            backstory_display = "_Not written yet._"

        body = card(
            "## \U0001f4dc Background",
            TextDisplay(
                "Every adventurer carries a history. Choose an alignment, "
                "select the languages this character knows, and write a "
                "backstory to bring them to life."
            ),
            divider(),
            key_value({"Backstory": backstory_display}),
            color=discord.Color.dark_purple(),
        )

        backstory_btn = StatefulButton(
            label="Edit Backstory" if self._backstory else "Write Backstory",
            style=discord.ButtonStyle.primary,
            emoji="\U0001f4dd",
            callback=self._open_backstory_modal,
        )

        alignment_select = StatefulSelect(
            placeholder="Choose an alignment...",
            options=[
                discord.SelectOption(label=a, value=a, default=(a == self._alignment))
                for a in ALIGNMENTS
            ],
            callback=self._on_alignment_selected,
        )

        # Pre-select Common (always known) and any racial languages
        # based on the race chosen in step 1. Previously selected
        # languages are preserved across step rebuilds.
        racial_defaults = {"Common"} | set(RACIAL_LANGUAGES.get(self._race, []))
        selected = set(self._languages) if self._languages else racial_defaults
        language_select = StatefulSelect(
            placeholder="Select languages known...",
            options=[
                discord.SelectOption(label=lang, value=lang, default=(lang in selected))
                for lang in LANGUAGES
            ],
            min_values=1,
            max_values=len(LANGUAGES),
            callback=self._on_languages_selected,
        )

        # ``toggle_section`` auto-selects the Enabled/Disabled label and
        # green/red style from ``active=``, so the destiny row collapses
        # to one call instead of a hand-rolled label/style branch + card.
        destiny_row = toggle_section(
            "**Heroic Destiny** -- fate has marked this character for greatness",
            active=self._heroic_destiny,
            callback=self._on_destiny_toggled,
        )

        return [
            body,
            ActionRow(backstory_btn),
            ActionRow(alignment_select),
            ActionRow(language_select),
            destiny_row,
        ]

    async def _open_backstory_modal(self, interaction):
        """Open a modal for the backstory paragraph."""
        backstory_input = TextInput(
            label="Backstory",
            placeholder="Where did your character come from? What drives them?",
            default=self._backstory or None,
            required=True,
            min_length=20,
            max_length=1500,
            style=discord.TextStyle.paragraph,
        )

        async def on_submitted(modal_interaction, values):
            self._backstory = (backstory_input.value or "").strip()
            await self._refresh_wizard()

        modal = Modal(
            title="Character Backstory",
            inputs=[backstory_input],
            callback=on_submitted,
        )
        await self.open_modal(interaction, modal)

    async def _on_alignment_selected(self, interaction, values):
        self._alignment = values[0]
        await self._refresh_wizard()

    async def _on_languages_selected(self, interaction, values):
        self._languages = list(values)
        await self._refresh_wizard()

    async def _on_destiny_toggled(self, interaction):
        self._heroic_destiny = not self._heroic_destiny
        await self._refresh_wizard()

    async def validate_background(self):
        if len(self._backstory) < 20:
            return False, "Write at least 20 characters of backstory before continuing."
        if not self._alignment:
            return False, "Choose an alignment before continuing."
        if not self._languages:
            return False, "Select at least one language before continuing."
        return True, ""

    # // ========================================( Step 5 - Destiny (conditional) )======================================== // #

    async def build_destiny(self):
        """Flavor card rendered only when Heroic Destiny is enabled.

        Reached via the conditional ``condition=lambda v: v._heroic_destiny``
        on the step definition. No validator -- a user who flips Heroic
        Destiny off on an earlier back-nav simply stops seeing this step
        on re-entry; no stale required-field gate blocks them.
        """
        return [
            card(
                "## \u2728 Heroic Destiny",
                TextDisplay(
                    f"A prophecy has marked **{self._name}** for greatness. "
                    "When the campaign begins, the DM will consult the "
                    "*Book of Fates* and assign a personal destiny arc "
                    "tied to the character's chosen alignment."
                ),
                color=discord.Color.gold(),
            ),
        ]

    # // ========================================( Step 6 - Review )======================================== // #

    async def build_review(self):
        """Full character sheet composed from every prior step's state."""
        stats_line = " · ".join(f"{a} {self._scores[a]}" for a in ABILITIES)
        languages_line = ", ".join(self._languages) if self._languages else "None"

        backstory_preview = self._backstory
        if len(backstory_preview) > 400:
            backstory_preview = backstory_preview[:397] + "..."

        sheet = card(
            f"## \U0001f4dc {self._name}",
            TextDisplay(f"*{self._race} {self._class} - {self._subclass}*"),
            divider(),
            key_value(
                {
                    "Attributes": stats_line,
                    "Alignment": self._alignment,
                    "Languages": languages_line,
                    "Heroic Destiny": "Yes" if self._heroic_destiny else "No",
                }
            ),
            divider(),
            TextDisplay(f"**Backstory**\n> {backstory_preview}"),
            divider(),
            TextDisplay(
                "-# Press **Create Character** to finalize the sheet, or "
                "**Previous** to revisit any earlier step."
            ),
            color=discord.Color.green(),
        )
        return [sheet]

    # // ========================================( Finish )======================================== // #

    async def on_finish(self, interaction):
        """Post the finalized character sheet as an ephemeral card followup.

        ``WizardLayoutView.on_finish`` is the method hook that fires when
        the user clicks the finish button on the last step. Overriding
        it replaces the default exit-only behavior with a custom flow that
        echoes the completed sheet back to the user.

        The trailing ``await self.exit()`` respects
        ``exit_policy = "delete"``, so the wizard message is removed after
        the followup is sent. Setting
        ``exit_policy = "disable"`` would freeze the final review card
        in place instead of deleting it.
        """
        stats_line = " · ".join(f"{a} {self._scores[a]}" for a in ABILITIES)
        destiny_tag = " *(Hero of Destiny)*" if self._heroic_destiny else ""

        # Build a confirmation view with a single card summarizing the
        # finished character. DisplayLayoutView renders a pre-built
        # container without requiring a full subclass.
        body = card(
            f"## \U0001f3b2 {self._name}{destiny_tag}",
            TextDisplay(f"*{self._race} {self._class} - {self._subclass}*"),
            divider(),
            key_value(
                {
                    "Attributes": stats_line,
                    "Alignment": self._alignment,
                    "Languages": ", ".join(self._languages),
                }
            ),
            divider(),
            TextDisplay("-# Character created successfully."),
            color=discord.Color.green(),
        )
        await self.respond(interaction, view=DisplayLayoutView(container=body), ephemeral=True)
        await self.exit()


# // ========================================( Cog )======================================== // #


class V2WizardExample(commands.Cog, name="v2_wizard_example"):
    """D&D character creator showcasing the V2 multi-step wizard pattern."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2wizard",
        description="Start a five-step D&D character creator.",
    )
    async def v2wizard(self, context: Context) -> None:
        """Open the character creator wizard.

        Five steps lead from identity through class, abilities, and
        background to a final review card. Each step validates its own
        inputs before the wizard allows progression, and the finish
        button posts the completed sheet back to the invoking user.
        """
        view = CharacterCreatorView(context=context)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2WizardExample(bot=bot))
