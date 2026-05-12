# // ========================================( Modules )======================================== // #


from typing import Optional, Sequence

import discord
from discord import Embed
from discord.ui import View

from .base import _StatefulMixin

# // ========================================( Classes )======================================== // #


class StatefulView(_StatefulMixin, View):
    """Base class for all stateful V1 UI views."""

    async def send(
        self,
        content: Optional[str] = None,
        *,
        embed: Optional[Embed] = None,
        embeds: Optional[Sequence[Embed]] = None,
        file: Optional[discord.File] = None,
        files: Optional[Sequence[discord.File]] = None,
        ephemeral: bool = False,
    ):
        """Send this view as a message using the stored context or interaction.

        This is the preferred way to display a ``StatefulView``. It
        handles state registration, participant claiming, message
        re-fetching, and Discord-side delivery automatically.

        Args:
            content: Text content for the message.
            embed: A single embed to include.
            embeds: A list of embeds to include.
            file: Single attachment uploaded with the message. Mutually
                exclusive with ``files``.
            files: Sequence of attachments uploaded with the message.
                Mutually exclusive with ``file``. discord.py raises
                ``TypeError`` when both are supplied.
            ephemeral: Whether the message should be ephemeral
                (interaction-context only).

        Returns:
            The sent ``discord.Message`` on success, or ``None`` when a
            policy gate blocked the send and the library handled the
            user response internally.

        See ``_StatefulMixin._send_pipeline`` for the full exception
        contract and rollback semantics.
        """
        send_kwargs = {"view": self}
        if content is not None:
            send_kwargs["content"] = content
        if embed is not None:
            send_kwargs["embed"] = embed
        if embeds is not None:
            send_kwargs["embeds"] = embeds
        if file is not None:
            send_kwargs["file"] = file
        if files is not None:
            send_kwargs["files"] = files

        return await self._send_pipeline(send_kwargs, ephemeral=ephemeral)
