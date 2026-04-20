"""Exception types raised by CascadeUI at runtime.

Public API. Canonical import paths::

    from cascadeui import InstanceLimitError
    from cascadeui import PersistenceError, PersistenceInitError

Runtime conditions that callers branch on programmatically live here.
Input-validation errors raise stdlib ``TypeError`` / ``ValueError`` and
are not part of this module.
"""

# // ========================================( View Exceptions )======================================== // #


class InstanceLimitError(Exception):
    """Raised when an instance limit blocks a view from opening.

    Fires from ``send()`` under ``instance_policy="reject"`` and from
    ``register_participant()`` when the joining user already occupies a
    slot in another view of the same type.

    The :attr:`default_message` property holds a pre-formatted rejection
    string that omits internal class names::

        try:
            await view.send()
        except InstanceLimitError as e:
            await ctx.send(e.default_message, ephemeral=True)
    """

    def __init__(self, view_type: str, limit: int, blocked_user_id: int = None):
        self.view_type = view_type
        self.limit = limit
        self.blocked_user_id = blocked_user_id
        super().__init__(f"Session limit ({limit}) reached for {view_type}.")

    @property
    def default_message(self) -> str:
        """Pre-formatted rejection string safe to send in an interaction reply."""
        if self.limit == 1:
            return "You already have one of these open. Close it first."
        return f"You can only have {self.limit} of these open at once."


# // ========================================( Persistence Exceptions )======================================== // #


class PersistenceError(Exception):
    """Base class for all persistence-layer runtime errors.

    Catch this to handle any persistence failure without needing to know
    the specific subtype. Subclasses communicate the failure phase so
    recovery code can branch on it.
    """


class PersistenceConfigError(PersistenceError):
    """Raised when persistence configuration is invalid at setup time.

    Fires when a namespace config requires a capability the chosen
    backend does not declare (e.g. ``ScopedPersistence(ttl_days=30)``
    with a backend missing ``Capability.TTL_INDEX``) or when
    incompatible options are combined.
    """


class PersistenceInitError(PersistenceError):
    """Raised when a backend fails to initialize during setup.

    Wraps connection failures, table-creation errors, and permission
    problems surfaced by ``backend.initialize()``. Prevents the bot
    from starting against an unhealthy persistence layer.
    """


class PersistenceSchemaError(PersistenceError):
    """Raised when on-disk schema is incompatible with the library.

    Fires in two situations: a migrator fails mid-run (leaving the
    schema partially upgraded), or the on-disk schema version is
    higher than the library's current version (the database was
    written by a newer CascadeUI release).
    """


class PersistenceRehydrateError(PersistenceError):
    """Raised when rehydrate cannot restore state from the backend.

    Fires when a persisted JSON blob is corrupted, a required row is
    malformed, or the backend returns unexpected shape during the
    rehydrate phase of ``PersistenceMiddleware.initialize``. Per-view
    re-attachment failures do NOT raise this; they are logged and skipped.
    """
