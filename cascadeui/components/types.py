# // ========================================( Modules )======================================== // #


from typing import Optional, Union

import discord

# // ========================================( Type Aliases )======================================== // #


EmojiInput = Optional[Union[str, discord.Emoji, discord.PartialEmoji]]
"""Anything CascadeUI accepts as an ``emoji=`` argument.

Mirrors the union accepted by :class:`discord.ui.Button` and
:class:`discord.SelectOption`. Three string forms are recognized at the
discord.py boundary via :meth:`discord.PartialEmoji.from_str`:

* Unicode glyph (``"\\u2699\\ufe0f"`` or the literal ``"⚙️"``)
* Custom guild or application emoji (``"<:fire:1234567890>"``)
* Animated custom emoji (``"<a:dance:1234567890>"``)

A live :class:`discord.Emoji` (returned by ``bot.get_emoji`` or
``bot.fetch_application_emoji``) and a :class:`discord.PartialEmoji`
instance are also accepted directly.
"""


MediaInput = Union[str, discord.File]
"""Anything CascadeUI accepts where a media reference is required.

Mirrors the union accepted by :class:`discord.ui.MediaGallery`,
:class:`discord.ui.Thumbnail`, and :class:`discord.ui.File`. Two forms:

* A URL string -- either an arbitrary remote URL
  (``"https://cdn.example.com/img.png"``) or the
  ``"attachment://<filename>"`` reference scheme for files uploaded
  alongside the same message.
* A :class:`discord.File` instance, in which case the underlying
  ``.uri`` (``"attachment://<filename>"``) is used. The same file
  object must also be passed via ``view.send(files=[...])`` (or
  ``refresh(attachments=[...])`` for in-place swaps) so the bytes
  travel with the message.
"""


MAX_SELECT_OPTIONS = 25
"""Discord's hard cap on the number of options in a single select menu.

A :class:`discord.ui.Select` (and CascadeUI's ``StatefulSelect`` / ``Dropdown``)
rejects more than this at the API boundary. The V2 ``choice_row`` builder
enforces it, raising ``ValueError`` past the cap.
"""
