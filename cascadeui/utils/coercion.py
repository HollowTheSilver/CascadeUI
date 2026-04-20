# // ========================================( Modules )======================================== // #


from typing import Iterable, Optional, Set

# // ========================================( Functions )======================================== // #


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
