# // ========================================( Modules )======================================== // #


from discord.ui import View

from .base import _StatefulMixin


# // ========================================( Classes )======================================== // #


class StatefulView(_StatefulMixin, View):
    """Base class for all stateful V1 UI views."""

    async def send(self, content=None, *, embed=None, embeds=None, ephemeral=False):
        """Send this view as a message using the stored context or interaction.

        This is the preferred way to display a ``StatefulView``. It
        handles state registration, participant claiming, message
        re-fetching, and Discord-side delivery automatically.

        Args:
            content: Text content for the message.
            embed: A single embed to include.
            embeds: A list of embeds to include.
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

        return await self._send_pipeline(send_kwargs, ephemeral=ephemeral)
