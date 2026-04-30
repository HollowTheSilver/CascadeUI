# // ========================================( Modules )======================================== // #


from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence, Tuple, Union

import discord
from discord.components import MediaGalleryItem
from discord.enums import SeparatorSpacing
from discord.ui import (
    ActionRow,
    Container,
    MediaGallery,
    Section,
    Separator,
    TextDisplay,
    Thumbnail,
)

from ..base import StatefulButton
from ..types import EmojiInput

# Discord content-length ceiling for a single TextDisplay component.
_TEXTDISPLAY_MAX_CHARS = 4000

# Regional indicator emoji for alpha axis preset (🇦 through 🇿, 26 glyphs).
_ALPHA_LABELS = [chr(0x1F1E6 + i) for i in range(26)]

# Keycap emoji for numeric axis preset (1️⃣ through 🔟, 10 glyphs). No
# single-glyph keycap exists beyond 10; callers needing 11+ rows/cols must
# supply a custom Sequence[str].
_NUMERIC_LABELS = [f"{d}\ufe0f\u20e3" for d in "123456789"] + ["\U0001f51f"]

# Default fill / corner glyph: black large square. Matches the visual weight
# of most emoji cells and provides a clean default for pure play-area grids.
_DEFAULT_FILL = "\u2b1b"

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
            or a raw int. When ``None`` and called inside a view's
            ``build_ui()``, the active theme's ``accent_colour`` is
            used automatically.
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
    if color is None:
        from ...theming.context import get_current_theme

        theme = get_current_theme()
        if theme:
            color = theme.get_style("accent_colour")
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
    emoji: EmojiInput = None,
    custom_id: Optional[str] = None,
) -> Section:
    """Build a Section with a StatefulButton accessory.

    V2's signature pattern -- text and an action button on the same line  --
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
    button_kwargs = {"label": label, "style": style, "emoji": emoji, "callback": callback}
    if custom_id is not None:
        button_kwargs["custom_id"] = custom_id
    return Section(
        TextDisplay(text),
        accessory=StatefulButton(**button_kwargs),
    )


def toggle_section(
    text: str,
    *,
    active: bool,
    callback: Callable,
    labels: Tuple[str, str] = ("Enabled", "Disabled"),
    emoji: EmojiInput = None,
    custom_id: Optional[str] = None,
) -> Section:
    """Build a Section with a green/red toggle button accessory.

    Auto-selects emoji, label, and button style based on the ``active``
    boolean. Common for settings panels and module toggles.

    Args:
        text: Display text (supports markdown). Does NOT auto-add an
            emoji prefix -- include your own if desired.
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
    button_kwargs = {
        "label": labels[0] if active else labels[1],
        "style": discord.ButtonStyle.success if active else discord.ButtonStyle.danger,
        "emoji": emoji,
        "callback": callback,
    }
    if custom_id is not None:
        button_kwargs["custom_id"] = custom_id
    return Section(
        TextDisplay(text),
        accessory=StatefulButton(**button_kwargs),
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
            f"**{member.display_name}**\\nAdmin",
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


def link_section(
    text: str,
    *,
    label: str,
    url: str,
    emoji: EmojiInput = None,
) -> Section:
    """Build a Section with a link button accessory.

    Completes the ``*_section`` family for the three Section accessory
    shapes: action (StatefulButton), image (Thumbnail), and link
    (link-style Button). Link buttons have no callback because the
    platform handles navigation directly.

    Args:
        text: Display text for the section (supports markdown).
        label: Button label.
        url: Destination URL. The button opens this URL in a browser.
        emoji: Optional button emoji.

    Returns:
        A ``Section`` with a ``TextDisplay`` and link-style ``Button``
        accessory.

    Example::

        link_section(
            "Full documentation is on GitHub Pages.",
            label="Open Docs",
            url="https://hollowthesilver.github.io/CascadeUI/",
        )
    """
    return Section(
        TextDisplay(text),
        accessory=discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.link,
            url=url,
            emoji=emoji,
        ),
    )


