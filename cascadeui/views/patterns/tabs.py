# // ========================================( Modules )======================================== // #


import logging
from typing import Callable, ClassVar, Dict, List, Optional, Tuple, Union

import discord
from discord import Interaction
from discord.ui import ActionRow

from ...components.base import StatefulButton
from ..base import _StatefulMixin
from ..view import StatefulView
from ..layout import StatefulLayoutView


logger = logging.getLogger(__name__)


# // ========================================( Constants )======================================== // #


_VALID_OVERFLOW_PRESETS: frozenset = frozenset({"fill", "balance", "pin_first", "pin_last"})
_ACTION_ROW_MAX = 5
_MESSAGE_ROW_MAX = 5


# // ========================================( Shared Mixin )======================================== // #


class _BaseTabMixin:
    """Version-agnostic tab logic shared by ``TabView`` and ``TabLayoutView``.

    Holds the customization attribute surface, the `on_tab_switched` hook,
    the switch callback factory, and the `switch_tab` / `active_tab` public
    surface. V1 and V2 subclasses supply only the button-construction and
    refresh paths that genuinely differ between component systems.

    Internal. Not exported. The public hierarchy
    (`TabView` / `TabLayoutView`) is unchanged.
    """

    active_tab_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.primary
    inactive_tab_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    tab_overflow_policy: ClassVar[Union[str, Tuple[int, ...]]] = "fill"

    _BUTTON_STYLE_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BUTTON_STYLE_ATTRS,
        "active_tab_style",
        "inactive_tab_style",
    )

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "tab_overflow_policy" in cls.__dict__:
            cls._validate_tab_overflow_policy(cls.__dict__["tab_overflow_policy"])

    @classmethod
    def _validate_tab_overflow_policy(cls, value) -> None:
        """Class-time validation for ``tab_overflow_policy``.

        Unambiguous typos raise ``ValueError``. Tuple/button-count drift
        is handled at runtime in ``_build_tab_rows`` -- tab counts are
        not known at class-definition time, so equality checks against
        the declared tuple belong in the runtime tier.
        """
        if isinstance(value, str):
            if value not in _VALID_OVERFLOW_PRESETS:
                valid = sorted(_VALID_OVERFLOW_PRESETS)
                raise ValueError(
                    f"{cls.__name__}.tab_overflow_policy {value!r} is not a valid preset. "
                    f"Valid presets: {valid}. Or pass a tuple of ints (e.g. (3, 3))."
                )
            return

        if isinstance(value, tuple):
            if not value:
                raise ValueError(
                    f"{cls.__name__}.tab_overflow_policy tuple must contain at least one row width."
                )
            if len(value) > _MESSAGE_ROW_MAX:
                raise ValueError(
                    f"{cls.__name__}.tab_overflow_policy has {len(value)} rows; "
                    f"Discord allows at most {_MESSAGE_ROW_MAX} component rows per message."
                )
            for i, width in enumerate(value):
                if not isinstance(width, int) or isinstance(width, bool):
                    raise ValueError(
                        f"{cls.__name__}.tab_overflow_policy row {i} is {width!r}; "
                        f"tuple entries must be integers."
                    )
                if width < 1:
                    raise ValueError(
                        f"{cls.__name__}.tab_overflow_policy row {i} is {width}; "
                        f"row widths must be >= 1."
                    )
                if width > _ACTION_ROW_MAX:
                    raise ValueError(
                        f"{cls.__name__}.tab_overflow_policy row {i} is {width}; "
                        f"ActionRow allows at most {_ACTION_ROW_MAX} buttons."
                    )
            return

        raise ValueError(
            f"{cls.__name__}.tab_overflow_policy must be a preset string "
            f"({sorted(_VALID_OVERFLOW_PRESETS)}) or a tuple of ints, got {type(value).__name__}."
        )

    def _build_tab_rows(
        self, buttons: List[StatefulButton]
    ) -> List[List[StatefulButton]]:
        """Split ``buttons`` into rows per ``tab_overflow_policy``.

        Default implementation branches on the attribute. Subclasses
        override for genuinely bespoke splits (conditional row widths,
        runtime-computed layouts). The return shape -- a list of lists
        of buttons -- is consumed identically by V1 (row index) and V2
        (ActionRow wrapping).
        """
        if not buttons:
            return []

        policy = self.tab_overflow_policy

        if isinstance(policy, tuple):
            return self._rows_from_tuple(buttons, policy)

        if policy == "balance":
            return self._rows_balance(buttons)
        if policy == "pin_first":
            return self._rows_pin_first(buttons)
        if policy == "pin_last":
            return self._rows_pin_last(buttons)
        return self._rows_fill(buttons)

    @staticmethod
    def _rows_fill(buttons: List[StatefulButton]) -> List[List[StatefulButton]]:
        return [buttons[i : i + _ACTION_ROW_MAX] for i in range(0, len(buttons), _ACTION_ROW_MAX)]

    @staticmethod
    def _rows_balance(buttons: List[StatefulButton]) -> List[List[StatefulButton]]:
        n = len(buttons)
        row_count = max(1, -(-n // _ACTION_ROW_MAX))
        base, extra = divmod(n, row_count)
        rows: List[List[StatefulButton]] = []
        start = 0
        for i in range(row_count):
            width = base + (1 if i < extra else 0)
            rows.append(buttons[start : start + width])
            start += width
        return rows

    @classmethod
    def _rows_pin_first(cls, buttons: List[StatefulButton]) -> List[List[StatefulButton]]:
        if len(buttons) <= 1:
            return [list(buttons)]
        return [buttons[:1]] + cls._rows_fill(buttons[1:])

    @classmethod
    def _rows_pin_last(cls, buttons: List[StatefulButton]) -> List[List[StatefulButton]]:
        if len(buttons) <= 1:
            return [list(buttons)]
        return cls._rows_fill(buttons[:-1]) + [buttons[-1:]]

    def _rows_from_tuple(
        self, buttons: List[StatefulButton], widths: Tuple[int, ...]
    ) -> List[List[StatefulButton]]:
        """Apply the declared tuple, auto-adjusting on button-count drift.

        Short declaration (``sum(widths) < N``): honor the declared split,
        then greedy-fill the remainder. Long declaration
        (``sum(widths) > N``): consume tuple entries until buttons run
        out; trailing entries are silently dropped. Both cases log a
        warning naming the class, declared tuple, actual tab count, and
        resolved rows -- the mismatch stays visible without blowing up a
        running bot.
        """
        n = len(buttons)
        declared_sum = sum(widths)
        rows: List[List[StatefulButton]] = []
        cursor = 0

        if declared_sum == n:
            for width in widths:
                rows.append(buttons[cursor : cursor + width])
                cursor += width
            return rows

        if declared_sum < n:
            for width in widths:
                rows.append(buttons[cursor : cursor + width])
                cursor += width
            remainder = buttons[cursor:]
            rows.extend(self._rows_fill(remainder))
            resolved = tuple(len(r) for r in rows)
            logger.warning(
                "%s.tab_overflow_policy=%r declares %d buttons but %d tab(s) were built; "
                "remaining buttons packed via fill into rows %r.",
                type(self).__name__,
                widths,
                declared_sum,
                n,
                resolved,
            )
            return rows

        for width in widths:
            remaining = n - cursor
            if remaining <= 0:
                break
            take = min(width, remaining)
            rows.append(buttons[cursor : cursor + take])
            cursor += take
        resolved = tuple(len(r) for r in rows)
        logger.warning(
            "%s.tab_overflow_policy=%r declares %d buttons but only %d tab(s) were built; "
            "trailing row widths dropped; resolved rows %r.",
            type(self).__name__,
            widths,
            declared_sum,
            n,
            resolved,
        )
        return rows

    async def on_tab_switched(self, index: int) -> None:
        """Called after ``self._active_tab`` is updated, before the refresh.

        Default is a no-op. Override for analytics, async setup, or
        validation that should fire on every tab change.
        """
        return None

    def _make_switch_callback(self, index: int):
        async def callback(interaction: Interaction):
            self._active_tab = index
            await self.on_tab_switched(index)
            await self._refresh_tabs()

        return callback

    @property
    def active_tab(self) -> str:
        """Name of the currently active tab."""
        return self._tab_names[self._active_tab]

    async def switch_tab(self, name: str):
        """Switch to a tab by name and refresh the view.

        Raises ``ValueError`` if the tab name is not found.
        """
        try:
            index = self._tab_names.index(name)
        except ValueError:
            raise ValueError(f"Tab '{name}' not found. Available: {self._tab_names}")
        self._active_tab = index
        await self.on_tab_switched(index)
        await self._refresh_tabs()


# // ========================================( V1: TabView )======================================== // #


class TabView(_BaseTabMixin, StatefulView):
    """Tabbed interface with button-based tab switching.

    Each tab is defined by a name and a builder function that returns
    an embed (and optionally extra components) for that tab's content.

    Customization:
        Override ``active_tab_style`` / ``inactive_tab_style`` class
        attributes (both ``discord.ButtonStyle``) to theme the tab row.

    Override hook:
        ``async def on_tab_switched(self, index: int)`` runs every time
        the active tab changes. Default implementation is a no-op -- the
        content rebuild runs separately via ``_refresh_tabs``. Use this
        hook for analytics, async setup, or validation without having
        to reimplement the tab-button wiring.
    """

    def __init__(self, *args, tabs: Optional[Dict[str, Callable]] = None, **kwargs):
        super().__init__(*args, **kwargs)

        self._tabs: Dict[str, Callable] = tabs or {}
        self._tab_names: List[str] = list(self._tabs.keys())
        self._active_tab: int = 0
        self._tab_buttons: List[StatefulButton] = []

        self._build_tab_buttons()
        self._build_extra_items()

    def _build_extra_items(self):
        """Hook for subclasses to add components alongside the tab row.

        Called once during init, after the tab buttons are built. Items
        added here persist across tab switches because ``_refresh_tabs``
        mutates button styles in place rather than clearing the view.
        Override to add components on rows 1-4 (row 0 is the tab row).
        """
        pass

    def _build_tab_buttons(self):
        """Create one button per tab, stored on ``self._tab_buttons``.

        Buttons are added once in ``__init__`` and mutated in place in
        ``_refresh_tabs``, so subclass additions to the view survive
        every switch without being clobbered. Row assignment comes from
        ``_build_tab_rows``, which honors ``tab_overflow_policy`` for
        tab counts that spill past the five-per-row ActionRow cap.
        """
        for i, name in enumerate(self._tab_names):
            style = self.active_tab_style if i == 0 else self.inactive_tab_style
            button = StatefulButton(
                label=name,
                style=style,
                custom_id=f"tab_{i}",
                callback=self._make_switch_callback(i),
            )
            self._tab_buttons.append(button)

        for row_idx, row_buttons in enumerate(self._build_tab_rows(self._tab_buttons)):
            for button in row_buttons:
                button.row = row_idx
                self.add_item(button)

    async def _refresh_tabs(self):
        """Mutate tab button styles in place and rebuild active content."""
        for i, button in enumerate(self._tab_buttons):
            button.style = (
                self.active_tab_style if i == self._active_tab else self.inactive_tab_style
            )

        tab_name = self._tab_names[self._active_tab]
        builder = self._tabs[tab_name]
        embed = await builder()

        await self.refresh(embed=embed)


# // ========================================( V2: TabLayoutView )======================================== // #


class TabLayoutView(_BaseTabMixin, StatefulLayoutView):
    """Tabbed interface with button-based tab switching for V2 layouts.

    The V2 equivalent of ``TabView``. Each tab is defined by a name and
    a builder returning a list of V2 components for that tab's content.

    Customization + override hook mirror ``TabView``.
    """

    def __init__(self, *args, tabs: Optional[Dict[str, Callable]] = None, **kwargs):
        super().__init__(*args, **kwargs)

        self._tabs: Dict[str, Callable] = tabs or {}
        self._tab_names: List[str] = list(self._tabs.keys())
        self._active_tab: int = 0
        self._tab_buttons: List[StatefulButton] = []
        # Tab buttons spill across multiple ActionRows when there are
        # more than five tabs, because a single ActionRow maxes at five
        # interactive children. A LayoutView tolerates several rows.
        self._tab_rows: List[ActionRow] = []
        self._extra_items: List = []

        self._build_tab_buttons()

        # Snapshot extra items: anything added by the subclass in
        # _build_extra_items() is preserved through tab switches.
        pre_extra = list(self.children)
        self._build_extra_items()
        self._extra_items = [c for c in self.children if c not in pre_extra]

    def _build_extra_items(self):
        """Hook for subclasses to add components alongside the tab row.

        Called ONCE during init. Items added here are snapshotted by the
        framework and preserved through every tab switch; the tab row is
        mutated in place. Override to add components that should persist
        regardless of which tab is active.
        """
        pass

    def _build_tab_buttons(self):
        """Create ActionRow(s) of tab buttons once at init.

        Row grouping comes from ``_build_tab_rows``, which honors
        ``tab_overflow_policy``. Each inner list is wrapped in a single
        ``ActionRow`` because ActionRow is the V2 container for
        interactive rows of up to five buttons.
        """
        for i, name in enumerate(self._tab_names):
            style = self.active_tab_style if i == 0 else self.inactive_tab_style
            button = StatefulButton(
                label=name,
                style=style,
                custom_id=f"tab_{i}",
                callback=self._make_switch_callback(i),
            )
            self._tab_buttons.append(button)

        for row_buttons in self._build_tab_rows(self._tab_buttons):
            row = ActionRow(*row_buttons)
            self._tab_rows.append(row)
            self.add_item(row)

    async def _refresh_tabs(self):
        """Mutate tab buttons in place and rebuild active tab content.

        The tab-button ActionRow and any items registered through
        ``_build_extra_items()`` keep their identity across refreshes;
        only the tab content children are rebuilt.
        """
        for i, button in enumerate(self._tab_buttons):
            button.style = (
                self.active_tab_style if i == self._active_tab else self.inactive_tab_style
            )

        # Clear and re-add in order: tab rows, content, extras.
        self.clear_items()
        for row in self._tab_rows:
            self.add_item(row)

        tab_name = self._tab_names[self._active_tab]
        builder = self._tabs[tab_name]
        content = await builder()

        if isinstance(content, list):
            for item in content:
                self.add_item(item)
        else:
            self.add_item(content)

        for extra in self._extra_items:
            self.add_item(extra)

        await self.refresh()

    async def send(self, **kwargs):
        """Build initial tab content before sending.

        Tab builders are async and cannot run in ``__init__``, so the
        first tab's content is built here before the message is sent.
        Children are re-assembled in canonical order (tab row, content,
        extras) so items registered by ``_build_extra_items()`` render
        beneath the initial tab content on first display.
        """
        if self._tab_names:
            builder = self._tabs[self._tab_names[self._active_tab]]
            content = await builder()

            self.clear_items()
            for row in self._tab_rows:
                self.add_item(row)
            if isinstance(content, list):
                for item in content:
                    self.add_item(item)
            else:
                self.add_item(content)
            for extra in self._extra_items:
                self.add_item(extra)

        return await super().send(**kwargs)
