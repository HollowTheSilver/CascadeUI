"""
V2 Attachments -- CascadeUI File and Attachment Surface
========================================================

Demonstrates the V2 media builder family with local file uploads:

    * ``gallery()``         : MediaGallery from MediaInput references
    * ``image_section()``   : Section with Thumbnail accessory
    * ``file_attachment()`` : Downloadable File component card

The bytes and the reference are independent. The builder emits an
``attachment://<filename>`` reference into the component tree; the
matching ``discord.File`` carries the bytes alongside the message via
``view.send(files=[...])`` (initial send) or
``view.refresh(attachments=[...])`` (in-place edit). Both halves must
travel together, or Discord renders an unresolved placeholder.

Commands:
    /attach_gallery   gallery(*media: MediaInput) with two avatars
    /attach_section   Stacked image_section() rows with Thumbnail accessories
    /attach_download  file_attachment() with an in-memory text report
    /attach_swap      Initial send + refresh(attachments=[...]) swap

Source assets: Discord avatar CDN URLs and in-memory ``BytesIO`` blobs;
no external host or local files needed. ``ctx.author.display_avatar.url``
and ``ctx.bot.user.display_avatar.url`` resolve indefinitely against
Discord's CDN.

See ``docs/guide/components.md#local-file-attachments`` for the
conceptual reference.

Usage:
    Load this cog in your bot. Requires aiohttp (transitive via discord.py).
"""

# // ========================================( Modules )======================================== // #


import io

import aiohttp
import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from cascadeui import (
    StatefulButton,
    StatefulLayoutView,
    card,
    divider,
    fetch_as_file,
    file_attachment,
    gallery,
    image_section,
)

# // ========================================( Helpers )======================================== // #


def _bot_report_file() -> discord.File:
    """Build an in-memory text report for the ``file_attachment`` demo.

    ``file_attachment()`` renders downloadable files (PDF, ZIP, TXT, ...)
    as inline cards. The contents are a plain UTF-8 blob so the example
    runs without external assets.
    """
    payload = (
        "CascadeUI v3.3.0 attachment surface report\n"
        "------------------------------------------\n"
        "Builders: gallery(), image_section(), file_attachment()\n"
        "Type alias: MediaInput = Union[str, discord.File]\n"
        "Send kwargs: view.send(file=) and view.send(files=[...])\n"
        "Refresh: view.refresh(attachments=[...])  -- replacement list\n"
    )
    return discord.File(io.BytesIO(payload.encode("utf-8")), filename="report.txt")


# // ========================================( Views )======================================== // #


class _GalleryView(StatefulLayoutView):
    """MediaGallery layout: ``gallery(*media)`` with attached files."""

    owner_only = True
    exit_policy = "delete"
    timeout = 180.0
    state_scope = None

    def __init__(self, *args, files, **kwargs):
        super().__init__(*args, **kwargs)
        # gallery() accepts MediaInput per item -- ``discord.File``
        # instances resolve via ``.uri`` (``attachment://<filename>``)
        # at the builder boundary; the bytes upload via ``send(files=)``.
        self.add_item(
            card(
                "## Gallery layout",
                TextDisplay(
                    "-# `gallery(*media: MediaInput)` accepts URL strings "
                    "or `discord.File` instances. Bytes upload via `files=`."
                ),
                divider(),
                gallery(*files),
            )
        )
        self.add_exit_button()


class _SectionView(StatefulLayoutView):
    """Section + Thumbnail layout: one row per attached file."""

    owner_only = True
    exit_policy = "delete"
    timeout = 180.0
    state_scope = None

    def __init__(self, *args, files, labels, **kwargs):
        super().__init__(*args, **kwargs)
        # ``image_section.url`` accepts the same MediaInput union; one
        # Section per file renders a stacked Thumbnail layout.
        self.add_item(
            card(
                "## Section layout",
                TextDisplay(
                    "-# `image_section(text, *, url)` pairs caption text "
                    "with a Thumbnail accessory."
                ),
                divider(),
                *(image_section(label, url=f) for label, f in zip(labels, files)),
            )
        )
        self.add_exit_button()


class _DownloadView(StatefulLayoutView):
    """``file_attachment()`` layout: downloadable card with one file."""

    owner_only = True
    exit_policy = "delete"
    timeout = 180.0
    state_scope = None

    def __init__(self, *args, file, **kwargs):
        super().__init__(*args, **kwargs)
        # ``file_attachment()`` renders the file as an inline downloadable
        # card. MediaInput accepts the ``discord.File`` directly -- ``.uri``
        # emits ``attachment://report.txt`` to match the upload below.
        self.add_item(
            card(
                "## Download layout",
                TextDisplay(
                    "-# `file_attachment(url: MediaInput)` renders a "
                    "downloadable card. Click to download `report.txt`."
                ),
                divider(),
                file_attachment(file),
            )
        )
        self.add_exit_button()