def confirm_section(
    text: str,
    *,
    on_confirm: Callable,
    on_cancel: Callable,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    confirm_emoji: EmojiInput = "\u2705",
    cancel_emoji: EmojiInput = "\u274c",
) -> List:
    """Build a confirm/cancel prompt as a list of V2 children.

    Returns a ``[Section, ActionRow]`` pair rather than a single
    component so the caller can splat it into ``card(...)`` or add
    it directly to a view alongside other content. The Section holds
    the prompt text; the ActionRow holds the paired success/danger
    buttons.

    Args:
        text: Prompt text shown above the buttons (supports markdown).
        on_confirm: Async callback ``(interaction) -> None`` for the
            confirm button.
        on_cancel: Async callback ``(interaction) -> None`` for the
            cancel button.
        confirm_label: Confirm button label. Defaults to ``"Confirm"``.
        cancel_label: Cancel button label. Defaults to ``"Cancel"``.
        confirm_emoji: Confirm button emoji. Defaults to a green check.
        cancel_emoji: Cancel button emoji. Defaults to a red cross.

    Returns:
        A ``[TextDisplay, ActionRow]`` list ready to splat into
        ``card()`` or ``add_item`` loops.

    Example::

        card(
            "## Delete Server Data",
            *confirm_section(
                "This cannot be undone.",
                on_confirm=self._do_delete,
                on_cancel=self._do_cancel,
                confirm_label="Delete",
            ),
            color=discord.Color.red(),
        )
    """
    return [
        TextDisplay(text),
        ActionRow(
            StatefulButton(
                label=confirm_label,
                style=discord.ButtonStyle.success,
                emoji=confirm_emoji,
                callback=on_confirm,
            ),
            StatefulButton(
                label=cancel_label,
                style=discord.ButtonStyle.danger,
                emoji=cancel_emoji,
                callback=on_cancel,
            ),
        ),
    ]


# // ========================================( Buttons & Rows )======================================== // #


def button_row(
    buttons: Dict[str, Callable],
    *,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: EmojiInput = None,
) -> ActionRow:
    """Build an ActionRow from a ``{label: callback}`` mapping.

    Common shorthand for the hand-rolled ``ActionRow(StatefulButton(...),
    StatefulButton(...), ...)`` pattern repeated across dashboards,
    settings panels, and wizard nav rows. Every button in the row
    shares ``style`` and ``emoji``; for per-button customization,
    build the ``ActionRow`` by hand.

    Args:
        buttons: Mapping of ``label -> async callback``. Dict insertion
            order determines button order.
        style: Shared button style (default: secondary).
        emoji: Shared button emoji.

    Returns:
        A single ``ActionRow`` containing one ``StatefulButton`` per
        mapping entry.

    Raises:
        ValueError: If ``buttons`` is empty or exceeds Discord's 5
            buttons-per-ActionRow limit.

    Example::

        button_row(
            {
                "Save": self._save,
                "Reset": self._reset,
                "Cancel": self._cancel,
            },
            style=discord.ButtonStyle.primary,
        )
    """
    if not buttons:
        raise ValueError("button_row: buttons mapping must not be empty.")
    if len(buttons) > 5:
        raise ValueError(
            f"button_row: {len(buttons)} buttons exceeds Discord's "
            f"5-per-ActionRow limit. Split into multiple rows."
        )
    return ActionRow(
        *(
            StatefulButton(label=label, style=style, emoji=emoji, callback=callback)
            for label, callback in buttons.items()
        )
    )


