# // ========================================( Modules )======================================== // #


import logging
from typing import ClassVar, Dict, List, Optional

import discord
from discord.ui import ActionRow, Container, TextDisplay

from ...components.base import DynamicPersistentButton
from ...components.patterns.v2 import card, divider
from ...components.types import EmojiInput
from ...utils.helpers import slugify
from ..layout import StatefulLayoutView
from ..persistent import _PersistentMixin
from .types import RoleCategory

logger = logging.getLogger(__name__)


# // ========================================( Registries )======================================== // #


# Maps category slug -> RoleCategory instance. The button class looks up
# cardinality flags + role list by slug at click time.
_role_category_registry: Dict[str, RoleCategory] = {}

# Maps category slug -> owning view class. The button class looks up the
# view class to dispatch hook classmethods (on_role_assigned, etc.).
# Kept disjoint from the category registry so the two can evolve
# independently if the shape ever needs to diverge.
_role_view_class_registry: Dict[str, type] = {}


def _category_slug(name: str) -> str:
    """Normalize a category name to a custom_id-safe slug."""
    return slugify(name)


# // ========================================( Role Toggle Button )======================================== // #


async def _respond_safe(
    interaction: discord.Interaction, content: Optional[str] = None, **kwargs
) -> None:
    """Send an interaction response, falling back to followup if already acked.

    Mirrors ``_StatefulMixin.respond`` but as a module-level helper
    because ``_RoleToggleButton.on_click`` has no view instance to call
    ``self.respond`` on. Used by every default hook in
    ``_BaseRolesMixin``.
    """
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


class _RoleToggleButton(
    DynamicPersistentButton,
    template=r"roles:(?P<category_slug>[a-z0-9_\-]+):(?P<role_id>[0-9]+)",
):
    """Internal dynamic button that routes role-toggle clicks.

    Declared once at module import; all role buttons in every
    ``RolesLayoutView`` subclass route through this class via the
    template match. At click time the button looks up category metadata
    and the owning view class from the module registries and delegates
    to the view class's ``_handle_role_click`` classmethod.

    Not part of the public surface -- users subclass
    ``RolesLayoutView`` / ``PersistentRolesLayoutView`` instead.
    """

    def __init__(
        self,
        *,
        category_slug: str,
        role_id: int,
        label: Optional[str] = None,
        style: Optional[discord.ButtonStyle] = None,
        emoji: EmojiInput = None,
    ):
        custom_id = f"roles:{category_slug}:{role_id}"
        button = discord.ui.Button(
            label=label or category_slug,
            custom_id=custom_id,
            style=style or discord.ButtonStyle.secondary,
            emoji=emoji,
        )
        super().__init__(button)
        self.category_slug = category_slug
        self.role_id = role_id

    async def on_click(self, interaction: discord.Interaction) -> None:
        category = _role_category_registry.get(self.category_slug)
        view_class = _role_view_class_registry.get(self.category_slug)

        if category is None or view_class is None:
            # Stale button click against a slug that is no longer
            # registered -- usually a code reload dropped the category.
            # Log and ack the interaction so the user does not see a
            # Discord "interaction failed" toast.
            logger.warning(
                f"_RoleToggleButton click with unknown slug {self.category_slug!r}; "
                f"panel state out of sync with code"
            )
            await _respond_safe(
                interaction,
                "This role panel is out of date. An admin needs to re-post it.",
                ephemeral=True,
            )
            return

        await view_class._handle_role_click(interaction, category, self.role_id)


# // ========================================( Shared Mixin )======================================== // #


