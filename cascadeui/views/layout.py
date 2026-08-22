# // ========================================( Modules )======================================== // #


import asyncio
import logging
from typing import Any, Dict, Optional, Sequence

import discord
from discord.ui import ActionRow, Item, LayoutView, TextDisplay
from discord.ui.view import BaseView

from ..components.base import StatefulButton
from ..components.types import EmojiInput
from .base import _StatefulMixin

logger = logging.getLogger(__name__)

# // ========================================( Helpers )======================================== // #


# The phrase that marks an over-capacity rejection. A pattern that wants to
# restate the overflow in its own vocabulary matches on this rather than on a
# copy of the sentence, so a reworded message cannot silently stop matching.
_OVER_CAPACITY_MARKER = "40-component limit"


def count_components(item: Item) -> int:
    """Count *item* and everything under it, the way Discord counts.

    Matches discord.py's own accounting: the node itself plus every
    descendant, including a Section's accessory. Leaf items report 1.

    Pairs with :attr:`StatefulLayoutView.total_components` for a budget
    check before composing: what the view already holds, plus what this
    subtree would add, against
    :data:`~cascadeui.MAX_MESSAGE_COMPONENTS`. Those are the same two
    numbers the over-capacity message reports, so a caller's own check and
    the library's enforcement cannot disagree by transcription.

    Raises:
        TypeError: *item* is a view rather than a component. A view is not
            a node in its own tree, so counting one here reports a total
            one higher than Discord's -- the transcription error this
            pairing exists to remove. Read
            :attr:`StatefulLayoutView.total_components` instead.
    """
    if isinstance(item, BaseView):
        raise TypeError(
            f"count_components expects a component, got {type(item).__name__}, "
            f"which is a view.\n"
            f"  Fix: read view.total_components -- a view is not a node in "
            f"its own tree, and counting it here would report one more "
            f"component than Discord does."
        )
    if not isinstance(item, Item):
        raise TypeError(
            f"count_components expects a component, got {type(item).__name__}.\n"
            f"  Fix: measure the component, not what it will be built from "
            f"-- a builder wraps a bare string into a node, and this would "
            f"report one for it either way."
        )
    walk = getattr(item, "walk_children", None)
    if walk is None:
        return 1
    return 1 + sum(1 for _ in walk())


