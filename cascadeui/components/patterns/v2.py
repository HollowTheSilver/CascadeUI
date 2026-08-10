# // ========================================( Modules )======================================== // #


import inspect
import logging
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import discord
from discord.components import MediaGalleryItem
from discord.enums import SeparatorSpacing
from discord.ui import (
    ActionRow,
    Container,
    File,
    MediaGallery,
    Section,
    Separator,
    TextDisplay,
    Thumbnail,
)

from ...utils.guards import coerce_colour, normalize_mapping
from ...utils.hooks import await_maybe, call_hook_safe
from ..base import StatefulButton, StatefulSelect
from ..types import MAX_COMPONENT_ID, MAX_SELECT_OPTIONS, EmojiInput, MediaInput

logger = logging.getLogger(__name__)


async def _guard_hook(hook, *args, owner) -> None:
    """Run a composite's post-event hook without letting it skip the render.

    These hooks fire after the cursor or flag has already moved but before
    the edit ships, so a raising override would leave the composite's state
    ahead of what the user can see. The view-side patterns get the same
    protection through ``_StatefulMixin._call_hook_safe``.
    """
    await call_hook_safe(hook, *args, owner=type(owner).__name__, log=logger)


# Discord content-length ceiling for a single TextDisplay component.
_TEXTDISPLAY_MAX_CHARS = 4000

# Discord's custom_id ceiling. discord.py stores the value unchecked, so an
# oversized id constructs cleanly and fails only at HTTP send.
_CUSTOM_ID_MAX_CHARS = 100


def _check_custom_id_length(base: Optional[str], builder: str) -> Optional[str]:
    """Reject an oversized ``custom_id`` the builder passes through unchanged.

    The sibling of :func:`_stamp_custom_id` for builders that emit one
    button and append no suffix. Both paths owe the caller the same
    construction-time error: routing only the composing builders through a
    check leaves the others reporting the same mistake as a later, less
    directed failure.
    """
    if base is not None and len(base) > _CUSTOM_ID_MAX_CHARS:
        raise ValueError(
            f"{builder}: custom_id {base!r} is {len(base)} characters, over "
            f"Discord's {_CUSTOM_ID_MAX_CHARS}-character cap.\n"
            f"  Fix: shorten custom_id to at most {_CUSTOM_ID_MAX_CHARS} "
            f"characters."
        )
    return base


def _stamp_custom_id(base: Optional[str], suffix: str) -> Optional[str]:
    """Suffix a caller's base ``custom_id``, rejecting an oversized result.

    The builders compose these ids rather than passing the caller's string
    through, so the length of what ships is the library's to answer for: a
    base well inside the cap can be pushed past it by a suffix the caller
    never sees. Raising here names the base; the alternative surfaces as an
    HTTP 400 with no component named.
    """
    if base is None:
        return None
    composed = f"{base}_{suffix}"
    if len(composed) > _CUSTOM_ID_MAX_CHARS:
        raise ValueError(
            f"custom_id {composed!r} is {len(composed)} characters, over "
            f"Discord's {_CUSTOM_ID_MAX_CHARS}-character cap. The builder "
            f"appends {'_' + suffix!r} to the base custom_id.\n"
            f"  Fix: shorten custom_id to at most "
            f"{_CUSTOM_ID_MAX_CHARS - len(suffix) - 1} characters."
        )
    return composed


def _require_sync_result(value, owner: str, param: str):
    """Reject an awaitable returned where a rendered value was wanted.

    The constructor rejects a coroutine function, but that predicate only
    recognises the object as one: an instance whose ``__call__`` is async
    passes it and returns a coroutine anyway. Rendering is synchronous, so
    there is nothing to await it with, and the two consumers fail far apart.
    A revealed coroutine reaches ``add_item`` and raises ``expected Item not
    coroutine`` on the caller's own line, which is loud but unnamed. A
    summary coroutine is truthy, so it becomes a ``TextDisplay`` body, clears
    the placement validator (which skips non-string content deliberately),
    and surfaces as a JSON serialization error at HTTP send, naming neither
    the parameter nor the class.

    The coroutine is closed before raising so it does not also emit a
    "never awaited" warning from the collector.
    """
    if inspect.isawaitable(value):
        value.close()
        raise TypeError(
            f"{owner}: {param} must be synchronous; it returned "
            f"{type(value).__name__}.\n"
            f"  Fix: load async data in the host's on_load() and have {param} "
            f"read the result."
        )
    return value


def _check_button_styles(builder: str, **styles) -> None:
    """Reject a button style that is not a ``discord.ButtonStyle``.

    discord.py stores whatever it is handed and reads ``.value`` only when
    the payload is built, so a bare string like ``"danger"`` constructs
    cleanly and fails at send with an attribute error naming neither the
    builder nor the parameter. The class-attribute validator has always
    checked the same type on ``*_button_style`` attributes; this is that
    check for the values passed straight to a builder.
    """
    for name, value in styles.items():
        if not isinstance(value, discord.ButtonStyle):
            raise TypeError(
                f"{builder}: {name} must be a discord.ButtonStyle, got "
                f"{type(value).__name__}: {value!r}\n"
                f"  Fix: pass a member, e.g. discord.ButtonStyle.danger."
            )


def _check_label_pair(labels, builder: str) -> None:
    """Reject a toggle's ``labels`` that is not an on/off pair of strings.

    Both labels are read by index, so a one-item sequence raises
    ``IndexError`` from inside the builder. A bare string is worse: it
    indexes without complaint and the button renders single characters,
    which no exception ever reports. ``cycle_button`` already rejects a
    label list that does not match its values; this is the same check for
    the fixed pair the toggles take.
    """
    if isinstance(labels, str) or not isinstance(labels, (tuple, list)) or len(labels) != 2:
        raise ValueError(
            f"{builder}: labels must be a two-item sequence of "
            f"(active, inactive) strings, got {labels!r}"
        )
    if not all(isinstance(label, str) for label in labels):
        raise TypeError(
            f"{builder}: both labels must be strings, got "
            f"({', '.join(type(label).__name__ for label in labels)})"
        )


def _with_id(component, component_id: Optional[int], builder: str):
    """Stamp a caller-supplied ``id`` onto the component a builder returns.

    ``id`` is Discord's per-component integer, unique within a message and
    assigned sequentially from 1 for anything that omits it. It is unrelated
    to ``custom_id``: every component carries one, including the display-only
    types that have no ``custom_id`` at all.

    ``id`` is a plain attribute on every component type the builders
    return, so it is assigned after construction rather than threaded
    through each constructor.

    Validation lives here because discord.py performs none: a string, a
    float, or a negative reaches the payload untouched and returns an opaque
    HTTP 400. Zero is rejected rather than passed through, since Discord
    reads it as absent and assigns a number the caller did not choose.
    """
    if component_id is None:
        return component
    if isinstance(component_id, bool) or not isinstance(component_id, int):
        raise TypeError(
            f"{builder}(id=...) must be an int, got "
            f"{type(component_id).__name__}: {component_id!r}\n"
            f"  Fix: Pass an int between 1 and {MAX_COMPONENT_ID}, or omit id= "
            f"and let Discord assign one."
        )
    if not 1 <= component_id <= MAX_COMPONENT_ID:
        raise ValueError(
            f"{builder}(id={component_id}) is outside Discord's range "
            f"(1 to {MAX_COMPONENT_ID}).\n"
            f"  Discord reads 0 as absent and rejects the rest with HTTP 400.\n"
            f"  Fix: Pass an id in range, or omit id= to have Discord assign one."
        )
    component.id = component_id
    return component


def _require_text(value: str, builder: str, param: str) -> str:
    """Reject a caller-formatted string that would ship an empty TextDisplay.

    Discord requires non-empty ``content`` and rejects the whole payload, so
    one blank line takes down every component beside it. The condition is
    data-triggered: a formatter renders nine entries and fails on the tenth
    whose optional fields all happen to be absent. Naming the builder and the
    parameter points at the formatter; the pre-flight validator that
    backstops this can only name the primitive.
    """
    if not value:
        raise ValueError(
            f"{builder}: {param} is empty. Discord rejects a TextDisplay with "
            f"no content, and the whole message fails with it.\n"
            f"  Fix: pass a non-empty string, or skip the component when the "
            f"formatter has nothing to render."
        )
    return value


