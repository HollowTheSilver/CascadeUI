"""Tests for RolesLayoutView and PersistentRolesLayoutView."""

# // ========================================( Modules )======================================== // #


from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cascadeui.views.patterns.roles import (
    PersistentRolesLayoutView,
    RolesLayoutView,
    _BaseRolesMixin,
    _category_slug,
    _role_category_registry,
    _role_view_class_registry,
    _RoleToggleButton,
)
from cascadeui.views.patterns.types import RoleCategory
from cascadeui.views.persistent import _PersistentMixin

# // ========================================( Fixtures )======================================== // #


@pytest.fixture
def clean_role_registries():
    """Snapshot both registries and restore after each test.

    Classes declared inside a test function accumulate into the
    module-level registries; without this snapshot, later tests would
    see classes from earlier tests and collision checks would drift.
    """
    cat_snap = dict(_role_category_registry)
    view_snap = dict(_role_view_class_registry)
    yield
    _role_category_registry.clear()
    _role_category_registry.update(cat_snap)
    _role_view_class_registry.clear()
    _role_view_class_registry.update(view_snap)


def _make_role(role_id: int, name: str = "SomeRole") -> MagicMock:
    """Build a Discord role mock with the fields the pattern reads."""
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    return role


def _make_member(user_id: int = 42, roles=None) -> MagicMock:
    """Build a Discord member mock with a role list and async role mutation."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.roles = roles or []
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def _make_interaction(member, guild_roles=None) -> MagicMock:
    """Build an interaction whose guild.get_role returns roles from a map."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = member
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    guild = MagicMock(spec=discord.Guild)
    role_map = {r.id: r for r in (guild_roles or [])}
    guild.get_role = MagicMock(side_effect=lambda rid: role_map.get(rid))
    interaction.guild = guild
    return interaction


# // ========================================( RoleCategory Validation )======================================== // #


class TestRoleCategoryValidation:
    """RoleCategory.__post_init__ rejects bad input at construction time."""

    def test_valid_minimal(self):
        cat = RoleCategory(name="Colors", roles={"Red": 111})
        assert cat.exclusive is False
        assert cat.required is False

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="name must be a non-empty string"):
            RoleCategory(name="", roles={"Red": 111})

    def test_rejects_empty_roles(self):
        with pytest.raises(ValueError, match="roles must be a non-empty dict"):
            RoleCategory(name="Colors", roles={})

    def test_rejects_non_int_role_id(self):
        with pytest.raises(ValueError, match="roles values must be integer"):
            RoleCategory(name="Colors", roles={"Red": "not-an-int"})

    def test_rejects_bool_role_id(self):
        # bool is int subclass but never a valid Discord role ID
        with pytest.raises(ValueError, match="roles values must be integer"):
            RoleCategory(name="Colors", roles={"Red": True})

    def test_rejects_empty_role_label(self):
        with pytest.raises(ValueError, match="roles keys must be non-empty strings"):
            RoleCategory(name="Colors", roles={"": 111})

    def test_rejects_non_bool_exclusive(self):
        with pytest.raises(ValueError, match="exclusive must be a bool"):
            RoleCategory(name="Colors", roles={"Red": 111}, exclusive="yes")  # type: ignore

    def test_rejects_non_bool_required(self):
        with pytest.raises(ValueError, match="required must be a bool"):
            RoleCategory(name="Colors", roles={"Red": 111}, required=1)  # type: ignore


# // ========================================( Subclass Registration )======================================== // #


class TestSubclassRegistration:
    """Subclasses register categories into the module-level registries."""

    def test_subclass_registers_categories(self, clean_role_registries):
        class _RegRoles(RolesLayoutView):
            categories = [
                RoleCategory(name="TestRegColors", roles={"Red": 111, "Blue": 222}),
            ]

        slug = _category_slug("TestRegColors")
        assert slug in _role_category_registry
        assert slug in _role_view_class_registry
        assert _role_view_class_registry[slug] is _RegRoles

    def test_collision_across_classes_raises(self, clean_role_registries):
        class _FirstRoles(RolesLayoutView):
            categories = [RoleCategory(name="CollidingName", roles={"A": 111})]

        with pytest.raises(ValueError, match="name collision"):

            class _SecondRoles(RolesLayoutView):  # noqa: F841
                categories = [RoleCategory(name="CollidingName", roles={"B": 222})]

    def test_empty_categories_does_not_register(self, clean_role_registries):
        snapshot_size = len(_role_category_registry)

        class _NoRoles(RolesLayoutView):
            pass  # inherits categories=[]

        assert len(_role_category_registry) == snapshot_size

    def test_non_role_category_entry_raises(self, clean_role_registries):
        with pytest.raises(TypeError, match="must be RoleCategory"):

            class _BadRoles(RolesLayoutView):  # noqa: F841
                categories = [{"name": "raw-dict", "roles": {"A": 111}}]  # type: ignore

    def test_category_over_five_roles_raises(self, clean_role_registries):
        # A category renders as one ActionRow (max 5 buttons); >5 roles is
        # caught at class-definition time with a directed error, not deferred
        # to discord.py's terser construction error inside build_ui().
        with pytest.raises(ValueError, match="at most 5 buttons"):

            class _TooMany(RolesLayoutView):  # noqa: F841
                categories = [
                    RoleCategory(
                        name="SixColors",
                        roles={"R": 1, "O": 2, "Y": 3, "G": 4, "B": 5, "V": 6},
                    )
                ]


