"""Tests for ``cascadeui.components.base.DynamicPersistentButton``.

Covers the primitive's contracts:

- Subclass registration into ``_dynamic_button_classes`` at class-
  definition time via ``__init_subclass__``.
- ``discord.py``'s ``template=`` requirement is enforced on every
  subclass; there is no "abstract intermediate base" pattern.
- Default ``from_custom_id`` extracts and snowflake-coerces captures.
- ``callback`` binds ``_CURRENT_INTERACTION`` during ``on_click``.
- Bot registration happens inside ``PersistenceMiddleware.initialize``.
"""

# // ========================================( Modules )======================================== // #


import re
from unittest.mock import MagicMock

import discord
import pytest

from cascadeui.components.base import (
    _SNOWFLAKE_CAPTURES,
    DynamicPersistentButton,
    _dynamic_button_classes,
)
from cascadeui.state.store import _CURRENT_INTERACTION

# // ========================================( Fixtures )======================================== // #


@pytest.fixture
def clean_registry():
    """Snapshot ``_dynamic_button_classes`` and restore after each test.

    Test classes defined inside a test function accumulate into the
    module-level registry; without this snapshot, later tests would see
    classes defined by earlier tests and the registration assertions
    would drift.
    """
    snapshot = dict(_dynamic_button_classes)
    yield
    _dynamic_button_classes.clear()
    _dynamic_button_classes.update(snapshot)


def _build_match(template: str, custom_id: str) -> re.Match:
    """Compile *template* and return the match against *custom_id*.

    Mirrors how discord.py's dispatcher produces the ``match`` argument
    passed to ``from_custom_id``.
    """
    m = re.match(template, custom_id)
    assert m is not None, f"template {template!r} did not match {custom_id!r}"
    return m


# // ========================================( Subclass Registration )======================================== // #


class TestSubclassRegistration:
    """Subclasses auto-register into the module-level registry."""

    def test_subclass_registers(self, clean_registry):
        class _ConcreteButton(
            DynamicPersistentButton,
            template=r"concrete:(?P<role_id>[0-9]+)",
        ):
            def __init__(self, *, role_id: int):
                super().__init__(
                    discord.ui.Button(
                        label="x",
                        custom_id=f"concrete:{role_id}",
                        style=discord.ButtonStyle.primary,
                    )
                )
                self.role_id = role_id

        key = f"{_ConcreteButton.__module__}.{_ConcreteButton.__qualname__}"
        assert key in _dynamic_button_classes
        assert _dynamic_button_classes[key] is _ConcreteButton

    def test_base_class_itself_not_in_registry(self, clean_registry):
        # __init_subclass__ runs for subclasses, not for the declaring
        # class. DynamicPersistentButton's never-match sentinel template
        # satisfies discord.py's requirement but the class is never added
        # to the registry because its own __init_subclass__ does not fire
        # on itself.
        assert not any(cls is DynamicPersistentButton for cls in _dynamic_button_classes.values())

    def test_missing_template_kwarg_rejected_by_discord_py(self, clean_registry):
        # discord.py enforces template= on every DynamicItem subclass at
        # class-definition time. This confirms the library's contract
        # lines up with the upstream constraint -- there is no
        # "abstract intermediate base" pattern available.
        with pytest.raises(TypeError, match="template"):

            class _MissingTemplate(DynamicPersistentButton):  # noqa: F841
                pass

    def test_qualified_key_prevents_bare_name_collision(self, clean_registry):
        # The registry key includes module + qualname so two classes
        # with the same bare name in different module paths do not
        # clobber each other.
        class _NamedButton(
            DynamicPersistentButton,
            template=r"named:(?P<role_id>[0-9]+)",
        ):
            def __init__(self, *, role_id: int):
                super().__init__(
                    discord.ui.Button(
                        label="n",
                        custom_id=f"named:{role_id}",
                        style=discord.ButtonStyle.primary,
                    )
                )
                self.role_id = role_id

        key = f"{_NamedButton.__module__}.{_NamedButton.__qualname__}"
        assert "TestSubclassRegistration" in key, "qualname must include enclosing class"


# // ========================================( from_custom_id Coercion )======================================== // #


class TestFromCustomIdCoercion:
    """Default ``from_custom_id`` extracts captures and coerces snowflakes."""

    async def test_role_id_coerced_to_int(self, clean_registry):
        class _RoleButton(
            DynamicPersistentButton,
            template=r"role:(?P<role_id>[0-9]+)",
        ):
            def __init__(self, *, role_id: int):
                super().__init__(
                    discord.ui.Button(
                        label="r", custom_id=f"role:{role_id}", style=discord.ButtonStyle.primary
                    )
                )
                self.role_id = role_id

        match = _build_match(r"role:(?P<role_id>[0-9]+)", "role:123456789")
        item = MagicMock()
        item.custom_id = "role:123456789"
        interaction = MagicMock()

        instance = await _RoleButton.from_custom_id(interaction, item, match)

        assert isinstance(instance, _RoleButton)
        assert instance.role_id == 123456789
        assert isinstance(instance.role_id, int)

    async def test_snowflake_capture_set_pins_canonical_names(self, clean_registry):
        # _SNOWFLAKE_CAPTURES is the contract for which named groups get
        # int-coerced in from_custom_id. Pin the set to its canonical
        # five names so adding a new snowflake-shaped name without
        # extending the set is caught.
        assert _SNOWFLAKE_CAPTURES == {
            "user_id",
            "guild_id",
            "channel_id",
            "role_id",
            "message_id",
        }

    async def test_non_snowflake_capture_preserved_as_string(self, clean_registry):
        class _CategoryButton(
            DynamicPersistentButton,
            template=r"cat:(?P<category>[a-z_]+):(?P<role_id>[0-9]+)",
        ):
            def __init__(self, *, category: str, role_id: int):
                super().__init__(
                    discord.ui.Button(
                        label="c",
                        custom_id=f"cat:{category}:{role_id}",
                        style=discord.ButtonStyle.primary,
                    )
                )
                self.category = category
                self.role_id = role_id

        match = _build_match(r"cat:(?P<category>[a-z_]+):(?P<role_id>[0-9]+)", "cat:mod_staff:42")
        item = MagicMock()
        item.custom_id = "cat:mod_staff:42"
        interaction = MagicMock()

        instance = await _CategoryButton.from_custom_id(interaction, item, match)

        assert instance.category == "mod_staff"
        assert isinstance(instance.category, str)
        assert instance.role_id == 42
        assert isinstance(instance.role_id, int)