def cycle_button(
    *,
    values: Sequence[Any],
    on_change: Callable,
    labels: Optional[Sequence[str]] = None,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: EmojiInput = None,
    start: int = 0,
) -> StatefulButton:
    """Build a button that cycles through a fixed list of values.

    The first genuinely stateful V2 helper. The button tracks its own
    index on the returned instance (``button._cycle_index``); clicking
    advances to the next value (wrapping) and updates the button's
    label before the user's ``on_change`` callback runs. The caller
    is responsible for the view refresh that reflects the new state.

    Common use case: a single "Preset" button that cycles through
    ``["Low", "Medium", "High"]`` instead of three radio-style toggles
    consuming three ActionRow slots.

    Args:
        values: The sequence of values to cycle through. Must be
            non-empty.
        on_change: Async callback ``(interaction, value) -> None``
            called with the *new* value after the index advances.
        labels: Optional display labels, one per value. Defaults to
            ``str(value)`` for each entry.
        style: Button style (default: secondary).
        emoji: Optional button emoji.
        start: Index to start at (default: 0).

    Returns:
        A ``StatefulButton`` with ``_cycle_index``, ``_cycle_values``,
        and ``_cycle_labels`` attributes set for introspection.

    Raises:
        ValueError: If ``values`` is empty, ``start`` is out of range,
            or ``labels`` length does not match ``values`` length.

    Example::

        cycle_button(
            values=["Low", "Medium", "High"],
            on_change=self._on_preset_changed,
            emoji="\u2699\ufe0f",
        )
    """
    if not values:
        raise ValueError("cycle_button: values must not be empty.")
    resolved_labels = list(labels) if labels is not None else [str(v) for v in values]
    if len(resolved_labels) != len(values):
        raise ValueError(
            f"cycle_button: labels length ({len(resolved_labels)}) must "
            f"match values length ({len(values)})."
        )
    if not 0 <= start < len(values):
        raise ValueError(
            f"cycle_button: start={start} is out of range for " f"{len(values)} values."
        )

    async def _cycle_callback(interaction):
        button._cycle_index = (button._cycle_index + 1) % len(button._cycle_values)
        button.label = button._cycle_labels[button._cycle_index]
        await on_change(interaction, button._cycle_values[button._cycle_index])

    button = StatefulButton(
        label=resolved_labels[start],
        style=style,
        emoji=emoji,
        callback=_cycle_callback,
    )
    button._cycle_index = start
    button._cycle_values = list(values)
    button._cycle_labels = resolved_labels
    return button


def toggle_button(
    *,
    active: bool,
    on_toggle: Callable,
    labels: Tuple[str, str] = ("Enabled", "Disabled"),
    emoji: EmojiInput = None,
) -> StatefulButton:
    """Build a standalone boolean toggle button.

    Distinct from :func:`toggle_section`, which wraps the same button
    shape in a Section with display text on the left. Use
    ``toggle_button`` when the button stands alone in an ActionRow
    without accompanying text.

    The button tracks its own state on the returned instance
    (``button._toggle_active``) and flips it before calling the
    user's ``on_toggle`` callback with the *new* state.

    Args:
        active: Initial state. ``True`` renders a green "active" button,
            ``False`` renders a red "inactive" button.
        on_toggle: Async callback ``(interaction, active) -> None``
            called with the new state after the flip.
        labels: ``(active_label, inactive_label)`` tuple. Defaults to
            ``("Enabled", "Disabled")``.
        emoji: Optional button emoji.

    Returns:
        A ``StatefulButton`` with ``_toggle_active`` attribute set
        for introspection.

    Example::

        button_row({}) + [toggle_button(
            active=True,
            on_toggle=self._on_dark_mode_toggled,
            labels=("Dark", "Light"),
        )]
    """

    async def _toggle_callback(interaction):
        button._toggle_active = not button._toggle_active
        button.label = labels[0] if button._toggle_active else labels[1]
        button.style = (
            discord.ButtonStyle.success if button._toggle_active else discord.ButtonStyle.danger
        )
        await on_toggle(interaction, button._toggle_active)

    button = StatefulButton(
        label=labels[0] if active else labels[1],
        style=discord.ButtonStyle.success if active else discord.ButtonStyle.danger,
        emoji=emoji,
        callback=_toggle_callback,
    )
    button._toggle_active = active
    return button


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


def stats_card(
    title: str,
    stats: Dict[str, Any],
    *,
    color: Optional[Union[discord.Colour, int]] = None,
    footer: Optional[str] = None,
) -> Container:
    """Build a titled Container showing a dict of stats as key-value lines.

    Thin composition of ``card(title, key_value(stats), ...)`` -- the
    pattern repeats across dashboards, server-info panels, and
    debug/inspector views often enough to earn its own helper.
    ``title`` is wrapped in a heading automatically; callers can
    pre-format it with ``##`` prefix for finer control.

    Args:
        title: Card title text. If it does not start with ``#``, it
            is rendered as a second-level heading (``## {title}``).
        stats: Mapping of label to value for the key-value body.
        color: Container accent colour. When ``None`` and called inside
            a view's ``build_ui()``, the active theme's ``accent_colour``
            is used automatically.
        footer: Optional footer line rendered in Discord's subtext
            style (``-# {footer}``) below the stats.

    Returns:
        A ``Container`` with a heading ``TextDisplay``, a divider,
        the key-value body, and an optional footer line.

    Example::

        stats_card(
            "Server Overview",
            {"Members": 42, "Channels": 12, "Roles": 5},
            color=discord.Color.green(),
            footer="Updated just now",
        )
    """
    if color is None:
        from ...theming.context import get_current_theme

        theme = get_current_theme()
        if theme:
            color = theme.get_style("accent_colour")
    heading = title if title.startswith("#") else f"## {title}"
    children: List[Any] = [
        TextDisplay(heading),
        Separator(visible=True, spacing=SeparatorSpacing.small),
        key_value(stats),
    ]
    if footer:
        children.append(TextDisplay(f"-# {footer}"))
    return Container(*children, accent_colour=color)


