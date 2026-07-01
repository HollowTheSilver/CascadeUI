# // ========================================( Modules )======================================== // #


import re

# // ========================================( Functions )======================================== // #


def slugify(text: str) -> str:
    """Convert a display string to a safe ``custom_id`` fragment.

    Lowercases the text and replaces non-alphanumeric runs with a single
    underscore.  Useful for building deterministic ``custom_id`` values
    from user-facing labels in persistent views::

        from cascadeui.utils import slugify

        custom_id = f"roles:{slugify(category)}:{slugify(role_name)}"
    """
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# A custom Discord emoji token: ``<:name:id>`` or animated ``<a:name:id>``.
_CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]+:\d+>$")

# Codepoint blocks that hold the bulk of unicode emoji: the main pictograph
# blocks, dingbats and misc symbols, arrows, technical and geometric shapes,
# and the regional-indicator flags. A heuristic, not a Unicode-perfect
# classifier -- enough to gate "did the user type an emoji?" without a
# dependency. Multi-codepoint sequences (ZWJ families, skin-tone modifiers,
# flags) match because their base pictographs fall in these ranges.
_EMOJI_CHAR_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # emoji, pictographs, symbols & pictographs extended
    "\U00002600-\U000027bf"  # misc symbols + dingbats
    "\U00002b00-\U00002bff"  # misc symbols and arrows
    "\U00002190-\U000021ff"  # arrows
    "\U00002300-\U000023ff"  # misc technical
    "\U000025a0-\U000025ff"  # geometric shapes
    "]"
)


def is_emoji(text: str) -> bool:
    """Heuristic check for a single emoji or a custom Discord emoji token.

    Returns ``True`` for a unicode emoji (including ZWJ, skin-tone, and flag
    sequences) or a custom Discord token (``<:name:id>`` / ``<a:name:id>``).
    Returns ``False`` for plain text, shortcodes (``:trophy:``), bare ASCII,
    and empty input.

    Discord has no native emoji input, so a modal text field is the usual way
    to accept an arbitrary emoji; the ``emoji()`` validator in
    :mod:`cascadeui.validation` gates that field. The match covers the main
    emoji unicode ranges plus the custom-token shape rather than a
    Unicode-perfect classifier, which keeps it dependency-free::

        from cascadeui.utils import is_emoji

        is_emoji("\U0001f3c6")        # True  (a unicode emoji)
        is_emoji("<:custom:123>")     # True  (a custom server emoji)
        is_emoji(":trophy:")          # False (a shortcode, not an emoji)
    """
    text = (text or "").strip()
    if not text:
        return False
    if _CUSTOM_EMOJI_RE.match(text):
        return True
    # Prose and shortcodes carry ASCII letters; an emoji string does not.
    if any(c.isascii() and c.isalpha() for c in text):
        return False
    return bool(_EMOJI_CHAR_RE.search(text))