class _BaseRolesMixin:
    """Shared role-panel machinery for the V2 pair.

    Holds class attributes, format hooks, event hooks, and the
    cardinality-enforcement logic in ``_handle_role_click``. Concrete
    subclasses ship the V2 ``build_ui`` that composes category cards.

    Internal. Not exported. The public hierarchy
    (``RolesLayoutView`` / ``PersistentRolesLayoutView``) is unchanged.

    **API grammar exception:** event hooks (``on_role_assigned``,
    ``on_role_removed``, ``on_role_swap``, ``on_role_required_block``,
    ``on_role_error``) are classmethods, not instance methods. The
    dispatch path goes through ``DynamicPersistentButton`` which has no
    view instance at click time -- the hook classmethods read class
    attributes (``cls.assigned_message``, etc.) and send responses via
    the module-level ``_respond_safe`` helper. Override signature uses
    ``cls`` instead of ``self``; ``super()`` still works normally.
    """

    # Declared by concrete subclasses. Empty list on the base is the
    # abstract default; the pattern validates at __init_subclass__ time
    # that non-base subclasses have at least one category.
    categories: ClassVar[List[RoleCategory]] = []

    # === Heading ===
    title: Optional[str] = "Server Roles"
    subtitle: Optional[str] = None

    # === Mode hints ===
    # Text-size Unicode glyphs (not emoji) so all four hints render at the
    # same visual weight on one line. Emoji code points (U+1F518 RADIO
    # BUTTON et al.) trigger Discord's emoji renderer and produce a glyph
    # noticeably larger than adjacent text characters, breaking visual
    # alignment when paired with the asterisk.
    hint_normal: Optional[str] = None
    hint_exclusive: Optional[str] = "◉"  # ◉ fisheye -- "select one"
    hint_required: Optional[str] = "*"
    hint_exclusive_required: Optional[str] = "◉ *"  # ◉ *

    # === Response messages (format placeholders: {role}, {category}, {removed}) ===
    assigned_message: str = "Gave you **{role}**."
    removed_message: str = "Removed **{role}**."
    required_message: str = "You must keep at least one **{category}** role."
    swap_message: str = "Switched to **{role}** (removed {removed})."
    role_error_message: str = "Could not update roles: {error}"

    # === Registration ===

    def __init_subclass__(cls, **kwargs):
        """Register each category's slug against this view class.

        Collision policy: if the same slug is already registered under a
        different class path (module + qualname), raise ``ValueError``.
        Same path re-registration is a hot reload -- overwrite silently.
        """
        super().__init_subclass__(**kwargs)

        own_categories = cls.__dict__.get("categories")
        if not own_categories:
            # Intermediate mixin subclasses (RolesLayoutView itself,
            # PersistentRolesLayoutView itself) inherit categories=[];
            # only user subclasses declaring their own list register.
            return

        new_path = f"{cls.__module__}.{cls.__qualname__}"
        for category in own_categories:
            if not isinstance(category, RoleCategory):
                raise TypeError(
                    f"{cls.__name__}.categories entries must be RoleCategory "
                    f"instances (got {type(category).__name__})"
                )
            slug = _category_slug(category.name)
            existing_cls = _role_view_class_registry.get(slug)
            if existing_cls is not None and existing_cls is not cls:
                existing_path = f"{existing_cls.__module__}.{existing_cls.__qualname__}"
                if existing_path != new_path:
                    raise ValueError(
                        f"RoleCategory name collision: {category.name!r} "
                        f"(slug {slug!r}) is declared on both "
                        f"{existing_path} and {new_path}. Category names "
                        f"must be globally unique across every "
                        f"RolesLayoutView subclass in the process."
                    )
            _role_category_registry[slug] = category
            _role_view_class_registry[slug] = cls

    # === Format hooks (all classmethods; override for custom rendering) ===

    @classmethod
    def format_category_title(cls, category: RoleCategory) -> str:
        """Heading line for one category. Default: ``f"### {category.name}"``."""
        if category.icon:
            return f"### {category.icon} {category.name}"
        return f"### {category.name}"

    @classmethod
    def format_category_hint(cls, category: RoleCategory) -> Optional[str]:
        """Hint string rendered under the category heading.

        Routes to one of ``hint_normal`` / ``hint_exclusive`` /
        ``hint_required`` / ``hint_exclusive_required`` based on the
        category's boolean cardinality flags. Returns ``None`` when the
        routed hint attribute is ``None`` (no hint rendered).
        """
        if category.exclusive and category.required:
            return cls.hint_exclusive_required
        if category.exclusive:
            return cls.hint_exclusive
        if category.required:
            return cls.hint_required
        return cls.hint_normal

    @classmethod
    def format_button_label(cls, role_name: str, role_id: int, category: RoleCategory) -> str:
        """Button label. Default: ``role_name``."""
        return role_name

    @classmethod
    def format_button_emoji(
        cls, role_name: str, role_id: int, category: RoleCategory
    ) -> EmojiInput:
        """Button emoji. Default: ``None``."""
        return None

    @classmethod
    def format_button_style(
        cls, role_name: str, role_id: int, category: RoleCategory
    ) -> discord.ButtonStyle:
        """Button style. Default: ``category.button_style`` or ``secondary``."""
        return category.button_style or discord.ButtonStyle.secondary

    @classmethod
    def build_category_card(cls, category: RoleCategory) -> Container:
        """Render one category as a Container with title, hint, and buttons.

        Default composes the smaller ``format_*`` hooks. Override only
        when the whole card layout needs to change (different structure,
        multiple rows, custom components); override ``format_*`` hooks
        for decorative tweaks.
        """
        items: list = [TextDisplay(cls.format_category_title(category))]

        hint = cls.format_category_hint(category)
        if hint:
            items.append(TextDisplay(hint))

        if category.description:
            items.append(TextDisplay(category.description))

        items.append(divider())

        slug = _category_slug(category.name)
        buttons = []
        for role_name, role_id in category.roles.items():
            buttons.append(
                _RoleToggleButton(
                    category_slug=slug,
                    role_id=role_id,
                    label=cls.format_button_label(role_name, role_id, category),
                    style=cls.format_button_style(role_name, role_id, category),
                    emoji=cls.format_button_emoji(role_name, role_id, category),
                )
            )
        items.append(ActionRow(*buttons))

        return card(*items, color=category.color)

    # === Click handler (internal) ===

    @classmethod
    async def _handle_role_click(
        cls,
        interaction: discord.Interaction,
        category: RoleCategory,
        role_id: int,
    ) -> None:
        """Apply cardinality, mutate roles, dispatch the right hook.

        Internal entry point called by ``_RoleToggleButton.on_click``.
        Users override the individual ``on_role_*`` hooks rather than
        this method.
        """
        guild = interaction.guild
        if guild is None:
            await _respond_safe(interaction, "Role panels only work in a server.", ephemeral=True)
            return

        member = interaction.user
        role = guild.get_role(role_id)
        if role is None:
            await cls.on_role_error(
                interaction,
                f"Role with ID {role_id} not found in this server.",
            )
            return

        category_role_ids = set(category.roles.values())
        is_present = role in member.roles

        try:
            if is_present:
                # Removal path
                if category.required:
                    current_in_category = [r for r in member.roles if r.id in category_role_ids]
                    if len(current_in_category) <= 1:
                        await cls.on_role_required_block(interaction, member, role, category)
                        return

                await member.remove_roles(role, reason="Role panel toggle")
                await cls.on_role_removed(interaction, member, role, category)
            else:
                # Assignment path
                roles_removed: List[discord.Role] = []
                if category.exclusive:
                    roles_removed = [
                        r for r in member.roles if r.id in category_role_ids and r.id != role.id
                    ]
                    if roles_removed:
                        await member.remove_roles(*roles_removed, reason="Role panel swap")

                await member.add_roles(role, reason="Role panel toggle")

                if roles_removed:
                    await cls.on_role_swap(interaction, member, role, roles_removed, category)
                else:
                    await cls.on_role_assigned(interaction, member, role, category)
        except discord.Forbidden as exc:
            logger.warning(
                f"Missing permission to toggle role {role.name!r} for " f"{member}: {exc}"
            )
            await cls.on_role_error(interaction, exc)
        except discord.HTTPException as exc:
            logger.warning(f"HTTP error toggling role {role.name!r}: {exc}")
            await cls.on_role_error(interaction, exc)

    # === Event hooks (classmethods; override for custom behavior) ===

    @classmethod
    async def on_role_assigned(
        cls,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        category: RoleCategory,
    ) -> None:
        """Called after a role is added (no swap). Default: ephemeral assigned_message."""
        message = cls.assigned_message.format(role=role.name, category=category.name)
        await _respond_safe(interaction, message, ephemeral=True)

    @classmethod
    async def on_role_removed(
        cls,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        category: RoleCategory,
    ) -> None:
        """Called after a role is removed. Default: ephemeral removed_message."""
        message = cls.removed_message.format(role=role.name, category=category.name)
        await _respond_safe(interaction, message, ephemeral=True)

    @classmethod
    async def on_role_swap(
        cls,
        interaction: discord.Interaction,
        member: discord.Member,
        role_added: discord.Role,
        roles_removed: List[discord.Role],
        category: RoleCategory,
    ) -> None:
        """Called after an exclusive-mode swap. Default: ephemeral swap_message."""
        removed = ", ".join(f"**{r.name}**" for r in roles_removed)
        message = cls.swap_message.format(
            role=role_added.name, category=category.name, removed=removed
        )
        await _respond_safe(interaction, message, ephemeral=True)

    @classmethod
    async def on_role_required_block(
        cls,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        category: RoleCategory,
    ) -> None:
        """Called when a required-category removal is rejected. Default: ephemeral required_message."""
        message = cls.required_message.format(role=role.name, category=category.name)
        await _respond_safe(interaction, message, ephemeral=True)

    @classmethod
    async def on_role_error(
        cls,
        interaction: discord.Interaction,
        error,
    ) -> None:
        """Called when role mutation fails. Default: ephemeral role_error_message.

        Receives either an ``Exception`` (discord.Forbidden,
        discord.HTTPException) or a string describing the failure.
        """
        message = cls.role_error_message.format(error=error)
        await _respond_safe(interaction, message, ephemeral=True)