def progress_bar(
    value: Union[int, float],
    max_value: Union[int, float],
    *,
    width: int = 20,
    filled: str = "\u2588",
    empty: str = "\u2591",
    show_percent: bool = True,
) -> TextDisplay:
    """Build a text-based progress bar as a TextDisplay.

    V2 equivalent of the V1 ``ProgressBar`` composite. Renders
    ``[████████████░░░░░░░░] 60%`` using Unicode block glyphs by
    default. ``value`` is clamped to ``[0, max_value]`` so callers
    do not need to guard against overshoots.

    Args:
        value: Current progress.
        max_value: Maximum progress. Must be positive.
        width: Number of glyph cells (default: 20).
        filled: Glyph for completed cells. Defaults to U+2588 (full
            block).
        empty: Glyph for remaining cells. Defaults to U+2591 (light
            shade).
        show_percent: Append a trailing ``N%`` after the bar.

    Returns:
        A ``TextDisplay`` with the rendered bar.

    Raises:
        ValueError: If ``max_value <= 0`` or ``width <= 0``.

    Example::

        progress_bar(7, 10, width=10)  # [███████░░░] 70%
    """
    if max_value <= 0:
        raise ValueError(f"progress_bar: max_value must be positive, got {max_value}.")
    if width <= 0:
        raise ValueError(f"progress_bar: width must be positive, got {width}.")

    ratio = max(0.0, min(1.0, value / max_value))
    fill_cells = round(ratio * width)
    bar = filled * fill_cells + empty * (width - fill_cells)
    text = f"[{bar}]"
    if show_percent:
        text = f"{text} {int(round(ratio * 100))}%"
    return TextDisplay(text)


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


# // ========================================( Navigation )======================================== // #


def tab_nav(
    tabs: Dict[str, Callable],
    *,
    active: Optional[str] = None,
    active_style: discord.ButtonStyle = discord.ButtonStyle.primary,
    inactive_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
) -> ActionRow:
    """Build an ActionRow of tab-styled buttons for manual-control views.

    Lighter alternative to :class:`TabLayoutView` for views that want
    tab-style navigation without buying into the full Tab pattern's
    lifecycle (async builders, ``on_tab_switched``, refresh-contract
    parity). Each tab is just a button the view handles in its own
    callback.

    The tab matching ``active`` renders with ``active_style``; all
    others render with ``inactive_style``. If ``active`` is not
    supplied, the first tab is marked active.

    Args:
        tabs: Mapping of ``label -> async callback``. Insertion order
            determines display order.
        active: Label of the tab that should render as active. Must
            match a key in ``tabs``. Defaults to the first key.
        active_style: Style for the active tab (default: primary).
        inactive_style: Style for inactive tabs (default: secondary).

    Returns:
        An ``ActionRow`` of ``StatefulButton`` tabs.

    Raises:
        ValueError: If ``tabs`` is empty, exceeds 5 entries, or
            ``active`` is not a key in ``tabs``.

    Example::

        tab_nav(
            {
                "Stats": self._show_stats,
                "Settings": self._show_settings,
                "Help": self._show_help,
            },
            active="Stats",
        )
    """
    if not tabs:
        raise ValueError("tab_nav: tabs mapping must not be empty.")
    if len(tabs) > 5:
        raise ValueError(
            f"tab_nav: {len(tabs)} tabs exceeds Discord's 5-per-ActionRow "
            f"limit. Use TabLayoutView for views requiring more tabs."
        )
    if active is None:
        active = next(iter(tabs))
    elif active not in tabs:
        raise ValueError(
            f"tab_nav: active={active!r} is not a key in tabs " f"(keys: {list(tabs)})."
        )

    return ActionRow(
        *(
            StatefulButton(
                label=label,
                style=active_style if label == active else inactive_style,
                callback=callback,
            )
            for label, callback in tabs.items()
        )
    )


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
    if descriptions is not None and len(descriptions) != len(urls):
        raise ValueError(
            f"gallery: descriptions length ({len(descriptions)}) must match "
            f"urls length ({len(urls)}). Pad with None for URLs that should "
            f"have no description."
        )

    items = []
    for i, url in enumerate(urls):
        desc = descriptions[i] if descriptions is not None else None
        kwargs = {"media": url}
        if desc is not None:
            kwargs["description"] = desc
        items.append(MediaGalleryItem(**kwargs))
    return MediaGallery(*items)


