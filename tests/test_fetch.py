"""Tests for ``cascadeui.fetch_as_file`` helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cascadeui import fetch_as_file

# // ========================================( Helpers )======================================== // #


def _mock_session(body: bytes) -> MagicMock:
    """Build an aiohttp-session-shaped mock that returns ``body`` on read.

    aiohttp's session.get(url) returns an async-context-manager whose
    __aenter__ resolves to a response object; response.read() is async.
    The mock reproduces both surfaces so fetch_as_file's two await
    points (the get-context entry and the read) both resolve.
    """
    mock_resp = MagicMock()
    mock_resp.read = AsyncMock(return_value=body)

    mock_get_ctx = MagicMock()
    mock_get_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_get_ctx.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.get = MagicMock(return_value=mock_get_ctx)
    return session


# // ========================================( Session reuse )======================================== // #


class TestFetchAsFileWithSession:
    """``fetch_as_file`` reuses a caller-provided ``aiohttp.ClientSession``."""

    async def test_returns_discord_file(self):
        session = _mock_session(b"image bytes")
        result = await fetch_as_file("https://example.com/a.png", "a.png", session=session)
        assert isinstance(result, discord.File)

    async def test_filename_matches_uri(self):
        session = _mock_session(b"image bytes")
        result = await fetch_as_file("https://example.com/a.png", "avatar.png", session=session)
        assert result.filename == "avatar.png"
        assert result.uri == "attachment://avatar.png"

    async def test_session_get_called_with_url(self):
        session = _mock_session(b"image bytes")
        await fetch_as_file("https://example.com/a.png", "a.png", session=session)
        session.get.assert_called_once_with("https://example.com/a.png")

    async def test_body_preserved_in_file(self):
        session = _mock_session(b"original bytes")
        result = await fetch_as_file("https://example.com/a.png", "a.png", session=session)
        # The discord.File wraps a BytesIO; the fp is positioned at 0
        # until the file is consumed by a send.
        assert result.fp.read() == b"original bytes"


# // ========================================( Temporary session ) ======================================== // #


class TestFetchAsFileWithoutSession:
    """``fetch_as_file`` creates a temporary session when none is supplied."""

    async def test_creates_and_disposes_session(self):
        body = b"temp-session bytes"
        temp_session = _mock_session(body)
        # Mock the ClientSession constructor as an async context manager
        # returning the temp_session.
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=temp_session)
        session_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "cascadeui.utils.fetch.aiohttp.ClientSession",
            return_value=session_ctx,
        ) as ctor:
            result = await fetch_as_file("https://example.com/a.png", "a.png")

        ctor.assert_called_once_with()
        session_ctx.__aenter__.assert_awaited_once()
        session_ctx.__aexit__.assert_awaited_once()
        temp_session.get.assert_called_once_with("https://example.com/a.png")
        assert isinstance(result, discord.File)
        assert result.fp.read() == body


# // ========================================( Forwarded kwargs )======================================== // #


class TestFetchAsFileKwargForwarding:
    """``spoiler`` and ``description`` reach the ``discord.File`` constructor."""

    async def test_spoiler_defaults_false(self):
        session = _mock_session(b"x")
        result = await fetch_as_file("https://example.com/a.png", "a.png", session=session)
        assert result.spoiler is False

    async def test_spoiler_true_propagates(self):
        session = _mock_session(b"x")
        result = await fetch_as_file(
            "https://example.com/a.png", "a.png", session=session, spoiler=True
        )
        assert result.spoiler is True

    async def test_description_propagates(self):
        session = _mock_session(b"x")
        result = await fetch_as_file(
            "https://example.com/a.png",
            "a.png",
            session=session,
            description="alt text for the image",
        )
        assert result.description == "alt text for the image"

    async def test_description_defaults_none(self):
        session = _mock_session(b"x")
        result = await fetch_as_file("https://example.com/a.png", "a.png", session=session)
        assert result.description is None


# // ========================================( Public surface )======================================== // #


class TestFetchAsFilePublicSurface:
    """``fetch_as_file`` is exported at the package root and via cascadeui.utils."""

    def test_importable_from_package_root(self):
        from cascadeui import fetch_as_file as root_export

        assert callable(root_export)

    def test_importable_from_utils(self):
        from cascadeui.utils import fetch_as_file as utils_export

        assert callable(utils_export)

    def test_same_reference_at_both_paths(self):
        from cascadeui import fetch_as_file as root_export
        from cascadeui.utils import fetch_as_file as utils_export

        assert root_export is utils_export
