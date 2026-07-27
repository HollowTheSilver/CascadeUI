"""Tests for PersistentLayoutView (V2 persistent views)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ui import LayoutView
from helpers import make_interaction as _make_interaction

from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.persistent import PersistentLayoutView, _persistent_view_classes


class TestPersistentLayoutViewInit:
    """Init, registry, and validation tests."""

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(PersistentLayoutView, StatefulLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(PersistentLayoutView, LayoutView)

    def test_subclass_auto_registered(self):
        class _TestV2Panel(PersistentLayoutView):
            pass

        key = _TestV2Panel._class_session_key()
        assert key in _persistent_view_classes
        assert _persistent_view_classes[key] is _TestV2Panel
        # Qualified path keeps unrelated cogs from colliding on bare class name.
        assert key.endswith("._TestV2Panel")

    def test_persistence_key_required(self):
        with pytest.raises(ValueError, match="persistence_key"):

            class _NoKeyV2(PersistentLayoutView):
                pass

            _NoKeyV2()

    def test_timeout_forced_to_none(self):
        class _TimeoutV2(PersistentLayoutView):
            pass

        view = _TimeoutV2(persistence_key="test:v2:timeout")
        assert view.timeout is None

    def test_owner_only_defaults_false(self):
        class _OwnerV2(PersistentLayoutView):
            pass

        view = _OwnerV2(persistence_key="test:v2:owner")
        assert view.owner_only is False

    def test_persistent_marker(self):
        class _MarkerV2(PersistentLayoutView):
            pass

        view = _MarkerV2(persistence_key="test:v2:marker")
        assert view._persistent is True


class TestPersistentLayoutViewSend:
    """Send method validation tests."""

    async def test_ephemeral_raises(self):
        class _EphV2(PersistentLayoutView):
            pass

        interaction = _make_interaction()
        view = _EphV2(interaction=interaction, persistence_key="test:v2:eph")

        with pytest.raises(ValueError, match="cannot be sent as ephemeral"):
            await view.send(ephemeral=True)


class TestBuilderCustomIdsSurviveRestart:
    """Auto-generated ids are per-instance, so a persistent view must reject them.

    ``_stabilize_custom_ids`` rewrites auto ids to anchors prefixed with the
    view's own uuid. That rewrite clears both signals the validator used to
    detect a missing id: the 32-hex pattern stops matching, and discord.py's
    ``custom_id`` setter flips ``_provided_custom_id`` to True. The rewrite
    marks itself instead.
    """

    @staticmethod
    async def _cb(interaction):
        pass

    def test_builder_without_custom_id_is_rejected(self):
        from cascadeui.components.patterns.v2 import button_row

        class _Panel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(button_row({"Yes": TestBuilderCustomIdsSurviveRestart._cb}))

        view = _Panel(persistence_key="p")
        view.build_ui()

        with pytest.raises(ValueError, match="missing a custom_id"):
            view._validate_custom_ids()

    def test_builder_with_custom_id_yields_ids_stable_across_instances(self):
        from cascadeui.components.patterns.v2 import button_row, confirm_section, tab_nav

        cb = TestBuilderCustomIdsSurviveRestart._cb

        class _Panel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(button_row({"Yes": cb, "No": cb}, custom_id="vote"))
                self.add_item(tab_nav({"A": cb, "B": cb}, custom_id="tab"))
                for item in confirm_section("Sure?", on_confirm=cb, on_cancel=cb, custom_id="ok"):
                    self.add_item(item)

        def ids_for_a_fresh_instance():
            view = _Panel(persistence_key="p")
            view.build_ui()
            view._validate_custom_ids()
            return [c.custom_id for c in view.walk_children() if getattr(c, "custom_id", None)]

        first = ids_for_a_fresh_instance()
        assert first == [
            "vote_0",
            "vote_1",
            "tab_0",
            "tab_1",
            "ok_confirm",
            "ok_cancel",
        ]
        # A restart builds a new instance; the ids Discord holds must still match.
        assert ids_for_a_fresh_instance() == first

    def test_stabilization_leaves_non_interactive_items_alone(self):
        """Every discord.ui.Item sets _provided_custom_id, so the walk must
        discriminate by type or it stamps ids onto display components.
        """
        from discord.ui import ActionRow, Container, Separator, TextDisplay

        from cascadeui.components.base import StatefulButton

        class _View(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(
                    Container(
                        TextDisplay("hello"),
                        Separator(),
                        ActionRow(
                            StatefulButton(
                                label="Go",
                                callback=TestBuilderCustomIdsSurviveRestart._cb,
                            )
                        ),
                    )
                )

        view = _View(interaction=_make_interaction())
        view.build_ui()

        stamped = {
            type(c).__name__
            for c in view.walk_children()
            if getattr(c, "_cascadeui_stabilized", False)
        }
        assert stamped == {"StatefulButton"}


# // ========================================( Class )======================================== // #


class TestPersistentValidateCoversIds:
    """``validate()`` on a persistent view runs the id check ``send()`` runs.

    The base entry runs uniqueness and placement. A persistent ``send()`` runs
    a third check for auto-generated ids, which is the one that catches the
    dead-buttons-after-restart shape. Without the override, a test written to
    the documented offline recipe passes a panel that would be rejected at
    send, leaving that class uncoverable offline.
    """

    @staticmethod
    async def _cb(interaction):
        pass

    def test_auto_generated_id_rejected(self):
        from discord.ui import ActionRow

        from cascadeui.components.base import StatefulButton

        cb = TestPersistentValidateCoversIds._cb

        class _Panel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Go", callback=cb)))

        view = _Panel(persistence_key="panel:auto")
        view.build_ui()
        with pytest.raises(ValueError, match="missing a custom_id"):
            view.validate()

    def test_explicit_id_passes(self):
        from discord.ui import ActionRow

        from cascadeui.components.base import StatefulButton

        cb = TestPersistentValidateCoversIds._cb

        class _Panel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="p_go", callback=cb)))

        view = _Panel(persistence_key="panel:explicit")
        view.build_ui()
        view.validate()
