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

CascadeUI state is ephemeral by default. A slot reaches the backend only
once it is declared persistent; :func:`is_persistent_slot` names the
routes that do so. Everything else stays in memory and is skipped by
:class:`PersistenceMiddleware`.
"""

# // ========================================( Modules )======================================== // #


import logging
from typing import Any, Callable, Optional, Set

logger = logging.getLogger(__name__)

# // ========================================( Persistent registry )======================================== // #


# Slots declared persistent are written through to the backend by
# ``PersistenceMiddleware``; everything else is skipped. The routes that
# add a name here are listed on ``is_persistent_slot`` below, so the list
# lives in one place rather than in every comment that mentions it. The
# registry is module-level so the marker is sticky across calls: once a
# slot is declared persistent, every future write to that slot name
# inherits the contract.
_PERSISTENT_SLOTS: Set[str] = set()


def is_persistent_slot(name: str) -> bool:
    """Return True when ``name`` was declared persistent.

    All three declaration paths land here: ``access_slot(..., persistent=True)``,
    the ``persistent_slots`` class attribute on a view, and a
    ``SlotPolicy(persistent=True)`` declared in ``ApplicationPersistence.slots``
    or registered through ``PersistenceManager.register_slot_policy``.
    """
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
        if not callable(key):
            raise TypeError(
                f"slot_property({name!r}) key= must be a callable taking the "
                f"instance and returning the lookup key, e.g. "
                f"lambda self: self.user_id; got {type(key).__name__}: {key!r}"
            )
        self._field = name
        self._slot = slot
        self._key = key
        self._default = default
        self._attr_name = name  # overridden by __set_name__ when used in a class body
        # Owner classes whose key= has already been reported as raising. A
        # descriptor is read on every attribute access, so reporting per
        # read buries the one line that matters under thousands of copies
        # and teaches an operator to filter the logger out. The failure
        # belongs to the declaration, not to any one read. Keyed by owner
        # rather than a bare flag because one descriptor is shared by every
        # subclass that inherits it, and the attribute the key reaches for
        # may exist on some of them.
        self._key_failed: Set[str] = set()

    def __set_name__(self, owner, name):
        self._attr_name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        # Split the two reasons a read can come up empty. A store that is
        # not wired yet is the ordinary case and stays silent. A key
        # callable that raises is a mistake in the declaration, and folding
        # it into the same swallow turned a typo into a default that looks
        # correct in every test.
        state = getattr(getattr(instance, "state_store", None), "state", None)
        if not isinstance(state, dict):
            return self._default
        try:
            key = self._key(instance)
        except Exception as e:
            owner_name = type(instance).__name__
            if owner_name not in self._key_failed:
                self._key_failed.add(owner_name)
                logger.warning(
                    f"slot_property {owner_name}.{self._attr_name} key= "
                    f"raised {type(e).__name__}: {e}. Reading {self._default!r} "
                    f"until it stops raising."
                )
            return self._default
        return read_slot(state, self._slot, key, self._field, default=self._default)

    def __repr__(self) -> str:
        return (
            f"slot_property(name={self._field!r}, slot={self._slot!r}, "
            f"default={self._default!r})"
        )