def _coerce_media_ref(value: MediaInput, *, owner: str, param: str) -> str:
    """Resolve a ``MediaInput`` to the URL string Discord's API consumes.

    Strings pass through unchanged. A :class:`discord.File` resolves to
    its ``.uri`` (the ``"attachment://<filename>"`` reference built from
    the normalized filename). The builder emits only the reference
    string; the bytes travel separately through
    ``view.send(files=[...])``.

    Only the ``.uri`` is extracted -- the ``description`` and ``spoiler``
    attributes of the source :class:`discord.File` are NOT forwarded.
    Builders that wrap the reference in a component carrying those
    fields (``image_section``, ``file_attachment``) accept them as
    explicit kwargs, so the metadata is never silently lost on the
    documented call paths.
    """
    if isinstance(value, discord.File):
        return value.uri
    if isinstance(value, (str, discord.UnfurledMediaItem)):
        return value
    # An Asset is one attribute away from correct (``member.display_avatar``
    # instead of ``member.display_avatar.url``), and every docstring example
    # here shows the ``.url`` form, so the near miss is the likely one. It is
    # coerced rather than refused, matching how the leaderboard's ``banner=``
    # has always treated the same objects. Checked after UnfurledMediaItem,
    # which carries a ``.url`` of its own but is a media reference already and
    # would lose its other fields on the way through.
    url = getattr(value, "url", None)
    if isinstance(url, str):
        return url
    raise TypeError(
        f"{owner} {param} must be a URL string, a discord.File, or an object "
        f"with a string .url attribute (e.g. discord.Asset); got "
        f"{type(value).__name__}: {value!r}"
    )


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
    id: Optional[int] = None,
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

    Raises:
        ValueError: If a child is a ``Container`` (including ``alert()``,
            ``card()``, and ``stats_card()`` returns). Discord forbids a
            Container inside a Container, so no tree holding the result
            could ever send; place the child as a sibling instead.

    Example::

        card(
            "## Server Info",
            key_value({"Members": 42, "Roles": 5}),
            color=discord.Color.green(),
        )
    """
    for index, child in enumerate(children):
        # A Container is never a legal Container child, so the mistake is
        # decidable here, while the composing call is still on the stack.
        # Left in, the same tree fails at whichever seam ships the view,
        # where the placement validator names Container indexes rather
        # than this call -- on a push, that surfaces as a failed
        # navigation instead of a construction error.
        if isinstance(child, Container):
            raise ValueError(
                f"card: child {index} is a Container ({type(child).__name__}), "
                f"and Discord forbids a Container inside a Container. alert(), "
                f"card(), and stats_card() all build Containers.\n"
                f"  Fix: place it as a sibling of the card (its own add_item "
                f"call or page entry), not as a card child."
            )
    color = coerce_colour(color, owner="card", param="color")
    theme_managed = color is None
    if theme_managed:
        from ...theming.context import get_current_theme

        theme = get_current_theme()
        if theme:
            color = theme.get_style("accent_colour")
    items = [
        TextDisplay(_require_text(c, "card", "a string child")) if isinstance(c, str) else c
        for c in children
    ]
    container = Container(
        *items,
        accent_colour=color,
        spoiler=spoiler,
    )
    # No explicit color means the accent stays theme-managed: the view's
    # render seams re-resolve it against the live theme, so the card is
    # themed no matter where it was built and follows runtime theme
    # changes. An explicit color is never marked and never touched.
    container._cascadeui_theme_accent = theme_managed
    return _with_id(
        container,
        id,
        "card",
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
    disabled: bool = False,
    id: Optional[int] = None,
) -> Section:
    """Build a Section with a StatefulButton accessory.

    Renders V2's signature pattern, text and an action button on the same
    line, as a one-liner instead of 5+ lines.

    Args:
        text: Display text for the section (supports markdown).
        label: Button label.
        callback: Async callback ``(interaction) -> None``.
        style: Button style (default: secondary).
        emoji: Optional button emoji.
        disabled: Render the accessory button greyed out and
            non-interactive (default: ``False``). Use for a card-embedded
            action that is not yet available (an incomplete form, a no-op
            transition) instead of accepting the click and rejecting it.

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
    _check_button_styles("action_section", style=style)
    button_kwargs = {
        "label": label,
        "style": style,
        "emoji": emoji,
        "callback": callback,
        "disabled": disabled,
    }
    _check_custom_id_length(custom_id, "action_section")
    if custom_id is not None:
        button_kwargs["custom_id"] = custom_id
    return _with_id(
        Section(
            TextDisplay(_require_text(text, "action_section", "text")),
            accessory=StatefulButton(**button_kwargs),
        ),
        id,
        "action_section",
    )


