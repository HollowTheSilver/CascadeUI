# // ========================================( Modules )======================================== // #


from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

import discord
from discord import SelectOption

from ..utils.coercion import coerce_snowflake_id
from .base import StatefulComponent, StatefulSelect

# // ========================================( Default Value Helpers )======================================== // #


def _wrap_default_values(
    values: Optional[Iterable[Any]], default_type: str
) -> List[discord.SelectDefaultValue]:
    """Coerce a sequence of ints/snowflakes/SelectDefaultValue into the discord.py shape.

    The four specialized selects (RoleSelect, UserSelect, ChannelSelect)
    each carry a single default-value type ("role", "user", "channel").
    This helper accepts the permissive input shape -- raw ``int`` IDs,
    objects with ``.id`` attributes (any ``discord.abc.Snowflake``), or
    pre-built :class:`discord.SelectDefaultValue` instances -- and
    returns the typed list discord.py's ``default_values=`` parameter
    expects.

    Empty input returns an empty list. Raises ``TypeError`` for inputs
    that are neither int-like nor a ``SelectDefaultValue`` (matches
    :func:`coerce_snowflake_id`'s contract).
    """
    if not values:
        return []
    out: List[discord.SelectDefaultValue] = []
    for value in values:
        if isinstance(value, discord.SelectDefaultValue):
            out.append(value)
            continue
        snowflake_id = coerce_snowflake_id(value)
        # SelectDefaultValue stores type verbatim; a bare string breaks to_dict().
        out.append(
            discord.SelectDefaultValue(
                id=snowflake_id, type=discord.SelectDefaultValueType[default_type]
            )
        )
    return out


def _wrap_mentionable_defaults(
    values: Optional[Iterable[Any]],
) -> List[discord.SelectDefaultValue]:
    """Resolve MentionableSelect defaults via type inference.

    MentionableSelect accepts both users and roles, so the type cannot
    be inferred from a raw ``int`` ID. This helper accepts:

    - :class:`discord.Member` / :class:`discord.User` -> ``type="user"``
    - :class:`discord.Role` -> ``type="role"``
    - Pre-built :class:`discord.SelectDefaultValue` (passed through)

    Raw integers are rejected because the type cannot be determined;
    callers with bare IDs construct ``SelectDefaultValue`` explicitly.
    """
    if not values:
        return []
    out: List[discord.SelectDefaultValue] = []
    for value in values:
        if isinstance(value, discord.SelectDefaultValue):
            out.append(value)
        elif isinstance(value, discord.Role):
            out.append(
                discord.SelectDefaultValue(id=value.id, type=discord.SelectDefaultValueType.role)
            )
        elif isinstance(value, (discord.Member, discord.User)):
            out.append(
                discord.SelectDefaultValue(id=value.id, type=discord.SelectDefaultValueType.user)
            )
        else:
            raise TypeError(
                f"MentionableSelect default values must be Member, User, Role, "
                f"or SelectDefaultValue (got {type(value).__name__}: {value!r}). "
                f"Raw int IDs cannot be auto-typed; construct "
                f"discord.SelectDefaultValue(id=..., "
                f"type=discord.SelectDefaultValueType.user or .role) "
                f"explicitly for those."
            )
    return out


# // ========================================( Classes )======================================== // #


class Dropdown(StatefulSelect):
    """A dropdown select menu with state management."""

    def __init__(
        self,
        options: List[Union[SelectOption, Dict[str, Any]]],
        placeholder: Optional[str] = None,
        callback: Optional[Callable] = None,
        **kwargs,
    ):
        # Dicts are converted; SelectOptions pass through. Anything else is
        # rejected here rather than appended untouched: a plain string became
        # an option with no label and no value, and a single option dict
        # passed without its list iterated as its own keys, both surfacing
        # only when Discord tried to render the select.
        if hasattr(options, "items"):
            raise TypeError(
                "Dropdown options must be a list of option dicts, not a single "
                "mapping. Iterating a mapping yields its keys.\n"
                "  Fix: wrap it in a list, e.g. options=[{'label': ..., 'value': ...}]."
            )
        processed_options = []
        for index, opt in enumerate(options):
            if isinstance(opt, dict):
                processed_options.append(
                    SelectOption(
                        label=opt.get("label", "Option"),
                        value=opt.get("value", opt.get("label", "Option")),
                        description=opt.get("description"),
                        emoji=opt.get("emoji"),
                        default=opt.get("default", False),
                    )
                )
            elif isinstance(opt, SelectOption):
                processed_options.append(opt)
            else:
                raise TypeError(
                    f"Dropdown options[{index}] must be a dict or a SelectOption, "
                    f"got {type(opt).__name__}: {opt!r}\n"
                    f"  Fix: pass {{'label': ..., 'value': ...}} per option."
                )

        super().__init__(
            options=processed_options, placeholder=placeholder, callback=callback, **kwargs
        )


class RoleSelect(discord.ui.RoleSelect, StatefulComponent):
    """A role select menu with state management.

    Accepts a permissive ``default_values=`` kwarg: pass raw ``int``
    role IDs, ``discord.Role`` objects, or pre-built
    :class:`discord.SelectDefaultValue` instances. CascadeUI coerces
    each entry to the discord.py shape, wrapping bare IDs/Snowflakes
    with ``type="role"`` automatically. Use :meth:`set_default_values`
    to update defaults after construction.
    """

    _DEFAULT_VALUE_TYPE = "role"

    def __init__(
        self,
        placeholder: Optional[str] = None,
        callback: Optional[Callable] = None,
        default_values: Optional[Sequence[Any]] = None,
        **kwargs,
    ):
        if default_values is not None:
            kwargs["default_values"] = _wrap_default_values(
                default_values, self._DEFAULT_VALUE_TYPE
            )
        super().__init__(placeholder=placeholder, **kwargs)

        self.original_callback = callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)

    def set_default_values(self, values: Optional[Sequence[Any]]) -> None:
        """Replace the default-value list with *values*.

        Accepts the same permissive input shape as the constructor:
        raw ``int`` role IDs, ``discord.Role`` objects, or pre-built
        ``discord.SelectDefaultValue`` instances. ``None`` or an empty
        sequence clears the defaults.
        """
        self.default_values = _wrap_default_values(values, self._DEFAULT_VALUE_TYPE)


