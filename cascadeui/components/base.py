# // ========================================( Modules )======================================== // #


import inspect
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

import discord
from discord.ui import Item

from ..state.actions import ActionCreators
from ..state.store import _CURRENT_INTERACTION

logger = logging.getLogger(__name__)


# // ========================================( Classes )======================================== // #


class StatefulComponent:
    """Base mixin for components that interact with state."""

    def create_stateful_callback(self, component, original_callback=None):
        """Create a callback that updates state."""
        component_id = getattr(component, "custom_id", None) or str(id(component))

        # Pre-compute whether to pass select values to the callback.
        # When the component is a select and the callback accepts a second
        # positional parameter, component.values is passed automatically.
        _pass_values = False
        if original_callback and hasattr(component, "values"):
            try:
                sig = inspect.signature(original_callback)
                params = [
                    p
                    for p in sig.parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                ]
                _pass_values = len(params) >= 2
            except (ValueError, TypeError):
                _pass_values = False

        async def stateful_callback(interaction):
            # Get view from the component itself
            view = component.view

            if not view:
                logger.error(f"Could not find view for component {component_id}")

                # Call original callback if provided
                if original_callback:
                    return await original_callback(interaction)
                return

            # Get component value
            value = None
            if hasattr(component, "value"):
                value = component.value
            elif hasattr(component, "values"):
                value = component.values
            elif isinstance(component, discord.ui.Button):
                value = True

            # Bind the live interaction for the acting-view fast path in
            # ``_StatefulMixin.refresh()``. Scope-narrow: set for the original
            # callback + dispatch sequence, reset in the ``finally`` so
            # subsequent interactions on the same event loop task never see
            # a stale value.
            token = _CURRENT_INTERACTION.set(interaction)
            try:
                # Call original callback FIRST so it can respond to the interaction
                # before state dispatch triggers on_state_changed notifications
                if original_callback:
                    if _pass_values and component.values is not None:
                        await original_callback(interaction, component.values)
                    else:
                        await original_callback(interaction)

                # Skip dispatch if the callback destroyed the view (exit, push, etc.)
                if view.is_finished():
                    return

                # Then dispatch state update (may trigger on_state_changed on views)
                payload = ActionCreators.component_interaction(
                    component_id=component_id,
                    view_id=view.id,
                    user_id=interaction.user.id,
                    value=value,
                )
                await view.dispatch("COMPONENT_INTERACTION", payload)
            finally:
                _CURRENT_INTERACTION.reset(token)

        return stateful_callback


class StatefulButton(discord.ui.Button, StatefulComponent):
    """A button that interacts with state."""

    def __init__(self, *args, callback=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)


# Sentinel used for the placeholder option injected when ``options=[]``.
# The value is namespaced so it never collides with a real caller value,
# and the label is an em-dash to read as "nothing here" at a glance.
_EMPTY_SELECT_VALUE = "__cascadeui_empty__"
_EMPTY_SELECT_OPTION = discord.SelectOption(label="\u2014", value=_EMPTY_SELECT_VALUE)


class StatefulSelect(discord.ui.Select, StatefulComponent):
    """A select menu that interacts with state.

    When ``options=[]`` is passed, a disabled placeholder option is
    substituted automatically and the select is forced to ``disabled=True``.
    This absorbs the Discord error 50035 that otherwise fires for
    dynamically-filtered selects whose filter produces an empty list --
    callers can pass the filtered list directly without a bespoke
    "render a disabled fallback" branch at every usage site.
    """

    def __init__(self, *args, callback=None, **kwargs):
        # Dynamic filters (e.g. "abilities still below the cap") may leave
        # ``options`` empty. Discord rejects zero-option selects; swap in
        # a single disabled placeholder so the select still renders and
        # the surrounding layout stays stable across state changes.
        options = kwargs.get("options")
        if options is not None and len(options) == 0:
            kwargs["options"] = [_EMPTY_SELECT_OPTION]
            kwargs["disabled"] = True

        super().__init__(*args, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)

    def set_selected(self, value: Union[str, Iterable[str], None]) -> None:
        """Mark which option(s) render with ``default=True``.

        The canonical way to reflect state in a select without rebuilding
        the whole component. Walks ``self.options`` once and sets
        ``opt.default = (opt.value in targets)``, so callers can drop the
        ``clear_items()`` + ``add_item()`` rebuild dance entirely::

            def build_ui(self):
                current = self.scoped_state.get("settings", {}).get("theme")
                self._theme_select.set_selected(current)
                return {"embed": self.build_embed()}

        Accepts three shapes:

        - ``None`` or an empty iterable clears every ``default`` flag.
        - A single ``str`` marks one matching option (the single-select
          common case).
        - An iterable of strings marks every matching option, for
          selects with ``max_values > 1``.

        Values that do not match any existing option are silently
        ignored. State-driven rebuilds may temporarily reference values
        that no longer exist (e.g. after a config migration drops an
        enum variant); silently no-op keeps the render alive rather
        than crashing the rebuild path.
        """
        if value is None:
            targets: set = set()
        elif isinstance(value, str):
            targets = {value}
        else:
            targets = set(value)

        for opt in self.options:
            opt.default = opt.value in targets

    def get_selected(self) -> List[str]:
        """Return the values of all options currently marked ``default=True``.

        Always returns a list for type stability, matching discord.py's
        ``Select.values`` convention (which is also always a list even
        for ``max_values == 1``). Single-select views typically read the
        result as::

            current = self._theme_select.get_selected()
            current_theme = current[0] if current else "default"

        Multi-select views (``max_values > 1``) can iterate the list
        directly. Returns an empty list when no option is marked default.
        """
        return [opt.value for opt in self.options if opt.default]
