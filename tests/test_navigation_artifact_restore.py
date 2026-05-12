"""Cross-pattern regression: rebuild paths preserve the auto back button.

Every pattern that overrides ``clear_items()`` and rebuilds the component
tree from scratch must call :meth:`_restore_navigation_artifacts` before
returning. Without that step, the auto back button injected by ``push()``
gets stripped on the first state-driven rebuild and the user is stranded
inside the pushed view with no way to navigate back.

The bug class is generic: paginated page turns, tab switches, form
re-layout, menu refresh, role panel rebuild, and wizard step advance all
share the same shape. This file exercises every affected rebuild seam
with the same asserting harness so the contract stays uniform.
"""

# // ========================================( Modules )======================================== // #


from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import ActionRow, Container, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.views.patterns import (
    FormLayoutView,
    MenuLayoutView,
    PaginatedLayoutView,
    RolesLayoutView,
    TabLayoutView,
    WizardLayoutView,
)
from cascadeui.views.patterns.menu import MenuView
from cascadeui.views.patterns.types import RoleCategory

# // ========================================( Helpers )======================================== // #


def _attach_message(view) -> None:
    """Stub the view's message ref so refresh()'s edit call resolves."""
    view._message = MagicMock()
    view._message.edit = AsyncMock()


# // ========================================( Tests )======================================== // #


class TestBackButtonSurvivesRebuild:
    """Each pattern's rebuild path restores the auto back button."""

    async def test_paginated_layout_view(self):
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[
                [TextDisplay("A")],
                [TextDisplay("B")],
            ],
        )
        _attach_message(view)
        view._add_back_button()
        back_row = view._auto_back_item
        assert back_row in list(view.children)

        view.current_page = 1
        await view._update_page()

        assert back_row in list(view.children)

    async def test_tab_layout_view(self):
        async def build_a():
            return TextDisplay("Tab A content")

        async def build_b():
            return TextDisplay("Tab B content")

        view = TabLayoutView(
            interaction=_make_interaction(),
            tabs={"A": build_a, "B": build_b},
        )
        _attach_message(view)
        view._add_back_button()
        back_row = view._auto_back_item
        assert back_row in list(view.children)

        await view._refresh_tabs()

        assert back_row in list(view.children)

    async def test_wizard_layout_view(self):
        async def step_one():
            return TextDisplay("Step 1")

        async def step_two():
            return TextDisplay("Step 2")

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[
                {"name": "One", "builder": step_one},
                {"name": "Two", "builder": step_two},
            ],
        )
        _attach_message(view)
        view._add_back_button()
        back_row = view._auto_back_item
        assert back_row in list(view.children)

        await view._rebuild_step_content()

        assert back_row in list(view.children)

    def test_form_layout_view(self):
        view = FormLayoutView(
            interaction=_make_interaction(),
            fields=[{"name": "username", "label": "Username", "type": "text"}],
        )
        _attach_message(view)
        view._add_back_button()
        back_row = view._auto_back_item
        assert back_row in list(view.children)

        view._rebuild_display()

        assert back_row in list(view.children)

    def test_menu_layout_view(self):
        async def open_child(interaction):
            pass

        from cascadeui.views.layout import StatefulLayoutView

        class _Child(StatefulLayoutView):
            pass

        view = MenuLayoutView(
            interaction=_make_interaction(),
            categories=[
                {"label": "First", "view": _Child, "callback": open_child},
                {"label": "Second", "view": _Child, "callback": open_child},
            ],
        )
        _attach_message(view)
        view._add_back_button()
        back_row = view._auto_back_item
        assert back_row in list(view.children)

        view.build_ui()

        assert back_row in list(view.children)

    def test_menu_view_v1(self):
        async def open_child(interaction):
            pass

        from cascadeui.views.view import StatefulView

        class _Child(StatefulView):
            pass

        view = MenuView(
            interaction=_make_interaction(),
            categories=[
                {"label": "First", "view": _Child, "callback": open_child},
                {"label": "Second", "view": _Child, "callback": open_child},
            ],
        )
        _attach_message(view)
        view._add_back_button()
        back_btn = view._auto_back_item
        assert back_btn in list(view.children)

        view.build_ui()

        assert back_btn in list(view.children)

    def test_roles_layout_view(self):
        # ``RolesLayoutView`` declares ``categories`` as a class attribute
        # (validated in ``__init_subclass__``), not a constructor kwarg.
        class _Roles(RolesLayoutView):
            categories = [
                RoleCategory(name="Colors", roles={"Red": 111, "Blue": 222}),
            ]

        view = _Roles(interaction=_make_interaction())
        _attach_message(view)
        view._add_back_button()
        back_row = view._auto_back_item
        assert back_row in list(view.children)

        view.build_ui()

        assert back_row in list(view.children)


class TestRestoreHelperIdempotency:
    """``_restore_navigation_artifacts`` must be safe to call from any
    rebuild context, including views that were never pushed.
    """

    def test_no_op_when_back_item_unset(self):
        """A view that never went through push() has no _auto_back_item.
        The helper must short-circuit cleanly, not raise AttributeError.
        """
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[[TextDisplay("A")]],
        )
        # Sanity: no back item registered.
        assert getattr(view, "_auto_back_item", None) is None

        # Should not raise.
        view._restore_navigation_artifacts()

    def test_no_double_add_when_already_present(self):
        """Calling the helper twice must not duplicate the back item in
        the children list.
        """
        view = PaginatedLayoutView(
            interaction=_make_interaction(),
            pages=[[TextDisplay("A")]],
        )
        view._add_back_button()
        back_row = view._auto_back_item
        count_before = list(view.children).count(back_row)
        assert count_before == 1

        view._restore_navigation_artifacts()
        view._restore_navigation_artifacts()

        count_after = list(view.children).count(back_row)
        assert count_after == 1