# // ========================================( Format Hooks ) ======================================== // #


class _SampleRolesForHooks(RolesLayoutView):
    """Reused across format-hook tests; registers its own category slugs."""

    categories = [
        RoleCategory(name="HookNormal", roles={"A": 1001}, exclusive=False, required=False),
        RoleCategory(name="HookExclusive", roles={"B": 1002}, exclusive=True, required=False),
        RoleCategory(name="HookRequired", roles={"C": 1003}, exclusive=False, required=True),
        RoleCategory(name="HookBoth", roles={"D": 1004}, exclusive=True, required=True),
    ]


class TestFormatHooks:
    """format_* classmethods produce the expected strings."""

    def test_hint_routes_normal(self):
        cat = _SampleRolesForHooks.categories[0]
        assert _SampleRolesForHooks.format_category_hint(cat) is None

    def test_hint_routes_exclusive(self):
        cat = _SampleRolesForHooks.categories[1]
        assert _SampleRolesForHooks.format_category_hint(cat) == "◉"

    def test_hint_routes_required(self):
        cat = _SampleRolesForHooks.categories[2]
        assert _SampleRolesForHooks.format_category_hint(cat) == "*"

    def test_hint_routes_exclusive_required(self):
        cat = _SampleRolesForHooks.categories[3]
        assert _SampleRolesForHooks.format_category_hint(cat) == "◉ *"

    def test_title_includes_icon_when_set(self):
        cat = RoleCategory(name="WithIcon", roles={"X": 1}, icon="🎨")
        assert _SampleRolesForHooks.format_category_title(cat) == "### 🎨 WithIcon"

    def test_title_without_icon(self):
        cat = _SampleRolesForHooks.categories[0]
        assert _SampleRolesForHooks.format_category_title(cat) == "### HookNormal"

    def test_button_style_defaults_secondary(self):
        cat = _SampleRolesForHooks.categories[0]
        style = _SampleRolesForHooks.format_button_style("A", 1001, cat)
        assert style == discord.ButtonStyle.secondary

    def test_button_style_uses_category_style(self):
        cat = RoleCategory(
            name="StyledCat",
            roles={"X": 1},
            button_style=discord.ButtonStyle.success,
        )
        assert _SampleRolesForHooks.format_button_style("X", 1, cat) == discord.ButtonStyle.success


# // ========================================( Subclass Overrides )======================================== // #


class TestAttributeOverrides:
    """Tier 1 class attribute overrides change behavior without method override."""

    def test_hint_override(self, clean_role_registries):
        class _CustomHints(RolesLayoutView):
            categories = [
                RoleCategory(name="OverrideHints", roles={"A": 1}, exclusive=True),
            ]
            hint_exclusive = "🎯 one"

        cat = _CustomHints.categories[0]
        assert _CustomHints.format_category_hint(cat) == "🎯 one"

    def test_assigned_message_override(self, clean_role_registries):
        class _CustomMessages(RolesLayoutView):
            categories = [
                RoleCategory(name="OverrideMsg", roles={"A": 1}),
            ]
            assigned_message = "✅ Got **{role}**."

        # Format the message directly to verify the override is read.
        formatted = _CustomMessages.assigned_message.format(role="TestRole", category="X")
        assert formatted == "✅ Got **TestRole**."


# // ========================================( Cardinality Behavior )======================================== // #


class _SampleRolesForClick(RolesLayoutView):
    categories = [
        RoleCategory(name="ClickNormal", roles={"A": 2001, "B": 2002}),
        RoleCategory(
            name="ClickExclusive",
            roles={"C": 2003, "D": 2004},
            exclusive=True,
        ),
        RoleCategory(
            name="ClickRequired",
            roles={"E": 2005, "F": 2006},
            required=True,
        ),
    ]


