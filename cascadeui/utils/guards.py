# // ========================================( Modules )======================================== // #


from typing import Any, Dict, Optional

import discord

# // ========================================( Functions )======================================== // #


def normalize_mapping(value: Any, *, owner: str, param: str) -> Dict[Any, Any]:
    """Resolve a ``{key: value}`` argument, absorbing the pair-sequence form.

    Several builders take a mapping of label to callback or label to text.
    A sequence of two-item pairs is the same data in the order the caller
    wrote it, and reads naturally enough that it is a common first guess;
    left alone it fails on ``.items()`` with an ``AttributeError`` naming a
    method rather than a parameter. The pair form is unambiguous, so it is
    converted rather than refused.

    Any mapping passes through untouched, not only ``dict``: the builders
    have always accepted one, and narrowing to a concrete type here would
    reject every ``Mapping`` subclass that works today.

    Args:
        value: The caller's argument.
        owner: Builder or class name for the error message.
        param: Parameter name for the error message.

    Returns:
        The mapping itself, or a new ``dict`` built from a pair sequence.

    Raises:
        TypeError: The value is neither a mapping nor a sequence of pairs.
    """
    if hasattr(value, "items"):
        return value
    try:
        return dict(value)
    except (TypeError, ValueError):
        raise TypeError(
            f"{owner} {param} must be a mapping of {{key: value}}, or a sequence "
            f"of (key, value) pairs; got {type(value).__name__}: {value!r}"
        ) from None


def coerce_colour(value: Any, *, owner: str, param: str) -> Optional[discord.Colour]:
    """Resolve a colour argument to ``discord.Colour``, absorbing the int form.

    discord.py coerces an ``int`` in ``Embed.colour``'s setter but stores one
    verbatim on ``Container.accent_colour``, so the same hex literal that
    themes a V1 embed leaves a bare ``int`` on a V2 container and every
    reader downstream has to handle both types. Coercing at the boundary
    keeps one type in the tree.

    ``Colour`` itself range-checks nothing: ``Colour(True)`` serializes as
    ``true`` and an out-of-range int as a number Discord rejects, both
    surfacing as an HTTP 400 far from the literal that caused it.

    Args:
        value: ``None``, a ``discord.Colour``, or a 24-bit ``int``.
        owner: Builder or class name for the error message.
        param: Parameter name for the error message.

    Returns:
        ``None``, or the value as a ``discord.Colour``.

    Raises:
        TypeError: The value is neither a ``Colour`` nor an ``int``.
        ValueError: The int falls outside Discord's 24-bit colour range.
    """
    if value is None or isinstance(value, discord.Colour):
        return value
    # bool is an int subclass, so True would otherwise become Colour(1).
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value <= 0xFFFFFF:
            raise ValueError(
                f"{owner} {param} must be between 0x000000 and 0xFFFFFF; got {value:#x}"
            )
        return discord.Colour(value)
    raise TypeError(
        f"{owner} {param} must be a discord.Colour or an int; "
        f"got {type(value).__name__}: {value!r}"
    )
