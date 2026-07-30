# // ========================================( Modules )======================================== // #


from typing import Any, ClassVar, Dict, List, Optional

import discord
from discord import Interaction
from discord.ui import ActionRow

from ...components.base import StatefulButton
from ...components.patterns.v2 import action_section, card
from ...utils.hooks import await_maybe
from ..base import _StatefulMixin
from ..layout import StatefulLayoutView
from ..view import StatefulView

# // ========================================( Shared Mixin )======================================== // #


class _BaseMenuMixin:
    """Version-agnostic menu logic shared by ``MenuView`` and ``MenuLayoutView``.

    Holds the customization attributes, the ``on_category_selected`` hook,
    and the ``_make_push_callback`` factory. V1 and V2 subclasses supply
    the per-category render path and the ``nav_rebuild`` shape a plain
    destination falls back to when it names none of its own.

    Internal. Not exported. The public hierarchy
    (``MenuView`` / ``MenuLayoutView``) is unchanged.
    """

    menu_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.primary
    auto_exit_button: ClassVar[bool] = True

    # The method this menu's own ``nav_rebuild`` calls on a destination.
    # A category destination that lacks it renders some other way, so the
    # fallback stays clear of it. V1 and V2 subclasses each name theirs.
    _category_render_hook: ClassVar[str] = ""

    _BUTTON_STYLE_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BUTTON_STYLE_ATTRS,
        "menu_style",
    )
    _BOOL_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BOOL_ATTRS,
        "auto_exit_button",
    )

    async def on_category_selected(
        self, category: Dict[str, Any], index: int, interaction: Interaction
    ) -> None:
        """Called before pushing to the selected category's view.

        Default is a no-op. Override for analytics, pre-push setup, or
        guard logic (raise to cancel the push).
        """
        return None

    def _validate_categories(self, categories: List[Dict[str, Any]]) -> None:
        """Reject a destination this menu could never navigate to.

        A message carries its component version one way, so ``push()`` blocks
        a V1 menu from reaching a V2 destination. The category list names
        every destination up front, which makes construction the seam that
        can answer for it: left to the push, the mismatch reaches the user
        as a dead button.
        """
        from discord.ui import LayoutView as _LayoutView

        self_is_v2 = isinstance(self, _LayoutView)
        for index, category in enumerate(categories):
            # Shape first: every read below and in the item builders assumes a
            # mapping with both keys, so a tuple entry otherwise fails inside
            # this check with an AttributeError naming neither the entry nor
            # the expected shape, and a missing key surfaces later as a bare
            # KeyError from the builder. Duck-typed rather than isinstance so
            # any mapping keeps working, not only dict.
            if not hasattr(category, "get") or not hasattr(category, "__getitem__"):
                raise TypeError(
                    f"{type(self).__name__} categories[{index}] must be a mapping "
                    f"with 'label' and 'view', got {type(category).__name__}: "
                    f"{category!r}\n"
                    f"  Fix: pass {{'label': ..., 'view': ...}} per category."
                )
            for key in ("label", "view"):
                if key not in category:
                    raise ValueError(
                        f"{type(self).__name__} categories[{index}] is missing "
                        f"required key {key!r}.\n"
                        f"  Fix: every category needs both 'label' and 'view'; "
                        f"'emoji', 'description', 'style' and 'rebuild' are optional."
                    )
            view_cls = category.get("view")
            if not isinstance(view_cls, type):
                continue
            if issubclass(view_cls, _LayoutView) is self_is_v2:
                continue
            source = "V2 (LayoutView)" if self_is_v2 else "V1 (View)"
            target = "V1 (View)" if self_is_v2 else "V2 (LayoutView)"
            raise TypeError(
                f"{type(self).__name__} is {source} and category "
                f"{category.get('label')!r} points at {view_cls.__name__}, "
                f"which is {target}. A menu cannot push across versions. "
                f"Fix: give the category a {source} view."
            )

    def _make_push_callback(self, category: Dict[str, Any], index: int):
        view_cls = category["view"]
        # An explicit per-category rebuild wins. Otherwise a destination that
        # names its own nav_rebuild keeps it, and one that renders through
        # some other seam entirely (on_load, or its own __init__) is left
        # alone, since the menu's fallback calls a method it does not have.
        # What remains is the plain view the fallback was written for.
        rebuild = category.get("rebuild")
        if (
            rebuild is None
            and getattr(view_cls, "nav_rebuild", None) is None
            and hasattr(view_cls, self._category_render_hook)
        ):
            rebuild = type(self).nav_rebuild

        async def callback(interaction: Interaction):
            await await_maybe(self.on_category_selected(category, index, interaction))
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
            ``push(rebuild=...)``. A destination naming its own
            ``nav_rebuild`` uses that; one naming none falls back to
            ``lambda v: {"embed": v.build_embed()}``. Optional.

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

    _category_render_hook: ClassVar[str] = "build_embed"
    nav_rebuild = staticmethod(lambda v: {"embed": v.build_embed()})

    async def send(
        self,
        content: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
        **kwargs,
    ):
        """Send the view, using ``build_embed()`` when no content is given.

        The hub card is the menu's own render, so sending without one ships
        the category buttons over an empty body. An explicit ``embed`` or
        ``content`` wins.
        """
        if embed is None and content is None:
            embed = self.build_embed()
        return await super().send(
            content=content,
            embed=embed,
            **kwargs,
        )

    def __init__(
        self,
        *args,
        categories: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._categories: List[Dict[str, Any]] = categories or []
        self._validate_categories(self._categories)
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

    def _build_category_button(self, category: Dict[str, Any], index: int) -> StatefulButton:
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

        # Restore the navigation back button if push() added one.
        self._restore_navigation_artifacts()

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
        description (str): Text displayed in the ``action_section``. Without
            one, the label renders as the section text. Optional.
        style (ButtonStyle): Per-category override. Falls back to
            ``menu_style``. Optional.
        rebuild (callable): Per-category rebuild callable passed to
            ``push(rebuild=...)``. A destination naming its own
            ``nav_rebuild`` uses that; one naming none falls back to
            ``lambda v: v.build_ui()``. Optional.

    Customization:
        Override ``menu_style`` to set the default button style for all
        category items. Override ``build_header()`` / ``build_footer()``
        to add components above or below the category list. Set
        ``auto_exit_button = True`` to auto-add an exit button in an
        ActionRow at the bottom.

    Override hooks:
        ``on_category_selected(category, index, interaction)`` fires
        before the push. Default is a no-op.
        ``_build_category_item(category, index)`` controls how a single
        category is rendered. Default creates an ``action_section()``.
    """

    _category_render_hook: ClassVar[str] = "build_ui"
    nav_rebuild = staticmethod(lambda v: v.build_ui())

    def __init__(
        self,
        *args,
        categories: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._categories: List[Dict[str, Any]] = categories or []
        self._validate_categories(self._categories)
        self._build_ui_sync()

    def build_header(self):
        """Return V2 components for the area above category items.

        Override to add a title card, summary, or status display.
        Returns a list of V2 components or a single component; the
        default renders no header.
        """
        return []

    def build_footer(self):
        """Return V2 components for the area below category items.

        Override to add notes, status text, or extra action buttons.
        Returns a list of V2 components or a single component; the
        default renders no footer.
        """
        return []

    def _build_category_item(self, category: Dict[str, Any], index: int):
        """Build a single category's V2 action_section.

        Override to customize how individual categories are rendered.
        Must return a V2 component (typically an ``action_section()``).
        """
        # ``description`` is optional, but a Section needs non-empty text or
        # Discord rejects the whole message. Falling back to the label gives
        # the row a title line; the button beside it carries the same word.
        return action_section(
            category.get("description") or f"**{category['label']}**",
            label=category["label"],
            emoji=category.get("emoji"),
            callback=self._make_push_callback(category, index),
            style=category.get("style", self.menu_style),
        )

    def _build_category_card(self, items):
        """Wrap the category action_section items in a V2 card.

        Override to customize the card's accent color or structure. The
        default builds a theme-managed ``card()``: the ambient theme
        context supplies the accent, and the render-time resolution
        keeps it aligned with the view's live theme.
        """
        return card(*items)

    def build_ui(self):
        """Rebuild the full component tree from header, categories, footer."""
        self.clear_items()

        header = self.build_header()
        if header:
            items = header if isinstance(header, list) else [header]
            for item in items:
                self.add_item(item)

        category_items = [
            self._build_category_item(cat, i) for i, cat in enumerate(self._categories)
        ]
        if category_items:
            self.add_item(self._build_category_card(category_items))

        footer = self.build_footer()
        if footer:
            items = footer if isinstance(footer, list) else [footer]
            for item in items:
                self.add_item(item)

        if self.auto_exit_button:
            self.add_item(ActionRow(self.make_exit_button()))

        # Restore the navigation back button if push() added one.
        self._restore_navigation_artifacts()