# // ========================================( V2 Roles )======================================== // #


class RolesLayoutView(_BaseRolesMixin, StatefulLayoutView):
    """V2 role self-assign panel with cardinality-aware category buttons.

    Declares categories via the class-level ``categories`` attribute.
    Each category renders as a Container with an accent color, the
    category title, an optional mode hint, and an ActionRow of toggle
    buttons. Every button is a ``DynamicPersistentButton`` subclass
    declared once at module import -- clicks route by template match,
    not by per-button instance tracking.

    Users who only need a session-scoped role panel (no restart
    survival) use this class directly. Users who want the panel to
    survive restarts use :class:`PersistentRolesLayoutView`.

    Example::

        class MyRoles(RolesLayoutView):
            categories = [
                RoleCategory(
                    name="Colors",
                    roles={"Red": 111, "Blue": 222},
                    exclusive=True,
                    color=discord.Color.red(),
                ),
            ]

        view = MyRoles(context=ctx)
        await view.send()

    See :doc:`/guide/patterns` for customization options (hint strings,
    response messages, button emoji/style hooks).
    """

    owner_only = False
    exit_policy = "delete"
    state_scope = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_ui()

    def build_ui(self) -> None:
        self.clear_items()

        if self.title:
            self.add_item(TextDisplay(f"## {self.title}"))
        if self.subtitle:
            self.add_item(TextDisplay(self.subtitle))

        for category in self.categories:
            self.add_item(self.build_category_card(category))

        # Restore the navigation back button if push() added one.
        self._restore_navigation_artifacts()


