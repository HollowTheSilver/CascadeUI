"""Slot helpers and the slot_property descriptor.

Three tools cover slot ownership across the read/write split:

``access_slot(state, name, key)`` is the write/init helper. It walks
``state["application"][name][key]``, auto-vivifying with a default
factory along the way. Use it inside reducers and inside
``seed_initial_state`` where the snapshot is deep-copied or funneled
through a batch, so in-place mutation is safe.

``read_slot(state, name, *path, default=None)`` is the pure-read
counterpart. Walks an arbitrary-depth path without seeding, mutating,
or registering persistence. Use it inside ``state_selector`` methods
and ``@computed`` selectors, where the library passes the live store
state by reference -- an ``access_slot`` call there would mutate
authoritative state as a side effect of reading. Variadic beyond the
slot name, so ``read_slot(state, "stats", guild_id, user_id, "combat",
"wins")`` reads a five-level path with graceful fallback.

``slot_property(name, slot=..., key=..., default=...)`` is the read
descriptor for single-field reads on a view class. Returns the named
field from the keyed slot at attribute-access time, with a graceful
default for missing keys. Use it to replace 5-line ``@property``
accessors that all do the same
``state.get("application", {}).get("X", {}).get(self.user_id, {}).get("Y", default)``
walk. For paths deeper than the canonical three levels, declare a
plain ``@property`` and call ``read_slot`` inside it.

CascadeUI state is ephemeral by default. Opt a slot in to persistence
by passing ``persistent=True`` to :func:`access_slot`; everything else
stays in memory and is skipped by :class:`PersistenceMiddleware`.
"""

# // ========================================( Modules )======================================== // #


from typing import Any, Callable, Optional, Set


# // ========================================( Persistent registry )======================================== // #


# Slots marked ``persistent=True`` via ``access_slot`` are written
# through to the backend by ``PersistenceMiddleware``. Everything else
# is skipped. The registry is module-level so the marker is sticky
# across calls: once a slot is declared persistent, every future write
# to that slot name inherits the contract.
_PERSISTENT_SLOTS: Set[str] = set()


def is_persistent_slot(name: str) -> bool:
    """Return True when ``name`` was declared persistent via ``access_slot``."""
    return name in _PERSISTENT_SLOTS


# // ========================================( Functions )======================================== // #


def access_slot(
    state: dict,
    name: str,
    key: Optional[Any] = None,
    *,
    default_factory: Optional[Callable[[], Any]] = None,
    persistent: bool = False,
) -> Any:
    """Return ``state["application"][name][key]``, auto-vivifying as it walks.

    When ``key`` is ``None``, returns the slot dict at
    ``state["application"][name]`` (creating it if missing). When ``key``
    is supplied, returns the value stored at that key, calling
    ``default_factory()`` to seed it on first access. ``default_factory``
    defaults to ``dict``, so omitting it gives an empty dict per key.

    The helper mutates ``state`` in place. Safe to call from reducers
    (which receive a deep-copied state) and from ``seed_initial_state``
    (where the surrounding batch absorbs the change). Calling on the live
    ``store.state`` outside a dispatch bypasses subscriber notification --
    do that only during the seed hook, which the library funnels into the
    send-pipeline batch.

    Setting ``persistent=True`` registers the slot name as write-through:
    :class:`PersistenceMiddleware` writes the slot to the backend on
    every change. The registration is sticky -- any subsequent call with
    the same ``name`` inherits persistence without needing to re-pass
    the kwarg. Declare it once (typically inside ``seed_initial_state``)
    and writes from reducers, helpers, or elsewhere flow to disk.

    Args:
        state: The state dict to walk. Either a reducer's snapshot or
            ``store.state`` from inside ``seed_initial_state``.
        name: The slot name under ``state["application"]``. One slot per
            feature (e.g. ``"battleship"``, ``"settings"``).
        key: Optional sub-key. When supplied, the return value is the
            value stored at ``state["application"][name][key]``.
        default_factory: Zero-arg callable invoked to seed a missing key.
            Defaults to ``dict``.
        persistent: When True, mark the slot write-through.
            ``PersistenceMiddleware`` persists every change to the backend.

    Returns:
        The slot dict (when ``key`` is ``None``) or the keyed value.
    """
    if persistent:
        _PERSISTENT_SLOTS.add(name)
    app = state.setdefault("application", {})
    slot = app.setdefault(name, {})
    if key is None:
        return slot
    if key not in slot:
        slot[key] = default_factory() if default_factory is not None else {}
    return slot[key]


