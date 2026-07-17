"""Every pattern must survive being a navigation destination.

Navigation resolves a rebuild for the view it is about to show: the
destination's own ``nav_rebuild``, or one the source supplies. Whichever
wins has to be runnable against that destination, and nothing here mocks
that resolution: each test drives the real seam and then executes what it
produced.

The pairwise coverage is the point. A rebuild that calls a method its
destination does not define raises ``AttributeError`` on the user's click,
and a suite that exercises each pattern only in isolation never sees it.
"""

# // ========================================( Modules )======================================== // #


import asyncio
from unittest.mock import AsyncMock, patch

import discord
import pytest
from discord.ui import Container, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui import FormField
from cascadeui.views.patterns import (
    FormLayoutView,
    FormView,
    MenuLayoutView,
    MenuView,
    PaginatedLayoutView,
    PaginatedView,
    TabLayoutView,
    TabView,
    WizardLayoutView,
    WizardView,
)

# // ========================================( Constants )======================================== // #


V1_PATTERNS = [MenuView, FormView, PaginatedView, TabView, WizardView]
V2_PATTERNS = [
    MenuLayoutView,
    FormLayoutView,
    PaginatedLayoutView,
    TabLayoutView,
    WizardLayoutView,
]
ALL_PATTERNS = V1_PATTERNS + V2_PATTERNS

MENU_PAIRS = [(MenuView, dest) for dest in V1_PATTERNS] + [
    (MenuLayoutView, dest) for dest in V2_PATTERNS
]


# // ========================================( Helpers )======================================== // #


async def _v1_builder():
    # V1 tabs and steps both wrap the return as {"embed": ...}.
    return discord.Embed(title="Content")


async def _v2_builder():
    return [Container(TextDisplay("content"))]


def _construct(view_cls):
    """Build a minimal instance of any pattern.

    Each pattern takes a different required argument, so a matrix over all
    of them needs one place that knows every shape.
    """
    kwargs = {"interaction": _make_interaction()}
    if view_cls in (MenuView, MenuLayoutView):
        return view_cls(categories=[], **kwargs)
    if view_cls in (FormView, FormLayoutView):
        return view_cls(fields=[FormField(id="a", label="A")], **kwargs)
    if view_cls in (PaginatedView, PaginatedLayoutView):
        return view_cls(pages=["one", "two"], **kwargs)
    if view_cls is TabView:
        return view_cls(tabs={"Tab": _v1_builder}, **kwargs)
    if view_cls is TabLayoutView:
        return view_cls(tabs={"Tab": _v2_builder}, **kwargs)
    if view_cls is WizardView:
        return view_cls(steps=[{"name": "Step", "builder": _v1_builder}], **kwargs)
    if view_cls is WizardLayoutView:
        return view_cls(steps=[{"name": "Step", "builder": _v2_builder}], **kwargs)
    raise AssertionError(f"no construction known for {view_cls.__name__}")


async def _run_rebuild(rebuild, view):
    """Execute a resolved rebuild the way ``_apply_navigation_edit`` does."""
    result = rebuild(view)
    if asyncio.iscoroutine(result):
        result = await result
    return result


def _category_callback(menu, label):
    """Return the push callback behind a category button.

    V1 keeps its buttons in a flat list; V2 nests them inside the card, so
    the lookup walks the tree.
    """
    buttons = getattr(menu, "_category_buttons", None)
    if buttons:
        for button in buttons:
            if button.label == label:
                return button.callback
    for child in menu.walk_children():
        if getattr(child, "callback", None) is not None and getattr(child, "label", None) == label:
            return child.callback
    raise AssertionError(f"no category button labelled {label!r} on {type(menu).__name__}")


# // ========================================( Class )======================================== // #


class TestNavRebuildRunsOnItsOwnPattern:
    """A pattern's own ``nav_rebuild`` renders that pattern."""

    @pytest.mark.parametrize("view_cls", ALL_PATTERNS, ids=lambda c: c.__name__)
    async def test_nav_rebuild_is_runnable(self, view_cls):
        view = _construct(view_cls)
        rebuild = view_cls.nav_rebuild
        if rebuild is None:
            # The view renders through some other seam, which navigation
            # already drives. Nothing to execute.
            return
        await _run_rebuild(rebuild, view)

    @pytest.mark.parametrize("view_cls", V1_PATTERNS, ids=lambda c: c.__name__)
    async def test_v1_nav_rebuild_yields_edit_kwargs(self, view_cls):
        # _apply_navigation_edit splats a dict into the edit and silently
        # ignores anything else, so a V1 rebuild returning the wrong shape
        # loses its content instead of raising.
        result = await _run_rebuild(view_cls.nav_rebuild, _construct(view_cls))
        assert isinstance(result, dict)
        assert {"embed", "embeds", "content"} & result.keys()


# // ========================================( Class )======================================== // #


class TestMenuCategoryDestinationMatrix:
    """Every pattern reachable from a menu category, both versions.

    The pair is what matters. Two crashes shipped here: a V1 menu forced
    ``build_embed()`` onto V1 patterns that define none, and a V2 menu
    forced ``build_ui()`` onto V2 patterns that define none. Each pattern's
    own tests passed throughout.
    """

    @pytest.mark.parametrize("menu_cls,dest_cls", MENU_PAIRS, ids=lambda c: c.__name__)
    async def test_menu_hands_the_destination_a_runnable_rebuild(self, menu_cls, dest_cls):
        menu = menu_cls(
            interaction=_make_interaction(),
            categories=[{"label": "Target", "view": dest_cls}],
        )

        with patch.object(menu, "push", new_callable=AsyncMock) as mock_push:
            await _category_callback(menu, "Target")(_make_interaction())
            _, kwargs = mock_push.call_args
            rebuild = kwargs["rebuild"]

        if rebuild is None:
            return
        await _run_rebuild(rebuild, _construct(dest_cls))

    @pytest.mark.parametrize(
        "menu_cls,dest_cls",
        [(MenuView, dest) for dest in V2_PATTERNS]
        + [(MenuLayoutView, dest) for dest in V1_PATTERNS],
        ids=lambda c: c.__name__,
    )
    def test_cross_version_category_is_rejected_at_construction(self, menu_cls, dest_cls):
        # A message carries its component version one way, so this push could
        # never land. The category list names the destination up front, so the
        # mistake is answerable here rather than on a user's click.
        with pytest.raises(TypeError, match="cannot push across versions"):
            menu_cls(
                interaction=_make_interaction(),
                categories=[{"label": "Mismatched", "view": dest_cls}],
            )

    @pytest.mark.parametrize("menu_cls,dest_cls", MENU_PAIRS, ids=lambda c: c.__name__)
    async def test_menu_pushes_the_class_it_was_given(self, menu_cls, dest_cls):
        menu = menu_cls(
            interaction=_make_interaction(),
            categories=[{"label": "Target", "view": dest_cls}],
        )

        with patch.object(menu, "push", new_callable=AsyncMock) as mock_push:
            await _category_callback(menu, "Target")(_make_interaction())

            args, _ = mock_push.call_args
            assert args[0] is dest_cls
