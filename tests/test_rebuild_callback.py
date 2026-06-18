"""Tests for the rebuild callback parameter on push() and pop()."""

# // ========================================( Modules )======================================== // #


from unittest.mock import AsyncMock, MagicMock

from helpers import make_interaction as _make_interaction

from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.view import StatefulView

# // ========================================( Fixtures )======================================== // #


class _HubView(StatefulLayoutView):
    """Root view for push/pop tests."""

    pass


class _SubView(StatefulLayoutView):
    """Target view for push tests."""

    pass


# // ========================================( Push Rebuild )======================================== // #


class TestPushRebuild:
    """push() with rebuild= calls rebuild and edits + acks in one round-trip."""

    async def test_sync_rebuild_called(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        tracker = MagicMock()
        new_view = await hub.push(_SubView, interaction, rebuild=tracker)

        tracker.assert_called_once_with(new_view)

    async def test_async_rebuild_called(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        tracker = AsyncMock()
        new_view = await hub.push(_SubView, interaction, rebuild=tracker)

        tracker.assert_called_once_with(new_view)

    async def test_interaction_acked_via_edit_message(self):
        interaction = _make_interaction(is_done=False)
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        await hub.push(_SubView, interaction, rebuild=lambda v: None)

        # Fast path acks via edit_message, not a separate defer.
        interaction.response.edit_message.assert_awaited_once()
        interaction.response.defer.assert_not_called()

    async def test_already_deferred_not_double_deferred(self):
        interaction = _make_interaction(is_done=True)
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        await hub.push(_SubView, interaction, rebuild=lambda v: None)

        interaction.response.defer.assert_not_called()

    async def test_message_edited_with_new_view(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await hub.push(_SubView, interaction, rebuild=lambda v: None)

        interaction.response.edit_message.assert_called_once_with(view=new_view)

    async def test_no_rebuild_still_edits(self):
        """The Discord message edit fires regardless of whether a rebuild
        callback is supplied. ``rebuild`` is a pre-edit hook for views that
        need post-construction setup; the navigation contract is that the
        new view replaces the old on screen.
        """
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await hub.push(_SubView, interaction)

        interaction.response.edit_message.assert_called_once_with(view=new_view)

    async def test_returns_new_view(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await hub.push(_SubView, interaction, rebuild=lambda v: None)

        assert isinstance(new_view, _SubView)


# // ========================================( Pop Rebuild )======================================== // #


class TestPopRebuild:
    """pop() with rebuild= calls rebuild and edits + acks in one round-trip."""

    async def _push_then_pop(self, *, rebuild=None):
        """Helper: send hub, push sub, then pop with rebuild.

        Uses separate interactions for send/push/pop so call assertions
        on the pop interaction are clean.
        """
        send_interaction = _make_interaction()
        hub = _HubView(interaction=send_interaction)
        await hub.send()

        push_interaction = _make_interaction()
        sub = await hub.push(_SubView, push_interaction)

        pop_interaction = _make_interaction()
        return await sub.pop(pop_interaction, rebuild=rebuild), pop_interaction

    async def test_sync_rebuild_called(self):
        tracker = MagicMock()

        restored, _ = await self._push_then_pop(rebuild=tracker)

        tracker.assert_called_once_with(restored)

    async def test_async_rebuild_called(self):
        tracker = AsyncMock()

        restored, _ = await self._push_then_pop(rebuild=tracker)

        tracker.assert_called_once_with(restored)

    async def test_interaction_acked_via_edit_message(self):
        _, pop_interaction = await self._push_then_pop(rebuild=lambda v: None)

        pop_interaction.response.edit_message.assert_awaited_once()
        pop_interaction.response.defer.assert_not_called()

    async def test_message_edited_with_restored_view(self):
        restored, pop_interaction = await self._push_then_pop(rebuild=lambda v: None)

        pop_interaction.response.edit_message.assert_called_once_with(view=restored)

    async def test_no_rebuild_still_edits(self):
        """Pop matches push: the message edit fires whether or not a
        rebuild callback is supplied.
        """
        restored, pop_interaction = await self._push_then_pop(rebuild=None)

        pop_interaction.response.edit_message.assert_called_once_with(view=restored)

    async def test_empty_stack_skips_rebuild(self):
        """pop() on empty stack returns None and does not call rebuild."""
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        tracker = MagicMock()
        result = await hub.pop(interaction, rebuild=tracker)

        assert result is None
        tracker.assert_not_called()


# // ========================================( V1 Compat )======================================== // #


class _V1Hub(StatefulView):
    pass


class _V1Sub(StatefulView):
    pass


class TestRebuildV1:
    """rebuild= works with V1 views too."""

    async def test_push_rebuild_v1(self):
        interaction = _make_interaction()
        hub = _V1Hub(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        tracker = MagicMock()
        new_view = await hub.push(_V1Sub, interaction, rebuild=tracker)

        tracker.assert_called_once_with(new_view)
        assert isinstance(new_view, _V1Sub)


# // ========================================( Dict Return )======================================== // #


class TestRebuildDictReturn:
    """When rebuild returns a dict, extra kwargs are passed to the message edit."""

    async def test_dict_kwargs_merged_into_edit(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        embed = MagicMock()
        new_view = await hub.push(_SubView, interaction, rebuild=lambda v: {"embed": embed})

        interaction.response.edit_message.assert_called_once_with(view=new_view, embed=embed)

    async def test_none_return_no_extra_kwargs(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await hub.push(_SubView, interaction, rebuild=lambda v: None)

        interaction.response.edit_message.assert_called_once_with(view=new_view)

    async def test_parent_message_ref_preserved_through_push(self):
        """The parent's plain ``Message`` ref carries through push.
        The ``edit_original_response`` return value is interaction-bound
        with a 15-minute lifetime; the plain ref provides the channel
        endpoint that subsequent edits need.
        """
        interaction = _make_interaction()
        edit_response = MagicMock(id=42)
        interaction.edit_original_response = AsyncMock(return_value=edit_response)

        parent_message = MagicMock(id=1, channel=MagicMock(id=2))
        hub = _HubView(interaction=interaction)
        hub._message = parent_message

        new_view = await hub.push(_SubView, interaction, rebuild=lambda v: None)

        assert new_view._message is parent_message
        assert new_view._message is not edit_response

    async def test_async_dict_return(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        embed = MagicMock()

        async def async_rebuild(v):
            return {"embed": embed, "content": "hello"}

        new_view = await hub.push(_SubView, interaction, rebuild=async_rebuild)

        interaction.response.edit_message.assert_called_once_with(
            view=new_view, embed=embed, content="hello"
        )
