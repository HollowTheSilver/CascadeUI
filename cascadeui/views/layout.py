# // ========================================( Modules )======================================== // #


import asyncio
import logging
from typing import Any, Dict, Optional, Sequence

import discord
from discord.ui import ActionRow, Item, LayoutView

from ..components.base import StatefulButton
from .base import _StatefulMixin

logger = logging.getLogger(__name__)

# // ========================================( Classes )======================================== // #


class StatefulLayoutView(_StatefulMixin, LayoutView):
    """Base class for all stateful V2 UI views.

    V2 views define their content via V2 components (Container, Section,
    TextDisplay, etc.) added as children. There are no content/embed/embeds
    parameters -- the component tree IS the message content.

    Interactive components (buttons, selects) must be wrapped in an
    ``ActionRow`` before being added to the view.
    """

    # Subclass config: V2 placement validation. When ``True`` (default),
    # ``_send_pipeline`` walks the assembled component tree and raises
    # ``ValueError`` on placements that Discord's API would reject with
    # HTTP 400 (Container nesting, standalone Button at top level,
    # Section accessory not in {Button, Thumbnail}, Modal-only types in
    # a LayoutView, Section nested inside Section, etc). The check runs
    # before the Discord round-trip, so the violation surfaces with a
    # clear path string instead of a terse 400 response. Set to
    # ``False`` only when discord.py / Discord has updated an
    # enforcement rule the validator has not caught up to yet.
    validate_placement: bool = True

    # ``validate_placement`` is V2-only -- ``_BOOL_ATTRS`` extends the
    # mixin's tuple so the validator's class-time bool check fires for
    # ``StatefulLayoutView`` subclasses without touching the V1 surface.
    _BOOL_ATTRS = _StatefulMixin._BOOL_ATTRS + ("validate_placement",)

    async def send(
        self,
        *,
        file: Optional[discord.File] = None,
        files: Optional[Sequence[discord.File]] = None,
        ephemeral: bool = False,
    ):
        """Send this V2 view as a message.

        Unlike V1's ``send()``, there are no
        ``content``/``embed``/``embeds`` parameters -- V2 views carry
        their display content as children (Container, TextDisplay,
        Section, etc.).

        See ``_StatefulMixin._send_pipeline`` for the full exception
        contract and rollback semantics.

        Args:
            file: Single attachment uploaded with the message. Pair with
                ``"attachment://<filename>"`` references inside
                ``gallery()``, ``image_section()``, or
                ``file_attachment()`` to render local files inline.
                Mutually exclusive with ``files``.
            files: Sequence of attachments uploaded with the message.
                Mutually exclusive with ``file``. discord.py raises
                ``TypeError`` when both are supplied.
            ephemeral: Whether the message should be ephemeral
                (interaction-context only).

        Returns:
            The sent ``discord.Message`` on success, or ``None`` when a
            policy gate blocked the send and the library handled the
            user response internally.
        """
        send_kwargs: Dict[str, Any] = {"view": self}
        if file is not None:
            send_kwargs["file"] = file
        if files is not None:
            send_kwargs["files"] = files
        return await self._send_pipeline(send_kwargs, ephemeral=ephemeral)

    def _install_refresh_button(self, button: StatefulButton) -> None:
        """V2 wraps the refresh button in an ActionRow before adding it."""
        self.add_item(ActionRow(button))

    # // ==================( V2 Layout Helpers )================== // #

    def _add_back_button(self):
        """Add a back button wrapped in an ActionRow for V2 layout views."""

        async def back_callback(interaction):
            await self._safe_defer(interaction)
            prev_view = await self.pop(interaction)
            if prev_view is None:
                # V2 messages ARE their components -- view=None would produce
                # an empty message.  Freeze instead.
                try:
                    self._freeze_components()
                    await self._bounded(interaction.edit_original_response(view=self))
                except asyncio.TimeoutError:
                    logger.debug(
                        f"Back-navigation freeze stalled past {self.edit_timeout}s "
                        f"in {type(self).__name__}."
                    )
                except discord.HTTPException as e:
                    logger.debug(
                        f"Back-navigation freeze failed in {type(self).__name__}: "
                        f"status={e.status} code={e.code}"
                    )
            # When prev_view is non-None, pop() routed through _apply_navigation_edit
            # which already swapped the message to the restored view.

        action_row = ActionRow(
            StatefulButton(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="\u25c0",
                custom_id=f"nav_back_{self.id[:8]}",
                callback=back_callback,
            )
        )
        # Stash the row so paginated / tabbed / wizard rebuild paths that
        # call ``clear_items()`` can restore the navigation back button
        # after recomposing their own component tree.
        self._auto_back_item = action_row
        self.add_item(action_row)

    def add_exit_button(
        self,
        label="Exit",
        style=discord.ButtonStyle.secondary,
        emoji="\u274c",
        delete_message=False,
        custom_id=None,
        **kwargs,
    ):
        """Add a button that exits this view, wrapped in an ActionRow.

        Thin wrapper over :meth:`make_exit_button` that attaches the
        result inside an ``ActionRow``. For ``PersistentLayoutView``
        subclasses, pass a ``custom_id``.
        """
        button = self.make_exit_button(
            label=label,
            style=style,
            emoji=emoji,
            delete_message=delete_message,
            custom_id=custom_id,
        )
        self.add_item(ActionRow(button))
        return button

    def clear_row(self, row: int):
        """No-op for V2 layout views.

        V2 views use a tree structure (Container > ActionRow > Button) rather
        than the flat row-based layout of V1. Use ``clear_items()`` or remove
        specific children instead.
        """
        pass


class DisplayLayoutView(StatefulLayoutView):
    """No-subclass V2 view that renders a pre-built container.

    Use when the goal is to ``send()`` a V2 component (typically as an
    ephemeral response) without authoring a full ``StatefulLayoutView``
    subclass. Interactive items inside the container still dispatch
    through the normal pipeline -- this class trades per-instance state
    for the ability to instantiate directly.

    Defaults differ from ``StatefulLayoutView`` to match the common
    one-shot use case:
        * ``owner_only = False`` -- display cards are typically public.
        * ``state_scope = None`` -- no per-scope state slice.

    Example:
        body = card(
            TextDisplay("## Stats"),
            key_value({"Games": "5", "Wins": "3"}),
        )
        await DisplayLayoutView(context=context, container=body).send(ephemeral=True)
    """

    owner_only = False
    state_scope = None

    def __init__(self, *args, container: Item, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._container = container
        self.build_ui()

    def build_ui(self) -> None:
        self.clear_items()
        self.add_item(self._container)