# // ========================================( Grids )======================================== // #


AxisLabels = Union[Sequence[str], Literal["alpha", "numeric"], None]
CellKey = Union[int, Tuple[int, int]]


def _resolve_axis(
    labels: AxisLabels,
    length: int,
    axis_name: str,
) -> Optional[List[str]]:
    """Resolve an axis-label spec into a concrete list of strings.

    Returns ``None`` when no labels are desired. Raises ``ValueError``
    for preset caps and custom-sequence length mismatches.
    """
    if labels is None:
        return None
    if labels == "alpha":
        if length > 26:
            raise ValueError(
                f"alpha preset supports up to 26 labels; got {length}. "
                f"Pass a custom Sequence[str] for larger axes."
            )
        return _ALPHA_LABELS[:length]
    if labels == "numeric":
        if length > 10:
            raise ValueError(
                f"numeric preset supports up to 10 labels (keycap "
                f"1\ufe0f\u20e3 through \U0001f51f); got {length}. "
                f"Pass a custom Sequence[str] for larger axes."
            )
        return list(_NUMERIC_LABELS[:length])
    # Custom sequence path.
    resolved = list(labels)
    if len(resolved) != length:
        raise ValueError(
            f"{axis_name} length {len(resolved)} does not match {axis_name.split('_')[0]}s {length}"
        )
    return resolved


