# // ========================================( Modules )======================================== // #


from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

import discord
from discord.components import MediaGalleryItem
from discord.enums import SeparatorSpacing
from discord.ui import Container, MediaGallery, Section, Separator, TextDisplay, Thumbnail

from .base import StatefulButton

# // ========================================( Cards )======================================== // #


def card(
    *children,
    color: Optional[Union[discord.Colour, int]] = None,
    spoiler: bool = False,
) -> Container:
    """Build a themed Container from children.

    The most common V2 pattern: a Container wrapping a heading, body
    content, and interactive components. Strings are automatically
    wrapped in ``TextDisplay``, so you can mix raw text and V2
    components freely.

    Args:
        *children: V2 components or strings. Strings are wrapped in
            ``TextDisplay`` automatically. Typical first child is a
            heading string: ``"## Server Info"``.
        color: Container accent colour. Accepts ``discord.Colour``
            or a raw int.
        spoiler: Whether the container should be hidden behind a
            spoiler.

    Returns:
        A ``Container`` ready to be added to a ``StatefulLayoutView``.

    Example::

        card(
            "## Server Info",
            key_value({"Members": 42, "Roles": 5}),
            color=discord.Color.green(),
        )
    """
    items = [TextDisplay(c) if isinstance(c, str) else c for c in children]
    return Container(
        *items,
        accent_colour=color,
        spoiler=spoiler,
    )


# // ========================================( Sections )======================================== // #


def action_section(
    text: str,
    *,
    label: str,
    callback: Callable,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: Optional[str] = None,
) -> Section:
    """Build a Section with a StatefulButton accessory.

    V2's signature pattern — text and an action button on the same line —
    as a one-liner instead of 5+ lines.

    Args:
        text: Display text for the section (supports markdown).
        label: Button label.
        callback: Async callback ``(interaction) -> None``.
        style: Button style (default: secondary).
        emoji: Optional button emoji.

    Returns:
        A ``Section`` with a ``TextDisplay`` and ``StatefulButton`` accessory.

    Example::

        action_section(
            "View and manage active bot modules",
            label="Modules",
            callback=self._go_to_modules,
            style=discord.ButtonStyle.primary,
        )
    """
    return Section(
        TextDisplay(text),
        accessory=StatefulButton(
            label=label,
            style=style,
            emoji=emoji,
            callback=callback,
        ),
    )


def toggle_section(
    text: str,
    *,
    active: bool,
    callback: Callable,
    labels: Tuple[str, str] = ("Enabled", "Disabled"),
) -> Section:
    """Build a Section with a green/red toggle button accessory.

    Auto-selects emoji, label, and button style based on the ``active``
    boolean. Common for settings panels and module toggles.

    Args:
        text: Display text (supports markdown). Does NOT auto-add an
            emoji prefix — include your own if desired.
        active: Current toggle state. ``True`` renders a green
            "Enabled" button, ``False`` renders a red "Disabled" button.
        callback: Async callback ``(interaction) -> None``.
        labels: ``(active_label, inactive_label)`` tuple. Defaults to
            ``("Enabled", "Disabled")``.

    Returns:
        A ``Section`` with toggle button accessory.

    Example::

        toggle_section(
            "\u2705 **Moderation**",
            active=True,
            callback=self._toggle_moderation,
        )
    """
    return Section(
        TextDisplay(text),
        accessory=StatefulButton(
            label=labels[0] if active else labels[1],
            style=discord.ButtonStyle.success if active else discord.ButtonStyle.danger,
            callback=callback,
        ),
    )


def image_section(
    text: str,
    *,
    url: str,
    description: Optional[str] = None,
    spoiler: bool = False,
) -> Section:
    """Build a Section with a Thumbnail accessory.

    Text on the left, image on the right. Useful for profile cards,
    server info panels, or any content with an associated icon.

    Args:
        text: Display text (supports markdown).
        url: Image URL for the thumbnail.
        description: Optional alt text for the thumbnail (up to 256 chars).
        spoiler: Whether the thumbnail is hidden behind a spoiler.

    Returns:
        A ``Section`` with a ``Thumbnail`` accessory.

    Example::

        image_section(
            "**HollowTheSilver**\\nAdmin",
            url=member.display_avatar.url,
        )
    """
    kwargs = {"media": url, "spoiler": spoiler}
    if description is not None:
        kwargs["description"] = description
    return Section(
        TextDisplay(text),
        accessory=Thumbnail(**kwargs),
    )


