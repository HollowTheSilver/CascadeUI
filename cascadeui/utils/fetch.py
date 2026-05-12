# // ========================================( Modules )======================================== // #


import io
from typing import Optional

import aiohttp
import discord

# // ========================================( Functions )======================================== // #


async def fetch_as_file(
    url: str,
    filename: str,
    *,
    session: Optional[aiohttp.ClientSession] = None,
    spoiler: bool = False,
    description: Optional[str] = None,
) -> discord.File:
    """Fetch ``url`` into an in-memory :class:`discord.File`.

    Wraps the standard ``aiohttp`` GET + ``BytesIO`` + ``discord.File``
    construction so cogs that pull remote assets into
    ``view.send(files=[...])`` payloads do not repeat the boilerplate.
    Pairs with the V2 media builders (``gallery()``, ``image_section()``,
    ``file_attachment()``) which accept the returned file directly via
    the :data:`MediaInput` union.

    A ``discord.File`` is consumed on its first send. A view that
    re-sends or refreshes its attachments must construct a fresh file
    each time; reusing the same instance produces a zero-byte upload
    that Discord renders as a broken placeholder.

    Args:
        url: HTTP/HTTPS source URL the running event loop's
            :class:`aiohttp.ClientSession` can reach.
        filename: Filename stored on the resulting ``discord.File``.
            Becomes the ``attachment://<filename>`` reference Discord
            resolves against the uploaded bytes.
        session: Optional shared :class:`aiohttp.ClientSession`. When
            supplied, the fetch reuses the caller's TCP pool. When
            ``None``, a temporary session opens and closes around the
            single fetch; passing a session is preferred for any code
            path that fetches more than one URL.
        spoiler: Forwarded to :class:`discord.File`. Marks the
            attachment as a spoiler on Discord's side.
        description: Forwarded to :class:`discord.File`. Used as alt
            text for image attachments.

    Returns:
        A :class:`discord.File` wrapping a :class:`io.BytesIO` of the
        response body, ready to pass to ``view.send(files=[...])`` or
        ``view.refresh(attachments=[...])``.

    Raises:
        aiohttp.ClientError: Network or HTTP errors from the GET. The
            partially-consumed response stream is released by aiohttp's
            own context-manager cleanup before the exception propagates.
    """
    if session is not None:
        async with session.get(url) as resp:
            data = await resp.read()
    else:
        async with aiohttp.ClientSession() as temp_session:
            async with temp_session.get(url) as resp:
                data = await resp.read()
    return discord.File(
        io.BytesIO(data),
        filename=filename,
        spoiler=spoiler,
        description=description,
    )