class _SwapView(StatefulLayoutView):
    """Mid-session attachment swap via ``refresh(attachments=[...])``.

    Holds the candidate URLs and toggles between them on the Swap button
    click. Each swap fetches fresh bytes (a ``discord.File`` is consumed
    by its initial send), rebuilds the gallery against the new file, and
    ships ``refresh(attachments=[new_file])`` -- a replacement list, not
    additive.

    The filename increments monotonically (``swap_0.png``, ``swap_1.png``,
    ``swap_2.png``, ...) so the ``attachment://`` reference in the
    component tree pairs with the freshly-uploaded file on each swap
    without colliding with any cached upload from a prior swap.
    """

    owner_only = True
    exit_policy = "delete"
    timeout = 180.0
    state_scope = None

    def __init__(self, *args, urls, **kwargs):
        super().__init__(*args, **kwargs)
        self._urls = list(urls)
        self._url_index = 0
        self._swap_count = 0
        self._build_tree()

    def _current_filename(self) -> str:
        return f"swap_{self._swap_count}.png"

    def _build_tree(self) -> None:
        # Rebuilt on every swap so the gallery's reference matches the
        # filename of the next uploaded file. ``clear_items`` followed
        # by ``add_item`` is the canonical V2 rebuild pattern. The Swap
        # button is wrapped in an ``ActionRow``; bare interactive items
        # cannot sit at the LayoutView's top level.
        self.clear_items()
        self.add_item(
            card(
                "## Swap layout",
                TextDisplay(
                    f"-# Showing image {self._url_index + 1}/{len(self._urls)}. "
                    f"Swap count: {self._swap_count}. "
                    f"Click Swap to `refresh(attachments=[...])`."
                ),
                divider(),
                gallery(f"attachment://{self._current_filename()}"),
            )
        )
        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Swap",
                    style=discord.ButtonStyle.primary,
                    custom_id="attach_swap_button",
                    callback=self._on_swap,
                )
            )
        )
        self.add_exit_button()

    async def _on_swap(self, interaction: discord.Interaction) -> None:
        # Compute the next state in local variables first; commit to
        # ``self`` only after the fetch succeeds. A failing fetch
        # otherwise leaves the filename reference one step ahead of the
        # actual upload, and the next click would emit a stale
        # ``attachment://swap_N.png`` with no matching file in the
        # attachment list.
        next_index = (self._url_index + 1) % len(self._urls)
        next_count = self._swap_count + 1
        new_file = await fetch_as_file(self._urls[next_index], f"swap_{next_count}.png")
        self._url_index = next_index
        self._swap_count = next_count
        self._build_tree()
        # ``refresh(attachments=[...])`` REPLACES the message's attachment
        # list. The previous upload is removed; only ``new_file`` remains.
        await self.refresh(attachments=[new_file])


# // ========================================( Cog )======================================== // #


class V2AttachmentsExample(commands.Cog, name="v2_attachments_example"):
    """File-attachment surface showcase: gallery, image_section, file_attachment, and refresh swap."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="attach_gallery",
        description="Demo: gallery() with discord.File via MediaInput.",
    )
    async def attach_gallery(self, ctx: Context) -> None:
        # ``display_avatar.with_size(N)`` returns an Asset whose ``.url``
        # carries ``?size=N``. Discord's CDN serves avatars at any power
        # of two from 16 through 4096; 128 keeps gallery/Thumbnail
        # payloads small without visible quality loss at the rendered
        # size. Reuse one aiohttp session across both fetches so the
        # requests share a TCP pool.
        bot_user = ctx.bot.user
        async with aiohttp.ClientSession() as session:
            avatar_a = await fetch_as_file(
                ctx.author.display_avatar.with_size(128).url,
                "avatar_a.png",
                session=session,
            )
            avatar_b = await fetch_as_file(
                (
                    bot_user.display_avatar.with_size(128).url
                    if bot_user
                    else ctx.author.display_avatar.with_size(128).url
                ),
                "avatar_b.png",
                session=session,
            )
        view = _GalleryView(context=ctx, files=[avatar_a, avatar_b])
        await view.send(files=[avatar_a, avatar_b])

    @commands.hybrid_command(
        name="attach_section",
        description="Demo: image_section() with discord.File.",
    )
    async def attach_section(self, ctx: Context) -> None:
        bot_user = ctx.bot.user
        async with aiohttp.ClientSession() as session:
            avatar_a = await fetch_as_file(
                ctx.author.display_avatar.with_size(128).url,
                "avatar_a.png",
                session=session,
            )
            avatar_b = await fetch_as_file(
                (
                    bot_user.display_avatar.with_size(128).url
                    if bot_user
                    else ctx.author.display_avatar.with_size(128).url
                ),
                "avatar_b.png",
                session=session,
            )
        labels = [
            f"**{ctx.author.display_name}**\nInvoker",
            f"**{bot_user.name if bot_user else 'Bot'}**\nBot",
        ]
        view = _SectionView(context=ctx, files=[avatar_a, avatar_b], labels=labels)
        await view.send(files=[avatar_a, avatar_b])

    @commands.hybrid_command(
        name="attach_download",
        description="Demo: file_attachment() with an in-memory text report.",
    )
    async def attach_download(self, ctx: Context) -> None:
        report = _bot_report_file()
        view = _DownloadView(context=ctx, file=report)
        # Singular ``file=`` kwarg -- the single-file shape mirrors
        # discord.py's ``Messageable.send(file=)`` signature.
        await view.send(file=report)

    @commands.hybrid_command(
        name="attach_swap",
        description="Demo: refresh(attachments=[...]) mid-session swap.",
    )
    async def attach_swap(self, ctx: Context) -> None:
        # Single fetch -- the no-session branch creates and disposes a
        # temporary session for this one request. Multi-fetch commands
        # above promote to an explicit ``async with aiohttp.ClientSession()``.
        # ``with_size(128)`` keeps swap-callback payloads small so each
        # click resolves quickly.
        bot_user = ctx.bot.user
        urls = [
            ctx.author.display_avatar.with_size(128).url,
            (
                bot_user.display_avatar.with_size(128).url
                if bot_user
                else ctx.author.display_avatar.with_size(128).url
            ),
        ]
        initial = await fetch_as_file(urls[0], "swap_0.png")
        view = _SwapView(context=ctx, urls=urls)
        await view.send(files=[initial])


async def setup(bot) -> None:
    await bot.add_cog(V2AttachmentsExample(bot=bot))
