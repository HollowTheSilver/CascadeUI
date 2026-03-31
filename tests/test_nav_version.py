"""Tests for cross-version navigation enforcement (V1 <-> V2)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from cascadeui.views.base import StatefulView
from cascadeui.views.layout import StatefulLayoutView
from helpers import make_interaction as _make_interaction


class _V1View(StatefulView):
    """A minimal V1 view for nav tests."""

    pass


class _V2View(StatefulLayoutView):
    """A minimal V2 view for nav tests."""

    pass


class TestNavVersionEnforcement:
    """Push/pop between V1 and V2 must raise TypeError."""

    async def test_push_v1_to_v2_raises(self):
        interaction = _make_interaction()
        v1 = _V1View(interaction=interaction)
        v1._message = MagicMock(id=1, channel=MagicMock(id=2))

        with pytest.raises(TypeError, match="Cannot push/pop between"):
            await v1.push(_V2View, interaction)

    async def test_push_v2_to_v1_raises(self):
        interaction = _make_interaction()
        v2 = _V2View(interaction=interaction)
        v2._message = MagicMock(id=1, channel=MagicMock(id=2))

        with pytest.raises(TypeError, match="Cannot push/pop between"):
            await v2.push(_V1View, interaction)

    async def test_push_v1_to_v1_works(self):
        interaction = _make_interaction()
        v1 = _V1View(interaction=interaction)
        v1._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await v1.push(_V1View, interaction)
        assert isinstance(new_view, _V1View)

    async def test_push_v2_to_v2_works(self):
        interaction = _make_interaction()
        v2 = _V2View(interaction=interaction)
        v2._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await v2.push(_V2View, interaction)
        assert isinstance(new_view, _V2View)

    async def test_replace_v1_to_v2_allowed(self):
        """replace() is a one-way transition -- no version enforcement."""
        interaction = _make_interaction()
        v1 = _V1View(interaction=interaction)
        v1._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await v1.replace(_V2View, interaction)
        assert isinstance(new_view, _V2View)

    async def test_replace_v2_to_v1_allowed(self):
        interaction = _make_interaction()
        v2 = _V2View(interaction=interaction)
        v2._message = MagicMock(id=1, channel=MagicMock(id=2))

        new_view = await v2.replace(_V1View, interaction)
        assert isinstance(new_view, _V1View)
