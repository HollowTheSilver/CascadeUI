"""Tests for ``cascadeui.fetch_as_file`` helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from cascadeui import fetch_as_file

# // ========================================( Helpers )======================================== // #


async def _chunks(body: bytes):
    yield body


def _mock_session(body: bytes) -> MagicMock:
    """Build an aiohttp-session-shaped mock that returns ``body`` on read.

    aiohttp's session.get(url) returns an async-context-manager whose
    __aenter__ resolves to a response object; response.read() is async.
    The mock reproduces both surfaces so fetch_as_file's two await
    points (the get-context entry and the read) both resolve.
    """
    mock_resp = MagicMock()
    mock_resp.read = AsyncMock(return_value=body)
    mock_resp.content_length = len(body)
    mock_resp.content.iter_chunked = MagicMock(side_effect=lambda size: _chunks(body))

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


# // ========================================( Real server )======================================== // #


@pytest.fixture
async def server():
    """A loopback aiohttp server, so the status check and size cap run against real responses."""
    from aiohttp import web

    async def ok(request):
        return web.Response(body=b"png bytes")

    async def missing(request):
        return web.Response(status=404, text="<html>Not Found</html>")

    async def big(request):
        return web.Response(body=b"x" * 5000)

    async def chunked(request):
        # Sent without a Content-Length, so only the running count can refuse it.
        resp = web.StreamResponse()
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for _ in range(5):
            await resp.write(b"x" * 1000)
        await resp.write_eof()
        return resp

    app = web.Application()
    for path, handler in (("/ok", ok), ("/missing", missing), ("/big", big), ("/chunked", chunked)):
        app.router.add_get(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


class TestFetchAsFileAgainstAServer:
    """An error status raises and a body past ``max_bytes`` is refused as it arrives."""

    async def test_a_body_under_the_limit_is_returned(self, server):
        result = await fetch_as_file(f"{server}/ok", "a.png")
        assert result.fp.read() == b"png bytes"

    async def test_an_error_status_raises_instead_of_returning_the_error_page(self, server):
        import aiohttp

        with pytest.raises(aiohttp.ClientResponseError) as caught:
            await fetch_as_file(f"{server}/missing", "a.png")
        assert caught.value.status == 404

    @pytest.mark.parametrize("path", ["/big", "/chunked"])
    async def test_a_body_past_max_bytes_is_refused(self, server, path):
        with pytest.raises(ValueError, match="larger than max_bytes"):
            await fetch_as_file(f"{server}{path}", "a.png", max_bytes=1500)

    async def test_max_bytes_none_reads_any_size(self, server):
        result = await fetch_as_file(f"{server}/chunked", "a.png", max_bytes=None)
        assert len(result.fp.read()) == 5000

    def test_the_default_limit_is_the_largest_upload_a_bot_can_make(self):
        import inspect

        default = inspect.signature(fetch_as_file).parameters["max_bytes"].default
        assert default == discord.Guild._PREMIUM_GUILD_LIMITS[3].filesize

    async def test_a_declared_size_past_max_bytes_is_refused_before_reading(self):
        session = _mock_session(b"x")
        resp = session.get.return_value.__aenter__.return_value
        resp.content_length = 10**9
        with pytest.raises(ValueError, match="larger than max_bytes"):
            await fetch_as_file("https://example.com/a.png", "a.png", session=session)
        resp.content.iter_chunked.assert_not_called()

    @pytest.mark.parametrize("bad, error", [("10", TypeError), (True, TypeError), (0, ValueError)])
    async def test_a_bad_max_bytes_is_refused_before_any_request(self, bad, error):
        session = _mock_session(b"x")
        with pytest.raises(error, match="max_bytes"):
            await fetch_as_file(
                "https://example.com/a.png", "a.png", session=session, max_bytes=bad
            )
        session.get.assert_not_called()


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