class EmojiGrid(TextDisplay):
    """A string-rendered emoji board, packaged as a live ``TextDisplay``.

    ``EmojiGrid`` subclasses :class:`discord.ui.TextDisplay` so it plugs
    directly into any V2 container (``card()``, ``Container(...)``,
    ``Section(...)`` text slots) with no wrapper. Cell mutation is eager:
    every ``__setitem__``, ``fill_rect``, or ``clear`` call rebuilds the
    ``content`` attribute, so the component always reflects the current
    state at send time.

    Axis labels are fully optional. The four combinations (no labels,
    row labels only, col labels only, both) are all legal. The top-left
    ``corner`` cell is only rendered when *both* ``row_labels`` and
    ``col_labels`` are set, since that is the only layout where the row
    and column header strips intersect. With col labels only, the
    header row is flush-aligned with the data rows (no leading cell).

    Cell keys accept either a flat integer index (``row * cols + col``)
    or a ``(row, col)`` tuple. Bulk assignment accepts any iterable of
    keys and applies the same value to each:

        grid[3] = "\U0001f525"
        grid[2, 5] = "\U0001f525"
        grid[[3, 4, 5]] = "\u2b1c"
        grid.fill_rect((0, 0), (2, 4), "\U0001f7e6")

    Args:
        rows: Number of play-area rows (>= 1).
        cols: Number of play-area columns (>= 1).
        fill: Default cell emoji. Applied to every cell at construction
            and used as the reset value for ``clear()``.
        row_labels: ``"alpha"`` (\U0001f1e6-\U0001f1ff, up to 26),
            ``"numeric"`` (1\ufe0f\u20e3-\U0001f51f, up to 10), a custom
            ``Sequence[str]`` of length ``rows``, or ``None`` for no
            row prefix.
        col_labels: Same options as ``row_labels``, length ``cols``.
        corner: Top-left glyph rendered at the intersection of the row
            and column headers. Defaults to ``fill`` when both axes are
            labeled. Raises ``ValueError`` if supplied explicitly without
            both ``row_labels`` and ``col_labels``, since the corner has
            nowhere to render.
        cell_sep: String inserted between cells within a row and between
            the row label and the first cell. Defaults to a single space.
    """

    def __init__(
        self,
        rows: int,
        cols: int,
        *,
        fill: str = _DEFAULT_FILL,
        row_labels: AxisLabels = None,
        col_labels: AxisLabels = None,
        corner: Optional[str] = None,
        cell_sep: str = " ",
    ) -> None:
        if not isinstance(rows, int) or not isinstance(cols, int):
            raise TypeError(
                f"rows and cols must be int, got rows={type(rows).__name__} "
                f"cols={type(cols).__name__}"
            )
        if rows < 1 or cols < 1:
            raise ValueError(f"rows and cols must be >= 1, got rows={rows} cols={cols}")
        if not isinstance(fill, str):
            raise TypeError(f"fill must be str, got {type(fill).__name__}")

        self._rows = rows
        self._cols = cols
        self._fill = fill
        self._cell_sep = cell_sep
        self._row_labels = _resolve_axis(row_labels, rows, "row_labels")
        self._col_labels = _resolve_axis(col_labels, cols, "col_labels")

        if corner is not None and (self._row_labels is None or self._col_labels is None):
            raise ValueError(
                "corner is only rendered when both row_labels and col_labels "
                "are set; drop the corner= argument or supply both axes"
            )
        self._corner = corner if corner is not None else fill

        # Flat cell buffer, row-major. All cells start at fill.
        self._cells: List[str] = [fill] * (rows * cols)

        # Initial content + construction-time length cap. TextDisplay must
        # be constructed with its initial content, so _render() runs before
        # super().__init__().
        rendered = self._render()
        if len(rendered) > _TEXTDISPLAY_MAX_CHARS:
            raise ValueError(
                f"grid render exceeds {_TEXTDISPLAY_MAX_CHARS}-character "
                f"TextDisplay limit: got {len(rendered)} chars. Reduce "
                f"rows/cols or use shorter cell glyphs."
            )
        super().__init__(rendered)

    # -------------------- Read-only shape --------------------

    @property
    def rows(self) -> int:
        """Number of play-area rows (excludes any header row)."""
        return self._rows

    @property
    def cols(self) -> int:
        """Number of play-area columns (excludes any row-label column)."""
        return self._cols

    # -------------------- Key normalization --------------------

    def _normalize(self, key: CellKey) -> int:
        """Convert a cell key into a flat index, validating bounds."""
        if isinstance(key, int):
            if key < 0 or key >= self._rows * self._cols:
                raise IndexError(
                    f"cell index {key} out of range for {self._rows}x{self._cols} grid"
                )
            return key
        if isinstance(key, tuple) and len(key) == 2:
            r, c = key
            if not isinstance(r, int) or not isinstance(c, int):
                raise TypeError(f"tuple cell key must be (int, int), got {key!r}")
            if r < 0 or r >= self._rows or c < 0 or c >= self._cols:
                raise IndexError(
                    f"cell coordinate ({r}, {c}) out of range for "
                    f"{self._rows}x{self._cols} grid"
                )
            return r * self._cols + c
        raise TypeError(f"cell key must be int or (row, col) tuple, got {type(key).__name__}")

    # -------------------- Cell API --------------------

    def __getitem__(self, key: CellKey) -> str:
        return self._cells[self._normalize(key)]

    def __setitem__(
        self,
        key: Union[CellKey, Iterable[CellKey]],
        value: str,
    ) -> None:
        if not isinstance(value, str):
            raise TypeError(f"cell value must be str, got {type(value).__name__}")
        # Single key (int or 2-tuple) goes through _normalize directly.
        if isinstance(key, int) or (isinstance(key, tuple) and len(key) == 2):
            self._cells[self._normalize(key)] = value
        else:
            # Iterable of keys -- bulk assign, single rebuild at the end.
            try:
                keys = list(key)
            except TypeError as exc:
                raise TypeError(
                    f"cell key must be int, (row, col) tuple, or iterable; "
                    f"got {type(key).__name__}"
                ) from exc
            for k in keys:
                self._cells[self._normalize(k)] = value
        self.content = self._render()

    def fill_rect(
        self,
        top_left: Tuple[int, int],
        bottom_right: Tuple[int, int],
        value: str,
    ) -> None:
        """Assign ``value`` to every cell in the inclusive rectangle."""
        if not isinstance(value, str):
            raise TypeError(f"cell value must be str, got {type(value).__name__}")
        r1, c1 = top_left
        r2, c2 = bottom_right
        if r1 > r2 or c1 > c2:
            raise ValueError(
                f"fill_rect top_left {top_left} must be <= bottom_right "
                f"{bottom_right} on both axes"
            )
        # Bounds-check the corners; _normalize handles the detail.
        self._normalize((r1, c1))
        self._normalize((r2, c2))
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                self._cells[r * self._cols + c] = value
        self.content = self._render()

    def clear(self) -> None:
        """Reset every cell to the original ``fill`` value."""
        self._cells = [self._fill] * (self._rows * self._cols)
        self.content = self._render()

    # -------------------- Rendering --------------------

    def _render(self) -> str:
        sep = self._cell_sep
        lines: List[str] = []

        if self._col_labels is not None:
            # The corner cell is the intersection of the row and column
            # label strips -- it only has a place when both axes exist.
            # Without row labels the header is flush with the data rows.
            if self._row_labels is not None:
                header_cells = [self._corner] + list(self._col_labels)
            else:
                header_cells = list(self._col_labels)
            lines.append(sep.join(header_cells))

        for r in range(self._rows):
            row_cells = self._cells[r * self._cols : (r + 1) * self._cols]
            if self._row_labels is not None:
                lines.append(sep.join([self._row_labels[r]] + row_cells))
            else:
                lines.append(sep.join(row_cells))

        return "\n".join(lines)