# // ========================================( Content )======================================== // #


def key_value(data: Dict[str, Any]) -> TextDisplay:
    """Build a TextDisplay from a dict of key-value pairs.

    Each key is rendered in bold, followed by its value. Pairs are
    separated by newlines.

    Args:
        data: Mapping of label to value. Values are converted to
            strings via ``str()``.

    Returns:
        A ``TextDisplay`` with formatted key-value text.

    Example::

        key_value({"Members": 42, "Roles": 5, "Channels": 12})
        # Renders as:
        # **Members:** 42
        # **Roles:** 5
        # **Channels:** 12
    """
    lines = [f"**{key}:** {value}" for key, value in data.items()]
    return TextDisplay("\n".join(lines))


_ALERT_STYLES = {
    "success": ("\u2705", discord.Colour.green),
    "warning": ("\u26a0\ufe0f", discord.Colour.gold),
    "error": ("\u274c", discord.Colour.red),
    "info": ("\u2139\ufe0f", discord.Colour.blurple),
}


def alert(message: str, level: str = "info") -> Container:
    """Build a colored Container for status messages.

    Four levels with matching emoji and accent colour:
    ``success`` (green), ``warning`` (gold), ``error`` (red),
    ``info`` (blurple).

    Args:
        message: Alert text (supports markdown).
        level: One of ``"success"``, ``"warning"``, ``"error"``,
            ``"info"``. Defaults to ``"info"``.

    Returns:
        A ``Container`` with an emoji-prefixed ``TextDisplay`` and
        matching accent colour.

    Example::

        alert("Settings saved successfully", level="success")
        alert("This action cannot be undone", level="warning")
    """
    if level not in _ALERT_STYLES:
        raise ValueError(f"Unknown alert level '{level}'. Expected: {list(_ALERT_STYLES)}")

    emoji, color_factory = _ALERT_STYLES[level]
    return Container(
        TextDisplay(f"{emoji} {message}"),
        accent_colour=color_factory(),
    )


# // ========================================( Separators )======================================== // #


def divider(large: bool = False) -> Separator:
    """A visible separator line between content blocks.

    Args:
        large: Use large spacing around the divider. Defaults to
            small spacing.
    """
    size = SeparatorSpacing.large if large else SeparatorSpacing.small
    return Separator(visible=True, spacing=size)


def gap(large: bool = False) -> Separator:
    """Invisible spacing between content blocks (no visible line).

    Args:
        large: Use large spacing. Defaults to small spacing.
    """
    size = SeparatorSpacing.large if large else SeparatorSpacing.small
    return Separator(visible=False, spacing=size)


# // ========================================( Media )======================================== // #


def gallery(
    *urls: str,
    descriptions: Optional[Sequence[Optional[str]]] = None,
) -> MediaGallery:
    """Build a MediaGallery from image URLs.

    Simplifies the ``MediaGallery(MediaGalleryItem(...), ...)`` nesting
    into a flat call with URLs.

    Args:
        *urls: Image URLs (up to 10).
        descriptions: Optional sequence of descriptions matching each
            URL positionally. Use ``None`` for items without a
            description.

    Returns:
        A ``MediaGallery`` component.

    Example::

        gallery(
            "https://example.com/a.png",
            "https://example.com/b.png",
            descriptions=["First image", None],
        )
    """
    items = []
    for i, url in enumerate(urls):
        desc = None
        if descriptions and i < len(descriptions):
            desc = descriptions[i]
        kwargs = {"media": url}
        if desc is not None:
            kwargs["description"] = desc
        items.append(MediaGalleryItem(**kwargs))
    return MediaGallery(*items)
