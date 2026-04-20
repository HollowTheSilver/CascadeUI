# // ========================================( Modules )======================================== // #


from typing import Any, Callable, ClassVar, Dict, List, Optional

import discord
from discord import Interaction
from discord.ui import ActionRow

from ...components.base import StatefulButton
from ...components.patterns.v2 import action_section, card
from ..base import _StatefulMixin
from ..view import StatefulView
from ..layout import StatefulLayoutView


# // ========================================( Shared Mixin )======================================== // #


class _BaseMenuMixin:
    """Version-agnostic menu logic shared by ``MenuView`` and ``MenuLayoutView``.

    Holds the customization attributes, the ``on_category_selected`` hook,
    and the ``_make_push_callback`` factory. V1 and V2 subclasses supply
    the per-category render path and the default rebuild lambda that
    ``push()`` should use when a category dict omits its own.

    Internal. Not exported. The public hierarchy
    (``MenuView`` / ``MenuLayoutView``) is unchanged.
    """

    menu_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.primary
    auto_exit_button: ClassVar[bool] = True

    _BUTTON_STYLE_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BUTTON_STYLE_ATTRS,
        "menu_style",
    )
    _BOOL_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BOOL_ATTRS,
        "auto_exit_button",
    )

    # Subclasses provide the default rebuild lambda for push() since the
    # V1 contract (returns embed kwargs) and V2 contract (rebuilds tree)
    # differ. Set on each concrete subclass.
    _default_rebuild: ClassVar[Optional[Callable]] = None

    async def on_category_selected(
        self, category: Dict[str, Any], index: int, interaction: Interaction
    ) -> None:
        """Called before pushing to the selected category's view.

        Default is a no-op. Override for analytics, pre-push setup, or
        guard logic (raise to cancel the push).
        """
        return None

    def _make_push_callback(self, category: Dict[str, Any], index: int):
        view_cls = category["view"]
        rebuild = category.get("rebuild", self._default_rebuild)

        async def callback(interaction: Interaction):
            await self.on_category_selected(category, index, interaction)
            await self.push(view_cls, interaction, rebuild=rebuild)

        return callback

    @property
    def categories(self) -> List[Dict[str, Any]]:
        """The category list this menu was constructed with."""
        return self._categories


# // ========================================( V1: MenuView )======================================== // #


