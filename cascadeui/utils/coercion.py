# // ========================================( Modules )======================================== // #


import time
from typing import Any, Dict, FrozenSet, Iterable, Optional, Set

from discord.utils import DISCORD_EPOCH

# // ========================================( Constants )======================================== // #


# A snowflake carries its creation time in bits 63-22, counted from Discord's
# epoch. An id whose timestamp bits decode to the epoch day itself carries no
# timestamp at all -- those bits are effectively zero, which is what an
# application's own counter looks like. Discord did not exist on that day, so
# the floor rejects every such id without excluding any real one: the earliest
# real snowflakes on record are four orders of magnitude above it.
_SNOWFLAKE_FLOOR_MS = DISCORD_EPOCH + 86_400_000

# Tolerance for clock skew between this host and Discord's id generators.
_SNOWFLAKE_SKEW_MS = 86_400_000


# // ========================================( Functions )======================================== // #


def is_snowflake(value: Any) -> bool:
    """Return whether *value* is shaped like a real Discord snowflake ID.

    Decodes the creation timestamp Discord packs into bits 63-22 and reports
    whether it lands in the window where real IDs exist: after Discord's epoch
    day and no later than now. An application's own identifier (a database row
    ID, a match number, a Unix timestamp) leaves those bits near zero and
    decodes to the epoch day, so it fails.

    The check is a heuristic on shape, not a lookup. It answers "could this
    integer be a Discord ID at all", never "does this user exist". An
    application ID large enough to land inside the window passes, and nothing
    short of asking Discord could say otherwise.

    Args:
        value: Any value. Non-integers (and ``bool``) return ``False``.

    Returns:
        ``True`` if *value* could be a Discord snowflake, ``False`` otherwise.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    if value < 0 or value.bit_length() > 64:
        return False
    created_ms = (value >> 22) + DISCORD_EPOCH
    return _SNOWFLAKE_FLOOR_MS <= created_ms <= (time.time() * 1000) + _SNOWFLAKE_SKEW_MS


def coerce_snowflake_id(value) -> Optional[int]:
    """Coerce a value to a Discord snowflake ID (``int``).

    Accepts ``None``, an ``int``, or any object with an ``.id`` attribute
    holding an ``int`` (matching ``discord.abc.Snowflake``: Member, User,
    Object, Guild, Channel, Role, Message, etc.).

    This is the canonical input boundary for any CascadeUI parameter that
    nominally expects a user, guild, or channel ID. Coercing silently for
    valid Snowflake-shaped objects matches discord.py's own duck-typed
    conventions and prevents the most common user mistake -- passing
    ``ctx.author`` where ``ctx.author.id`` is expected.

    Args:
        value: ``None``, an ``int``, or a ``discord.abc.Snowflake``-shaped
            object.

    Returns:
        ``None`` if *value* is ``None``, otherwise the coerced ``int``.

    Raises:
        TypeError: If *value* is neither ``None``, an ``int``, nor an object
            with an ``int`` ``.id`` attribute.
    """
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    snowflake_id = getattr(value, "id", None)
    if isinstance(snowflake_id, int) and not isinstance(snowflake_id, bool):
        return snowflake_id
    raise TypeError(
        f"Expected int or object with .id: int attribute (Snowflake), "
        f"got {type(value).__name__}: {value!r}"
    )


def coerce_snowflake_id_set(values: Optional[Iterable]) -> Set[int]:
    """Coerce an iterable of snowflake-shaped values into a ``set[int]``.

    Each element is passed through :func:`coerce_snowflake_id`. ``None``
    elements are not permitted (a ``set`` of user IDs containing ``None``
    is always a bug). An empty or ``None`` *values* argument returns an
    empty set.

    Args:
        values: An iterable of ``int`` or ``Snowflake``-shaped objects, or
            ``None``.

    Returns:
        A ``set[int]`` of coerced IDs.

    Raises:
        TypeError: If any element is not coercible to ``int``, or if any
            element is ``None``.
    """
    if not values:
        return set()
    result: Set[int] = set()
    for v in values:
        if v is None:
            raise TypeError("None is not a valid snowflake ID inside a collection")
        result.add(coerce_snowflake_id(v))
    return result


def coerce_snowflake_match(
    match_dict: Dict[str, Any],
    snowflake_keys: FrozenSet[str],
) -> Dict[str, Any]:
    """Coerce named regex capture groups to ``int`` for known snowflake keys.

    Complements :func:`coerce_snowflake_id` / :func:`coerce_snowflake_id_set`
    for the regex-template case: ``DynamicItem`` subclasses receive a
    ``match`` object whose ``.groupdict()`` holds string values (or
    ``None`` for optional groups). This helper converts those strings
    to ``int`` for any key known to hold a Discord snowflake, leaving
    non-snowflake keys unchanged.

    Args:
        match_dict: The raw ``match.groupdict()`` from a regex template
            match.
        snowflake_keys: Frozen set of group names to coerce (e.g.
            ``{"user_id", "role_id"}``).

    Returns:
        A new ``dict`` with snowflake-named keys coerced to ``int``.
        Keys not in *snowflake_keys* pass through unchanged. ``None``
        values survive for optional groups.

    Raises:
        ValueError: If a snowflake key's value is a non-digit string.
            A template written as ``(?P<user_id>[0-9]+)`` cannot trip
            this because the regex guarantees digits; it fires only
            when a subclass names a capture after a snowflake field
            but uses a non-digit pattern (a programmer error).
    """
    out = dict(match_dict)
    for key in snowflake_keys & match_dict.keys():
        raw = match_dict[key]
        if raw is None:
            continue
        out[key] = int(raw)
    return out