def count_characters(item: Item) -> int:
    """Sum the display text in *item* and everything under it.

    The character counterpart of :func:`count_components`, and it counts
    what discord.py counts: ``TextDisplay`` content, and nothing else.
    Text in a button label, a select placeholder, or an option label
    occupies the message without reaching this total, exactly as it does
    not reach :meth:`discord.ui.LayoutView.content_length`.

    Pairs with that method for a budget check before composing, the way
    :func:`count_components` pairs with
    :attr:`StatefulLayoutView.total_components`::

        if view.content_length() + count_characters(card) > MAX_MESSAGE_CHARACTERS:
            card = shorter_card()

    Measuring a subtree by adding it to a throwaway view instead borrows
    that view's component limit, so a subtree over the component budget
    fails a character measurement with an error about components.

    Raises:
        TypeError: *item* is a view rather than a component. Read
            ``view.content_length()`` for a view's own total.
    """
    if isinstance(item, BaseView):
        raise TypeError(
            f"count_characters expects a component, got {type(item).__name__}, "
            f"which is a view.\n"
            f"  Fix: call view.content_length() -- discord.py supplies a "
            f"view's own total."
        )
    if not isinstance(item, Item):
        raise TypeError(
            f"count_characters expects a component, got {type(item).__name__}.\n"
            f"  Fix: measure the component, not what it will be built from "
            f"-- a builder wraps a bare string into a node, and this would "
            f"report zero for it either way."
        )
    total = len(item.content) if isinstance(item, TextDisplay) else 0
    walk = getattr(item, "walk_children", None)
    if walk is None:
        return total
    return total + sum(len(child.content) for child in walk() if isinstance(child, TextDisplay))


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
        allowed_mentions: Optional[discord.AllowedMentions] = None,
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
            allowed_mentions: Mention rules for this message. Overrides
                the ``allowed_mentions`` class attribute; when both are
                ``None`` the bot's client-level rules apply.
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
        mentions = self._resolve_allowed_mentions(allowed_mentions)
        if mentions is not None:
            send_kwargs["allowed_mentions"] = mentions
        return await self._send_pipeline(send_kwargs, ephemeral=ephemeral)

    @property
    def total_components(self) -> int:
        """How many components this view currently holds, counted recursively.

        The number Discord counts and discord.py enforces against: every
        Container, Section, ActionRow, button, select, and text node, a
        Section's accessory included, and never the view itself.

        Read it with :data:`~cascadeui.MAX_MESSAGE_COMPONENTS` and
        :func:`count_components` for a budget check before adding a
        subtree::

            if view.total_components + count_components(card) > MAX_MESSAGE_COMPONENTS:
                card = trimmed_card()

        A tree can never exist over the cap, since ``add_item`` refuses the
        node that would cross it, so this reports a live count rather than
        a violation to discover later.
        """
        return getattr(self, "_total_children", 0) or 0

    def add_item(self, item: Item):
        """Add a top-level child, with a friendlier 40-component message.

        Discord caps a V2 message at 40 components, counted recursively,
        and discord.py raises a terse ``ValueError`` from ``add_item`` the
        moment the tree would cross it -- before the send and before the
        placement validator runs. This override re-raises with the running
        count and a directed fix. It is the aggregate-size sibling of
        ``validate_placement``'s per-node checks -- same error shape (count
        plus a one-line fix), at the construction seam instead of the
        pre-flight seam: discord.py owns the enforcement, the library owns
        the message.
        """
        try:
            return super().add_item(item)
        except ValueError as exc:
            if "maximum number of children exceeded" not in str(exc):
                raise
            raise ValueError(self._over_capacity_message(item)) from exc

    def _over_capacity_message(self, item: Item) -> str:
        """Explain a 40-component rejection in terms of the two counts.

        Reporting only the view's own count reads as a contradiction when
        the overflow arrives as one oversized subtree: an empty view
        genuinely holds zero components. The size of what is being added is
        the number that explains the rejection.
        """
        held = self.total_components
        incoming = count_components(item)
        return (
            f"Invalid V2 layout: this LayoutView would exceed Discord's "
            f"{_OVER_CAPACITY_MARKER} for a single message. It holds {held} "
            f"component(s) and {type(item).__name__} adds {incoming} more "
            f"({held + incoming} > 40), counted recursively across every "
            f"Container, Section, ActionRow, button, and text node "
            f"(a Section's accessory counts too).\n"
            f"  Fix: trim the tree -- drop or merge content nodes (fold "
            f"adjacent TextDisplays, remove a divider or Section "
            f"accessory), compact the pager to prev / go-to / next + Back "
            f"+ Exit in one ActionRow via "
            f"PaginatedRegion.control_buttons(view, compact=True), or "
            f"split the content across multiple messages."
        )

    def _install_refresh_button(self, button: StatefulButton) -> None:
        """V2 wraps the refresh button in an ActionRow before adding it."""
        self.add_item(ActionRow(button))

    # // ==================( V2 Layout Helpers )================== // #

    def _add_back_button(self):
        """Add a back button wrapped in an ActionRow for V2 layout views."""
        action_row = ActionRow(self.make_back_button(custom_id=f"nav_back_{self.id[:8]}"))
        # Stash the row so paginated / tabbed / wizard rebuild paths that
        # call ``clear_items()`` can restore the navigation back button
        # after recomposing their own component tree.
        self._auto_back_item = action_row
        self.add_item(action_row)

    def make_nav_row(
        self,
        *,
        back: bool = True,
        exit: bool = True,
        back_label: str = "Back",
        exit_label: str = "Exit",
        back_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        back_emoji: EmojiInput = "◀",
        exit_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        exit_emoji: EmojiInput = "❌",
        delete_message: Optional[bool] = None,
        back_custom_id: Optional[str] = None,
        exit_custom_id: Optional[str] = None,
    ) -> ActionRow:
        """Return an ``ActionRow`` combining a Back and/or an Exit button.

        Builds the common pushed-sub-view footer -- a Back button that pops
        the navigation stack next to an Exit button -- in one call. Both
        buttons are on by default; set ``back=False`` or ``exit=False`` to
        drop either. With :meth:`on_load` defined, Back re-reads the restored
        parent's data automatically, so no rebuild callback is needed.

        ``back_label`` / ``back_style`` / ``back_emoji`` (and the ``exit_``
        equivalents) customize each button -- a relabeled Back such as
        ``make_nav_row(back_label="Leagues", back_emoji="\U0001f3e0")`` needs
        no manual composition. ``delete_message`` defaults to ``None``, which
        defers the Exit button to the view's ``exit_policy``. For persistent
        subclasses, pass ``back_custom_id`` / ``exit_custom_id`` so the
        buttons survive a restart.
        """
        if not back and not exit:
            raise ValueError("make_nav_row() requires at least one of back= or exit=.")
        buttons = []
        if back:
            buttons.append(
                self.make_back_button(
                    label=back_label,
                    style=back_style,
                    emoji=back_emoji,
                    custom_id=back_custom_id,
                )
            )
        if exit:
            buttons.append(
                self.make_exit_button(
                    label=exit_label,
                    style=exit_style,
                    emoji=exit_emoji,
                    delete_message=delete_message,
                    custom_id=exit_custom_id,
                )
            )
        return ActionRow(*buttons)

    def add_exit_button(
        self,
        label="Exit",
        style=discord.ButtonStyle.secondary,
        emoji="\u274c",
        delete_message=None,
        custom_id=None,
    ):
        """Add a button that exits this view, wrapped in an ActionRow.

        Thin wrapper over :meth:`make_exit_button` that attaches the
        result inside an ``ActionRow``. ``delete_message=None`` (the
        default) defers to the view's ``exit_policy``. For
        ``PersistentLayoutView`` subclasses, pass a ``custom_id``.

        Takes no ``row``: V2 lays out by tree position, not row index, so
        a V1-style ``row=`` argument raises here rather than being
        accepted and ignored.
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
        self._build_ui_sync()

    def build_ui(self) -> None:
        self.clear_items()
        self.add_item(self._container)