def toggle_section(
    text: str,
    *,
    active: bool,
    callback: Callable,
    labels: Tuple[str, str] = ("Enabled", "Disabled"),
    emoji: EmojiInput = None,
    custom_id: Optional[str] = None,
    disabled: bool = False,
    id: Optional[int] = None,
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
        disabled: Render the toggle button greyed out and non-interactive
            (default: ``False``). Use when the toggle is locked by another
            condition (a parent setting that gates it).

    Returns:
        A ``Section`` with toggle button accessory.

    Example::

        toggle_section(
            "\u2705 **Moderation**",
            active=True,
            callback=self._toggle_moderation,
        )
    """
    _check_label_pair(labels, "toggle_section")
    button_kwargs = {
        "label": labels[0] if active else labels[1],
        "style": discord.ButtonStyle.success if active else discord.ButtonStyle.danger,
        "emoji": emoji,
        "callback": callback,
        "disabled": disabled,
    }
    _check_custom_id_length(custom_id, "toggle_section")
    if custom_id is not None:
        button_kwargs["custom_id"] = custom_id
    return _with_id(
        Section(
            TextDisplay(_require_text(text, "toggle_section", "text")),
            accessory=StatefulButton(**button_kwargs),
        ),
        id,
        "toggle_section",
    )


def image_section(
    text: str,
    *more_text: str,
    url: MediaInput,
    description: Optional[str] = None,
    spoiler: bool = False,
    id: Optional[int] = None,
) -> Section:
    """Build a Section with a Thumbnail accessory.

    Text on the left, image on the right. Useful for profile cards,
    server info panels, or any content with an associated icon.

    Extra positional strings become additional text lines in the same
    Section, which is how a two-line entry (a name above a stat line,
    beside an avatar) renders without hand-building the ``Section``.
    Discord caps a Section at three text children.

    Args:
        text: Display text (supports markdown).
        *more_text: Additional text lines rendered under ``text`` inside
            the same Section. At most two, per Discord's three-child cap.
            Empty lines are skipped, so a formatter that produces nothing
            for one entry shortens that entry instead of failing the send.
        url: Image reference for the thumbnail. Accepts either a URL
            string (remote or ``attachment://`` form) or a
            :class:`discord.File` instance whose ``.uri`` is used.
            File-backed references require the same ``discord.File`` to
            be passed via ``view.send(files=[...])``.
        description: Optional alt text for the thumbnail (up to 256 chars).
        spoiler: Whether the thumbnail is hidden behind a spoiler.

    Returns:
        A ``Section`` with a ``Thumbnail`` accessory.

    Example::

        image_section(
            f"**{member.display_name}**",
            "Admin",
            url=member.display_avatar.url,
        )
    """
    # An empty line is dropped rather than rendered. Discord rejects a text
    # display with empty content and fails the whole message, so a formatter
    # that returns "" for one entry would otherwise take down every component
    # beside it. A Section with one text child plus an accessory is legal, so
    # the entry still renders.
    lines = tuple(line for line in (text, *more_text) if line)
    if not lines:
        raise ValueError(
            "image_section: at least one non-empty text line is required. "
            "Discord rejects a Section with no text children."
        )
    if len(lines) > 3:
        raise ValueError(
            f"image_section: {len(lines)} text lines exceeds Discord's "
            f"3-children-per-Section limit. Merge lines with a newline, or "
            f"move the overflow into its own component."
        )
    kwargs = {
        "media": _coerce_media_ref(url, owner="image_section", param="url"),
        "spoiler": spoiler,
    }
    if description is not None:
        kwargs["description"] = description
    return _with_id(
        Section(
            *(TextDisplay(line) for line in lines),
            accessory=Thumbnail(**kwargs),
        ),
        id,
        "image_section",
    )


def link_section(
    text: str,
    *,
    label: str,
    url: str,
    emoji: EmojiInput = None,
    disabled: bool = False,
    id: Optional[int] = None,
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
        disabled: Render the link greyed out and unclickable. Matches
            the ``disabled`` parameter on the other ``*_section``
            builders.

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
    return _with_id(
        Section(
            TextDisplay(_require_text(text, "link_section", "text")),
            accessory=discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.link,
                url=url,
                emoji=emoji,
                disabled=disabled,
            ),
        ),
        id,
        "link_section",
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
    confirm_style: discord.ButtonStyle = discord.ButtonStyle.success,
    cancel_style: discord.ButtonStyle = discord.ButtonStyle.danger,
    custom_id: Optional[str] = None,
) -> List:
    """Build a confirm/cancel prompt as a list of V2 children.

    Returns a ``[TextDisplay, ActionRow]`` pair rather than a single
    component so the caller can splat it into ``card(...)`` or add
    it directly to a view alongside other content. The TextDisplay holds
    the prompt text; the ActionRow holds the confirm and cancel buttons,
    green and red by default and restyled through the two style
    parameters below.

    Args:
        text: Prompt text shown above the buttons (supports markdown).
        on_confirm: Callback ``(interaction) -> None`` for the confirm
            button. Sync or async.
        on_cancel: Callback ``(interaction) -> None`` for the cancel
            button. Sync or async.
        confirm_label: Confirm button label. Defaults to ``"Confirm"``.
        cancel_label: Cancel button label. Defaults to ``"Cancel"``.
        confirm_style: Confirm button style. Defaults to
            ``ButtonStyle.success``, which reads confirm-is-good. Pass
            ``ButtonStyle.danger`` when confirming is the destructive
            choice, or the button that deletes renders green.
        cancel_style: Cancel button style. Defaults to
            ``ButtonStyle.danger``. Pair it with ``ButtonStyle.secondary``
            when the styles above are swapped, so the safe option does not
            render as the alarming one.
        confirm_emoji: Confirm button emoji. Defaults to a green check.
        cancel_emoji: Cancel button emoji. Defaults to a red cross.
        custom_id: Base id for the two buttons, which become
            ``{custom_id}_confirm`` and ``{custom_id}_cancel``. Required
            inside a ``PersistentLayoutView``; omit it anywhere else.

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
                confirm_style=discord.ButtonStyle.danger,
                cancel_style=discord.ButtonStyle.secondary,
            ),
            color=discord.Color.red(),
        )
    """
    _check_button_styles("confirm_section", confirm_style=confirm_style, cancel_style=cancel_style)
    return [
        TextDisplay(_require_text(text, "confirm_section", "text")),
        ActionRow(
            StatefulButton(
                label=confirm_label,
                style=confirm_style,
                emoji=confirm_emoji,
                custom_id=_stamp_custom_id(custom_id, "confirm"),
                callback=on_confirm,
            ),
            StatefulButton(
                label=cancel_label,
                style=cancel_style,
                emoji=cancel_emoji,
                custom_id=_stamp_custom_id(custom_id, "cancel"),
                callback=on_cancel,
            ),
        ),
    ]


# // ========================================( Buttons & Rows )======================================== // #


def button_row(
    buttons: Union[Mapping[str, Callable], Sequence[Tuple[str, Callable]]],
    *,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: EmojiInput = None,
    custom_id: Optional[str] = None,
    id: Optional[int] = None,
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
        custom_id: Base id for the row, suffixed per button as
            ``{custom_id}_0``, ``{custom_id}_1``, and so on in mapping
            order. Required inside a ``PersistentLayoutView``; omit it
            anywhere else.

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
    _check_button_styles("button_row", style=style)
    buttons = normalize_mapping(buttons, owner="button_row", param="buttons")
    if not buttons:
        raise ValueError("button_row: buttons mapping must not be empty.")
    if len(buttons) > 5:
        raise ValueError(
            f"button_row: {len(buttons)} buttons exceeds Discord's "
            f"5-per-ActionRow limit. Split into multiple rows."
        )
    return _with_id(
        ActionRow(
            *(
                StatefulButton(
                    label=label,
                    style=style,
                    emoji=emoji,
                    custom_id=_stamp_custom_id(custom_id, str(i)),
                    callback=callback,
                )
                for i, (label, callback) in enumerate(buttons.items())
            )
        ),
        id,
        "button_row",
    )


def cycle_button(
    *,
    values: Sequence[Any],
    on_change: Callable,
    labels: Optional[Sequence[str]] = None,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: EmojiInput = None,
    start: int = 0,
    custom_id: Optional[str] = None,
    id: Optional[int] = None,
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
        custom_id: Explicit id for the button. Required inside a
            ``PersistentLayoutView``; omit it anywhere else.

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
    _check_button_styles("cycle_button", style=style)
    if not values:
        raise ValueError("cycle_button: values must not be empty.")
    _check_custom_id_length(custom_id, "cycle_button")
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
        await await_maybe(on_change(interaction, button._cycle_values[button._cycle_index]))

    button = StatefulButton(
        label=resolved_labels[start],
        style=style,
        emoji=emoji,
        custom_id=custom_id,
        callback=_cycle_callback,
    )
    button._cycle_index = start
    button._cycle_values = list(values)
    button._cycle_labels = resolved_labels
    return _with_id(
        button,
        id,
        "cycle_button",
    )


def toggle_button(
    *,
    active: bool,
    on_toggle: Callable,
    labels: Tuple[str, str] = ("Enabled", "Disabled"),
    emoji: EmojiInput = None,
    custom_id: Optional[str] = None,
    id: Optional[int] = None,
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
        custom_id: Explicit id for the button. Required inside a
            ``PersistentLayoutView``; omit it anywhere else.

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

    _check_custom_id_length(custom_id, "toggle_button")
    _check_label_pair(labels, "toggle_button")

    async def _toggle_callback(interaction):
        button._toggle_active = not button._toggle_active
        button.label = labels[0] if button._toggle_active else labels[1]
        button.style = (
            discord.ButtonStyle.success if button._toggle_active else discord.ButtonStyle.danger
        )
        await await_maybe(on_toggle(interaction, button._toggle_active))

    button = StatefulButton(
        label=labels[0] if active else labels[1],
        style=discord.ButtonStyle.success if active else discord.ButtonStyle.danger,
        emoji=emoji,
        custom_id=custom_id,
        callback=_toggle_callback,
    )
    button._toggle_active = active
    return _with_id(
        button,
        id,
        "toggle_button",
    )


# // ========================================( Choices )======================================== // #


class Choice(NamedTuple):
    """One option in a :func:`choice_row`.

    The rich form of a choice. ``choice_row`` also accepts a plain
    ``{label: value}`` dict for the common case; ``Choice`` adds a
    per-option emoji and a per-option dropdown description.

    Attributes:
        label: Display text for the option.
        value: The value handed to ``on_select`` when this option is
            picked. Any Python object -- the builder maps it to and from
            the string Discord requires on select option values, so the
            callback always receives the real value.
        emoji: Optional emoji shown on the button or select option.
        description: Optional second line shown on the select option.
            Ignored when the control renders as buttons (buttons have no
            description slot).
    """

    label: str
    value: Any
    emoji: EmojiInput = None
    description: Optional[str] = None


def _normalize_choices(
    options: Union[Mapping[str, Any], Sequence[Tuple[str, Any]], Sequence[Choice]],
) -> List[Choice]:
    """Resolve dict or Choice-sequence input into a list of ``Choice``."""
    if hasattr(options, "items"):
        return [Choice(label=str(label), value=value) for label, value in options.items()]
    # An int or any other non-iterable dies on the ``for`` below with its own
    # message, which names neither the builder nor the parameter. Strings and
    # bytes iterate, but into characters, so they arrive as a row of
    # one-letter options rather than as the mistake they are.
    if isinstance(options, (str, bytes)) or not hasattr(options, "__iter__"):
        raise TypeError(
            f"choice_row: options must be a mapping of {{label: value}}, a sequence "
            f"of Choice, or a sequence of (label, value) pairs; got "
            f"{type(options).__name__}: {options!r}"
        )
    resolved: List[Choice] = []
    for opt in options:
        if isinstance(opt, Choice):
            resolved.append(opt)
            continue
        # A two-item sequence is the same data a mapping carries, in the order
        # the caller wrote it, so it converts rather than being refused. A
        # Choice is itself a tuple, which is why it is matched first.
        if isinstance(opt, (tuple, list)) and len(opt) == 2:
            resolved.append(Choice(label=str(opt[0]), value=opt[1]))
            continue
        raise TypeError(
            f"choice_row options must be a mapping, a sequence of Choice, or a "
            f"sequence of (label, value) pairs; got {type(opt).__name__}"
        )
    return resolved


def _selected_values(selected: Any, multi: bool) -> list:
    """Normalize ``selected`` into a list of active values.

    Single-select treats ``selected`` as one value; multi-select treats
    it as a collection. ``str``/``bytes`` are never iterated apart (a
    string value is one choice, not a set of characters).

    A list rather than a set because ``Choice.value`` is documented as any
    Python object, and membership against a set requires every value to be
    hashable: a list or dict value failed on the containment test even when
    nothing was selected. Options cap at 25, so the linear scan costs
    nothing measurable and equality is the comparison the caller expects.
    """
    if selected is None:
        return []
    if not multi:
        if isinstance(selected, (list, tuple, set, frozenset)):
            raise TypeError(
                "choice_row: with multi=False, selected must be a single value "
                f"(not a {type(selected).__name__}). Pass multi=True to select several."
            )
        return [selected]
    if isinstance(selected, (str, bytes)) or not hasattr(selected, "__iter__"):
        raise TypeError(
            "choice_row: with multi=True, selected must be a collection of "
            f"values (set/list/tuple), got {type(selected).__name__}"
        )
    return list(selected)


def choice_row(
    options: Union[Mapping[str, Any], Sequence[Tuple[str, Any]], Sequence[Choice]],
    *,
    on_select: Callable,
    selected: Any = None,
    multi: bool = False,
    disabled: bool = False,
    allow_reselect: bool = False,
    button_threshold: int = 5,
    active_style: discord.ButtonStyle = discord.ButtonStyle.primary,
    inactive_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    placeholder: Optional[str] = None,
    custom_id: str = "choice",
    id: Optional[int] = None,
) -> ActionRow:
    """Build a "choose one" (or "choose any") control from a set of options.

    Collapses the hand-rolled segmented control (a row of buttons where
    the active option is styled differently and disabled, each wired to set
    its value) into one call, and switches to a dropdown automatically
    when the option count outgrows a button row.

    The active option(s) render highlighted. In single-select the active
    button is disabled by default (re-picking it is a no-op); set
    ``allow_reselect=True`` to keep it clickable so a re-pick fires
    ``on_select`` again, for a control whose callback has a side effect
    beyond selection (re-opening the active option's editor). In
    multi-select the buttons become toggles, so an active option can be
    clicked to turn it off. ``on_select`` receives the picked value (single)
    or the full list of currently-selected values (multi). Discord requires
    select option values to be strings; the builder maps to and from that
    string form, so ``on_select`` always sees the real Python value.

    The builder is stateless: it reads ``selected`` at build time and
    renders the active option(s) from it. The host view owns the selection:
    the ``on_select`` callback stores the new value and rebuilds the row
    (``build_ui()`` then ``refresh()``, or a state dispatch) so the next
    render reflects the pick. Without a rebuild the control snaps back to the
    build-time ``selected`` on the following click.

    Args:
        options: Either a ``{label: value}`` dict (the common case) or a
            sequence of :class:`Choice` (when an option needs an emoji or
            a dropdown description). Insertion order is preserved.
        on_select: Async callback. Single-select: ``(interaction, value)``.
            Multi-select: ``(interaction, values)`` where ``values`` is the
            full list of selected values after the toggle.
        selected: The active value (single-select) or a collection of
            active values (multi-select). ``None`` means nothing selected.
        multi: When ``True``, multiple options can be active at once.
        disabled: When ``True``, every button or the dropdown renders
            greyed out and non-interactive. Use it for a read-only,
            locked, or closed state where the selection still shows but
            cannot change.
        allow_reselect: Single-select only. When ``True``, the active option
            stays clickable and a re-pick fires ``on_select`` with the active
            value again. Default ``False`` keeps the active option inert (a
            re-pick is a no-op). Ignored in multi-select, where active options
            are always clickable toggles.
        button_threshold: Render as buttons when the option count is at or
            below this (default 5, Discord's per-row button cap); render as
            a dropdown above it. ``0`` forces a dropdown for every count.
            Must be an int in ``0..5``.
        active_style: Button style for active options (default primary).
            Applies only to the button form; dropdowns have no style.
        inactive_style: Button style for inactive options (default
            secondary). Applies only to the button form.
        placeholder: Placeholder text for the dropdown form.
        custom_id: Base custom_id for the control. Defaults to ``"choice"``;
            pass a distinct value to every ``choice_row`` in the same view,
            or their buttons (and the dropdown) collide in Discord's
            dispatch table.

    Returns:
        A single ``ActionRow`` holding either the buttons or the dropdown.

    Raises:
        ValueError: ``options`` is empty, exceeds Discord's 25-option limit
            for a single control, or ``button_threshold`` is out of ``0..5``.
        TypeError: ``on_select`` is not callable, an option is neither a
            ``Choice`` nor part of a dict, or a multi-select ``selected`` is
            not a collection.

    Example::

        choice_row(
            {"Easy": Difficulty.EASY, "Hard": Difficulty.HARD},
            selected=self.difficulty,
            on_select=self._set_difficulty,
        )
    """
    _check_button_styles("choice_row", active_style=active_style, inactive_style=inactive_style)
    choices = _normalize_choices(options)
    if not choices:
        raise ValueError("choice_row: options must not be empty.")
    if len(choices) > MAX_SELECT_OPTIONS:
        raise ValueError(
            f"choice_row: {len(choices)} options exceeds Discord's "
            f"{MAX_SELECT_OPTIONS}-option limit for a single control. "
            f"Paginate or split the choices."
        )
    if (
        not isinstance(button_threshold, int)
        or isinstance(button_threshold, bool)
        or not 0 <= button_threshold <= 5
    ):
        raise ValueError(
            f"choice_row: button_threshold must be an int in 0..5, got {button_threshold!r}"
        )
    if not callable(on_select):
        raise TypeError(f"choice_row: on_select must be callable, got {type(on_select).__name__}")

    active = _selected_values(selected, multi)
    if len(choices) <= button_threshold:
        return _with_id(
            _choice_button_row(
                choices,
                active,
                on_select,
                multi,
                active_style,
                inactive_style,
                custom_id,
                disabled,
                allow_reselect,
            ),
            id,
            "choice_row",
        )
    return _with_id(
        _choice_select_row(
            choices, active, on_select, multi, placeholder, custom_id, disabled, allow_reselect
        ),
        id,
        "choice_row",
    )


def _make_single_choice_callback(value: Any, on_select: Callable):
    async def callback(interaction: discord.Interaction):
        await await_maybe(on_select(interaction, value))

    return callback


def _make_multi_choice_callback(value: Any, active: list, on_select: Callable):
    # active is the build-time snapshot; clicking toggles this value in or
    # out and hands the host the full new list. The host stores it and
    # rebuilds, so the next render's buttons capture the updated selection.
    #
    # A list, not a set: a Choice.value is any Python object, and the
    # construction path was widened to accept unhashable ones. Building a
    # set here put the same restriction back one click later, which is
    # strictly worse -- it moved the failure from where the options are
    # written to where a user pressed a button. Options cap at 25, so the
    # linear scan costs nothing.
    async def callback(interaction: discord.Interaction):
        # ``in`` is what the render path uses to decide which options draw
        # active, so toggling reads membership the same way and the two
        # cannot disagree about what is selected.
        if value in active:
            new = [v for v in active if v != value]
        else:
            new = [*active, value]
        await await_maybe(on_select(interaction, new))

    return callback


def _choice_button_row(
    choices: List[Choice],
    active: set,
    on_select: Callable,
    multi: bool,
    active_style: discord.ButtonStyle,
    inactive_style: discord.ButtonStyle,
    custom_id: str,
    disabled: bool,
    allow_reselect: bool,
) -> ActionRow:
    buttons = []
    for i, choice in enumerate(choices):
        is_active = choice.value in active
        if multi:
            callback = _make_multi_choice_callback(choice.value, active, on_select)
            # Whole-control disable only; a multi toggle is never self-disabled.
            button_disabled = disabled
        else:
            callback = _make_single_choice_callback(choice.value, on_select)
            # The active option is disabled (re-picking is a no-op) unless the
            # caller opts into allow_reselect; the whole control is disabled
            # when the caller passes disabled=True.
            button_disabled = disabled or (is_active and not allow_reselect)
        buttons.append(
            StatefulButton(
                label=choice.label,
                emoji=choice.emoji,
                style=active_style if is_active else inactive_style,
                disabled=button_disabled,
                custom_id=_stamp_custom_id(custom_id, str(i)),
                callback=callback,
            )
        )
    return ActionRow(*buttons)


def _choice_select_row(
    choices: List[Choice],
    active: set,
    on_select: Callable,
    multi: bool,
    placeholder: Optional[str],
    custom_id: str,
    disabled: bool,
    allow_reselect: bool,
) -> ActionRow:
    idx_to_value = [choice.value for choice in choices]
    options = [
        discord.SelectOption(
            label=choice.label,
            value=str(i),
            emoji=choice.emoji,
            description=choice.description,
            default=choice.value in active,
        )
        for i, choice in enumerate(choices)
    ]

    async def callback(interaction: discord.Interaction, values: List[str]):
        # StatefulSelect's 2-param protocol passes the raw Discord string
        # values (the str(i) option values), not the real choices. Resolve
        # each back to its Python value before handing it to on_select.
        resolved = [idx_to_value[int(v)] for v in values]
        if multi:
            await await_maybe(on_select(interaction, resolved))
        else:
            picked = resolved[0] if resolved else None
            # Match the button form: a single-select re-pick of the active
            # option is a no-op unless allow_reselect is set. Without this
            # guard the dropdown fires on_select on a re-pick while the button
            # form cannot, so the control's behavior would flip at
            # button_threshold. The host's post-callback defer acks the click.
            if not allow_reselect and picked in active:
                return
            await await_maybe(on_select(interaction, picked))

    select = StatefulSelect(
        options=options,
        custom_id=custom_id,
        placeholder=placeholder,
        min_values=0 if multi else 1,
        max_values=len(choices) if multi else 1,
        disabled=disabled,
        callback=callback,
    )
    return ActionRow(select)


# // ========================================( Content )======================================== // #


def key_value(
    data: Union[Mapping[str, Any], Sequence[Tuple[str, Any]]], id: Optional[int] = None
) -> TextDisplay:
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
    data = normalize_mapping(data, owner="key_value", param="data")
    lines = [f"**{key}:** {value}" for key, value in data.items()]
    return _with_id(
        TextDisplay(_require_text("\n".join(lines), "key_value", "data")),
        id,
        "key_value",
    )


_ALERT_STYLES = {
    "success": ("\u2705", discord.Colour.green),
    "warning": ("\u26a0\ufe0f", discord.Colour.gold),
    "error": ("\u274c", discord.Colour.red),
    "info": ("\u2139\ufe0f", discord.Colour.blurple),
}


def alert(message: str, level: str = "info", id: Optional[int] = None) -> Container:
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
    return _with_id(
        Container(
            TextDisplay(f"{emoji} {message}"),
            accent_colour=color_factory(),
        ),
        id,
        "alert",
    )


def stats_card(
    title: str,
    stats: Dict[str, Any],
    *,
    color: Optional[Union[discord.Colour, int]] = None,
    footer: Optional[str] = None,
    spoiler: bool = False,
    id: Optional[int] = None,
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
        spoiler: Render the whole card blurred until clicked. Matches
            ``card()``'s parameter of the same name.

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
    color = coerce_colour(color, owner="stats_card", param="color")
    theme_managed = color is None
    if theme_managed:
        from ...theming.context import get_current_theme

        theme = get_current_theme()
        if theme:
            color = theme.get_style("accent_colour")
    # A falsy title renders no heading rather than a bare "## ", matching
    # how LeaderboardLayoutView.build_title treats an empty title.
    children: List[Any] = []
    if title:
        children.append(TextDisplay(title if title.startswith("#") else f"## {title}"))
        children.append(Separator(visible=True, spacing=SeparatorSpacing.small))
    children.append(key_value(stats))
    if footer:
        children.append(TextDisplay(f"-# {footer}"))
    container = Container(*children, accent_colour=color, spoiler=spoiler)
    # Same theme-managed contract as ``card()``: see the note there.
    container._cascadeui_theme_accent = theme_managed
    return _with_id(
        container,
        id,
        "stats_card",
    )


def progress_bar(
    value: Union[int, float],
    max_value: Union[int, float],
    *,
    width: int = 20,
    filled: str = "\u2588",
    empty: str = "\u2591",
    show_percent: bool = True,
    id: Optional[int] = None,
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
    for name, number in (("value", value), ("max_value", max_value), ("width", width)):
        # Checked before the comparisons below, which are what fail otherwise:
        # a string operand raises a bare "'<=' not supported" naming neither
        # parameter, and the division further down does the same.
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise TypeError(
                f"progress_bar: {name} must be a number, got "
                f"{type(number).__name__}: {number!r}"
            )
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
    return _with_id(
        TextDisplay(text),
        id,
        "progress_bar",
    )


# // ========================================( Separators )======================================== // #


def _resolve_separator_large(large: Optional[bool]) -> bool:
    """Resolve a separator's spacing against the active theme.

    ``None`` (the default) reads the ambient theme's ``separator_spacing``
    style (``"small"`` / ``"large"``, defaulting to small outside a theme
    context); an explicit ``True`` / ``False`` always wins.
    """
    if large is not None:
        return large
    from ...theming.context import get_current_theme

    theme = get_current_theme()
    return bool(theme) and theme.get_style("separator_spacing") == "large"


def divider(large: Optional[bool] = None, id: Optional[int] = None) -> Separator:
    """A visible separator line between content blocks.

    Args:
        large: Use large spacing around the divider. ``None`` (default)
            follows the active theme's ``separator_spacing`` style;
            outside a theme context it falls back to small spacing.
    """
    size = SeparatorSpacing.large if _resolve_separator_large(large) else SeparatorSpacing.small
    return _with_id(
        Separator(visible=True, spacing=size),
        id,
        "divider",
    )


def gap(large: Optional[bool] = None, id: Optional[int] = None) -> Separator:
    """Invisible spacing between content blocks (no visible line).

    Args:
        large: Use large spacing. ``None`` (default) follows the active
            theme's ``separator_spacing`` style; outside a theme context
            it falls back to small spacing.
    """
    size = SeparatorSpacing.large if _resolve_separator_large(large) else SeparatorSpacing.small
    return _with_id(
        Separator(visible=False, spacing=size),
        id,
        "gap",
    )


# // ========================================( Navigation )======================================== // #


def tab_nav(
    tabs: Union[Mapping[str, Callable], Sequence[Tuple[str, Callable]]],
    *,
    active: Optional[str] = None,
    active_style: discord.ButtonStyle = discord.ButtonStyle.primary,
    inactive_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    custom_id: Optional[str] = None,
    id: Optional[int] = None,
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
        custom_id: Base id for the row, suffixed per tab as
            ``{custom_id}_0``, ``{custom_id}_1``, and so on in mapping
            order. Required inside a ``PersistentLayoutView``; omit it
            anywhere else.

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
    _check_button_styles("tab_nav", active_style=active_style, inactive_style=inactive_style)
    # Normalized first, before anything measures or indexes it. The size
    # guards below call len(), which a non-mapping answers with its own
    # error naming neither the builder nor the parameter, and which a
    # generator of pairs, accepted by every sibling that takes a mapping,
    # cannot answer at all. Normalizing here also resolves ``active``
    # against real keys: reading the raw argument bound it to a
    # ``(label, callback)`` tuple, so no tab rendered active.
    tabs = normalize_mapping(tabs, owner="tab_nav", param="tabs")
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

    return _with_id(
        ActionRow(
            *(
                StatefulButton(
                    label=label,
                    style=active_style if label == active else inactive_style,
                    custom_id=_stamp_custom_id(custom_id, str(i)),
                    callback=callback,
                )
                for i, (label, callback) in enumerate(tabs.items())
            )
        ),
        id,
        "tab_nav",
    )


# // ========================================( Pagination )======================================== // #


async def _rerender_host(view) -> None:
    """Re-run a host view's render path, then ship the edit.

    Shared by the V2 stateful composites (``PaginatedRegion``,
    ``Collapsible``). Host views rebuild through different seams, and the
    probe matches each in turn:

    - ``build_ui``-based views rebuild the tree from already-loaded data
      (cheap, no refetch), then ``refresh``.
    - ``TabLayoutView`` rebuilds the active tab through ``_refresh_tabs``,
      which re-runs the tab builder and ships the edit itself.
    - ``on_load``-based views rebuild inside ``on_load`` -- ``reload()`` is
      ``on_load`` + ``refresh``.
    - A bare ``refresh`` re-ships the current tree when the host matches
      none of the above.

    Probed by duck typing rather than ``isinstance`` so the component layer
    stays free of a ``views`` import.
    """
    if view is None or view.is_finished():
        return
    build = getattr(view, "build_ui", None)
    if build is not None:
        result = build()
        if inspect.isawaitable(result):
            await result
        await view.refresh()
        return
    refresh_tabs = getattr(view, "_refresh_tabs", None)
    if refresh_tabs is not None:
        await refresh_tabs()
        return
    reload = getattr(view, "reload", None)
    if reload is not None:
        await reload()
        return
    await view.refresh()


class PaginatedRegion:
    """A stateful pager for ONE region of a V2 layout view.

    The V2 sibling of the V1 :class:`PaginationControls` composite. Where
    :class:`~cascadeui.PaginatedLayoutView` owns the whole message and
    paginates it end to end, a ``PaginatedRegion`` paginates a single slice
    of items *inside* a host view's ``build_ui()`` and leaves the rest of
    the tree (headers, other cards, even a second region) to the host.
    Each instance holds its own page index, so two regions can live in one
    view without sharing a cursor.

    That index lives on the region, and the region is built in the host's
    ``__init__`` -- so a ``pop`` that reconstructs the host builds a fresh
    region on page one. Name it in the host's ``get_nav_state`` to carry it::

        def get_nav_state(self):
            return {"page": self.pager.page}

        def restore_nav_state(self, state):
            self.pager.set_page(state.get("page", 0))

    The host opts in deliberately: only it knows whether returning to a
    drill-down should resume where the user left off or start clean.

    The host owns the data and the per-item rendering; the region owns the
    page index, the slice math, and the navigation row. Inside the host's
    ``build_ui()`` the host sets the current item list on the region, reads
    the page slice, renders each item however it likes, and asks the region
    for its controls::

        class TaskListView(StatefulLayoutView):
            def __init__(self, tasks, **kwargs):
                super().__init__(**kwargs)
                self.tasks = tasks
                self.pager = PaginatedRegion(per_page=6)

            def build_ui(self):
                self.clear_items()
                self.pager.items = self.tasks
                rows = [
                    action_section(t.title, label="Open", callback=self._open(t))
                    for t in self.pager.page_items
                ]
                self.add_item(card("## Tasks", *rows, *self.pager.controls(self)))

    On a Prev/Next/jump click the region updates its index, re-runs the
    host's render path, and ships the edit. A host that builds its tree in
    ``build_ui()`` is rebuilt there (sync or async); a host that builds in
    ``on_load()`` is rebuilt through ``reload()``. Either way the new
    page's slice is rendered before the refresh. Beyond the bare
    three-button pager, first/last jump buttons and a go-to-page modal
    appear once the page count reaches ``jump_threshold``, matching the
    navigation surface of :class:`~cascadeui.PaginatedLayoutView`.

    Customization mirrors ``PaginatedLayoutView`` exactly. Each of the five
    navigation buttons (``first``, ``prev``, ``indicator``, ``next``,
    ``last``) exposes a ``{label, emoji, style}`` class-attribute triple,
    and ``jump_threshold`` is a class attribute. Subclass and override the
    ones to change::

        class WideRegion(PaginatedRegion):
            jump_threshold = 3
            prev_button_label = "Prev"
            next_button_label = "Next"

    The ``async def on_page_changed(self, page)`` hook runs after the page
    index updates and before the refresh -- the seam for analytics, async
    prefetch, or per-page validation. It mirrors the same hook on
    ``PaginatedView`` / ``PaginatedLayoutView``. An override that needs
    the host's data or helpers reads them through the read-only
    :attr:`host` property.

    Two regions in one view need distinct ``key`` values so their button
    custom_ids do not collide::

        self.left = PaginatedRegion(per_page=5, key="left")
        self.right = PaginatedRegion(per_page=5, key="right")

    Set ``per_page=1`` for a carousel that shows one item at a time.

    Args:
        items: Initial item list. May be omitted and set later via the
            ``items`` property (the usual path for data that loads in
            ``on_load``).
        per_page: Items per page. Must be a positive int. ``1`` produces
            a one-item-at-a-time carousel.
        key: Disambiguator baked into the nav button custom_ids. Two
            regions in the same view must use distinct keys.
    """

    # Page count at which first/last and go-to-page buttons appear.
    jump_threshold: ClassVar[int] = 5

    first_button_label: ClassVar[Optional[str]] = "⏮"
    first_button_emoji: ClassVar[EmojiInput] = None
    first_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    prev_button_label: ClassVar[Optional[str]] = "◀"
    prev_button_emoji: ClassVar[EmojiInput] = None
    prev_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    indicator_button_label: ClassVar[Optional[str]] = None  # default uses "Page {n}/{t}"
    indicator_button_emoji: ClassVar[EmojiInput] = None
    indicator_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.primary

    next_button_label: ClassVar[Optional[str]] = "▶"
    next_button_emoji: ClassVar[EmojiInput] = None
    next_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    last_button_label: ClassVar[Optional[str]] = "⏭"
    last_button_emoji: ClassVar[EmojiInput] = None
    last_button_style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        threshold = cls.__dict__.get("jump_threshold")
        if threshold is not None and (
            not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1
        ):
            raise ValueError(
                f"{cls.__name__}.jump_threshold must be a positive int, got {threshold!r}"
            )
        for attr in (
            "first_button_style",
            "prev_button_style",
            "indicator_button_style",
            "next_button_style",
            "last_button_style",
        ):
            style = cls.__dict__.get(attr)
            if style is not None and not isinstance(style, discord.ButtonStyle):
                raise TypeError(
                    f"{cls.__name__}.{attr} must be a discord.ButtonStyle, got {style!r}"
                )
        for attr in (
            "first_button_label",
            "prev_button_label",
            "indicator_button_label",
            "next_button_label",
            "last_button_label",
        ):
            label = cls.__dict__.get(attr)
            if label is not None and not isinstance(label, str):
                raise TypeError(f"{cls.__name__}.{attr} must be a str or None, got {label!r}")
        for attr in (
            "first_button_emoji",
            "prev_button_emoji",
            "indicator_button_emoji",
            "next_button_emoji",
            "last_button_emoji",
        ):
            emoji = cls.__dict__.get(attr)
            if emoji is not None and not isinstance(
                emoji, (str, discord.Emoji, discord.PartialEmoji)
            ):
                raise TypeError(
                    f"{cls.__name__}.{attr} must be a str, discord.Emoji, "
                    f"discord.PartialEmoji, or None, got {emoji!r}"
                )

    def __init__(
        self,
        *,
        items: Optional[Sequence[Any]] = None,
        per_page: int = 10,
        key: str = "page",
    ) -> None:
        if not isinstance(per_page, int) or isinstance(per_page, bool) or per_page < 1:
            raise ValueError(f"per_page must be a positive int, got {per_page!r}")
        if not isinstance(key, str) or not key:
            raise ValueError(f"key must be a non-empty str, got {key!r}")

        self._items: List[Any] = list(items) if items is not None else []
        self._per_page = per_page
        self._key = key

        self._page = 0
        # Captured by controls() so click callbacks can rebuild + refresh
        # the host view (mirrors ToggleGroup.add_to_view).
        self._view = None

    # // ----( Override hook )---- // #

    async def on_page_changed(self, page: int) -> None:
        """Called after the page index updates, before the refresh.

        ``page`` is the zero-based index of the new current page. Default
        is a no-op. Override for analytics, async prefetch, or per-page
        validation that should fire on every page turn. The host owns the
        data, so an override that prefetches reads it through
        :attr:`host` (set by the time any page can turn).
        """
        return None

    # // ----( Data + slice )---- // #

    @property
    def items(self) -> List[Any]:
        """The full item list the region paginates."""
        return self._items

    @items.setter
    def items(self, value: Sequence[Any]) -> None:
        self._items = list(value)
        self._clamp()

    @property
    def page(self) -> int:
        """Current zero-based page index."""
        return self._page

    @property
    def page_count(self) -> int:
        """Total number of pages for the current item list (minimum 1)."""
        return max(1, (len(self._items) + self._per_page - 1) // self._per_page)

    @property
    def page_items(self) -> List[Any]:
        """The slice of items on the current page."""
        self._clamp()
        start = self._page * self._per_page
        return self._items[start : start + self._per_page]

    @property
    def host(self) -> Optional[Any]:
        """The host ``StatefulLayoutView`` the region is attached to, or ``None``.

        Read-only. ``on_page_changed`` overrides read this to reach the
        host (refetching the host's data source on a page turn, or
        calling its ``respond`` for per-page feedback) instead of
        reaching into ``_view``. ``None`` until the host renders: the
        capture happens on the first ``controls(view)`` /
        ``control_buttons(view)`` call, inside the host's own build.
        """
        return self._view

    def set_page(self, index: int) -> None:
        """Move the cursor to a zero-based page index without re-rendering.

        Synchronous and render-free, unlike the same-named
        ``_BasePaginatedMixin.set_page``, which is ``async`` and edits the
        message. The split is deliberate: this one runs before the region
        is attached to anything (``restore_nav_state`` on a popped host),
        where there is no tree to re-render yet. Use :meth:`show_page` for
        the "jump and re-render" gesture the view-side method performs.

        Clamping waits while the region is empty. A host that loads its items
        in ``on_load`` sets the page before they arrive -- ``restore_nav_state``
        runs first, so a page carried across a ``pop`` lands here against an
        empty list -- and clamping then would quietly rewrite the index to
        zero, losing the page the user was on.

        Nothing out of range can render regardless: the ``items`` setter
        re-clamps on assignment, and both read paths (``page_items`` for
        the content and the nav builder for the button state) clamp before
        they read.

        A negative index counts from the end, so ``set_page(-1)`` is the
        last page. That is the only way to name the last page without
        first knowing how many there are, which matters after the item
        list changes: reading ``page_count`` to compute the index forces a
        fetch, and the jump would then load it again.

        **A seek is invisible until something rebuilds.** This method moves
        the cursor and nothing else, so the tree on screen still shows the
        page it was composed with. Reach for :meth:`show_page` to move and
        re-render together::

            await self.pager.show_page(-1)   # one fetch, one edit

        Pairing this with ``refresh()`` does not substitute: ``refresh``
        ships the existing tree without composing a new one, so the cursor
        advances and the rows do not. The mismatch is visible rather than
        silent when anything in the tree counts rows, since the changed
        total defeats the render-hash skip and an edit goes out carrying
        the new count above the old rows.

        Use this one where there is no tree to re-render yet: before the
        region is attached, or from ``restore_nav_state`` on a popped host.

        With no items there is one page and page zero is already the last,
        so a negative index resolves to zero rather than waiting.
        """
        if index < 0:
            index += self.page_count
        self._page = max(0, index)
        if self._items:
            self._clamp()

    def _clamp(self) -> None:
        self._page = max(0, min(self._page, self.page_count - 1))

    # // ----( Controls )---- // #

    def controls(self, view, *, compact: bool = False) -> List[ActionRow]:
        """Capture the host view and return the navigation row.

        Returns an empty list when the item list fits on a single page
        (nothing to navigate), so the result splats into a ``card(...)``
        or a sequence of ``add_item`` calls without adding stray rows::

            card("## Tasks", *rows, *self.pager.controls(self))

        ``view`` must be the host ``StatefulLayoutView``; the region calls
        its ``build_ui`` / ``refresh`` / ``open_modal`` on navigation.

        ``compact=True`` builds a three-button row (prev, go-to-page,
        next), dropping first/last and the standalone indicator. It trims
        the pager's own row to three nodes; to fuse the pager buttons into
        a host-owned row with Back/Exit, see :meth:`control_buttons`.
        """
        self._view = view
        if self.page_count <= 1:
            return []
        return [ActionRow(*self._build_nav_buttons(compact=compact))]

    def control_buttons(self, view, *, compact: bool = False) -> List:
        """Capture the host view and return the nav buttons unwrapped.

        The button-level counterpart to :meth:`controls`: the same wired
        prev / next / jump buttons, returned as a bare list instead of an
        ``ActionRow``, so a node-tight host packs them into a row it owns
        alongside its own Back / Exit buttons::

            ActionRow(
                *self.pager.control_buttons(self, compact=True),
                self.make_back_button(),
                self.make_exit_button(),
            )

        Mirrors the :meth:`~cascadeui.StatefulLayoutView.make_back_button`
        (primitive) / :meth:`~cascadeui.StatefulLayoutView.make_nav_row`
        (wrapper) split, with ``controls`` as the wrapper. Returns an empty
        list on a single page.

        ``compact=True`` returns three buttons (prev, go-to-page, next),
        the shape that fuses with Back + Exit inside one five-button
        ActionRow (3 + 2 = 5). The full set is up to five buttons and is
        meant for a row of its own; fusing it with other buttons overflows
        the per-row budget.
        """
        self._view = view
        if self.page_count <= 1:
            return []
        return self._build_nav_buttons(compact=compact)

    def _build_nav_buttons(self, compact: bool = False) -> List:
        """Build the navigation buttons unwrapped.

        ``compact`` drops first/last and forces the middle node to the
        clickable go-to button, leaving prev + go-to + next -- three
        nodes a node-tight host can fuse with its own Back/Exit row.

        The return list is mixed: the below-threshold full-mode indicator
        is a bare ``discord.ui.Button``, not a ``StatefulButton``, which is
        why the annotation stays the unparameterized ``List``.
        """
        # Button state is a read of the cursor, and the cursor can be set
        # before the items it indexes arrive (set_page defers its clamp for
        # exactly that case). Clamp here as page_items does, so a host that
        # builds its controls before assigning items cannot render prev/next
        # flags and an indicator that disagree with the content.
        self._clamp()
        total = self.page_count
        show_jump = (not compact) and total >= self.jump_threshold
        at_first = self._page == 0
        at_last = self._page >= total - 1

        buttons: List = []

        if show_jump:
            buttons.append(
                StatefulButton(
                    label=self.first_button_label or "⏮",
                    emoji=self.first_button_emoji,
                    style=self.first_button_style,
                    custom_id=f"region_{self._key}_first",
                    disabled=at_first,
                    callback=self._make_jump(lambda: 0),
                )
            )

        buttons.append(
            StatefulButton(
                label=self.prev_button_label or "◀",
                emoji=self.prev_button_emoji,
                style=self.prev_button_style,
                custom_id=f"region_{self._key}_prev",
                disabled=at_first,
                callback=self._make_step(-1),
            )
        )

        # Middle node: clickable go-to when jumps are available (full mode
        # at or above jump_threshold, or any compact row), else a
        # non-interactive page indicator. indicator_button_style governs the
        # clickable node only; the disabled indicator stays secondary, since
        # a disabled button in an accent colour reads as one that is broken.
        if show_jump or compact:
            buttons.append(
                StatefulButton(
                    label=self._resolve_goto_label(),
                    emoji=self.indicator_button_emoji,
                    style=self.indicator_button_style,
                    custom_id=f"region_{self._key}_goto",
                    callback=self._open_goto_modal,
                )
            )
        else:
            buttons.append(
                discord.ui.Button(
                    label=self._resolve_indicator_label(),
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"region_{self._key}_indicator",
                    disabled=True,
                )
            )

        buttons.append(
            StatefulButton(
                label=self.next_button_label or "▶",
                emoji=self.next_button_emoji,
                style=self.next_button_style,
                custom_id=f"region_{self._key}_next",
                disabled=at_last,
                callback=self._make_step(1),
            )
        )

        if show_jump:
            buttons.append(
                StatefulButton(
                    label=self.last_button_label or "⏭",
                    emoji=self.last_button_emoji,
                    style=self.last_button_style,
                    custom_id=f"region_{self._key}_last",
                    disabled=at_last,
                    callback=self._make_jump(lambda: self.page_count - 1),
                )
            )

        return buttons

    # // ----( Labels )---- // #

    def _resolve_indicator_label(self) -> str:
        if self.indicator_button_label is not None:
            return self.indicator_button_label
        return f"Page {self._page + 1}/{self.page_count}"

    def _resolve_goto_label(self) -> str:
        if self.indicator_button_label is not None:
            return self.indicator_button_label
        return f"{self._page + 1}/{self.page_count}"

    # // ----( Callbacks )---- // #

    def _make_step(self, delta: int):
        async def callback(interaction: discord.Interaction):
            previous = self._page
            self._page += delta
            self._clamp()
            await _guard_hook(self.on_page_changed, self._page, owner=self)
            await self._rerender(previous_page=previous)

        return callback

    def _make_jump(self, target_fn: Callable[[], int]):
        # target_fn re-resolves at click time so "last" tracks the live
        # item count, not the count captured when the row was built.
        async def callback(interaction: discord.Interaction):
            previous = self._page
            self._page = target_fn()
            self._clamp()
            await _guard_hook(self.on_page_changed, self._page, owner=self)
            await self._rerender(previous_page=previous)

        return callback

    async def _open_goto_modal(self, interaction: discord.Interaction):
        region = self
        view = self._view
        total = self.page_count

        class _GotoModal(discord.ui.Modal, title="Go to Page"):
            page_input = discord.ui.TextInput(
                label=f"Page number (1–{total})",
                placeholder=str(region._page + 1),
                min_length=1,
                max_length=len(str(total)),
                required=True,
            )

            async def on_submit(modal_self, modal_interaction: discord.Interaction):
                value = modal_self.page_input.value.strip()
                try:
                    page_num = int(value)
                except ValueError:
                    await view.respond(
                        modal_interaction,
                        f"'{value}' is not a valid page number.",
                        ephemeral=True,
                    )
                    return
                previous = region.page
                region.set_page(max(1, min(page_num, total)) - 1)
                await view._safe_defer(modal_interaction)
                await _guard_hook(region.on_page_changed, region.page, owner=region)
                await region._rerender(previous_page=previous)

        await view.open_modal(interaction, _GotoModal())

    async def show_page(self, index: int) -> None:
        """Jump to a zero-based page index and re-render the host.

        The counterpart to ``_BasePaginatedMixin.set_page`` on the view
        side: seeks the cursor, fires ``on_page_changed``, and ships the
        edit. Reach for this from a host callback that wants a
        programmatic jump (a search hit, a "find me" button). Requires the
        region to be attached (``controls(view)`` must have run), since
        there is no host to re-render before that.
        """
        previous = self._page
        self.set_page(index)
        await _guard_hook(self.on_page_changed, self._page, owner=self)
        if self._view is not None:
            await self._rerender(previous_page=previous)

    async def _rerender(self, *, previous_page: Optional[int] = None) -> None:
        # Re-run the host's render path and ship the edit; shared with
        # Collapsible via _rerender_host.
        await _rerender_host(self._view)
        if previous_page is None or self._page == previous_page:
            return
        if getattr(self._view, "refresh_degraded", False):
            # The host's edit never reached Discord, so the region on screen
            # still shows the old slice. Put the cursor back, or the next
            # press pages on from a position the reader never saw. The host
            # rebuilds from this cursor on its next render; no second edit
            # here, because the connection is still down.
            self._page = previous_page


# // ========================================( Collapsible )======================================== // #


class Collapsible:
    """A trigger button that toggles an inline region of revealed content.

    The V2 collapsible (disclosure / expander) primitive. A button shows
    one label while collapsed; clicking it reveals a region of content (a
    ``choice_row``, a select, a card, more buttons) and swaps the trigger
    to its expanded label. Clicking again collapses. Each instance holds
    its own collapsed/expanded state, so two collapsibles in one view are
    independent.

    That state lives on the collapsible, which the host builds in
    ``__init__`` -- so a ``pop`` that reconstructs the host brings it back
    collapsed. Name it in the host's ``get_nav_state`` to carry it::

        def get_nav_state(self):
            return {"open": self.picker.expanded}

        def restore_nav_state(self, state):
            if state.get("open"):
                self.picker.expand()

    The collapsible owns the toggle mechanism (the boolean, the trigger,
    the rebuild) and leaves the content and the collapse policy to the
    host. The host supplies a ``reveal`` callable that returns the
    components to show while expanded, and renders the collapsible inside
    its ``build_ui()`` (or ``on_load()``)::

        class FilterView(StatefulLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.league_picker = Collapsible(
                    label="Edit Leagues",
                    expanded_label="Done",
                    reveal=lambda: choice_row(
                        LEAGUES, selected=self.league, on_select=self._pick_league
                    ),
                )

            def build_ui(self):
                self.clear_items()
                self.add_item(card("## Filters", key_value(self._filter_summary())))
                for item in self.league_picker.render(self):
                    self.add_item(item)

            async def _pick_league(self, interaction, value):
                self.league = value
                self.league_picker.collapse()   # collapse after the pick
                self.build_ui()
                await self.refresh()

    A click toggles the state and re-runs the host's render path (the same
    ``build_ui``-or-``reload`` seam ``PaginatedRegion`` uses). Collapse
    policy stays the host's: call ``collapse()`` when a revealed action
    completes, or leave it expanded. The companion stateful composite to
    ``PaginatedRegion``: where ``PaginatedRegion.controls(view)`` contributes
    only a nav row, ``Collapsible.render(view)`` contributes its whole widget.

    The same trigger covers every shape of the pattern: a single button
    that opens and closes the region, a relabeled "Cancel"/"Done" while
    open, or a styled state swap. Configure via ``expanded_label`` /
    ``expanded_style`` / ``expanded_emoji``. Pass ``summary`` to fuse the
    trigger into a Section beside a line of summary text (an
    ``action_section``) instead of a bare button row -- the shape a
    card-based disclosure wants.

    Args:
        label: Trigger label while collapsed.
        reveal: Zero-argument synchronous callable returning the revealed
            content -- a single component or a list. Called on every render
            while expanded, so its callbacks stay fresh across rebuilds.
            Async sources load in the host's ``on_load`` and ``reveal`` reads
            the result synchronously.
        summary: Optional zero-argument synchronous callable returning the
            trigger's body text. When set, ``render`` emits the trigger as an
            ``action_section`` (a Section carrying the text, with the trigger
            button as its accessory) instead of a bare ``ActionRow(button)``,
            so a card-based disclosure fuses the trigger beside its summary.
            Falls back to the bare button when the callable returns an empty
            value. Default ``None`` keeps the bare button row.
        expanded_label: Trigger label while expanded. Defaults to ``label``
            (the trigger keeps its text); set it to ``"Done"`` / ``"Cancel"``
            for a relabel-on-open.
        style: Trigger style while collapsed (default secondary).
        expanded_style: Trigger style while expanded (default secondary).
        emoji: Trigger emoji while collapsed.
        expanded_emoji: Trigger emoji while expanded. Defaults to ``emoji``.
        expanded: Initial state (default collapsed).
        trigger_first: When ``True`` (default), the trigger renders above
            the revealed content; when ``False``, below it (a "Cancel"
            button under the region).
        key: Disambiguator baked into the trigger custom_id. Two
            collapsibles in one view need distinct keys.
    """

    def __init__(
        self,
        *,
        label: str,
        reveal: Callable,
        summary: Optional[Callable[[], str]] = None,
        expanded_label: Optional[str] = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        expanded_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: EmojiInput = None,
        expanded_emoji: EmojiInput = None,
        expanded: bool = False,
        trigger_first: bool = True,
        key: str = "collapsible",
    ) -> None:
        if not isinstance(label, str) or not label:
            raise ValueError(f"label must be a non-empty str, got {label!r}")
        if expanded_label is not None and (
            not isinstance(expanded_label, str) or not expanded_label
        ):
            raise ValueError(
                f"expanded_label must be a non-empty str or None, got {expanded_label!r}"
            )
        if not callable(reveal):
            raise TypeError(f"reveal must be callable, got {type(reveal).__name__}")
        if inspect.iscoroutinefunction(reveal):
            raise TypeError(
                "reveal must be synchronous; load async data in the host's "
                "on_load() and have reveal read the result synchronously"
            )
        if summary is not None:
            if not callable(summary):
                raise TypeError(f"summary must be callable or None, got {type(summary).__name__}")
            if inspect.iscoroutinefunction(summary):
                raise TypeError(
                    "summary must be synchronous; load async data in the host's "
                    "on_load() and have summary read the result synchronously"
                )
        if not isinstance(key, str) or not key:
            raise ValueError(f"key must be a non-empty str, got {key!r}")
        if not isinstance(expanded, bool):
            raise TypeError(f"expanded must be a bool, got {type(expanded).__name__}")
        if not isinstance(trigger_first, bool):
            raise TypeError(f"trigger_first must be a bool, got {type(trigger_first).__name__}")
        for name, value in (("style", style), ("expanded_style", expanded_style)):
            if not isinstance(value, discord.ButtonStyle):
                raise TypeError(f"{name} must be a discord.ButtonStyle, got {value!r}")

        self._label = label
        self._reveal = reveal
        self._summary = summary
        self._expanded_label = expanded_label if expanded_label is not None else label
        self._style = style
        self._expanded_style = expanded_style
        self._emoji = emoji
        self._expanded_emoji = expanded_emoji if expanded_emoji is not None else emoji
        self._expanded = expanded
        self._trigger_first = trigger_first
        self._key = key
        self._view = None

    # // ----( Override hook )---- // #

    async def on_toggle(self, expanded: bool) -> None:
        """Called with the new ``expanded`` value, after the flag flips and
        before the re-render.

        ``expanded`` is the post-flip state (``True`` once revealed, ``False``
        once collapsed). Default is a no-op. Override to fetch async data when
        expanded, log toggle events, or run validation on every open/close.
        An override that needs the host's data or helpers reads them through
        :attr:`host` (set by the time the trigger can be clicked).
        """
        return None

    # // ----( State )---- // #

    @property
    def expanded(self) -> bool:
        """Whether the region is currently revealed."""
        return self._expanded

    def expand(self) -> None:
        """Reveal the region on the next render."""
        self._expanded = True

    def collapse(self) -> None:
        """Hide the region on the next render."""
        self._expanded = False

    @property
    def host(self) -> Optional[Any]:
        """The host ``StatefulLayoutView`` the collapsible is attached to, or ``None``.

        Read-only. ``on_toggle`` overrides read this to reach the host
        (loading data on expand, or calling its ``respond``) instead of
        reaching into ``_view``. ``None`` until the host renders: the
        capture happens on the first ``render(view)`` call, inside the
        host's own build.
        """
        return self._view

    def render(self, view) -> List[Any]:
        """Stash the host view and return the collapsible's components.

        Returns ``[trigger]`` while collapsed, or the trigger plus the
        ``reveal()`` content while expanded (ordered by ``trigger_first``).
        Splat the result into the host's tree::

            for item in self.disclosure.render(self):
                self.add_item(item)

        ``view`` must be the host ``StatefulLayoutView``; the trigger calls
        its ``build_ui`` / ``refresh`` on toggle. Returns the whole widget
        (trigger plus revealed content), unlike ``PaginatedRegion.controls``,
        which returns only its nav row while the host renders the content.
        """
        self._view = view
        trigger = self._build_trigger()
        if not self._expanded:
            return [trigger]

        revealed = _require_sync_result(self._reveal(), "Collapsible", "reveal")
        revealed = revealed if isinstance(revealed, list) else [revealed]
        return [trigger, *revealed] if self._trigger_first else [*revealed, trigger]

    def _build_trigger(self):
        """Return the trigger component for the current state.

        An ``action_section`` (Section + button accessory) when summary text
        is available, else a bare ``ActionRow(button)``.
        """
        # The trigger button relabels/restyles between collapsed and expanded.
        label = self._expanded_label if self._expanded else self._label
        style = self._expanded_style if self._expanded else self._style
        emoji = self._expanded_emoji if self._expanded else self._emoji
        custom_id = f"{self._key}_trigger"

        # With a summary, fuse the trigger into a Section beside its text via
        # action_section. Fall back to the bare button row when no summary is
        # set or the callable yields nothing (e.g. data not loaded yet) -- an
        # empty Section has no text to render.
        if self._summary is not None:
            text = _require_sync_result(self._summary(), "Collapsible", "summary")
            if text:
                return action_section(
                    text,
                    label=label,
                    callback=self._toggle,
                    style=style,
                    emoji=emoji,
                    custom_id=custom_id,
                )
        return ActionRow(
            StatefulButton(
                label=label,
                style=style,
                emoji=emoji,
                custom_id=custom_id,
                callback=self._toggle,
            )
        )

    async def _toggle(self, interaction: discord.Interaction) -> None:
        previous = self._expanded
        self._expanded = not self._expanded
        await _guard_hook(self.on_toggle, self._expanded, owner=self)
        await _rerender_host(self._view)
        if getattr(self._view, "refresh_degraded", False):
            # The trigger on screen still shows the previous state, so flip
            # back to match it. Self-healing either way (one more click would
            # resynchronize a binary toggle), but leaving them disagreed makes
            # the next click read as a no-op to the reader.
            self._expanded = previous


# // ========================================( Media )======================================== // #


def gallery(
    *media: MediaInput,
    descriptions: Optional[Sequence[Optional[str]]] = None,
    spoilers: Optional[Sequence[bool]] = None,
    id: Optional[int] = None,
) -> MediaGallery:
    """Build a MediaGallery from image references.

    Simplifies the ``MediaGallery(MediaGalleryItem(...), ...)`` nesting
    into a flat call.

    Args:
        *media: Image references (up to 10). Each accepts either a URL
            string (remote or ``attachment://`` form) or a
            :class:`discord.File` instance whose ``.uri`` is used.
            File-backed references require the same ``discord.File``
            objects to be passed via ``view.send(files=[...])``.
        spoilers: Optional sequence of per-item spoiler flags, one per
            media reference. Matches the parallel-sequence shape
            ``descriptions`` uses; pad with ``False`` for unblurred items.
        descriptions: Optional sequence of descriptions matching each
            reference positionally. Use ``None`` for items without a
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
    if not media:
        raise ValueError(
            "gallery: at least one media reference is required. Discord "
            "rejects empty MediaGallery components with HTTP 400 (1-10 "
            "items required)."
        )
    if len(media) > 10:
        raise ValueError(
            f"gallery: too many media references ({len(media)}). Discord "
            f"caps MediaGallery at 10 items. Split into multiple gallery() "
            f"calls."
        )
    if isinstance(descriptions, str):
        raise TypeError(
            f"gallery: descriptions must be a sequence with one entry per media "
            f"reference, not a single string; got {descriptions!r}. A string has "
            f"a length of its own, so it would be counted as that many descriptions."
        )
    if descriptions is not None and len(descriptions) != len(media):
        raise ValueError(
            f"gallery: descriptions length ({len(descriptions)}) must match "
            f"media length ({len(media)}). Pad with None for references that "
            f"should have no description."
        )
    if spoilers is not None and len(spoilers) != len(media):
        raise ValueError(
            f"gallery: spoilers length ({len(spoilers)}) must match media "
            f"length ({len(media)}). Pad with False for references that "
            f"should render unblurred."
        )

    items = []
    for i, ref in enumerate(media):
        desc = descriptions[i] if descriptions is not None else None
        kwargs = {"media": _coerce_media_ref(ref, owner="gallery", param=f"media[{i}]")}
        if desc is not None:
            kwargs["description"] = desc
        if spoilers is not None and spoilers[i]:
            kwargs["spoiler"] = True
        items.append(MediaGalleryItem(**kwargs))
    return _with_id(
        MediaGallery(*items),
        id,
        "gallery",
    )


def file_attachment(
    url: MediaInput,
    *,
    spoiler: bool = False,
    id: Optional[int] = None,
) -> File:
    """Build a File component for attachment display.

    Completes the V2 media family alongside ``gallery()``. Discord
    renders the file as a downloadable card inline with the rest of the
    V2 content.

    Args:
        url: File reference. Accepts either a remote URL, the
            ``attachment://<filename>`` form, or a
            :class:`discord.File` instance whose ``.uri`` is used. When
            a file-backed reference is supplied, the same
            ``discord.File`` must travel via ``view.send(files=[...])``.
        spoiler: Whether to flag the file as a spoiler.

    Returns:
        A ``File`` component ready to be added to a Container or
        ``StatefulLayoutView``.

    Example::

        card(
            "## Quarterly Report",
            file_attachment("attachment://q1_2026.pdf"),
            "Released April 15.",
        )
    """
    return _with_id(
        File(
            media=_coerce_media_ref(url, owner="file_attachment", param="url"),
            spoiler=spoiler,
        ),
        id,
        "file_attachment",
    )


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
        # cell_sep joins every row and corner renders into the header, so a
        # non-string reaches str.join during __init__ and fails there rather
        # than here, where fill is already checked.
        if not isinstance(cell_sep, str):
            raise TypeError(f"cell_sep must be str, got {type(cell_sep).__name__}")
        if corner is not None and not isinstance(corner, str):
            raise TypeError(f"corner must be str or None, got {type(corner).__name__}")

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
    id: Optional[int] = None,
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
    return _with_id(
        EmojiGrid(
            rows,
            cols,
            fill=fill,
            row_labels=row_labels,
            col_labels=col_labels,
            corner=corner,
            cell_sep=cell_sep,
        ),
        id,
        "emoji_grid",
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
    for r in range(rows):
        buttons = []
        for c in range(cols):
            btn = cell_factory(r, c)
            # Every cell is checked, not only the first. A factory that
            # returns something else for one position -- ``None`` to leave a
            # gap is the plausible one -- otherwise reached ActionRow and
            # failed there on an attribute the caller never mentioned,
            # naming no cell.
            if not isinstance(btn, discord.ui.Button):
                raise TypeError(
                    f"cell_factory must return discord.ui.Button, got "
                    f"{type(btn).__name__} at (row={r}, col={c})"
                )
            buttons.append(btn)
        action_rows.append(ActionRow(*buttons))
    return action_rows
