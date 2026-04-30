# // ========================================( Modules )======================================== // #


import logging
from typing import Any, Callable, ClassVar, Dict, List, Optional

import discord
from discord import Interaction
from discord.ui import ActionRow, Button

from ...components.base import StatefulButton
from ...components.patterns.v2 import card, progress_bar
from ...components.types import EmojiInput
from ..base import _StatefulMixin
from ..layout import StatefulLayoutView
from ..view import StatefulView
from .types import WizardSchema, _normalize_steps

logger = logging.getLogger(__name__)


# // ========================================( Shared Mixin )======================================== // #


class _BaseWizardMixin:
    """Version-agnostic wizard logic shared by ``WizardView`` and ``WizardLayoutView``.

    Holds the customization triples, the ``on_finish`` hook, label
    resolvers, and the ``_go_back`` / ``_go_next`` callback bodies.
    V1 and V2 subclasses supply only the button construction and refresh
    paths that genuinely differ between component systems.

    Internal. Not exported. The public hierarchy
    (``WizardView`` / ``WizardLayoutView``) is unchanged.
    """

    # // ----( Customization triples )---- // #

    back_button_label: ClassVar[Optional[str]] = None
    back_button_emoji: ClassVar[EmojiInput] = None
    back_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    next_button_label: ClassVar[Optional[str]] = None
    next_button_emoji: ClassVar[EmojiInput] = None
    next_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.primary

    finish_button_label: ClassVar[Optional[str]] = None
    finish_button_emoji: ClassVar[EmojiInput] = None
    finish_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.success

    # Callable(current, total) -> str. When None, defaults to "Step {n}/{total}".
    step_indicator_label: ClassVar[Optional[Callable[[int, int], str]]] = None

    # V2-only: render a progress bar above the current step's content.
    # Hidden automatically when only one visible step exists (no progress
    # to show). Override ``_build_progress_header`` to customize the
    # container. Has no effect on V1 ``WizardView``.
    show_progress_bar: ClassVar[bool] = True

    _BUTTON_STYLE_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BUTTON_STYLE_ATTRS,
        "back_button_style",
        "next_button_style",
        "finish_button_style",
    )
    _BOOL_ATTRS: ClassVar[tuple] = (
        *_StatefulMixin._BOOL_ATTRS,
        "show_progress_bar",
    )

    # // ----( Override hooks )---- // #

    async def on_finish(self, interaction: Interaction) -> None:
        """Called when the user clicks Finish on the last step.

        Default implementation exits the view; the post-callback defer in
        ``_scheduled_task`` acks the interaction when ``exit()`` routes
        through the channel endpoint. Override to persist wizard state,
        send a confirmation message, or transition to another view.
        """
        await self.exit()

    async def on_step_entered(self, step_index: int) -> None:
        """Called after the wizard advances to ``step_index``.

        Fires on forward (Next) and backward (Back) navigation once
        ``self._current_step`` reflects the new position, before the
        step's builder runs. Default is a no-op. Override to reset
        per-step state, log analytics, or refresh external data.

        Fire-and-forget: exceptions raised by an override are logged
        and swallowed so navigation always completes. Errors that must
        block the user belong in a ``validator`` on the step definition
        (which runs before the exit, and whose ``(False, error)`` return
        routes through ``on_validation_failed``).
        """

    async def on_step_exited(self, step_index: int) -> None:
        """Called before the wizard leaves ``step_index``.

        Fires on forward (Next) and backward (Back) navigation while
        ``self._current_step`` still points at the step being left.
        Default is a no-op. Override to commit per-step values, confirm
        discards, or log analytics.

        Fire-and-forget: exceptions raised by an override are logged
        and swallowed so navigation always completes.
        """

    async def on_validation_failed(
        self,
        step_index: int,
        error: str,
        interaction: Optional[Interaction] = None,
    ) -> None:
        """Called when a step validator returns ``(False, error)``.

        Default sends ``error`` as an ephemeral response on the
        interaction that triggered Next. Override to render errors
        inline in the step content, log, or transform the message.
        ``interaction`` may be ``None`` if the validation was triggered
        outside the usual Next click flow.

        Fire-and-forget: exceptions raised by an override are logged
        and swallowed so the failed-validation branch always returns
        cleanly (the step stays active either way; a validation failure
        never advances the wizard).
        """
        if interaction is None:
            return
        try:
            await self.respond(interaction, error, ephemeral=True)
        except discord.HTTPException as e:
            logger.debug(f"Could not send validation error in {self.__class__.__name__}: {e}")

    # // ----( Conditional step helpers )---- // #

    def _is_step_visible(self, step: Dict[str, Any]) -> bool:
        """Return whether ``step`` should participate in navigation.

        A step is visible when it has no ``condition`` key, or its
        ``condition(view)`` callable returns truthy at call time. The
        live view instance is passed as the sole argument so predicates
        can read ``self._scores``, ``self.values``, ``self.shared_data``
        or any other derived state without closing over construction-time
        variables. An exception inside the predicate is logged and the
        step is treated as visible (conservative fallback -- the user is
        more likely to want the step shown than silently skipped).
        """
        condition = step.get("condition")
        if condition is None:
            return True
        try:
            return bool(condition(self))
        except Exception as exc:
            logger.warning(
                f"Step condition in {type(self).__name__} raised; "
                f"treating step as visible: {exc}"
            )
            return True

    def _visible_step_indices(self) -> List[int]:
        return [i for i, step in enumerate(self._steps) if self._is_step_visible(step)]

    def _next_visible_index(self, from_index: int) -> Optional[int]:
        for i in range(from_index + 1, len(self._steps)):
            if self._is_step_visible(self._steps[i]):
                return i
        return None

    def _prev_visible_index(self, from_index: int) -> Optional[int]:
        for i in range(from_index - 1, -1, -1):
            if self._is_step_visible(self._steps[i]):
                return i
        return None

    # // ----( Label resolvers )---- // #

    def _resolve_step_label(self) -> str:
        visible = self._visible_step_indices()
        total = max(len(visible), 1)
        try:
            current_position = visible.index(self._current_step) + 1
        except ValueError:
            current_position = 1
        if self.step_indicator_label is not None:
            return self.step_indicator_label(current_position, total)
        return f"Step {current_position}/{total}"

    def _resolve_next_label(self, is_last: bool) -> str:
        if is_last:
            return self.finish_button_label or "Finish"
        return self.next_button_label or "Next"

    # // ----( Navigation callbacks )---- // #

    async def _call_hook_safe(self, hook, *args) -> None:
        """Run a fire-and-forget hook, logging any exception.

        Matches the wrapping pattern at ``base.py:_enforce_instance_limit``
        for ``on_replaced`` -- override errors must never block the
        surrounding navigation / lifecycle flow.
        """
        try:
            await hook(*args)
        except Exception as exc:
            logger.warning(f"{hook.__name__} raised in {type(self).__name__}: {exc}")

    async def _go_back(self, interaction: Interaction):
        prev = self._prev_visible_index(self._current_step)
        if prev is not None:
            old_index = self._current_step
            await self._call_hook_safe(self.on_step_exited, old_index)
            self._current_step = prev
            await self._call_hook_safe(self.on_step_entered, self._current_step)
            await self._refresh_wizard()

    async def _go_next(self, interaction: Interaction):
        step = self._steps[self._current_step] if self._steps else None
        if step and "validator" in step:
            valid, error = await step["validator"]()
            if not valid:
                await self._call_hook_safe(
                    self.on_validation_failed,
                    self._current_step,
                    error,
                    interaction,
                )
                return

        next_visible = self._next_visible_index(self._current_step)
        if next_visible is None:
            await self.on_finish(interaction)
            return

        old_index = self._current_step
        await self._call_hook_safe(self.on_step_exited, old_index)
        self._current_step = next_visible
        await self._call_hook_safe(self.on_step_entered, self._current_step)
        await self._refresh_wizard()

    # // ----( Properties )---- // #

    @property
    def current_step(self) -> int:
        return self._current_step

    @property
    def step_count(self) -> int:
        return len(self._steps)


