"""
V2 Form -- CascadeUI V2 Form & Validation
==========================================

A registration form built entirely from a declarative ``fields=[...]``
list on ``FormLayoutView``. No ``Modal`` subclass, no
``_create_form_controls`` override, no shadow validator map.
``FormLayoutView`` now renders ``"text"`` fields natively through a
grouped "Edit Text Fields" button that opens a single
:class:`cascadeui.Modal` pre-populated from ``self.values``; every
builtin validator factory is demonstrated in one place, along with one
async validator.

Each entry is a :class:`cascadeui.FormField` dataclass. The typed
variant validates ``id``, ``label``, and ``type`` at construction --
a typo like ``type="interger"`` raises ``ValueError`` immediately
instead of surfacing as a render-time mystery. Patterns also accept
the raw ``{...}`` dict shape for one-line prototypes; ``FormField``
is the recommended form once a field list grows past a couple of
entries.

Fields collected:

    - Username     text    min_length + regex + async "already taken" check
    - Email        text    regex
    - Password     text    min_length + regex
    - Age          integer typed numeric range (min_value + max_value)
    - Bio          text    max_length
    - Country      select  choices validator

Four text fields plus one integer field sit within Discord's 5-input
modal ceiling; the ceiling is enforced at construction time by
``FormLayoutView``, so a sixth text-style field would raise
``ValueError`` before any user ever clicked a button.

V2 features on display:

    - Native ``"text"`` field rendering via the grouped edit button
    - Typed ``"integer"`` field with numeric ``min_value`` / ``max_value``
      kwargs -- the pattern parses the string to ``int`` before validation,
      so range checks are type-preserving rather than string-matching
    - Four builtin validator factories (``min_length``, ``max_length``,
      ``regex``, ``choices``) + one async validator (simulated username
      uniqueness check)
    - Field grouping via the ``group`` kwarg -- fields with the same
      group label render together under one ``card()`` per group
    - ``on_field_changed`` hook -- selecting a country pre-fills the
      email field with a default domain hint for the next modal open
    - ``MODAL_SUBMITTED`` dispatch flows through the Redux pipeline, so
      text-edit hops show up in ``/inspect`` History with the form's
      view id as source
    - ``card()``, ``key_value()``, and ``divider()`` presentation via the
      default ``FormLayoutView`` display (no override needed)

Commands:
    /v2form   Open the registration form

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import asyncio

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cascadeui import (
    FormField,
    FormLayoutView,
    ValidationResult,
    choices,
    max_length,
    min_length,
    regex,
)

import logging

logger = logging.getLogger(__name__)


# Pretend this set lives in a database somewhere. The async validator
# below simulates a network round-trip before checking membership.
_TAKEN_USERNAMES = {"admin", "root", "cascadeui", "hollow"}


async def username_available(value, field, all_values):
    """Async validator: simulate a "username already taken" lookup.

    Real deployments would hit a database or HTTP API here; the
    ``asyncio.sleep`` keeps the demo honest about the I/O cost without
    dragging in any external dependencies.
    """
    await asyncio.sleep(0.2)
    if value and value.lower() in _TAKEN_USERNAMES:
        return ValidationResult(valid=False, message="Username is already taken")
    return ValidationResult(valid=True)


# // ========================================( View )======================================== // #


class RegistrationFormView(FormLayoutView):
    """Declarative V2 registration form.

    The entire form is defined by ``fields=[...]``. Text fields render
    via the grouped "Edit Text Fields" button that ``FormLayoutView``
    emits automatically; the select field renders inline as its own
    action row. On submit, ``_handle_submit`` receives the validated
    values and renders a confirmation embed.
    """

    instance_limit = 1
    instance_scope = "user"  # one open form per user, across guilds
    instance_policy = "reject"  # block a second concurrent form for the same user
    instance_limit_message = "You already have a registration form open. Finish or close it first."
    replace_policy = "delete"
    exit_policy = "delete"
    state_scope = None
    owner_only = True
    auto_defer = True

    def __init__(self, *args, **kwargs):
        # Each field is a ``FormField`` dataclass -- the typed construction
        # path catches typos (e.g. ``type="interger"``) at class-load time
        # rather than at first render. ``FormField`` lowers to the same
        # internal dict shape the pattern has always consumed via
        # ``to_dict()``, so every downstream helper keeps working unchanged.
        fields = [
            # Fields carry a ``group`` label so the V2 form renders one
            # ``card()`` per group. Ungrouped fields would render in a
            # single card at the top of the form. The group values here
            # ("Account", "Profile", "Location") are free-form labels --
            # only equality matters for grouping.
            FormField(
                id="username",
                label="Username",
                type="text",
                required=True,
                placeholder="3-20 chars, alphanumeric + underscores",
                min_length=3,
                max_length=20,
                validators=[
                    min_length(3),
                    regex(r"^[a-zA-Z0-9_]+$", "Alphanumeric and underscores only"),
                    username_available,
                ],
                group="Account",
            ),
            FormField(
                id="email",
                label="Email",
                type="text",
                required=True,
                placeholder="you@example.com",
                max_length=100,
                validators=[
                    regex(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", "Must be a valid email address"),
                ],
                group="Account",
            ),
            FormField(
                id="password",
                label="Password",
                type="text",
                required=True,
                placeholder="8+ chars, letters and digits",
                style=discord.TextStyle.short,
                min_length=8,
                max_length=64,
                validators=[
                    min_length(8),
                    regex(r"(?=.*[A-Za-z])(?=.*\d)", "Must contain letters and digits"),
                ],
                group="Account",
            ),
            # The Age field uses the ``integer`` typed variant so the
            # pattern parses and range-checks the value automatically --
            # ``min_value`` / ``max_value`` become numeric constraints,
            # not string validators running after parse.
            FormField(
                id="age",
                label="Age",
                type="integer",
                required=True,
                placeholder="13-120",
                min_value=13,
                max_value=120,
                group="Profile",
            ),
            FormField(
                id="bio",
                label="Bio",
                type="text",
                required=False,
                placeholder="Tell us about yourself (optional)",
                style=discord.TextStyle.paragraph,
                max_length=500,
                validators=[max_length(500)],
                group="Profile",
            ),
            FormField(
                id="country",
                label="Country",
                type="select",
                required=True,
                placeholder="Select your country...",
                options=[
                    {"label": "United States", "value": "us"},
                    {"label": "United Kingdom", "value": "uk"},
                    {"label": "Canada", "value": "ca"},
                    {"label": "Germany", "value": "de"},
                    {"label": "Japan", "value": "jp"},
                ],
                validators=[choices(["us", "uk", "ca", "de", "jp"])],
                group="Location",
            ),
        ]

        super().__init__(
            *args,
            title="Registration Form",
            fields=fields,
            **kwargs,
        )

    async def on_field_changed(self, field_id, old, new):
        """Fires after a field value changes (select, toggle, modal write-back).

        Fire-and-forget -- exceptions are logged by the pattern and do
        not block the state rebuild. The ``old`` / ``new`` pair lets the
        override react to the transition rather than just the current
        value (useful for diff-aware analytics, undo-style fallbacks,
        or skipping work when the value is unchanged).

        Here the email domain is derived from the country choice, so
        picking a country pre-fills the email field with a default
        domain hint the next time the text modal opens.
        """
        if field_id == "country" and new and not self.values.get("email"):
            default_domain = {
                "us": "example.com",
                "uk": "example.co.uk",
                "ca": "example.ca",
                "de": "example.de",
                "jp": "example.co.jp",
            }.get(new, "example.com")
            self.values["email"] = f"@{default_domain}"

    async def on_submit(self, interaction, values):
        """Render a confirmation embed and close the form."""
        embed = discord.Embed(
            title="\u2705 Registration Complete",
            color=discord.Color.green(),
        )
        embed.add_field(name="Username", value=values["username"], inline=True)
        embed.add_field(name="Email", value=values["email"], inline=True)
        embed.add_field(name="Age", value=str(values["age"]), inline=True)
        embed.add_field(name="Country", value=values["country"].upper(), inline=True)
        if values.get("bio"):
            embed.add_field(name="Bio", value=values["bio"], inline=False)
        await self.respond(interaction, embed=embed, ephemeral=True)
        await self.exit()


# // ========================================( Cog )======================================== // #


class V2FormExample(commands.Cog, name="v2_form_example"):
    """V2 declarative registration form with native text-field support."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="v2form",
        description="Open a V2 registration form with declarative fields and full validation.",
    )
    async def v2form(self, context: Context) -> None:
        """Open the registration form.

        Click "Edit Text Fields" to fill in username, email, password,
        age, and bio through a single modal. Pick a country from the
        dropdown, then hit Submit. Four builtin validator factories,
        one typed integer field with numeric range, and one async
        uniqueness check run on submit; failures surface per-field in
        an ephemeral embed.
        """
        view = RegistrationFormView(context=context)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(V2FormExample(bot=bot))
