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


MediaInput = Union[str, discord.File, discord.UnfurledMediaItem]
"""Anything CascadeUI accepts where a media reference is required.

Mirrors the union accepted by :class:`discord.ui.MediaGallery`,
:class:`discord.ui.Thumbnail`, and :class:`discord.ui.File`:

* A URL string -- either an arbitrary remote URL
  (``"https://cdn.example.com/img.png"``) or the
  ``"attachment://<filename>"`` reference scheme for files uploaded
  alongside the same message.
* A :class:`discord.File` instance, in which case the underlying
  ``.uri`` (``"attachment://<filename>"``) is used. The same file
  object must also be passed via ``view.send(files=[...])`` (or
  ``refresh(attachments=[...])`` for in-place swaps) so the bytes
  travel with the message.
* A :class:`discord.UnfurledMediaItem`, which is already a media
  reference and passes through untouched, keeping whatever fields it
  carries.

An object with a string ``url`` attribute (a :class:`discord.Asset`, so
``guild.icon`` and ``member.display_avatar`` work directly) resolves to
that URL. It is not part of the union because the union names what a
media reference *is*, and an Asset is a convenience the builders coerce.
"""


MAX_SELECT_OPTIONS = 25
"""Discord's hard cap on the number of options in a single select menu.

A :class:`discord.ui.Select` (and CascadeUI's ``StatefulSelect`` / ``Dropdown``)
rejects more than this at the API boundary. The V2 ``choice_row`` builder
enforces it, raising ``ValueError`` past the cap.
"""

MAX_MESSAGE_COMPONENTS = 40
"""Discord's cap on the components in a single V2 message, counted recursively.

Every node counts: each Container, Section, ActionRow, button, select, text
node, and a Section's accessory. Exactly this many is legal and the next one
is refused.

discord.py owns the enforcement and raises from ``add_item`` while the tree is
being built, so this constant is for a budget check a caller wants to run
*before* composing. It is a transcription of discord.py's own literal rather
than the source of it, which is why
:meth:`cascadeui.StatefulLayoutView.add_item` still reports the count from the
exception it catches instead of comparing against this number.
"""

MAX_MESSAGE_CHARACTERS = 4000
"""Discord's cap on the display text in a single V2 message, summed.

The per-node cap on one ``TextDisplay`` is also 4000, and the two are
different limits: a tree of ten short text nodes passes every per-node
check and can still cross this one. ``LayoutView.content_length`` supplies
the running total.

That counter reaches ``TextDisplay`` content and nothing else, so text in
a button label, a select placeholder, or an option label counts zero
toward it while still occupying the message. A screen built mostly of
labelled controls can therefore approach the cap while the total reads
well under it.

Nothing enforces it. Discord documents the limit and discord.py exposes
the counter without raising on it, so a tree over the cap builds cleanly,
passes the placement validator, and may be refused at send. The library warns
at its ship seams rather than raising, because the enforcing side of this
one is unobserved: refusing a tree Discord would have accepted is the
worse error. Compare ``content_length()`` against this constant to check a
budget before composing.
"""

MAX_COMPONENT_ID = 2**31 - 1
"""Upper bound on a component's numeric ``id``.

Discord documents ``id`` as a 32-bit integer, unique within a message and
assigned sequentially from 1 to every component that omits it. Zero reads as
absent, so the V2 builders reject it rather than let Discord substitute a
number the caller did not choose. Unrelated to ``custom_id``, which is a
string and exists only on the interactive components.
"""