# // ========================================( V1: WizardView )======================================== // #


class WizardView(_BaseWizardMixin, StatefulView):
    """Multi-step form with back/next navigation and per-step validation.

    Each step is defined by a builder function that returns an embed.
    Steps may carry an optional ``validator`` returning ``(valid, error)``
    that gates progression to the next step.

    Navigation buttons use the ``{back,next,finish}_button_{label,emoji,style}``
    class-attribute triples, mirroring the grammar of ``refresh_button_*``
    and ``text_edit_button_*`` elsewhere in the library.

    Override ``on_finish(interaction)`` to handle the final step. Default
    defers and calls ``self.exit()``.

    Usage:
        class SetupWizard(WizardView):
            back_button_label = "Previous"
            finish_button_label = "Create Character"
            finish_button_emoji = "\U0001f3b2"

            def __init__(self, *args, **kwargs):
                steps = [
                    {"name": "Welcome", "builder": self.build_welcome},
                    {"name": "Config", "builder": self.build_config,
                     "validator": self.validate_config},
                    {"name": "Confirm", "builder": self.build_confirm},
                ]
                super().__init__(*args, steps=steps, **kwargs)

            async def on_finish(self, interaction):
                await self.respond(interaction, "Setup complete!")
                await self.exit()
    """

    def __init__(
        self,
        *args,
        steps: Optional[List[Any]] = None,
        schema: Optional[WizardSchema] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._steps: List[Dict[str, Any]] = _normalize_steps(steps, schema, type(self).__name__)
        self._current_step: int = 0

        self._build_nav_buttons()

    def _build_nav_buttons(self):
        """Create back, step-indicator, and next buttons once at init.

        ``_refresh_wizard`` mutates these in place rather than rebuilding,
        so subclass additions (extra buttons added after ``super().__init__``)
        survive navigation.
        """
        self._back_btn = StatefulButton(
            label=self.back_button_label or "Back",
            emoji=self.back_button_emoji,
            style=self.back_button_style,
            custom_id="wizard_back",
            row=4,
            disabled=True,
            callback=self._go_back,
        )
        self.add_item(self._back_btn)

        self._step_indicator = discord.ui.Button(
            label=self._resolve_step_label(),
            style=discord.ButtonStyle.gray,
            custom_id="wizard_indicator",
            row=4,
            disabled=True,
        )
        self.add_item(self._step_indicator)

        is_last = self._next_visible_index(self._current_step) is None
        self._next_btn = StatefulButton(
            label=self._resolve_next_label(is_last),
            emoji=self.finish_button_emoji if is_last else self.next_button_emoji,
            style=self.finish_button_style if is_last else self.next_button_style,
            custom_id="wizard_next",
            row=4,
            callback=self._go_next,
        )
        self.add_item(self._next_btn)

    async def _refresh_wizard(self):
        """Update navigation state and rebuild current step content.

        Mutates the existing button instances in place; ``_build_nav_buttons``-registered
        items stay stable across step changes.
        """
        self._back_btn.disabled = self._prev_visible_index(self._current_step) is None
        self._step_indicator.label = self._resolve_step_label()

        is_last = self._next_visible_index(self._current_step) is None
        self._next_btn.label = self._resolve_next_label(is_last)
        self._next_btn.style = self.finish_button_style if is_last else self.next_button_style
        self._next_btn.emoji = self.finish_button_emoji if is_last else self.next_button_emoji

        if self._steps:
            step = self._steps[self._current_step]
            builder = step.get("builder")
            if builder:
                embed = await builder()
                await self.refresh(embed=embed)


# // ========================================( V2: WizardLayoutView )======================================== // #


class WizardLayoutView(_BaseWizardMixin, StatefulLayoutView):
    """Multi-step form with back/next navigation for V2 layouts.

    The V2 equivalent of ``WizardView``. Each step is defined by a builder
    returning a list of V2 components. Steps may carry an optional
    ``validator`` returning ``(valid, error)``.

    Customization triples and the ``on_finish`` hook mirror ``WizardView``.

    Usage:
        class SetupWizard(WizardLayoutView):
            back_button_label = "Previous"
            finish_button_label = "Create Character"

            def __init__(self, *args, **kwargs):
                steps = [
                    {"name": "Welcome", "builder": self.build_welcome},
                    {"name": "Config", "builder": self.build_config,
                     "validator": self.validate_config},
                    {"name": "Confirm", "builder": self.build_confirm},
                ]
                super().__init__(*args, steps=steps, **kwargs)

            async def on_finish(self, interaction):
                await self.respond(interaction, "Done!")
                await self.exit()
    """

    def __init__(
        self,
        *args,
        steps: Optional[List[Any]] = None,
        schema: Optional[WizardSchema] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._steps: List[Dict[str, Any]] = _normalize_steps(steps, schema, type(self).__name__)
        self._current_step: int = 0
        self._extra_items: List = []

        self._build_nav_buttons()

        # Snapshot extras: anything added by the subclass in
        # _build_extra_items() is preserved through step changes.
        pre_extra = list(self.children)
        self._build_extra_items()
        self._extra_items = [c for c in self.children if c not in pre_extra]

    def _build_extra_items(self):
        """Hook for subclasses to add components alongside the nav row.

        Called ONCE during init. Items added here are snapshotted by the
        framework and preserved through every step change; the nav row
        is mutated in place. Override to add components that should
        persist across steps regardless of step content.
        """
        pass

    def _build_nav_buttons(self):
        """Create back/indicator/next once in a single ActionRow.

        ``_refresh_wizard`` mutates these in place rather than rebuilding.
        Mirrors the V1 approach and preserves any extra items added via
        ``add_item`` after ``super().__init__``.
        """
        self._back_btn = StatefulButton(
            label=self.back_button_label or "Back",
            emoji=self.back_button_emoji,
            style=self.back_button_style,
            custom_id="wizard_back",
            disabled=True,
            callback=self._go_back,
        )

        self._step_indicator = Button(
            label=self._resolve_step_label(),
            style=discord.ButtonStyle.secondary,
            custom_id="wizard_indicator",
            disabled=True,
        )

        is_last = self._next_visible_index(self._current_step) is None
        self._next_btn = StatefulButton(
            label=self._resolve_next_label(is_last),
            emoji=self.finish_button_emoji if is_last else self.next_button_emoji,
            style=self.finish_button_style if is_last else self.next_button_style,
            custom_id="wizard_next",
            callback=self._go_next,
        )

        self._nav_row = ActionRow(self._back_btn, self._step_indicator, self._next_btn)
        self.add_item(self._nav_row)

    def _build_progress_header(self, visible_indices: List[int]):
        """Build the progress container shown above step content.

        Returns a ``card()`` wrapping a ``progress_bar`` sized to the
        visible-step count. The card inherits the view's theme accent
        through the active theme context. Override to return ``None``
        (suppress the header at runtime) or to substitute a richer
        container (stats card, step title, subtitle).
        """
        try:
            current_position = visible_indices.index(self._current_step) + 1
        except ValueError:
            current_position = 1
        total = len(visible_indices)
        return card(progress_bar(current_position, total, width=20, show_percent=True))

    async def _rebuild_step_content(self) -> None:
        """Clear and re-add children in canonical order.

        Order: progress header (when enabled), step content, nav row,
        extras. The nav row and ``_build_extra_items``-registered items
        keep their identity across step changes; only the step content
        is rebuilt from the current step's builder.

        The entire rebuild runs inside the view's theme context so
        ``card()`` calls in both ``_build_progress_header`` and the
        user's step builder inherit the view's accent colour.
        """
        from ...theming.context import _current_theme, set_current_theme

        token = set_current_theme(self.get_theme())
        try:
            self.clear_items()

            visible = self._visible_step_indices()
            if self.show_progress_bar and len(visible) > 1:
                header = self._build_progress_header(visible)
                if header is not None:
                    self.add_item(header)

            if self._steps:
                step = self._steps[self._current_step]
                builder = step.get("builder")
                if builder:
                    content = await builder()
                    if isinstance(content, list):
                        for item in content:
                            self.add_item(item)
                    else:
                        self.add_item(content)

            if self._nav_row is not None:
                self.add_item(self._nav_row)

            for extra in self._extra_items:
                self.add_item(extra)

            # Restore the navigation back button if push() added one.
            self._restore_navigation_artifacts()
        finally:
            _current_theme.reset(token)

    async def _refresh_wizard(self):
        """Update step content and mutate nav buttons in place."""
        self._back_btn.disabled = self._prev_visible_index(self._current_step) is None
        self._step_indicator.label = self._resolve_step_label()

        is_last = self._next_visible_index(self._current_step) is None
        self._next_btn.label = self._resolve_next_label(is_last)
        self._next_btn.style = self.finish_button_style if is_last else self.next_button_style
        self._next_btn.emoji = self.finish_button_emoji if is_last else self.next_button_emoji

        await self._rebuild_step_content()
        await self.refresh()

    async def send(self, **kwargs):
        """Build initial step content before sending.

        Step builders are async and cannot run in ``__init__``, so the
        first step's content is assembled here before the message is
        sent.
        """
        if self._steps:
            await self._rebuild_step_content()

        return await super().send(**kwargs)