def emoji_grid(
    rows: int,
    cols: int,
    *,
    fill: str = _DEFAULT_FILL,
    row_labels: AxisLabels = None,
    col_labels: AxisLabels = None,
    corner: Optional[str] = None,
    cell_sep: str = " ",
) -> EmojiGrid:
    """Build a string-rendered emoji grid as a live ``TextDisplay``.

    Thin factory over :class:`EmojiGrid`. Returns a subclass instance
    that can be dropped into any V2 container without a ``TextDisplay``
    wrapper::

        grid = emoji_grid(10, 10, fill="\U0001f7e6",
                          row_labels="alpha", col_labels="numeric")
        for idx in ship_cells:
            grid[idx] = "\u2b1c"
        card(grid, "Your fleet is ready.")

    See :class:`EmojiGrid` for the full parameter reference.
    """
    return EmojiGrid(
        rows,
        cols,
        fill=fill,
        row_labels=row_labels,
        col_labels=col_labels,
        corner=corner,
        cell_sep=cell_sep,
    )


def button_grid(
    rows: int,
    cols: int,
    cell_factory: Callable[[int, int], discord.ui.Button],
) -> List[ActionRow]:
    """Pack a rectangular button grid into ``ActionRow`` components.

    ``cell_factory(row, col)`` is called once per cell and must return a
    fully-built :class:`discord.ui.Button` (including any callback,
    label, style, and ``disabled`` state). The helper owns the iteration
    and ``ActionRow`` packing; per-cell logic stays in user code.

    Discord allows at most 5 rows of components and 5 components per
    row inside a LayoutView, so both dimensions must fall in ``1..5``.
    Non-square rectangles (e.g. 3x5, 2x4) are supported.

    Args:
        rows: Number of button rows (1-5).
        cols: Number of buttons per row (1-5).
        cell_factory: Callable that produces a ``Button`` for each cell.

    Returns:
        A list of ``ActionRow`` components, one per row, ready to be
        appended to a ``LayoutView`` or spread into ``card(*rows, ...)``.

    Raises:
        ValueError: If ``rows`` or ``cols`` is outside ``1..5``.
        TypeError: If ``cell_factory`` does not return a ``Button``.
    """
    if not isinstance(rows, int) or not isinstance(cols, int):
        raise TypeError(
            f"rows and cols must be int, got rows={type(rows).__name__} "
            f"cols={type(cols).__name__}"
        )
    if not (1 <= rows <= 5) or not (1 <= cols <= 5):
        raise ValueError(
            f"button_grid exceeds Discord's 5x5 component limit: got "
            f"{rows}x{cols} (both dimensions must be in 1..5)"
        )

    action_rows: List[ActionRow] = []
    first = True
    for r in range(rows):
        buttons = []
        for c in range(cols):
            btn = cell_factory(r, c)
            if first:
                if not isinstance(btn, discord.ui.Button):
                    raise TypeError(
                        f"cell_factory must return discord.ui.Button, got " f"{type(btn).__name__}"
                    )
                first = False
            buttons.append(btn)
        action_rows.append(ActionRow(*buttons))
    return action_rows
