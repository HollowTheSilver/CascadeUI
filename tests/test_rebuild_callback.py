"""Tests for the rebuild callback parameter on push() and pop()."""

# // ========================================( Modules )======================================== // #


from unittest.mock import AsyncMock, MagicMock

from cascadeui.views.base import StatefulView
from cascadeui.views.layout import StatefulLayoutView
from helpers import make_interaction as _make_interaction


# // ========================================( Fixtures )======================================== // #


class _HubView(StatefulLayoutView):
    """Root view for push/pop tests."""

    pass


class _SubView(StatefulLayoutView):
    """Target view for push tests."""

    pass


# // ========================================( Push Rebuild )======================================== // #


class TestPushRebuild:
    """push() with rebuild= should defer, call rebuild, and edit the message."""

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

    async def test_interaction_deferred(self):
        interaction = _make_interaction(is_done=False)
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        await hub.push(_SubView, interaction, rebuild=lambda v: None)

        interaction.response.defer.assert_called_once()

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

        interaction.edit_original_response.assert_called_once_with(view=new_view)

    async def test_no_rebuild_skips_defer_and_edit(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        await hub.push(_SubView, interaction)

        interaction.response.defer.assert_not_called()
        interaction.edit_original_response.assert_not_called()

    async def test_returns_new_view(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await hub.push(_SubView, interaction, rebuild=lambda v: None)

        assert isinstance(new_view, _SubView)


# // ========================================( Pop Rebuild )======================================== // #


class TestPopRebuild:
    """pop() with rebuild= should defer, call rebuild, and edit the message."""

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

    async def test_interaction_deferred(self):
        _, pop_interaction = await self._push_then_pop(rebuild=lambda v: None)

        pop_interaction.response.defer.assert_called_once()

    async def test_message_edited_with_restored_view(self):
        restored, pop_interaction = await self._push_then_pop(rebuild=lambda v: None)

        pop_interaction.edit_original_response.assert_called_once_with(view=restored)

    async def test_no_rebuild_skips_defer_and_edit(self):
        _, pop_interaction = await self._push_then_pop(rebuild=None)

        pop_interaction.response.defer.assert_not_called()
        pop_interaction.edit_original_response.assert_not_called()

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
    """When rebuild returns a dict, extra kwargs are passed to edit_original_response."""

    async def test_dict_kwargs_merged_into_edit(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        embed = MagicMock()
        new_view = await hub.push(
            _SubView, interaction, rebuild=lambda v: {"embed": embed}
        )

        interaction.edit_original_response.assert_called_once_with(
            view=new_view, embed=embed
        )

    async def test_none_return_no_extra_kwargs(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await hub.push(_SubView, interaction, rebuild=lambda v: None)

        interaction.edit_original_response.assert_called_once_with(view=new_view)

    async def test_message_captured_from_edit_response(self):
        interaction = _make_interaction()
        msg_mock = MagicMock(id=42)
        interaction.edit_original_response = AsyncMock(return_value=msg_mock)

        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await hub.push(_SubView, interaction, rebuild=lambda v: None)

        assert new_view._message is msg_mock

    async def test_async_dict_return(self):
        interaction = _make_interaction()
        hub = _HubView(interaction=interaction)
        hub._message = MagicMock(id=1, channel=MagicMock(id=2))

        embed = MagicMock()

        async def async_rebuild(v):
            return {"embed": embed, "content": "hello"}

        new_view = await hub.push(_SubView, interaction, rebuild=async_rebuild)

        interaction.edit_original_response.assert_called_once_with(
            view=new_view, embed=embed, content="hello"
        )