# // ========================================( callback / on_click )======================================== // #


class TestCallbackContextVar:
    """``callback`` binds ``_CURRENT_INTERACTION`` around ``on_click``."""

    async def test_current_interaction_bound_during_on_click(self, clean_registry):
        captured = {}

        class _BindingButton(
            DynamicPersistentButton,
            template=r"bind:(?P<role_id>[0-9]+)",
        ):
            def __init__(self, *, role_id: int):
                super().__init__(
                    discord.ui.Button(
                        label="b", custom_id=f"bind:{role_id}", style=discord.ButtonStyle.primary
                    )
                )
                self.role_id = role_id

            async def on_click(self, interaction):
                captured["live"] = _CURRENT_INTERACTION.get()

        # Baseline: contextvar is None before the callback fires so
        # the binding is detectable and the reset is verifiable after.
        reset_token = _CURRENT_INTERACTION.set(None)
        try:
            instance = _BindingButton(role_id=99)
            interaction = MagicMock()
            await instance.callback(interaction)
            assert captured["live"] is interaction
            assert _CURRENT_INTERACTION.get() is None, "contextvar must reset after callback"
        finally:
            _CURRENT_INTERACTION.reset(reset_token)

    async def test_context_resets_even_on_on_click_exception(self, clean_registry):
        class _RaisingButton(
            DynamicPersistentButton,
            template=r"raise:(?P<role_id>[0-9]+)",
        ):
            def __init__(self, *, role_id: int):
                super().__init__(
                    discord.ui.Button(
                        label="r", custom_id=f"raise:{role_id}", style=discord.ButtonStyle.primary
                    )
                )
                self.role_id = role_id

            async def on_click(self, interaction):
                raise RuntimeError("boom")

        reset_token = _CURRENT_INTERACTION.set(None)
        try:
            instance = _RaisingButton(role_id=1)
            interaction = MagicMock()
            with pytest.raises(RuntimeError, match="boom"):
                await instance.callback(interaction)
            assert _CURRENT_INTERACTION.get() is None, "contextvar must reset on exception"
        finally:
            _CURRENT_INTERACTION.reset(reset_token)


# // ========================================( Bot Registration )======================================== // #


class TestBotRegistration:
    """``PersistenceMiddleware.initialize`` registers subclasses with the bot."""

    async def test_add_dynamic_items_called_with_registered_subclasses(self, clean_registry):
        # Declare a fresh subclass the middleware should register.
        class _RegBotButton(
            DynamicPersistentButton,
            template=r"regbot:(?P<role_id>[0-9]+)",
        ):
            def __init__(self, *, role_id: int):
                super().__init__(
                    discord.ui.Button(
                        label="rb",
                        custom_id=f"regbot:{role_id}",
                        style=discord.ButtonStyle.primary,
                    )
                )
                self.role_id = role_id

        from cascadeui.persistence.backends.memory import InMemoryBackend
        from cascadeui.state.middleware.persistence import PersistenceMiddleware
        from cascadeui.state.singleton import get_store

        # ``add_dynamic_items`` is the contract under test; let the
        # middleware's initialize pipeline drive the call.
        # ``spec=discord.Client`` satisfies the constructor's isinstance
        # check and auto-vivifies ``add_dynamic_items`` (which exists
        # on Client). Pre-flagging the cleanup listener as installed
        # short-circuits ``_install_message_cleanup`` before it
        # tries to call ``bot.listen`` (which is on commands.Bot, not
        # Client, and would raise AttributeError on the spec'd mock).
        bot = MagicMock(spec=discord.Client)
        store = get_store()
        store._cleanup_listener_installed = True

        middleware = PersistenceMiddleware(backend=InMemoryBackend(), bot=bot)
        await middleware.initialize(store)

        bot.add_dynamic_items.assert_called_once()
        passed_classes = bot.add_dynamic_items.call_args.args
        assert _RegBotButton in passed_classes


# // ========================================( Custom ID Stabilization )======================================== // #


class TestCustomIdStabilization:
    """``DynamicPersistentButton`` instances must report
    ``_provided_custom_id = True`` so the parent view's stabilizer
    skips them. The custom_id is template-matched and rewriting it
    fails the regex match -- discord.py raises on assignment.
    """

    def test_provided_custom_id_flag_set_on_construction(self, clean_registry):
        class _StableButton(
            DynamicPersistentButton,
            template=r"stable:(?P<role_id>[0-9]+)",
        ):
            def __init__(self, *, role_id: int):
                super().__init__(
                    discord.ui.Button(
                        label="s",
                        custom_id=f"stable:{role_id}",
                        style=discord.ButtonStyle.primary,
                    )
                )

        item = _StableButton(role_id=999)
        assert item._provided_custom_id is True