class TestCardinalityBehavior:
    """_handle_role_click applies cardinality rules correctly."""

    async def test_normal_add(self):
        role = _make_role(2001, "A")
        member = _make_member(roles=[])
        interaction = _make_interaction(member, guild_roles=[role])
        cat = _SampleRolesForClick.categories[0]

        await _SampleRolesForClick._handle_role_click(interaction, cat, 2001)

        member.add_roles.assert_called_once()
        assert member.add_roles.call_args.args == (role,)
        # No swap occurred, so assigned_message fires (not swap_message)
        interaction.response.send_message.assert_called_once()
        msg = interaction.response.send_message.call_args.args[0]
        assert "Gave you" in msg and "A" in msg

    async def test_normal_remove(self):
        role = _make_role(2001, "A")
        member = _make_member(roles=[role])
        interaction = _make_interaction(member, guild_roles=[role])
        cat = _SampleRolesForClick.categories[0]

        await _SampleRolesForClick._handle_role_click(interaction, cat, 2001)

        member.remove_roles.assert_called_once()
        msg = interaction.response.send_message.call_args.args[0]
        assert "Removed" in msg and "A" in msg

    async def test_exclusive_swap(self):
        """Adding C when D is active removes D first."""
        role_c = _make_role(2003, "C")
        role_d = _make_role(2004, "D")
        member = _make_member(roles=[role_d])
        interaction = _make_interaction(member, guild_roles=[role_c, role_d])
        cat = _SampleRolesForClick.categories[1]

        await _SampleRolesForClick._handle_role_click(interaction, cat, 2003)

        # D was removed (swap), then C was added
        member.remove_roles.assert_called_once()
        assert member.remove_roles.call_args.args == (role_d,)
        member.add_roles.assert_called_once()
        assert member.add_roles.call_args.args == (role_c,)
        # swap_message fired, not assigned_message
        msg = interaction.response.send_message.call_args.args[0]
        assert "Switched" in msg

    async def test_exclusive_add_first_fires_assigned(self):
        """Adding C when nothing in category is active fires assigned_message."""
        role_c = _make_role(2003, "C")
        member = _make_member(roles=[])
        interaction = _make_interaction(member, guild_roles=[role_c])
        cat = _SampleRolesForClick.categories[1]

        await _SampleRolesForClick._handle_role_click(interaction, cat, 2003)

        member.add_roles.assert_called_once()
        member.remove_roles.assert_not_called()
        msg = interaction.response.send_message.call_args.args[0]
        assert "Gave you" in msg

    async def test_required_block_last_removal(self):
        """Removing E when it's the only ClickRequired role is rejected."""
        role_e = _make_role(2005, "E")
        member = _make_member(roles=[role_e])
        interaction = _make_interaction(member, guild_roles=[role_e])
        cat = _SampleRolesForClick.categories[2]

        await _SampleRolesForClick._handle_role_click(interaction, cat, 2005)

        member.remove_roles.assert_not_called()
        msg = interaction.response.send_message.call_args.args[0]
        assert "must keep" in msg or "at least one" in msg

    async def test_required_allows_removal_when_another_active(self):
        """Removing E when F is also active succeeds."""
        role_e = _make_role(2005, "E")
        role_f = _make_role(2006, "F")
        member = _make_member(roles=[role_e, role_f])
        interaction = _make_interaction(member, guild_roles=[role_e, role_f])
        cat = _SampleRolesForClick.categories[2]

        await _SampleRolesForClick._handle_role_click(interaction, cat, 2005)

        member.remove_roles.assert_called_once()
        msg = interaction.response.send_message.call_args.args[0]
        assert "Removed" in msg

    async def test_role_not_found_fires_error(self):
        member = _make_member()
        interaction = _make_interaction(member, guild_roles=[])
        cat = _SampleRolesForClick.categories[0]

        await _SampleRolesForClick._handle_role_click(interaction, cat, 9999)

        member.add_roles.assert_not_called()
        member.remove_roles.assert_not_called()
        msg = interaction.response.send_message.call_args.args[0]
        assert "not found" in msg.lower() or "error" in msg.lower()


# // ========================================( Hook Overrides )======================================== // #


class TestHookOverrides:
    """User can override on_role_* classmethods for custom behavior."""

    async def test_on_role_assigned_override_fires(self, clean_role_registries):
        captured = {}

        class _CustomHook(RolesLayoutView):
            categories = [RoleCategory(name="HookOverride", roles={"A": 3001})]

            @classmethod
            async def on_role_assigned(cls, interaction, member, role, category):
                captured["fired"] = True
                captured["role_name"] = role.name
                captured["category_name"] = category.name
                # Still respond so the interaction is acked
                await super().on_role_assigned(interaction, member, role, category)

        role = _make_role(3001, "A")
        member = _make_member(roles=[])
        interaction = _make_interaction(member, guild_roles=[role])
        cat = _CustomHook.categories[0]

        await _CustomHook._handle_role_click(interaction, cat, 3001)

        assert captured.get("fired") is True
        assert captured["role_name"] == "A"
        assert captured["category_name"] == "HookOverride"