def read_slot(
    state: dict,
    name: str,
    *path: Any,
    default: Any = None,
) -> Any:
    """Return ``state["application"][name][*path]`` without mutating state.

    Selector-safe counterpart to :func:`access_slot`. Walks the slot path
    via ``dict.get`` chains, never creating intermediate nodes and never
    touching ``_PERSISTENT_SLOTS``. Intended for ``state_selector`` methods
    and ``@computed`` selectors, which receive the live store state by
    reference -- any mutation there would corrupt subscriber-diff snapshots.

    When ``path`` is empty, returns the slot dict (or ``{}`` when absent).
    When ``path`` is supplied, walks each segment in order, returning
    ``default`` the moment any intermediate is missing or non-dict.

    Examples::

        read_slot(state, "visits")                   # slot dict
        read_slot(state, "visits", user_id)          # keyed sub
        read_slot(state, "visits", user_id, "count") # field
        read_slot(state, "stats", guild_id, user_id, "combat", "wins")

    Args:
        state: The state dict to read. Safe to pass ``store.state`` directly
            or a selector's ``state`` argument.
        name: The slot name under ``state["application"]``.
        *path: Zero or more segments to walk beneath the slot.
        default: Value returned when ``path`` is supplied but any
            intermediate step is missing or non-dict.

    Returns:
        The slot dict (when ``path`` is empty), the value at the walked
        path, or ``default`` when any intermediate is absent.
    """
    node = state.get("application", {}).get(name, {})
    if not path:
        return node
    sentinel = object()
    for segment in path:
        if not isinstance(node, dict):
            return default
        next_node = node.get(segment, sentinel)
        if next_node is sentinel:
            return default
        node = next_node
    return node


# // ========================================( Class )======================================== // #


class slot_property:
    """Read a field from an application slot, with a default for missing keys.

    Declared on a class as ``slot_property(name, slot=..., key=..., default=...)``.
    On attribute access, returns ``state["application"][slot][key(self)][name]``
    with ``default`` as the fallback for any missing intermediate.

    The descriptor reads from ``self.state_store.state`` and never mutates.
    Pair with :func:`access_slot` (the write/init helper) for a complete
    slot ownership story: one helper for seeding and reducer writes, one
    descriptor per readable field.

    Example::

        class BattleshipView(StatefulLayoutView):
            phase = slot_property("phase", slot="battleship",
                                  key=lambda self: self.user_id, default="setup")
            board = slot_property("board", slot="battleship",
                                  key=lambda self: self.user_id, default=None)

    The descriptor covers the canonical three-level shape
    (``application[slot][keyed][field]``). Deeper paths do not extend
    cleanly through the descriptor grammar; declare a plain ``@property``
    and call :func:`read_slot` inside it instead::

        class StatsView(StatefulLayoutView):
            @property
            def combat_wins(self):
                return read_slot(
                    self.state_store.state,
                    "stats", self.guild_id, self.user_id, "combat", "wins",
                    default=0,
                )

    The descriptor swallows ``KeyError``, ``TypeError``, and
    ``AttributeError`` along the lookup chain and returns ``default``. This
    matches the "graceful read" contract of ``dict.get`` and lets views
    safely declare slot reads before the slot has been seeded.
    """

    def __init__(
        self,
        name: str,
        *,
        slot: str,
        key: Callable[[Any], Any],
        default: Any = None,
    ):
        self._field = name
        self._slot = slot
        self._key = key
        self._default = default
        self._attr_name = name  # overridden by __set_name__ when used in a class body

    def __set_name__(self, owner, name):
        self._attr_name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        try:
            return read_slot(
                instance.state_store.state,
                self._slot,
                self._key(instance),
                self._field,
                default=self._default,
            )
        except (KeyError, TypeError, AttributeError):
            return self._default

    def __repr__(self) -> str:
        return (
            f"slot_property(name={self._field!r}, slot={self._slot!r}, "
            f"default={self._default!r})"
        )
