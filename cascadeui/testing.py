# // ========================================( Modules )======================================== // #


import discord

__all__ = ["StubClient", "StubInteraction", "stub_client", "stub_interaction"]


# // ========================================( Classes )======================================== // #


class StubClient(discord.Client):
    """A real ``discord.Client`` that never connects.

    Some patterns compose a different tree depending on whether a bot is
    bound, so a view built with no client is not the view that ships. The
    leaderboard is the case in the library today: with a client bound, a
    section-mode row resolves an avatar and renders a four-node
    ``image_section``, and with none it renders a one-node stacked
    ``TextDisplay``. A five-row page differs by fifteen components between
    the two, which is more than a third of the per-message budget.

    Binding this closes that gap. The user cache is empty, which is the
    same state a live bot reports for a member it has not seen, and the
    library's own cache-miss branch resolves a default-avatar URL from it.
    The composed tree is therefore node-for-node the one a live bot sends.

    Construction opens no connection and needs no token. Calling
    ``start()``, ``login()``, or ``run()`` on it is a mistake discord.py
    reports itself.
    """

    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.none())


class StubResponse:
    """The response slot of a :class:`StubInteraction`.

    Tracks whether the slot is spent, the way a real one does, so a
    library path that checks ``is_done()`` before answering behaves as it
    would against Discord. Whatever is said through it is recorded on the
    owning interaction rather than sent.
    """

    def __init__(self, interaction: "StubInteraction") -> None:
        self._interaction = interaction
        self._done = False

    def is_done(self) -> bool:
        return self._done

    def _claim(self) -> None:
        """Spend the slot, refusing a second answer as the real one does.

        A double-response is the mistake this double most needs to keep
        reporting: permitting it offline would let a test pass over a
        seam that raises in production, which is the failure the whole
        offline surface exists to remove.
        """
        if self._done:
            raise discord.InteractionResponded(self._interaction)
        self._done = True

    async def defer(self, *args, **kwargs) -> None:
        self._claim()
        self._interaction.deferred = True

    async def send_message(self, content: str = "", **kwargs) -> None:
        self._claim()
        self._interaction.replies.append(content)

    async def send_modal(self, modal) -> None:
        self._claim()
        self._interaction.modals.append(modal)


class StubFollowup:
    """The followup half, recording into the same list as the response slot."""

    def __init__(self, interaction: "StubInteraction") -> None:
        self._interaction = interaction

    async def send(self, content: str = "", **kwargs) -> None:
        self._interaction.replies.append(content)


class StubInteraction:
    """An interaction that records instead of sending.

    Carries what the library actually reads off one: a response slot, a
    followup, and a user id. ``replies`` collects everything said through
    either half, so a test can assert a validator rejection was reported
    without knowing whether the response slot or the followup carried it
    -- which depends on whether an ack backstop had already fired, and is
    not a caller's to predict.
    """

    def __init__(self, user_id: int = 1, guild_id: int = 2) -> None:
        self.response = StubResponse(self)
        self.followup = StubFollowup(self)
        self.user = discord.Object(id=user_id)
        self.guild_id = guild_id
        self.replies: list = []
        self.modals: list = []
        self.deferred = False

    @property
    def answered(self) -> bool:
        """Whether anything was said or acknowledged through this.

        Opening a modal counts: it spends the response slot, so a seam
        that answered that way has answered.
        """
        return bool(self.replies) or bool(self.modals) or self.deferred


# // ========================================( Functions )======================================== // #


def stub_client() -> StubClient:
    """Return a :class:`StubClient` for measuring a view offline.

    Pair it with :attr:`~cascadeui.StatefulLayoutView.total_components` and
    :data:`~cascadeui.MAX_MESSAGE_COMPONENTS` to check a bot-gated pattern's
    real component budget with no Discord connection::

        from cascadeui import MAX_MESSAGE_COMPONENTS
        from cascadeui.testing import stub_client

        view = MyLeaderboard(user_id=1, guild_id=2, bot=stub_client())
        await view.on_load()
        view.validate()
        assert view.total_components <= MAX_MESSAGE_COMPONENTS

    A persistent view takes its client through ``on_bind`` instead, which
    the library drives at send and at restore::

        view = MyPersistentBoard(persistence_key="board:1")
        await view.on_bind(stub_client())
        await view.on_load()
    """
    return StubClient()


def stub_interaction(user_id: int = 1, guild_id: int = 2) -> StubInteraction:
    """Return a :class:`StubInteraction` for driving a seam offline.

    The double :meth:`cascadeui.Modal.submit` expects. It records what is
    said rather than sending it, so a validator rejection is assertable
    without a Discord connection::

        from cascadeui.testing import stub_interaction

        interaction = stub_interaction()
        assert await modal.submit(interaction, {"Name": "ab"}) is False
        assert "at least 3" in interaction.replies[0]
    """
    return StubInteraction(user_id=user_id, guild_id=guild_id)
