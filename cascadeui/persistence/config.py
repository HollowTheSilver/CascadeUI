"""Namespace configuration objects for the persistence layer.

Two namespace configs declare which backend serves each logical area
and what capabilities the backend must provide:

- :class:`RegistryPersistence` -- ``PersistentView`` re-attachment rows.
- :class:`ApplicationPersistence` -- user reducer slots and their policies.

Each config validates its required capability set against the supplied
backend in ``__post_init__``. Mismatches raise
:class:`~cascadeui.exceptions.PersistenceConfigError` at
construction time rather than silently deferring to a runtime crash.

:class:`SlotPolicy` carries per-slot policy for application slots: TTL
and opt-in persistence. Slots default to ``persistent=False`` so the
store is ephemeral by default; users opt in per slot. TTL slots pair
with :class:`~cascadeui.persistence.protocols.Capability.TTL_INDEX` at
the backend.
"""

# // ========================================( Modules )======================================== // #


from dataclasses import dataclass, field
from typing import ClassVar, Optional

from ..exceptions import PersistenceConfigError
from .protocols import Capability, PersistenceBackend

# // ========================================( Namespace Names )======================================== // #


# Logical namespace names. Match the table names in schema.py so the
# manager can pass one string through to backend row/kv operations.
NAMESPACE_REGISTRY: str = "persistent_views"
NAMESPACE_APPLICATION: str = "application_slots"


# // ========================================( Slot Policy )======================================== // #


@dataclass(frozen=True)
class SlotPolicy:
    """Per-slot policy for application slots.

    Attributes
    ----------
    ttl_days
        Rows whose ``expires_at`` is older than ``now - ttl_days`` are
        pruned on auto-prune cycles. ``None`` means no TTL (slot persists
        indefinitely).
    persistent
        When ``True``, the slot is written through to the backend. When
        ``False`` (the default), the slot is in-memory only and skipped
        by :class:`PersistenceMiddleware`. Opt-in per slot mirrors
        :class:`~cascadeui.views.persistent.PersistentView`'s explicit
        subclass opt-in for views.

    ``persistent=False`` and ``ttl_days=N`` are mutually exclusive --
    in-memory slots never reach storage, so a TTL has nothing to prune.
    """

    ttl_days: Optional[int] = None
    persistent: bool = False

    def __post_init__(self) -> None:
        if self.ttl_days is not None:
            if not isinstance(self.ttl_days, int) or isinstance(self.ttl_days, bool):
                raise TypeError(
                    f"SlotPolicy.ttl_days must be int or None, "
                    f"got {type(self.ttl_days).__name__}"
                )
            if self.ttl_days <= 0:
                raise ValueError(
                    f"SlotPolicy.ttl_days must be positive, got {self.ttl_days}"
                )
        if not isinstance(self.persistent, bool):
            raise TypeError(
                f"SlotPolicy.persistent must be bool, "
                f"got {type(self.persistent).__name__}"
            )
        if not self.persistent and self.ttl_days is not None:
            raise ValueError(
                "SlotPolicy cannot set ttl_days without persistent=True -- "
                "in-memory slots never reach the backend."
            )


# // ========================================( Capability Validator )======================================== // #


def _validate_capabilities(
    config_name: str,
    required: Capability,
    backend: Optional[PersistenceBackend],
) -> None:
    """Raise :class:`PersistenceConfigError` if ``backend`` is missing
    any capability in ``required``. ``backend=None`` is a no-op (the
    namespace is opted out; see config docstrings)."""
    if backend is None:
        return
    declared = getattr(backend, "capabilities", Capability(0))
    missing = required & ~declared
    if missing:
        raise PersistenceConfigError(
            f"{config_name} requires {required!r}, but backend "
            f"{type(backend).__name__} declares only {declared!r}. "
            f"Missing: {missing!r}."
        )


# // ========================================( Registry Config )======================================== // #


@dataclass
class RegistryPersistence:
    """Configuration for the ``persistent_views`` registry namespace.

    Holds ``PersistentView`` re-attachment rows so views survive a bot
    restart. Relational by nature (one row per ``persistence_key``) and has
    no TTL -- rows live until the view unregisters or the user prunes
    them explicitly.

    Attributes
    ----------
    backend
        Backend serving this namespace. ``None`` opts the registry out
        of persistence; ``PersistentView`` classes still work in memory
        but will not survive a restart.
    """

    backend: Optional[PersistenceBackend]

    _logical_name: ClassVar[str] = NAMESPACE_REGISTRY
    _required_capabilities: ClassVar[Capability] = (
        Capability.RELATIONAL | Capability.SCHEMA_META
    )

    def __post_init__(self) -> None:
        _validate_capabilities(
            type(self).__name__, self._required_capabilities, self.backend
        )


# // ========================================( Application Config )======================================== // #


@dataclass
class ApplicationPersistence:
    """Configuration for the ``application_slots`` namespace.

    Application slots hold user reducer state. Each slot carries its
    own policy via :class:`SlotPolicy` -- ``persistent=True`` opts the
    slot in, ``ttl_days=N`` adds TTL pruning. Slots without an explicit
    policy use :class:`SlotPolicy` defaults (in-memory, no TTL).

    When any slot declares ``ttl_days``, :class:`PersistenceManager`
    starts a daily background sweeper at ``install_middleware()`` time
    that deletes rows whose ``expires_at`` has passed. No cadence
    configuration is exposed: TTLs are expressed in days, sub-day
    granularity is meaningless, and asking the user to also schedule a
    prune task is friction the library can absorb. Explicit prune
    (:meth:`PersistenceManager.prune_application`) remains available
    for devtools and one-off ops use.

    Attributes
    ----------
    backend
        Backend serving this namespace. ``None`` opts application slots
        out of persistence entirely.
    slots
        Mapping of slot name to :class:`SlotPolicy`. The library also
        supports runtime registration via ``manager.register_slot_policy``;
        this mapping is the static equivalent.
    """

    backend: Optional[PersistenceBackend]
    slots: dict[str, SlotPolicy] = field(default_factory=dict)

    _logical_name: ClassVar[str] = NAMESPACE_APPLICATION

    def __post_init__(self) -> None:
        # Validate slots mapping shape first so error messages point at
        # the user-authored config, not a downstream capability mismatch.
        if not isinstance(self.slots, dict):
            raise TypeError(
                f"ApplicationPersistence.slots must be a dict, "
                f"got {type(self.slots).__name__}"
            )
        for name, policy in self.slots.items():
            if not isinstance(name, str):
                raise TypeError(
                    f"ApplicationPersistence.slots keys must be str, "
                    f"got {type(name).__name__}"
                )
            if not isinstance(policy, SlotPolicy):
                raise TypeError(
                    f"ApplicationPersistence.slots[{name!r}] must be a "
                    f"SlotPolicy, got {type(policy).__name__}"
                )

        required = Capability.RELATIONAL | Capability.SCHEMA_META
        # TTL_INDEX is required only when at least one persistent slot
        # declares ttl_days. In-memory slots never reach the backend so
        # they impose no capability burden.
        if any(
            p.ttl_days is not None and p.persistent for p in self.slots.values()
        ):
            required |= Capability.TTL_INDEX

        _validate_capabilities(type(self).__name__, required, self.backend)
        # Record the effective required set for introspection + tests.
        object.__setattr__(self, "_required_capabilities", required)
