# // ========================================( Modules )======================================== // #


import io
from typing import Optional

import aiohttp
import discord

# The largest upload Discord accepts from a bot: a server at boost tier 3
# (discord.py's ``Guild._PREMIUM_GUILD_LIMITS``). A larger body can never be sent.
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# // ========================================( Functions )======================================== // #


async def _read_body(session: aiohttp.ClientSession, url: str, max_bytes: Optional[int]) -> bytes:
    """GET ``url`` and return its body, refusing an error status or a body past ``max_bytes``."""
    async with session.get(url) as resp:
        resp.raise_for_status()
        if max_bytes is None:
            return await resp.read()
        # The header fails fast; the count covers a body sent without one.
        if resp.content_length is not None and resp.content_length > max_bytes:
            raise ValueError(_too_large(url, max_bytes))
        body = bytearray()
        async for chunk in resp.content.iter_chunked(65536):
            body += chunk
            if len(body) > max_bytes:
                raise ValueError(_too_large(url, max_bytes))
        return bytes(body)


def _too_large(url: str, max_bytes: int) -> str:
    return (
        f"fetch_as_file: {url} is larger than max_bytes ({max_bytes} bytes). Pass "
        f"max_bytes=guild.filesize_limit to match the guild, or max_bytes=None for no limit."
    )


async def fetch_as_file(
    url: str,
    filename: str,
    *,
    session: Optional[aiohttp.ClientSession] = None,
    spoiler: bool = False,
    description: Optional[str] = None,
    max_bytes: Optional[int] = _MAX_UPLOAD_BYTES,
) -> discord.File:
    """Fetch ``url`` into an in-memory :class:`discord.File`.

    Wraps the standard ``aiohttp`` GET + ``BytesIO`` + ``discord.File``
    construction so cogs that pull remote assets into
    ``view.send(files=[...])`` payloads do not repeat the boilerplate.
    Pairs with the V2 media builders (``gallery()``, ``image_section()``,
    ``file_attachment()``) which accept the returned file directly via
    the :data:`MediaInput` union.

    The file can go with more than one send or edit the library makes
    (``send()``, ``refresh(attachments=[...])``, ``respond()``, the edit a
    navigation hook returns), since each reads it from its start. A
    direct discord.py call such as ``channel.send(file=...)`` does not,
    and uploads an empty file the second time, so pass it a new file.

    Args:
        url: HTTP/HTTPS source URL the running event loop's
            :class:`aiohttp.ClientSession` can reach.
        filename: Filename stored on the resulting ``discord.File``.
            Becomes the ``attachment://<filename>`` reference Discord
            resolves against the uploaded bytes.
        session: Optional shared :class:`aiohttp.ClientSession`. When
            supplied, the fetch reuses the caller's TCP pool and timeout.
            When ``None``, a temporary session opens and closes around
            the single fetch, with aiohttp's default timeout (five
            minutes in total); passing a session is preferred for any
            code path that fetches more than one URL, or that needs a
            shorter bound.
        spoiler: Forwarded to :class:`discord.File`. Marks the
            attachment as a spoiler on Discord's side.
        description: Forwarded to :class:`discord.File`. Used as alt
            text for image attachments.
        max_bytes: The largest body accepted, counted as it arrives so a
            larger one is never read in full. Defaults to 100 MiB, the
            most Discord accepts from a bot in any server; pass
            ``guild.filesize_limit`` for the target guild's own limit, or
            ``None`` for no limit.

    Returns:
        A :class:`discord.File` wrapping a :class:`io.BytesIO` of the
        response body, ready to pass to ``view.send(files=[...])`` or
        ``view.refresh(attachments=[...])``.

    Raises:
        aiohttp.ClientError: Network errors from the GET, and
            :class:`aiohttp.ClientResponseError` for an error status, so
            an error page is never returned as the file. The
            partially-consumed response stream is released by aiohttp's
            own context-manager cleanup before the exception propagates.
        ValueError: The body is larger than ``max_bytes``, or
            ``max_bytes`` is below 1.
        TypeError: ``max_bytes`` is not an int or ``None``.
    """
    if max_bytes is not None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError(
                f"fetch_as_file max_bytes must be an int or None, got {type(max_bytes).__name__}."
            )
        if max_bytes < 1:
            raise ValueError(f"fetch_as_file max_bytes must be at least 1, got {max_bytes}.")
    if session is not None:
        data = await _read_body(session, url, max_bytes)
    else:
        async with aiohttp.ClientSession() as temp_session:
            data = await _read_body(temp_session, url, max_bytes)
    return discord.File(
        io.BytesIO(data),
        filename=filename,
        spoiler=spoiler,
        description=description,
    )