# // ========================================( Persistent Variant )======================================== // #


class TestPersistentRolesLayoutView:
    """MRO composition and persistent defaults work."""

    def test_mro_includes_persistent_mixin(self):
        assert _PersistentMixin in PersistentRolesLayoutView.__mro__
        assert RolesLayoutView in PersistentRolesLayoutView.__mro__
        assert _BaseRolesMixin in PersistentRolesLayoutView.__mro__

    def test_persistent_defaults_override_base(self):
        # Base RolesLayoutView: exit_policy = "delete"
        # Persistent variant: exit_policy = "disable"
        assert RolesLayoutView.exit_policy == "delete"
        assert PersistentRolesLayoutView.exit_policy == "disable"

    def test_persistent_variant_registers_categories(self, clean_role_registries):
        class _PersistentSubclass(PersistentRolesLayoutView):
            categories = [RoleCategory(name="PersistentCat", roles={"X": 4001})]

        slug = _category_slug("PersistentCat")
        assert slug in _role_category_registry
        assert _role_view_class_registry[slug] is _PersistentSubclass

    def test_on_restore_signature_matches_persistent_base(self):
        """``on_restore`` is async with the ``(self, bot)`` shape required
        by the persistence rehydration path.
        """
        import inspect

        method = PersistentRolesLayoutView.on_restore
        assert inspect.iscoroutinefunction(method)
        sig = inspect.signature(method)
        assert list(sig.parameters.keys()) == ["self", "bot"]

    async def test_on_restore_rebuilds_and_ships_edit(self, clean_role_registries):
        """``on_restore`` re-runs ``build_ui`` and ``refresh`` so source-code
        edits to ``categories`` propagate to the displayed message on
        the next bot start.
        """

        class _RestoreRoles(PersistentRolesLayoutView):
            categories = [RoleCategory(name="RestoreCat", roles={"A": 7001, "B": 7002})]

        view = _RestoreRoles(persistence_key="test-restore-roles")

        mock_message = MagicMock()
        mock_message.id = 12345
        mock_message.edit = AsyncMock()
        view._message = mock_message

        # Bypass the render-hash short-circuit so the edit ships even
        # when the rebuilt tree matches what ``__init__``'s build_ui
        # cached. Production restart flows hit this path via either a
        # categories change or a tree_digest miss on first restore.
        view._last_tree_digest = None

        await view.on_restore(bot=MagicMock())

        assert len(view.children) >= 2
        mock_message.edit.assert_called_once()
        assert mock_message.edit.call_args.kwargs.get("view") is view


# // ========================================( Initial Render )======================================== // #


class TestInitialRender:
    """``build_ui()`` runs at construction so ``send()`` never ships an empty
    V2 message (Discord error 50006: Cannot send an empty message).
    """

    def test_base_view_has_children_after_init(self, clean_role_registries):
        class _RenderableRoles(RolesLayoutView):
            categories = [
                RoleCategory(name="InitialCat", roles={"A": 5001, "B": 5002}),
            ]

        view = _RenderableRoles()
        # Default title TextDisplay + one Container per category
        assert len(view.children) >= 2

    def test_persistent_view_has_children_after_init(self, clean_role_registries):
        class _RenderablePersistentRoles(PersistentRolesLayoutView):
            categories = [
                RoleCategory(name="PersistentInitialCat", roles={"X": 6001}),
            ]

        view = _RenderablePersistentRoles(persistence_key="test-roles-init")
        assert len(view.children) >= 2


# // ========================================( Dynamic Button Integration )======================================== // #


class TestDynamicButtonIntegration:
    """_RoleToggleButton is registered as a DynamicPersistentButton subclass."""

    def test_role_toggle_button_in_dynamic_registry(self):
        from cascadeui.components.base import _dynamic_button_classes

        key = f"{_RoleToggleButton.__module__}.{_RoleToggleButton.__qualname__}"
        assert key in _dynamic_button_classes
        assert _dynamic_button_classes[key] is _RoleToggleButton

    def test_button_custom_id_encodes_category_and_role(self):
        button = _RoleToggleButton(category_slug="test_slug", role_id=9999, label="TestLabel")
        assert button.item.custom_id == "roles:test_slug:9999"