# // ========================================( Persistent Roles )======================================== // #


class PersistentRolesLayoutView(_PersistentMixin, RolesLayoutView):
    """Persistent V2 role panel that survives bot restarts.

    Compose ``_PersistentMixin`` with ``RolesLayoutView`` so the
    admin-posted panel gets ``timeout=None``, restart re-attachment,
    and ``persistence_key`` dedup -- without duplicating the rendering
    logic.

    Because role buttons are ``DynamicPersistentButton`` subclasses
    (registered globally at module import), the persistent re-attach
    path differs from other persistent views: clicks route by
    ``custom_id`` template match against the globally-registered
    button class, not through an instance tracked in
    ``_persistent_view_classes``. The view itself still re-attaches
    (for message edit / visual state), but button clicks do not
    require the view instance to be alive.

    Example::

        class ServerRoles(PersistentRolesLayoutView):
            categories = [...]
            title = "Server Roles"

        view = ServerRoles(
            context=ctx,
            persistence_key=f"roles:panel:{ctx.guild.id}",
        )
        await view.send()
    """

    owner_only = False
    exit_policy = "disable"

    async def on_restore(self, bot):
        """Re-render the message from the current ``categories`` on every restart.

        Treats the class's ``categories`` attribute as the source of
        truth across restart boundaries. Source-code edits to role IDs,
        category names, button labels, or icons propagate to the
        displayed message on the next bot start instead of remaining
        frozen at the values that were live when ``send()`` first ran.

        Cost is zero when nothing changed: ``refresh()``'s render-hash
        short-circuit skips the Discord ``PATCH`` when the rebuilt tree
        is byte-identical to what the message already shows. Bots whose
        configuration has not changed pay nothing per restart; bots
        whose configuration has changed pay one edit per persistent
        role panel.
        """
        self.build_ui()
        await self.refresh()
