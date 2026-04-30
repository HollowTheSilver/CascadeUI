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
