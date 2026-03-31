# // ========================================( Modules )======================================== // #


from typing import Optional

import discord
from discord import Interaction
from discord.ui import ActionRow, LayoutView

from ..components.base import StatefulButton
from ..utils.logging import AsyncLogger
from .base import SessionLimitError, _StatefulMixin

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")


# // ========================================( Classes )======================================== // #


class StatefulLayoutView(_StatefulMixin, LayoutView):
    """Base class for all stateful V2 UI views.

    V2 views define their content via V2 components (Container, Section,
    TextDisplay, etc.) added as children. There are no content/embed/embeds
    parameters -- the component tree IS the message content.

    Interactive components (buttons, selects) must be wrapped in an
    ``ActionRow`` before being added to the view.
    """

    async def send(self, *, ephemeral=False):
        """Send this V2 view as a message.

        Unlike V1's ``send()``, there are no ``content``/``embed``/``embeds``
        parameters. V2 views carry their display content as children
        (Container, TextDisplay, Section, etc.).

        Args:
            ephemeral: Whether the message should be ephemeral (interaction only).
        """
        await self._enforce_session_limit()

        # Register in instance registry before state so the view is
        # tracked even if the state dispatch triggers subscribers that query it
        self.state_store.register_view(self)
        await self._register_state()

        if ephemeral:
            self._ephemeral = True
            if self.timeout is None:
                logger.warning(
                    f"{self.__class__.__name__}: ephemeral views with timeout=None will lose "
                    "editability after the interaction token expires (15 minutes). "
                    "Consider setting a finite timeout."
                )

        send_kwargs = {"view": self}

        try:
            if self.context and hasattr(self.context, "send"):
                if ephemeral:
                    send_kwargs["ephemeral"] = ephemeral
                message = await self.context.send(**send_kwargs)

            elif self.interaction:
                send_kwargs["ephemeral"] = ephemeral
                if not self.interaction.response.is_done():
                    await self.interaction.response.send_message(**send_kwargs)
                    message = await self.interaction.original_response()
                else:
                    message = await self.interaction.followup.send(**send_kwargs, wait=True)

            else:
                raise RuntimeError(
                    "StatefulLayoutView.send() requires either 'context' or "
                    "'interaction' to be set."
                )
        except Exception:
            self.state_store.unregister_view(self.id)
            raise

        self._message = message
        await self._update_message_state(message)
        return message

    # // ==================( V2 Layout Helpers )================== // #

    def _add_back_button(self):
        """Add a back button wrapped in an ActionRow for V2 layout views."""

        async def back_callback(interaction):
            await interaction.response.defer()
            prev_view = await self.pop(interaction)
            if prev_view:
                try:
                    await interaction.edit_original_response(view=prev_view)
                except discord.HTTPException:
                    pass
            else:
                # V2 messages ARE their components — view=None would produce
                # an empty message.  Freeze instead.
                try:
                    self._freeze_components()
                    await interaction.edit_original_response(view=self)
                except discord.HTTPException:
                    pass

        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Back",
                    style=discord.ButtonStyle.secondary,
                    emoji="\u25c0",
                    custom_id=f"nav_back_{self.id[:8]}",
                    callback=back_callback,
                )
            )
        )

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

        For PersistentLayoutView subclasses, pass a ``custom_id``.
        """
        async def exit_callback(interaction):
            await interaction.response.defer()
            await self.exit(delete_message=delete_message)

        button = StatefulButton(
            label=label,
            style=style,
            emoji=emoji,
            custom_id=custom_id,
            callback=exit_callback,
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