class ChannelSelect(discord.ui.ChannelSelect, StatefulComponent):
    """A channel select menu with state management.

    Accepts a permissive ``default_values=`` kwarg: pass raw ``int``
    channel IDs, ``discord.GuildChannel`` / ``discord.abc.GuildChannel``
    objects, or pre-built :class:`discord.SelectDefaultValue` instances.
    CascadeUI coerces each entry to the discord.py shape, wrapping
    bare IDs/Snowflakes with ``type="channel"`` automatically. Use
    :meth:`set_default_values` to update defaults after construction.

    **The callback receives partial channels, not full ones.** Where
    :class:`RoleSelect` and :class:`UserSelect` hand back complete
    ``Role`` and ``Member`` objects, this select yields discord.py's
    ``AppCommandChannel`` / ``AppCommandThread`` -- ``__slots__`` partials
    carrying only what the interaction payload resolved. They have no
    ``permissions_for()``, so a bot-permission check written against a
    full channel raises ``AttributeError`` on the first pick.

    ``.permissions`` is present but answers a different question: it is
    the *invoking user's* permissions in that channel, not the bot's. An
    admin checking it for send access gets their own allow, which is the
    case a bot-permission guard exists to refuse. Call ``.resolve()`` for
    the cached full channel, or ``await .fetch()`` when the cache may
    miss, then check permissions on the result::

        async def on_pick(interaction, values):
            channel = values[0].resolve() or await values[0].fetch()
            if not channel.permissions_for(interaction.guild.me).send_messages:
                ...
    """

    _DEFAULT_VALUE_TYPE = "channel"

    def __init__(
        self,
        placeholder: Optional[str] = None,
        callback: Optional[Callable] = None,
        default_values: Optional[Sequence[Any]] = None,
        **kwargs,
    ):
        if default_values is not None:
            kwargs["default_values"] = _wrap_default_values(
                default_values, self._DEFAULT_VALUE_TYPE
            )
        super().__init__(placeholder=placeholder, **kwargs)

        self.original_callback = callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)

    def set_default_values(self, values: Optional[Sequence[Any]]) -> None:
        """Replace the default-value list with *values*.

        Accepts the same permissive input shape as the constructor.
        """
        self.default_values = _wrap_default_values(values, self._DEFAULT_VALUE_TYPE)


class UserSelect(discord.ui.UserSelect, StatefulComponent):
    """A user select menu with state management.

    Accepts a permissive ``default_values=`` kwarg: pass raw ``int``
    user IDs, ``discord.Member`` / ``discord.User`` objects, or
    pre-built :class:`discord.SelectDefaultValue` instances.
    CascadeUI coerces each entry to the discord.py shape, wrapping
    bare IDs/Snowflakes with ``type="user"`` automatically. Use
    :meth:`set_default_values` to update defaults after construction.
    """

    _DEFAULT_VALUE_TYPE = "user"

    def __init__(
        self,
        placeholder: Optional[str] = None,
        callback: Optional[Callable] = None,
        default_values: Optional[Sequence[Any]] = None,
        **kwargs,
    ):
        if default_values is not None:
            kwargs["default_values"] = _wrap_default_values(
                default_values, self._DEFAULT_VALUE_TYPE
            )
        super().__init__(placeholder=placeholder, **kwargs)

        self.original_callback = callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)

    def set_default_values(self, values: Optional[Sequence[Any]]) -> None:
        """Replace the default-value list with *values*.

        Accepts the same permissive input shape as the constructor.
        """
        self.default_values = _wrap_default_values(values, self._DEFAULT_VALUE_TYPE)


class MentionableSelect(discord.ui.MentionableSelect, StatefulComponent):
    """A mentionable select menu with state management.

    Accepts both users and roles, so default values must carry their
    own type. The permissive ``default_values=`` kwarg accepts:

    - :class:`discord.Member` / :class:`discord.User` (auto-typed
      as ``"user"``)
    - :class:`discord.Role` (auto-typed as ``"role"``)
    - Pre-built :class:`discord.SelectDefaultValue` (passed through)

    Raw ``int`` IDs are rejected because the type cannot be inferred;
    callers with bare IDs construct ``SelectDefaultValue`` explicitly
    with ``type="user"`` or ``type="role"``.
    """

    def __init__(
        self,
        placeholder: Optional[str] = None,
        callback: Optional[Callable] = None,
        default_values: Optional[Sequence[Any]] = None,
        **kwargs,
    ):
        if default_values is not None:
            kwargs["default_values"] = _wrap_mentionable_defaults(default_values)
        super().__init__(placeholder=placeholder, **kwargs)

        self.original_callback = callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)

    def set_default_values(self, values: Optional[Sequence[Any]]) -> None:
        """Replace the default-value list with *values*.

        Accepts the same permissive input shape as the constructor.
        Type is inferred from the object class (``Member``/``User`` ->
        ``"user"``, ``Role`` -> ``"role"``); raw ``int`` IDs are
        rejected.
        """
        self.default_values = _wrap_mentionable_defaults(values)
