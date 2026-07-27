"""
V2 Wizard -- D&D Character Creator
==================================

A multi-step character creator that demonstrates ``WizardLayoutView``
handling the full range of wizard features in a single, richly composed
flow:

    - A live character-sheet preview card, shown on every step, that fills
      in as choices are made (with a name-seeded portrait via ``image_section``)
    - Controls folded into titled cards (``action_section`` / ``choice_row`` /
      ``toggle_section``) rather than bare rows floating beneath a text card
    - ``choice_row`` segmented controls that highlight the active option and
      auto-fold to a dropdown once the option set outgrows a button row
    - Cascading choices where later options depend on earlier ones
    - Structured modal inputs across all five wrapper types: the name
      modal pairs a ``TextInput`` with an optional ``FileUpload`` portrait
      (the upload replaces the generated preview image), and the
      background modal combines a paragraph ``TextInput`` with a
      ``RadioGroup``, a ``CheckboxGroup``, and a ``Checkbox`` in one form
    - Point-pool allocation with a live ``progress_bar``
    - Per-step validators that block progression with fail-loud errors
    - Navigation-button customization via the
      ``back/next/finish_button_{label,emoji,style}`` triples
    - ``on_finish`` as a method hook that posts the finished sheet

Emoji use Python's ``\\N{NAME}`` named escapes throughout: they read
clearly in source, grep cleanly, and avoid the raw-glyph pitfalls that
bite copy-paste and search-and-replace.

Commands:
    /v2wizard   Start the character creator

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import logging
from urllib.parse import quote

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    Checkbox,
    CheckboxGroup,
    DisplayLayoutView,
    FileUpload,
    Modal,
    RadioGroup,
    StatefulSelect,
    TextInput,
    WizardLayoutView,
    WizardStep,
    action_section,
    card,
    choice_row,
    divider,
    image_section,
    key_value,
    progress_bar,
    toggle_section,
)

# // ========================================( Character data )======================================== // #


logger = logging.getLogger(__name__)


RACES = ["Human", "Elf", "Dwarf", "Halfling"]

# Class availability is gated by race: Halflings cannot be Paladins,
# Dwarves cannot be Wizards, and so on. Picking a race narrows the class
# options on the next step.
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
# in the background step based on the chosen race.
LANGUAGES = ["Common", "Elvish", "Dwarvish", "Halfling", "Draconic", "Infernal", "Celestial"]
RACIAL_LANGUAGES = {
    "Human": [],
    "Elf": ["Elvish"],
    "Dwarf": ["Dwarvish"],
    "Halfling": ["Halfling"],
}

# Origin and tool proficiencies are collected inside the background modal
# via RadioGroup and CheckboxGroup, so the step card stays compact while
# the modal carries the structured detail.
ORIGINS = ["Noble", "Commoner", "Outlander"]
TOOLS = ["Smith's tools", "Thieves' tools", "Herbalism kit", "Cartographer's tools"]

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
    """Multi-step D&D character creator built on ``WizardLayoutView``.

    Step order:
        1. Identity     -- Name (modal) and race
        2. Class        -- Race-gated class and class-gated subclass
        3. Attributes   -- Point-pool allocation across six stats
        4. Background   -- Alignment, languages, destiny, and a backstory modal
        5. Destiny      -- Conditional flavor step (Heroic Destiny only)
        6. Review       -- Full character sheet summary
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
    # ``show_progress_bar = True`` tells ``WizardLayoutView`` to render a
    # proportional progress bar above every step.
    show_progress_bar = True

    # // ----( Navigation-button customization )---- // #
    # Every navigation button on every wizard step is built from these
    # class attributes. The back, next, and finish triples together form
    # the full customization surface.
    back_button_label = "Previous"
    back_button_emoji = "\N{LEFTWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}"
    back_button_style = discord.ButtonStyle.secondary
    next_button_label = "Continue"
    next_button_emoji = "\N{BLACK RIGHTWARDS ARROW}\N{VARIATION SELECTOR-16}"
    next_button_style = discord.ButtonStyle.primary
    finish_button_label = "Create Character"
    finish_button_emoji = "\N{GAME DIE}"
    finish_button_style = discord.ButtonStyle.success

    def __init__(self, *args, **kwargs):
        # Character sheet state. Every step reads from and writes to
        # these attributes; the preview and review compose them into cards.
        self._name: str = ""
        self._portrait: str = ""  # Uploaded portrait URL; falls back to DiceBear
        self._race: str = ""
        self._class: str = ""
        self._subclass: str = ""
        self._scores: dict[str, int] = {a: STARTING_SCORE for a in ABILITIES}
        self._backstory: str = ""
        self._origin: str = ""
        self._tools: list[str] = []
        self._haunted: bool = False
        self._alignment: str = ""
        self._languages: list[str] = []
        self._heroic_destiny: bool = False

        # Each step is a ``WizardStep`` dataclass. Builders are passed as
        # bound-method references (``self.build_identity``, not
        # ``self.build_identity()``) -- the wizard calls them each time a
        # step renders, reading whatever the instance attributes above hold
        # at that moment. An accidental trailing ``()`` raises ``ValueError``
        # at class-load time rather than on the first click. Review has no
        # validator because the finish button runs on the last step.
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
            # The Destiny step is conditional: it renders only when the
            # Heroic Destiny toggle on the Background step is set. Conditions
            # are re-evaluated on every navigation, so toggling the flag off
            # hides the step immediately and the step indicator re-flows.
            WizardStep(
                name="Destiny",
                builder=self.build_destiny,
                condition=lambda v: v._heroic_destiny,
            ),
            WizardStep(name="Review", builder=self.build_review),
        ]
        # Per-step analytics counters -- ``on_validation_failed`` records
        # validator rejections so post-hoc analysis can see which step the
        # user is repeatedly bouncing off.
        self._validation_failures: dict[int, int] = {}
        super().__init__(*args, steps=steps, **kwargs)

    # // ========================================( Lifecycle hooks )======================================== // #

    async def on_step_entered(self, step_index: int):
        """Fires after each step becomes active (initial send, next, back).

        Fire-and-forget -- exceptions raised here are logged but do not
        block navigation. Common uses: analytics, prefetch, per-step side
        effects that do not belong in the builder itself.
        """
        logger.info(
            "Wizard step entered: user=%s step=%s/%s",
            self.user_id,
            step_index + 1,
            self.step_count,
        )

    async def on_validation_failed(self, step_index: int, error: str, interaction):
        """Fires when the current step's validator returns ``(False, error)``.

        The counter here is additive analytics. The ``super()`` call is what
        preserves the built-in behavior: the base hook sends ``error`` to the
        user as an ephemeral response. An override that skips it silently hides
        why the wizard will not advance, so always call up unless you replace
        the feedback yourself with ``self.respond(interaction, ...)``.
        """
        self._validation_failures[step_index] = self._validation_failures.get(step_index, 0) + 1
        await super().on_validation_failed(step_index, error, interaction)

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

    # // ========================================( Live character sheet )======================================== // #

    def _portrait_url(self) -> str:
        """A deterministic character portrait seeded by the name.

        Uses DiceBear's ``adventurer`` style, so the portrait is stable for
        a given name and changes the moment the name does. A ``Section``
        accessory cannot be null, so a race/class fallback seed keeps the
        thumbnail present before a name is entered.
        """
        seed = self._name or f"{self._race or 'hero'}-{self._class or 'adventurer'}"
        return f"https://api.dicebear.com/9.x/adventurer/png?seed={quote(seed)}&size=256"

    def _build_sheet_preview(self):
        """The live character sheet, shown on top of every step.

        This panel is what makes the wizard read like an app rather than a
        form: every choice appears here immediately, so the character takes
        shape as the user moves through the steps. Only fields that have
        been filled render, so the card grows as the flow progresses.
        """
        name = self._name or "Unnamed Hero"
        lineage = f"{self._race} {self._class}".strip()
        subclass = f" \N{MIDDLE DOT} {self._subclass}" if self._subclass else ""
        summary = f"*{lineage}{subclass}*" if self._race else "*Pick a race to begin your legend.*"

        fields: dict[str, str] = {}
        if self._race:
            fields["Race"] = self._race
        if self._class:
            fields["Class"] = f"{self._class} ({self._subclass})" if self._subclass else self._class
        if self._points_remaining != POINT_POOL:
            fields["Attributes"] = " \N{MIDDLE DOT} ".join(
                f"{a} {self._scores[a]}" for a in ABILITIES
            )
        if self._alignment:
            fields["Alignment"] = self._alignment
        if self._origin or self._haunted:
            quirk = "haunted past" if self._haunted else ""
            fields["Origin"] = " \N{MIDDLE DOT} ".join(p for p in (self._origin, quirk) if p)
        if self._tools:
            fields["Tools"] = ", ".join(self._tools)
        if self._languages:
            fields["Languages"] = ", ".join(self._languages)
        if self._heroic_destiny:
            fields["Destiny"] = "Marked for greatness"

        # An uploaded portrait (from the name modal's FileUpload) replaces
        # the generated one the moment it lands.
        portrait = self._portrait or self._portrait_url()
        children: list = [image_section(f"### \N{SCROLL} {name}\n{summary}", url=portrait)]
        if fields:
            children.append(divider())
            children.append(key_value(fields))
        return card(*children, color=discord.Color.dark_teal())

    # // ========================================( Step 1 - Identity )======================================== // #

    async def build_identity(self):
        """Name (modal, folded into a card) + race as a segmented choice_row.

        The name opens a modal so a paragraph of free text does not have to
        squeeze into a select option; the button that opens it is folded into
        the card via ``action_section`` rather than left floating. Race is a
        ``choice_row`` so all four options show at once as segmented buttons.
        """
        name_summary = f"**Name:** {self._name}" if self._name else "**Name:** _not set_"

        controls = card(
            "## \N{PERFORMING ARTS} Identity",
            TextDisplay(
                "Every adventurer starts with a name and a lineage. The "
                "classes available on the next step depend on the race chosen."
            ),
            divider(),
            action_section(
                name_summary,
                label="Edit Name" if self._name else "Enter Name",
                callback=self._open_name_modal,
                style=discord.ButtonStyle.primary,
                emoji="\N{WRITING HAND}\N{VARIATION SELECTOR-16}",
            ),
            choice_row(
                {race: race for race in RACES},
                on_select=self._on_race_selected,
                selected=self._race or None,
                custom_id="wiz_race",
            ),
            color=discord.Color.blurple(),
        )
        return [self._build_sheet_preview(), controls]

    def _build_name_modal(self) -> Modal:
        """Compose the identity modal: name text plus an optional portrait.

        ``FileUpload`` demonstrates a structured modal input beyond text:
        the submitted attachment's URL replaces the generated portrait in
        the live sheet preview. Attachment URLs are CDN links tied to the
        upload, so the swap holds for the life of the session.
        """
        name_input = TextInput(
            label="Character Name",
            placeholder="e.g. Kael Ironbeard",
            default=self._name or None,
            required=True,
            min_length=1,
            max_length=40,
        )
        portrait_input = FileUpload(
            label="Portrait",
            description="Optional: upload an image to replace the generated portrait.",
            required=False,
            max_values=1,
        )

        async def on_submitted(modal_interaction, values):
            # The wrapper instances are captured by this closure at modal
            # construction time; ``.value`` / ``.values`` hold the submitted
            # payload after Discord delivers it.
            self._name = (name_input.value or "").strip()
            if portrait_input.values:
                self._portrait = portrait_input.values[0].url
            await self.refresh_content()

        return Modal(
            title="Name your character",
            inputs=[name_input, portrait_input],
            callback=on_submitted,
        )

    async def _open_name_modal(self, interaction):
        await self.open_modal(interaction, self._build_name_modal())

    async def _on_race_selected(self, interaction, value):
        if value != self._race:
            # Changing race invalidates any previous class and subclass
            # because the class pool is gated by race.
            self._race = value
            self._class = ""
            self._subclass = ""
        await self.refresh_content()

    async def validate_identity(self):
        if not self._name:
            return False, "Enter a character name before continuing."
        if not self._race:
            return False, "Choose a race before continuing."
        return True, ""

    # // ========================================( Step 2 - Class )======================================== // #

    async def build_class(self):
        """Class + subclass as segmented choice_rows, gated by the race.

        The subclass row appears only once a class is chosen, so the
        dependency chain is obvious.
        """
        children: list = [
            "## \N{CROSSED SWORDS}\N{VARIATION SELECTOR-16} Class",
            TextDisplay(
                f"A **{self._race}** can train as any of the classes below. "
                "A subclass appears once a class is chosen."
            ),
            divider(),
            choice_row(
                {c: c for c in self._available_classes()},
                on_select=self._on_class_selected,
                selected=self._class or None,
                custom_id="wiz_class",
            ),
        ]
        if self._class:
            children.append(TextDisplay("**Subclass**"))
            children.append(
                choice_row(
                    {s: s for s in self._available_subclasses()},
                    on_select=self._on_subclass_selected,
                    selected=self._subclass or None,
                    custom_id="wiz_subclass",
                )
            )
        controls = card(*children, color=discord.Color.dark_red())
        return [self._build_sheet_preview(), controls]

    async def _on_class_selected(self, interaction, value):
        if value != self._class:
            # Subclass pool is gated by class, so changing class clears
            # any stale subclass choice.
            self._class = value
            self._subclass = ""
        await self.refresh_content()

    async def _on_subclass_selected(self, interaction, value):
        self._subclass = value
        await self.refresh_content()

    async def validate_class(self):
        if not self._class:
            return False, "Choose a class before continuing."
        if not self._subclass:
            return False, "Choose a subclass before continuing."
        return True, ""

    # // ========================================( Step 3 - Abilities )======================================== // #

    async def build_abilities(self):
        """Point-pool allocation across six attributes.

        The pool is shown as a live ``progress_bar``; the increment select
        keeps its dynamic labels (too long for buttons), and Reset is folded
        into the card via ``action_section``.
        """
        lines = "  \N{MIDDLE DOT}  ".join(f"{a} **{self._scores[a]}**" for a in ABILITIES)

        # Only abilities below the cap that the pool can still afford are
        # offered. An empty list yields a disabled placeholder automatically.
        eligible = [
            a for a in ABILITIES if self._scores[a] < MAX_SCORE and self._points_remaining > 0
        ]
        if eligible:
            placeholder = "Spend a point on..."
        elif self._points_remaining == 0:
            placeholder = "Pool empty - press Continue"
        else:
            placeholder = "Every score is at the cap - press Reset"

        increment_select = StatefulSelect(
            placeholder=placeholder,
            options=[
                discord.SelectOption(
                    label=f"+1 {ABILITY_NAMES[a]} (now {self._scores[a]} \N{RIGHTWARDS ARROW} {self._scores[a] + 1})",
                    value=a,
                )
                for a in eligible
            ],
            callback=self._on_point_spent,
        )

        controls = card(
            "## \N{BAR CHART} Attributes",
            TextDisplay(
                f"Every attribute starts at {STARTING_SCORE}. Spend all {POINT_POOL} "
                f"points; scores cap at {MAX_SCORE}. Continue unlocks when the pool is empty."
            ),
            divider(),
            TextDisplay(lines),
            progress_bar(self._points_spent, POINT_POOL, width=12, show_percent=False),
            TextDisplay(f"-# {self._points_remaining} of {POINT_POOL} points remaining"),
            ActionRow(increment_select),
            action_section(
                "Start the allocation over.",
                label="Reset",
                callback=self._reset_scores,
                style=discord.ButtonStyle.secondary,
                emoji="\N{LEFTWARDS ARROW WITH HOOK}\N{VARIATION SELECTOR-16}",
            ),
            color=discord.Color.gold(),
        )
        return [self._build_sheet_preview(), controls]

    async def _on_point_spent(self, interaction, values):
        ability = values[0]
        if ability in self._scores and self._points_remaining > 0:
            if self._scores[ability] < MAX_SCORE:
                self._scores[ability] += 1
        await self.refresh_content()

    async def _reset_scores(self, interaction):
        self._scores = {a: STARTING_SCORE for a in ABILITIES}
        await self.refresh_content()

    async def validate_abilities(self):
        if self._points_remaining != 0:
            return (
                False,
                f"Allocate every point before continuing ({self._points_remaining} remaining).",
            )
        return True, ""

    # // ========================================( Step 4 - Background )======================================== // #

    async def build_background(self):
        """Alignment, languages, destiny, and a background modal, one card.

        Alignment and languages are ``choice_row``s (both outgrow a button
        row, so they fold to dropdowns); Heroic Destiny is a
        ``toggle_section``. The backstory button opens the structured
        background modal -- paragraph text, origin radio, tool checkboxes,
        and a quirk flag in a single form (see ``_build_background_modal``).
        """
        # Common (always known) plus any racial languages are known by
        # default. Seed them on first entry so the pre-selected options count
        # as chosen; without this the validator sees an empty list even
        # though the select shows them ticked.
        if not self._languages:
            self._languages = sorted({"Common"} | set(RACIAL_LANGUAGES.get(self._race, [])))

        if self._backstory:
            preview = self._backstory[:200] + ("..." if len(self._backstory) > 200 else "")
            backstory_summary = f"**Backstory:** {preview}"
        else:
            backstory_summary = "**Backstory:** _not written yet_"

        controls = card(
            "## \N{SCROLL} Background",
            TextDisplay(
                "Choose an alignment, the languages this character knows, and "
                "write a backstory to bring them to life."
            ),
            divider(),
            action_section(
                backstory_summary,
                label="Edit Background" if self._backstory else "Write Background",
                callback=self._open_backstory_modal,
                style=discord.ButtonStyle.primary,
                emoji="\N{MEMO}",
            ),
            TextDisplay("**Alignment**"),
            choice_row(
                {a: a for a in ALIGNMENTS},
                on_select=self._on_alignment_selected,
                selected=self._alignment or None,
                custom_id="wiz_alignment",
                placeholder="Choose an alignment...",
            ),
            TextDisplay("**Languages**"),
            choice_row(
                {lang: lang for lang in LANGUAGES},
                on_select=self._on_languages_selected,
                selected=set(self._languages),
                multi=True,
                custom_id="wiz_languages",
                placeholder="Select languages known...",
            ),
            toggle_section(
                "**Heroic Destiny** - fate has marked this character for greatness",
                active=self._heroic_destiny,
                callback=self._on_destiny_toggled,
            ),
            color=discord.Color.dark_purple(),
        )
        return [self._build_sheet_preview(), controls]

    def _build_background_modal(self) -> Modal:
        """Compose the background modal: one form, four input types.

        A paragraph ``TextInput``, a ``RadioGroup`` (pick-one origin), a
        ``CheckboxGroup`` (up to two tool proficiencies), and a ``Checkbox``
        flag share a single modal, so the step card stays compact while
        the form carries the structured detail. Current selections
        pre-fill via each option's ``default`` so re-opening the modal
        shows the character as already built.
        """
        backstory_input = TextInput(
            label="Backstory",
            placeholder="Where did your character come from? What drives them?",
            default=self._backstory or None,
            required=True,
            min_length=20,
            max_length=1500,
            style=discord.TextStyle.paragraph,
        )
        origin_input = RadioGroup(
            label="Origin",
            required=False,
            options=[{"label": o, "value": o, "default": o == self._origin} for o in ORIGINS],
        )
        tools_input = CheckboxGroup(
            label="Tool Proficiencies",
            description="Pick up to two.",
            required=False,
            max_values=2,
            options=[{"label": t, "value": t, "default": t in self._tools} for t in TOOLS],
        )
        haunted_input = Checkbox(
            label="Haunted Past",
            description="Something from long ago still follows this character.",
            default=self._haunted,
        )

        async def on_submitted(modal_interaction, values):
            self._backstory = (backstory_input.value or "").strip()
            self._origin = origin_input.value or ""
            self._tools = list(tools_input.values or [])
            self._haunted = bool(haunted_input.value)
            await self.refresh_content()

        return Modal(
            title="Character Background",
            inputs=[backstory_input, origin_input, tools_input, haunted_input],
            callback=on_submitted,
        )

    async def _open_backstory_modal(self, interaction):
        await self.open_modal(interaction, self._build_background_modal())

    async def _on_alignment_selected(self, interaction, value):
        self._alignment = value
        await self.refresh_content()

    async def _on_languages_selected(self, interaction, values):
        self._languages = sorted(values)
        await self.refresh_content()

    async def _on_destiny_toggled(self, interaction):
        self._heroic_destiny = not self._heroic_destiny
        await self.refresh_content()

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

        Reached via ``condition=lambda v: v._heroic_destiny`` on the step
        definition. No validator -- a user who flips Heroic Destiny off on an
        earlier back-nav simply stops seeing this step on re-entry.
        """
        return [
            self._build_sheet_preview(),
            card(
                "## \N{SPARKLES} Heroic Destiny",
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
        """Final review: the live sheet plus the full backstory and a prompt.

        The preview already shows the assembled stats, so review adds the
        full backstory (which the preview truncates) and the finish prompt.
        """
        backstory = self._backstory
        if len(backstory) > 600:
            backstory = backstory[:597] + "..."

        detail = card(
            "## \N{WHITE HEAVY CHECK MARK} Review",
            TextDisplay(f"**Backstory**\n> {backstory}"),
            divider(),
            TextDisplay(
                "-# Press **Create Character** to finalize the sheet, or "
                "**Previous** to revisit any earlier step."
            ),
            color=discord.Color.green(),
        )
        return [self._build_sheet_preview(), detail]

    # // ========================================( Finish )======================================== // #

    async def on_finish(self, interaction):
        """Post the finalized character sheet as an ephemeral card followup.

        ``WizardLayoutView.on_finish`` fires when the user clicks the finish
        button on the last step. Overriding it replaces the default exit-only
        behavior with a custom flow that echoes the completed sheet back.

        The trailing ``await self.exit()`` respects ``exit_policy = "delete"``,
        so the wizard message is removed after the followup is sent.
        """
        stats_line = " \N{MIDDLE DOT} ".join(f"{a} {self._scores[a]}" for a in ABILITIES)
        destiny_tag = " *(Hero of Destiny)*" if self._heroic_destiny else ""

        body = card(
            f"## \N{GAME DIE} {self._name}{destiny_tag}",
            TextDisplay(f"*{self._race} {self._class} - {self._subclass}*"),
            divider(),
            key_value(
                {
                    "Attributes": stats_line,
                    "Alignment": self._alignment,
                    "Languages": ", ".join(self._languages),
                    **({"Origin": self._origin} if self._origin else {}),
                    **({"Tools": ", ".join(self._tools)} if self._tools else {}),
                }
            ),
            divider(),
            TextDisplay("-# Character created successfully."),
            color=discord.Color.green(),
        )
        # send() registers the summary with the inspector, instance limits, and
        # state cleanup; a raw view= kwarg bypasses all three.
        summary = DisplayLayoutView(container=body, interaction=interaction)
        await summary.send(ephemeral=True)
        await self.exit()


# // ========================================( Cog )======================================== // #


class V2WizardExample(commands.Cog, name="v2_wizard_example"):
    """D&D character creator showcasing the V2 multi-step wizard pattern."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2wizard",
        description="Start a multi-step D&D character creator.",
    )
    async def v2wizard(self, context: Context) -> None:
        """Open the character creator wizard.

        The steps lead from identity through class, abilities, and background
        to a final review card. A live character-sheet preview fills in as
        the flow progresses, and the finish button posts the completed sheet
        back to the invoking user.
        """
        view = CharacterCreatorView(context=context)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2WizardExample(bot=bot))