class MenuView(_BaseMenuMixin, StatefulView):
    """Category-based navigation hub with push/pop drill-down.

    Each category is defined by a dict with ``label``, ``view`` (the
    target view class), and optional ``emoji``, ``description``, and
    ``style`` keys. The pattern auto-generates one button per category
    and wires push callbacks, eliminating the repetitive ``go_*`` methods
    that every hub view would otherwise need.

    Category dict keys:
        label (str): Button label. Required.
        view (type): View class to push to. Required.
        emoji (str): Button emoji. Optional.
        description (str): Not displayed in V1 (reserved for V2). Optional.
        style (ButtonStyle): Per-category override. Falls back to
            ``menu_style``. Optional.
        rebuild (callable): Per-category rebuild callable passed to
            ``push(rebuild=...)``. Falls back to the default V1 rebuild
            (``lambda v: {"embed": v.build_embed()}``). Optional.

    Customization:
        Override ``menu_style`` to set the default button style for all
        category buttons. Override ``build_embed()`` to provide the hub's
        embed content. Override ``_build_extra_items()`` to add components
        alongside the category buttons (e.g. a Reset button). Set
        ``auto_exit_button = True`` to auto-add an exit button.

    Override hooks:
        ``on_category_selected(category, index, interaction)`` fires
        before the push. Default is a no-op.
        ``_build_category_button(category, index)`` controls how a single
        category button is rendered. Default creates a ``StatefulButton``.
    """

    _default_rebuild = staticmethod(lambda v: {"embed": v.build_embed()})

    def __init__(
        self,
        *args,
        categories: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._categories: List[Dict[str, Any]] = categories or []
        self._category_buttons: List[StatefulButton] = []

        self._build_category_buttons()
        self._build_extra_items()

        if self.auto_exit_button:
            self.add_exit_button(row=4)

    def _build_extra_items(self):
        """Hook for subclasses to add components alongside category buttons.

        Called once during init, after category buttons are built but
        before the exit button. Override to add domain-specific controls
        (e.g. a Reset All button on a later row).
        """
        pass

    def _build_category_button(
        self, category: Dict[str, Any], index: int
    ) -> StatefulButton:
        """Build a single category button.

        Override to customize button appearance per category.
        """
        return StatefulButton(
            label=category["label"],
            style=category.get("style", self.menu_style),
            emoji=category.get("emoji"),
            row=index // 5,
            callback=self._make_push_callback(category, index),
        )

    def build_embed(self) -> discord.Embed:
        """Build the hub embed displayed alongside category buttons.

        Override to provide a summary card. Default returns a minimal
        embed with the class name as the title.
        """
        return discord.Embed(title="Menu")

    def _build_category_buttons(self):
        """Create one button per category and add to the view."""
        for i, category in enumerate(self._categories):
            button = self._build_category_button(category, i)
            self._category_buttons.append(button)
            self.add_item(button)

    def build_ui(self):
        """Rebuild category buttons and embed for state-driven updates."""
        self.clear_items()
        self._category_buttons.clear()

        self._build_category_buttons()
        self._build_extra_items()

        if self.auto_exit_button:
            self.add_exit_button(row=4)

        return {"embed": self.build_embed()}


# // ========================================( V2: MenuLayoutView )======================================== // #


class MenuLayoutView(_BaseMenuMixin, StatefulLayoutView):
    """Category-based navigation hub with push/pop drill-down for V2 layouts.

    The V2 equivalent of ``MenuView``. Each category generates an
    ``action_section()`` item with a description and inline push button.
    The pattern auto-generates the push callbacks, eliminating the
    repetitive ``go_*`` methods that every hub view would otherwise need.

    Category dict keys:
        label (str): Button label. Required.
        view (type): View class to push to. Required.
        emoji (str): Button emoji. Optional.
        description (str): Text displayed in the ``action_section``.
            Optional, defaults to empty string.
        style (ButtonStyle): Per-category override. Falls back to
            ``menu_style``. Optional.
        rebuild (callable): Per-category rebuild callable passed to
            ``push(rebuild=...)``. Falls back to ``lambda v: v.build_ui()``.
            Optional.

    Customization:
        Override ``menu_style`` to set the default button style for all
        category items. Override ``_build_header()`` / ``_build_footer()``
        to add components above or below the category list. Set
        ``auto_exit_button = True`` to auto-add an exit button in an
        ActionRow at the bottom.

    Override hooks:
        ``on_category_selected(category, index, interaction)`` fires
        before the push. Default is a no-op.
        ``_build_category_item(category, index)`` controls how a single
        category is rendered. Default creates an ``action_section()``.
    """

    _default_rebuild = staticmethod(lambda v: v.build_ui())

    def __init__(
        self,
        *args,
        categories: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._categories: List[Dict[str, Any]] = categories or []
        self.build_ui()

    def _build_header(self):
        """Return V2 components for the area above category items.

        Override to add a title card, summary, or status display.
        Returns a list of V2 components or a single component.
        Default returns an empty list.
        """
        return []

    def _build_footer(self):
        """Return V2 components for the area below category items.

        Override to add notes, status text, or extra action buttons.
        Returns a list of V2 components or a single component.
        Default returns an empty list.
        """
        return []

    def _build_category_item(self, category: Dict[str, Any], index: int):
        """Build a single category's V2 action_section.

        Override to customize how individual categories are rendered.
        Must return a V2 component (typically an ``action_section()``).
        """
        return action_section(
            category.get("description", ""),
            label=category["label"],
            emoji=category.get("emoji"),
            callback=self._make_push_callback(category, index),
            style=category.get("style", self.menu_style),
        )

    def _build_category_card(self, items):
        """Wrap the category action_section items in a V2 card.

        Override to customize the card's accent color or structure.
        The default pulls ``accent_colour`` from ``self.get_theme()``.
        """
        accent = None
        theme = self.get_theme()
        if theme:
            accent = theme.get_style("accent_colour")
        return card(*items, color=accent)

    def build_ui(self):
        """Rebuild the full component tree from header, categories, footer."""
        self.clear_items()

        header = self._build_header()
        if header:
            items = header if isinstance(header, list) else [header]
            for item in items:
                self.add_item(item)

        category_items = [
            self._build_category_item(cat, i) for i, cat in enumerate(self._categories)
        ]
        if category_items:
            self.add_item(self._build_category_card(category_items))

        footer = self._build_footer()
        if footer:
            items = footer if isinstance(footer, list) else [footer]
            for item in items:
                self.add_item(item)

        if self.auto_exit_button:
            self.add_item(ActionRow(self.make_exit_button()))
