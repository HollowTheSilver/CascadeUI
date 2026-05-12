# // ========================================( Modules )======================================== // #


import asyncio
import functools
import inspect
import logging
import time
import uuid
from datetime import datetime
from typing import Any, ClassVar, Dict, Optional, Set

import discord
from discord import Interaction
from discord.ui import Item, TextDisplay

from ..components.base import StatefulButton
from ..components.types import EmojiInput
from ..exceptions import InstanceLimitError
from ..state.actions import ActionCreators
from ..state.singleton import get_store
from ..state.store import _CURRENT_INTERACTION
from ..utils.coercion import coerce_snowflake_id, coerce_snowflake_id_set
from ..utils.errors import safe_execute, with_error_boundary
from ..utils.tasks import get_task_manager
from ._interaction import _InteractionMixin
from ._navigation import _NavigationMixin

logger = logging.getLogger(__name__)


# // ========================================( View Registry )======================================== // #


# Maps class name -> class for navigation stack resolution
_view_class_registry: Dict[str, type] = {}

# Kwargs that are ephemeral per-invocation and must NOT be saved for
# push/pop reconstruction.  _navigate_to() re-supplies these when
# building the next view, so persisting them would be wrong.
_NON_RECONSTRUCTIBLE_KWARGS = frozenset(
    {
        "context",
        "interaction",
        "message",
        "state_store",
        "session_id",
        "user_id",
        "guild_id",
        "parent",
    }
)


def _register_view_class(cls):
    """Auto-register view classes for nav stack class resolution.

    Keyed by the fully-qualified class path so sibling modules can reuse
    short class names without clobbering each other in the registry.
    """
    _view_class_registry[f"{cls.__module__}.{cls.__qualname__}"] = cls


# // ========================================( Mixin )======================================== // #


class _StatefulMixin(_InteractionMixin, _NavigationMixin):
    """View-agnostic state management shared by StatefulView (V1) and StatefulLayoutView (V2).

    This mixin contains all state integration, navigation, undo/redo, lifecycle,
    and session management logic. Concrete view classes combine it with either
    ``discord.ui.View`` (V1) or ``discord.ui.LayoutView`` (V2) and provide a
    version-specific ``send()`` method.
    """

    # Subclass config: state scoping ("user", "guild", "user_guild", "global", or None).
    # Governs which slice of the Redux store this view reads/writes via
    # ``_get_scoped_state`` and ``dispatch_scoped``. Distinct from
    # ``instance_scope``, which governs session-limit indexing.
    state_scope: Optional[str] = None

    # Subclass config: scoped bucket name. Names the slot inside
    # ``state["application"]`` where this view's ``dispatch_scoped``
    # writes land. Each subsystem owns its own bucket so
    # ``persistent_slots`` can opt one in without coupling to any other
    # subsystem's scoped data. When unset, falls back to the shared
    # ``"scoped"`` bucket so generic views keep working without
    # boilerplate. Only meaningful when ``state_scope`` is set.
    scoped_slot: Optional[str] = None

    # Subclass config: declarative slot-persistence opt-in. Each name
    # listed here is registered with the persistence middleware at class
    # definition time, so every ``access_slot`` write to that slot name
    # (from any reducer or helper) is flushed to disk. Equivalent to
    # calling ``access_slot(state, name, persistent=True)`` once, but
    # without requiring a ``seed_initial_state`` hook. The hook stays
    # available for views that genuinely need to pre-populate state.
    persistent_slots: ClassVar[tuple] = ()

    # Subclass config: enable undo/redo support
    enable_undo: bool = False
    undo_limit: int = 20

    # Subclass config: auto-add a back button when pushed onto nav stack
    auto_back_button: bool = False

    # Subclass config: session limiting
    instance_limit: Optional[int] = None  # None = unlimited
    instance_scope: str = "user_guild"  # "user", "guild", "user_guild", "global"
    instance_policy: str = "replace"  # "replace" or "reject"
    # Optional override for the default ephemeral message sent when a user
    # hits the session limit.  Falsy values fall back to
    # ``InstanceLimitError.default_message`` (singular/plural aware).  For
    # fully custom UX, override the ``on_instance_limit`` method instead.
    instance_limit_message: Optional[str] = None

    # Subclass config: participant capacity (lobby cap).
    # ``participant_limit`` caps the total number of users a single view
    # instance can hold (owner + non-owner participants combined). Distinct
    # from ``instance_limit``, which caps how many *separate* view instances
    # a single user can occupy. ``None`` (default) means unlimited.
    # Common pattern: a Werewolf lobby with ``participant_limit = 10``.
    participant_limit: Optional[int] = None
    # Static rejection message for capacity overflow. For dynamic UX
    # (mention the joiner, log the attempt, etc.) override the
    # ``on_participant_limit`` method instead.
    participant_limit_message: str = "This session is full."

    # Subclass config: auto-register participants from ``allowed_users``.
    # When True and the view has an ``allowed_users`` set, ``send()``
    # iterates the set and calls ``register_participant`` for each
    # non-owner ID before issuing the Discord send. All-or-nothing
    # rollback on the first rejection. False (default) means callers
    # register participants manually (e.g. via a join button).
    auto_register_participants: bool = False

    # Subclass config: participant replacement protection.
    # When True, views with active participants are excluded from
    # replacement candidates during session enforcement. If no
    # replaceable views remain, the session falls back to reject
    # behavior (``on_instance_limit`` fires on the new view). The
    # owner must explicitly exit the current view before starting
    # a new one. Has no effect on views without participants or
    # when ``instance_policy = "reject"``.
    protect_attached: bool = True

    # Subclass config: interaction ownership
    owner_only: bool = True  # Reject interactions from non-owners
    # Ephemeral message sent when a non-allowed user tries to interact.
    # For fully custom UX (logging, embeds, fallback views), override the
    # ``on_unauthorized`` method instead.
    unauthorized_message: str = "You cannot interact with this."
    # Ephemeral error description shown when a callback raises. The default
    # ``on_error`` wraps this in a red embed with title "Something went
    # wrong". For fully custom UX (different embed layout, DM the bot
    # owner, etc.), override the ``on_error`` method instead.
    error_message: str = "An unexpected error occurred while processing your interaction."

    # ``allowed_users`` is exposed via a property pair so assignments are
    # silently coerced at the call site. Users may pass either a set of
    # ``int`` IDs or a set of ``discord.abc.Snowflake``-shaped objects
    # (Member, User, Object); both forms are normalized to ``frozenset[int]``.
    # This catches the most common multi-user-view mistake -- passing
    # ``{ctx.author, opponent}`` instead of ``{ctx.author.id, opponent.id}``
    # -- at the assignment site instead of silently breaking interaction
    # routing later. ``None`` (the default) means "fall back to owner_only".
    @property
    def allowed_users(self) -> Optional[frozenset]:
        return getattr(self, "_allowed_users", None)

    @allowed_users.setter
    def allowed_users(self, value) -> None:
        if value is None:
            self._allowed_users = None
        else:
            self._allowed_users = frozenset(coerce_snowflake_id_set(value))

    @property
    def participants(self) -> frozenset:
        return frozenset(self._participants)

    # Subclass config: auto-defer safety net
    auto_defer: bool = True
    auto_defer_delay: float = 2.5

    # Subclass config: refresh throttling
    # When set to a positive int, enforces a minimum gap (in milliseconds)
    # between successful message edits on this view. Refreshes that land
    # inside the window are deferred via a single scheduled task that
    # re-enters ``on_state_changed`` once the window expires, so the edit
    # reflects the latest store state rather than whatever was current at
    # the deferred call's site. ``None`` (default) disables the proactive
    # cooldown. Independent of the always-on reactive 429 backoff path,
    # which writes to the same ``_refresh_not_before`` timestamp when
    # Discord returns an escalated rate-limit.
    refresh_cooldown_ms: Optional[int] = None

    # Subclass config: interaction serialization
    # When True, rapid button clicks are processed one at a time to prevent
    # racing message edits that cause "This interaction failed" errors.
    serialize_interactions: bool = True

    # Subclass config: session identity coalescing
    # When True, the auto-derived session_id drops the per-instance UUID
    # suffix so repeat opens of the same view class for the same user land
    # on one shared session (undo stack, shared_data, and nav history
    # survive close-and-reopen gestures). Default False treats each send
    # as an isolated session -- the safe polarity for transient lookups,
    # forms, games, wizards, and any flow whose state should not leak
    # across distinct invocations. Navigation chains (push/pop) are
    # unaffected either way: _navigate_to forwards session_id to children
    # explicitly. Pass session_id= at construction for full manual control.
    session_continuity: ClassVar[bool] = False

    # Subclass config: ephemeral refresh
    # Governs whether send() installs the refresh handoff -- a background
    # task that swaps in a "Continue Session" button shortly before the
    # 15-minute webhook token expires. The user clicks it to spawn a
    # fresh ephemeral via a new interaction token, bypassing the cliff.
    #
    # Default ``None`` means "derive from ``timeout`` at send() time":
    # any view with ``timeout=None`` or ``timeout > 900`` engages the
    # handoff (the view wants to outlive the 900s webhook cliff);
    # anything ``<= 900`` skips it (the token outlives the view).
    # Explicit ``True`` or ``False`` overrides the derivation. The
    # declared ``timeout`` is never rewritten -- the flag is the only
    # thing the library decides.
    auto_refresh_ephemeral: Optional[bool] = None
    refresh_warning_seconds: int = 90  # how early to swap before the 900s wall
    refresh_button_label: str = "Continue Session"
    refresh_button_emoji: EmojiInput = "\U0001f504"  # 🔄
    refresh_button_style: discord.ButtonStyle = discord.ButtonStyle.primary
    # Static message sent when the ephemeral refresh button fails to spawn
    # a replacement view (factory raised or returned None). For dynamic UX
    # (logging, custom embeds, etc.), override ``on_reopen_failure`` instead.
    reopen_failure_message: str = (
        "Could not refresh this view. Please reopen from the original command."
    )

    # Governs how an old view is cleaned up when instance_policy="replace"
    # exits it to make room for a new instance. "delete" (default) removes
    # the old message so the new view cleanly supplants it; "disable"
    # freezes the existing components in place, leaving the message in
    # the channel as a static record. The "disable" mode is useful for
    # audit trails or shared-context views where other users may have
    # been looking at the old view. Scoped to the replace transition
    # only -- bare exit() calls and on_timeout are governed by exit_policy.
    replace_policy: str = "delete"
    # Static message sent to the channel when this view is replaced and
    # has active participants. ``None`` (default) means silent replacement.
    # For dynamic UX (mentions, embeds, logging), override the
    # ``on_replaced`` method instead.
    replaced_message: Optional[str] = None

    # Default for bare exit() calls that pass no explicit delete_message
    # argument. "disable" (default) freezes the components in place,
    # matching the historical safe-by-default behavior; "delete" removes
    # the message. Explicit delete_message arguments to exit() always
    # override this policy. This governs on_timeout paths, manual close
    # buttons wired to self.exit(), and any other site that calls exit()
    # without specifying delete_message.
    exit_policy: str = "disable"

    # Persistent view marker -- overridden to True by PersistentView / PersistentLayoutView
    _persistent: bool = False

    # Class-attribute validation tables -- consumed by _validate_class_attributes.
    # Each entry: attribute name → set of accepted values (enum strings).
    _ENUM_ATTRS: ClassVar[dict] = {
        "instance_policy": {"reject", "replace"},
        "instance_scope": {"user", "guild", "user_guild", "global"},
        "replace_policy": {"delete", "disable"},
        "exit_policy": {"delete", "disable"},
        "state_scope": {None, "user", "guild", "user_guild", "global"},
    }
    # Attributes that must be a positive int (or None where noted).
    _POSITIVE_INT_ATTRS: ClassVar[tuple] = (
        "instance_limit",
        "participant_limit",
        "undo_limit",
        "refresh_warning_seconds",
        "refresh_cooldown_ms",
    )
    # Attributes that must be a positive float/int.
    _POSITIVE_NUMBER_ATTRS: ClassVar[tuple] = ("auto_defer_delay",)
    # Attributes that must be a bool.
    _BOOL_ATTRS: ClassVar[tuple] = (
        "owner_only",
        "auto_defer",
        "serialize_interactions",
        "enable_undo",
        "auto_back_button",
        "auto_register_participants",
        "protect_attached",
        "session_continuity",
    )
    # Attributes that must be a bool or None (None = "derive" sentinel).
    _OPTIONAL_BOOL_ATTRS: ClassVar[tuple] = ("auto_refresh_ephemeral",)
    # Attributes that must be a ``discord.ButtonStyle`` enum value. Empty on
    # the mixin; pattern subclasses (Wizard/Tab/Paginated) declare their own
    # button-style attributes here so the validator path is shared.
    _BUTTON_STYLE_ATTRS: ClassVar[tuple] = ("refresh_button_style",)
    # Snowflake-domain instance data -- coerced via the init pipeline,
    # never settable through set_class_attribute (those have their own
    # mutation paths and live as instance state, not class-level policy).
    _INSTANCE_DATA_ATTRS: ClassVar[frozenset] = frozenset(
        {"user_id", "guild_id", "allowed_users", "_participants"}
    )

    @classmethod
    def _validate_attribute_value(cls, name: str, value) -> None:
        """Validate one ``(name, value)`` pair against the lookup tables.

        Single source of truth for class-attribute validation rules.
        Both ``_validate_class_attributes`` (definition-time) and
        ``set_class_attribute`` (per-instance override) dispatch through
        here so the two paths can never drift apart. Names not present
        in any table silently no-op -- free-form attributes like
        ``*_message`` strings have no rule to enforce.
        """
        if name in cls._ENUM_ATTRS:
            allowed = cls._ENUM_ATTRS[name]
            if value not in allowed:
                raise ValueError(
                    f"{cls.__name__}.{name} must be one of "
                    f"{sorted(a for a in allowed if a is not None)!r}, got {value!r}"
                )
            return
        if name in cls._POSITIVE_INT_ATTRS:
            if value is None:
                return
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(
                    f"{cls.__name__}.{name} must be a positive int or None, got {value!r}"
                )
            return
        if name in cls._POSITIVE_NUMBER_ATTRS:
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{cls.__name__}.{name} must be a positive number, got {value!r}")
            return
        if name in cls._BOOL_ATTRS:
            if not isinstance(value, bool):
                raise ValueError(
                    f"{cls.__name__}.{name} must be a bool, got {type(value).__name__}"
                )
            return
        if name in cls._OPTIONAL_BOOL_ATTRS:
            if value is None:
                return
            if not isinstance(value, bool):
                raise ValueError(
                    f"{cls.__name__}.{name} must be a bool or None, " f"got {type(value).__name__}"
                )
            return
        if name in cls._BUTTON_STYLE_ATTRS:
            if not isinstance(value, discord.ButtonStyle):
                raise ValueError(
                    f"{cls.__name__}.{name} must be a discord.ButtonStyle, "
                    f"got {type(value).__name__}"
                )
            return
        if name == "subscribed_actions":
            if value is None:
                return
            if not isinstance(value, (set, frozenset)) or not all(
                isinstance(a, str) for a in value
            ):
                raise ValueError(
                    f"{cls.__name__}.subscribed_actions must be None or a set of strings, "
                    f"got {value!r}"
                )
            return
        if name == "theme":
            if value is None:
                return
            from ..theming.core import Theme

            if not isinstance(value, Theme):
                raise TypeError(
                    f"{cls.__name__}.theme must be a Theme instance or None, "
                    f"got {type(value).__name__}"
                )
            return
        if name == "scoped_slot":
            if value is None:
                return
            if not isinstance(value, str) or not value:
                raise TypeError(
                    f"{cls.__name__}.scoped_slot must be a non-empty string or None, "
                    f"got {value!r}"
                )
            return
        if name == "persistent_slots":
            if not isinstance(value, (list, tuple, set, frozenset)):
                raise TypeError(
                    f"{cls.__name__}.persistent_slots must be a list, tuple, set, "
                    f"or frozenset of slot names, got {type(value).__name__}"
                )
            for entry in value:
                if not isinstance(entry, str):
                    raise TypeError(
                        f"{cls.__name__}.persistent_slots entries must be strings, "
                        f"got {type(entry).__name__}: {entry!r}"
                    )
            return

    @classmethod
    def _validate_class_attributes(cls) -> None:
        """Validate subclass overrides of CascadeUI class attributes.

        Runs in ``__init_subclass__``, so a typo like
        ``instance_policy = "rejct"`` raises ``ValueError`` at *class
        definition time* -- i.e. at module import -- instead of failing
        silently or surfacing as a confusing runtime error deep inside
        the dispatch loop. Only attributes the subclass actually
        overrode (present in ``cls.__dict__``) are checked, so inherited
        defaults pay zero cost.
        """
        own = cls.__dict__
        for attr in cls._ENUM_ATTRS:
            if attr in own:
                cls._validate_attribute_value(attr, own[attr])
        for attr in cls._POSITIVE_INT_ATTRS:
            if attr in own:
                cls._validate_attribute_value(attr, own[attr])
        for attr in cls._POSITIVE_NUMBER_ATTRS:
            if attr in own:
                cls._validate_attribute_value(attr, own[attr])
        for attr in cls._BOOL_ATTRS:
            if attr in own:
                cls._validate_attribute_value(attr, own[attr])
        for attr in cls._OPTIONAL_BOOL_ATTRS:
            if attr in own:
                cls._validate_attribute_value(attr, own[attr])
        for attr in cls._BUTTON_STYLE_ATTRS:
            if attr in own:
                cls._validate_attribute_value(attr, own[attr])
        if "subscribed_actions" in own:
            cls._validate_attribute_value("subscribed_actions", own["subscribed_actions"])
        if "theme" in own:
            cls._validate_attribute_value("theme", own["theme"])
        if "scoped_slot" in own:
            cls._validate_attribute_value("scoped_slot", own["scoped_slot"])
        if "persistent_slots" in own:
            cls._validate_attribute_value("persistent_slots", own["persistent_slots"])
        if "scoped_slot" in own or "persistent_slots" in own:
            cls._validate_slot_coherence()

    @classmethod
    def _validate_slot_coherence(cls) -> None:
        """Catch the ``scoped_slot`` / ``persistent_slots`` copy-paste footgun.

        A view with ``scoped_slot = "my_stats"`` writes scoped data to the
        ``my_stats`` bucket. Declaring ``persistent_slots = ("scoped",)``
        alongside that custom slot means the view persists the default
        bucket -- which nothing writes to -- while ``my_stats`` remains
        transient. Data vanishes on restart with no warning. Raising at
        class-definition time makes the mismatch impossible to ship.

        Reads effective (inherited) values via ``getattr`` so a subclass
        that overrides only one of the pair is still checked.
        """
        scoped_slot = getattr(cls, "scoped_slot", None)
        persistent_slots = tuple(getattr(cls, "persistent_slots", ()))
        if scoped_slot is None:
            return
        if "scoped" not in persistent_slots:
            return
        raise ValueError(
            f'{cls.__name__}.persistent_slots includes "scoped" but '
            f"scoped_slot is {scoped_slot!r}. Writes via dispatch_scoped() "
            f'go to {scoped_slot!r}, not "scoped", so persistence will '
            f"not capture them. Set persistent_slots = ({scoped_slot!r},) "
            f"to match, or remove scoped_slot if you meant to use the "
            f"default bucket."
        )

    def set_class_attribute(self, name: str, value) -> None:
        """Override a class-level policy attribute on this instance.

        Runs the same validator pipeline as ``__init_subclass__`` so
        per-invocation overrides catch typos and out-of-range values
        immediately instead of failing silently. Use this when a policy
        attribute must be parameterized from per-invocation data -- e.g.
        a slash-command argument selecting ``participant_limit`` for a
        lobby. For static configuration, set the attribute on the class
        body where ``__init_subclass__`` validates it once at definition
        time.

        Snowflake-domain instance data (``user_id``, ``guild_id``,
        ``allowed_users``) is intentionally rejected -- those have their
        own coercion paths and supported mutation idioms, and they are
        not class-level policy. Free-form attributes like ``*_message``
        strings are accepted without validation because there is no
        rule to enforce.

        Raises ``ValueError`` if the name is unknown to the class, the
        name is instance data, or the value fails validation.
        """
        cls = type(self)
        if name in cls._INSTANCE_DATA_ATTRS:
            raise ValueError(
                f"{name!r} is instance data, not a class-level policy attribute. "
                f"Use the supported mutation path (constructor argument or "
                f"register_participant) instead."
            )
        if not hasattr(cls, name):
            raise ValueError(f"{cls.__name__!r} has no attribute named {name!r}")
        # Reject methods, properties, and other descriptors -- only plain
        # class-level data attributes are overridable via this method.
        for klass in cls.__mro__:
            if name in klass.__dict__:
                attr = klass.__dict__[name]
                if callable(attr) or isinstance(attr, (property, classmethod, staticmethod)):
                    raise ValueError(
                        f"{name!r} is a method or property on {cls.__name__}, "
                        f"not a class-level policy attribute"
                    )
                break
        cls._validate_attribute_value(name, value)
        setattr(self, name, value)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _register_view_class(cls)
        cls._validate_class_attributes()

        # Register declared persistent slot names with the module-level
        # set the middleware checks. Sticky by design -- once any class
        # declares a slot name persistent, every ``access_slot`` write to
        # that name is flushed regardless of which reducer performs it.
        slots = cls.__dict__.get("persistent_slots")
        if slots:
            from ..state.slots import _PERSISTENT_SLOTS

            for slot_name in slots:
                _PERSISTENT_SLOTS.add(slot_name)

        # Wrap __init__ on subclasses that define their own, so kwargs are
        # auto-captured for push/pop reconstruction.  Only the outermost
        # (most-derived) wrapper captures; intermediate classes skip via
        # the _pending_init_kwargs guard.
        if "__init__" in cls.__dict__:
            original_init = cls.__init__

            @functools.wraps(original_init)
            def _capturing_init(self, *args, **kw):
                if not hasattr(self, "_pending_init_kwargs"):
                    # Positional args (beyond *args pass-through) cannot be
                    # captured for push/pop reconstruction.  Fail fast so the
                    # error is obvious at construction time, not at pop() time.
                    if args:
                        raise TypeError(
                            f"{type(self).__name__}.__init__() received positional "
                            f"arguments {args!r} which cannot be captured for "
                            f"push/pop reconstruction. Use keyword arguments instead."
                        )
                    self._pending_init_kwargs = {
                        k: v for k, v in kw.items() if k not in _NON_RECONSTRUCTIBLE_KWARGS
                    }
                original_init(self, *args, **kw)

            cls.__init__ = _capturing_init

        # Wrap build_ui to set the theme context automatically and
        # stabilize auto-generated custom_ids after the tree is built.
        # Builder functions like card() and stats_card() read the theme
        # context as a fallback when no explicit color= is passed.
        # Stable custom_ids prevent the ViewStore dispatch race described
        # on ``_stabilize_custom_ids``.
        if "build_ui" in cls.__dict__:
            original_build = cls.build_ui

            if inspect.iscoroutinefunction(original_build):

                @functools.wraps(original_build)
                async def _themed_build_ui(self, *args, **kw):
                    from ..theming.context import _current_theme, set_current_theme

                    token = set_current_theme(self.get_theme())
                    try:
                        result = await original_build(self, *args, **kw)
                        self._stabilize_custom_ids()
                        return result
                    finally:
                        _current_theme.reset(token)

            else:

                @functools.wraps(original_build)
                def _themed_build_ui(self, *args, **kw):
                    from ..theming.context import _current_theme, set_current_theme

                    token = set_current_theme(self.get_theme())
                    try:
                        result = original_build(self, *args, **kw)
                        self._stabilize_custom_ids()
                        return result
                    finally:
                        _current_theme.reset(token)

            cls.build_ui = _themed_build_ui

    @classmethod
    def _class_session_key(cls) -> str:
        """Collision-free identifier for this view class.

        Returns the fully-qualified class path (``module.QualName``) by
        default, which the Python import system guarantees unique across
        a running process. Used as the internal discriminator for session
        IDs, the session index, the nav-stack class registry, and session
        origin tracking.

        Subclasses may set a ``session_class_key`` class attribute to
        override the default, but should only do so to intentionally
        unify two distinct classes into one session family (rare). The
        override is read via ``cls.__dict__`` so it does not inherit  --
        each class opts in for itself.
        """
        override = cls.__dict__.get("session_class_key")
        if override:
            return override
        return f"{cls.__module__}.{cls.__qualname__}"

    def __init__(self, *args, **kwargs):
        # Extract custom arguments before passing to View/LayoutView
        self.state_store = kwargs.pop("state_store", None) or get_store()
        self.session_id = kwargs.pop("session_id", None)
        self.user_id = kwargs.pop("user_id", None)
        self.guild_id = kwargs.pop("guild_id", None)
        self.context = kwargs.pop("context", None)
        self.interaction = kwargs.pop("interaction", None)
        self.theme = kwargs.pop("theme", None) or getattr(type(self), "theme", None)
        self._persistence_key = kwargs.pop("persistence_key", None)
        _parent = kwargs.pop("parent", None)
        if _parent is not None and not isinstance(_parent, _StatefulMixin):
            raise TypeError(
                f"parent= must be a StatefulView or StatefulLayoutView instance, "
                f"got {type(_parent).__name__}"
            )
        self._pending_parent = _parent

        # Merge any kwargs auto-captured by the __init_subclass__ wrapper.
        # This includes all reconstructible kwargs from the most-derived
        # class, before any of them were consumed by intermediate __init__s.
        # For direct StatefulView usage (no wrapper), fall back to explicit
        # capture of the base class's own reconstructible kwargs.
        self._init_kwargs = getattr(self, "_pending_init_kwargs", {})
        if hasattr(self, "_pending_init_kwargs"):
            del self._pending_init_kwargs
        else:
            # No wrapper ran -- StatefulView used directly, capture manually
            if self.theme is not None:
                self._init_kwargs["theme"] = self.theme
            if self._persistence_key is not None:
                self._init_kwargs["persistence_key"] = self._persistence_key

        # Initialize the discord.py base class (View or LayoutView)
        super().__init__(*args, **kwargs)

        # Unique identifier for this view instance
        self.id = str(uuid.uuid4())

        # Message reference -- _message is a plain Message (channel endpoint,
        # no token expiry).  _webhook_message is the original InteractionMessage
        # or WebhookMessage whose .edit() routes through the interaction webhook
        # (can update embeds on interaction-response messages, but token expires
        # after 15 minutes).  refresh() tries _webhook_message first for embed
        # edits, falling back to _message on token expiry.
        self._message = None
        self._webhook_message = None
        self._ephemeral = False

        # Render-hash short-circuit. Stores a structural digest of the
        # component tree as it was last shipped to Discord. refresh()
        # computes a fresh digest, compares, and skips the REST edit
        # when they match -- saving both wall time and rate-limit
        # pressure on views that re-render identical trees (e.g. a
        # MyShipsView refreshing because its sibling BattleshipView
        # dispatched, when nothing in MyShipsView actually changed).
        # ``None`` means no baseline has been recorded yet, so the
        # next refresh always runs through to the REST call.
        self._last_tree_digest: Optional[int] = None

        # Whether state registration has been done
        self._registered = False

        # Session origin: when a view is pushed via navigation, this is set to
        # the root view's class name so the entire nav chain is tracked under
        # one session index key.  None means this view IS the root.
        self._instance_root_class: Optional[str] = None

        # View-local navigation stack.  On push, the new view receives
        # parent._nav_stack + [entry_for_parent].  On pop, the restored
        # view receives current._nav_stack[:-1].  Each view owns its own
        # breadcrumb trail independently.
        self._nav_stack: list = []

        # Participants: non-owner users registered in the session index.
        # Used by multi-user views (games, collaborative tools) so that
        # session limiting applies to all participants, not just the owner.
        self._participants: Set[int] = set()

        # Attached children: tracked for automatic cleanup on exit/timeout.
        # Views registered via attach_child() are exited when this view exits.
        self._attached_children: list = []

        # Back-pointer to the parent that called attach_child(self), if any.
        # Used by _reopen_ephemeral to migrate the tracked-child slot from
        # the old instance to the refreshed one in a single seam.
        self._attached_to = None

        # Ephemeral refresh state. _reopen_factory is an optional callable
        # that returns a freshly constructed view; set by callers that need
        # to capture live references the constructor can't take.
        self._reopen_factory = None
        self._refresh_armed: bool = False
        self._reopen_in_flight: bool = False

        # Get task manager
        self.task_manager = get_task_manager()

        # Interaction serialization lock -- prevents racing message edits
        # from rapid button clicks that cause "This interaction failed"
        self._interaction_lock = asyncio.Lock()

        # Update coalescing -- prevents concurrent on_state_changed calls
        # on the same view when multiple dispatches converge on one subscriber
        self._update_lock = asyncio.Lock()
        self._update_pending = False

        # Refresh throttling state. ``_refresh_not_before`` is a monotonic
        # timestamp marking the earliest moment the next edit may ship;
        # written by the proactive cooldown path (after a successful edit
        # when ``refresh_cooldown_ms`` is set) and by the reactive 429 path
        # (when Discord returns a rate-limit). ``_deferred_refresh_task``
        # holds the single pending retry, so N refreshes inside the window
        # produce one scheduled task, not N.
        self._refresh_not_before: float = 0.0
        self._deferred_refresh_task: Optional[asyncio.Task] = None

        # Derive user_id, guild_id, and session_id from context/interaction
        if self.interaction is None and self.context is not None:
            if hasattr(self.context, "interaction") and self.context.interaction:
                self.interaction = self.context.interaction

            if self.user_id is None and hasattr(self.context, "author"):
                self.user_id = self.context.author.id

            if self.guild_id is None and hasattr(self.context, "guild") and self.context.guild:
                self.guild_id = self.context.guild.id

        if self.interaction is not None:
            if self.user_id is None:
                self.user_id = self.interaction.user.id
            if self.guild_id is None and self.interaction.guild:
                self.guild_id = self.interaction.guild_id

        # Coerce user_id / guild_id at the single derivation seam. Catches
        # both the kwargs path (caller passed ``user_id=ctx.author``) and
        # any future ingress that might bypass the explicit ``.id`` reads
        # above. Raises ``TypeError`` immediately if either value is not
        # an ``int`` or a ``Snowflake``-shaped object.
        self.user_id = coerce_snowflake_id(self.user_id)
        self.guild_id = coerce_snowflake_id(self.guild_id)

        if self.session_id is None and self.user_id is not None:
            # The fully-qualified class path isolates view hierarchies
            # (separate nav stacks, undo history, etc.) so sibling
            # modules with bare-class-name collisions stay apart.
            # Pushed/popped views inherit session_id from their parent
            # via _navigate_to(), so the chain always stays on one
            # session regardless of what this derivation produces.
            #
            # A per-instance UUID suffix is the default so distinct
            # opens of the same view class produce distinct sessions
            # (the safe polarity for forms, games, lookups, etc.).
            # Setting ``session_continuity = True`` on the subclass
            # drops the suffix, restoring the class-coalesced shape
            # for flows whose undo history or shared_data should
            # survive close-and-reopen gestures.
            class_key = type(self)._class_session_key()
            if type(self).session_continuity:
                self.session_id = f"{class_key}:user_{self.user_id}"
            else:
                self.session_id = f"{class_key}:user_{self.user_id}:{uuid.uuid4().hex[:8]}"

        # Action types this view cares about -- subclasses override at
        # class level (e.g. subscribed_actions = {"MY_ACTION", ...}).
        # Default is an empty set: the view receives no notifications
        # unless it opts in.  Set to None to receive all actions.
        if "subscribed_actions" not in type(self).__dict__:
            self.subscribed_actions: Optional[Set[str]] = set()

        # Build selector from the view's state_selector method (if overridden)
        selector = self._build_selector()

        # Subscribe to state updates with action filter and selector
        self.state_store.subscribe(
            self.id, self._handle_state_notification, self.subscribed_actions, selector
        )

        # Register for undo tracking if this view has it enabled
        if self.enable_undo:
            self.state_store._undo_enabled_views[self.id] = self.undo_limit

    def create_task(self, coro):
        """Create a task owned by this view."""
        return self.task_manager.create_task(self.id, coro)

    @property
    def persistence_key(self) -> str:
        """Stable identity token for the persistence subsystem.

        For `PersistentView` subclasses this is the registry key that
        ties a view class to its reattachment row across restarts. For
        any view that keys a persistent
        `access_slot(..., persistent=True)` on `self.persistence_key`,
        this is the lookup bucket for that slot.

        When `persistence_key=...` is passed at construction it returns
        that value. Otherwise it falls back to `self.id`, a fresh UUID
        generated per instance.

        The UUID fallback is stable within one instance but not across
        reconstruction. Keying a persistent slot on
        `self.persistence_key` without passing an explicit
        `persistence_key=` writes under a new UUID every restart,
        orphaning the previous instance's data on disk. Pass a
        domain-stable value
        (e.g. `persistence_key=f"counter:{user_id}"`) whenever the slot
        is persistent.
        """
        return self._persistence_key or self.id

    def _build_selector(self):
        """Build a selector function if the subclass overrides state_selector.

        Returns None if state_selector is not overridden (base implementation),
        which means the subscriber receives all matching notifications.
        """
        # Only use a selector if the subclass actually overrides state_selector
        if type(self).state_selector is not _StatefulMixin.state_selector:
            return lambda state: self.state_selector(state)
        return None

    def state_selector(self, state):
        """Extract the state slice this view cares about.

        Override this in subclasses to enable selector-based filtering.
        The view will only receive on_state_changed() calls when the
        return value of this method changes between dispatches.

        Args:
            state: The full application state dict.

        Returns:
            Any value. The store compares old vs new using equality.
        """
        return None

    async def _register_state(self):
        """Register this view in the state store. Called once on first send."""
        if self._registered:
            return
        self._registered = True

        if self.session_id:
            payload = ActionCreators.session_created(
                session_id=self.session_id, user_id=self.user_id
            )
            await self.state_store.dispatch("SESSION_CREATED", payload)

        # Register the view
        payload = ActionCreators.view_created(
            view_id=self.id,
            view_type=self.__class__.__name__,
            user_id=self.user_id,
            session_id=self.session_id,
        )
        await self.state_store.dispatch("VIEW_CREATED", payload)

    async def _update_message_state(self, message):
        """Update state store with message info after sending."""
        if message is None:
            return
        payload = ActionCreators.view_updated(
            view_id=self.id,
            message_id=str(message.id),
            channel_id=str(message.channel.id) if message.channel else None,
        )
        await self.dispatch("VIEW_UPDATED", payload)

    async def _send_pipeline(self, send_kwargs, *, ephemeral=False):
        """Shared send pipeline for V1 and V2 views.

        Handles instance enforcement, state registration, participant
        claiming, ephemeral timeout clamping, Discord delivery (via
        context or interaction), message re-fetch for token-free editing,
        cleanup listener installation, ephemeral refresh scheduling,
        and parent auto-attach. Rolls back all state on failure at
        every stage.

        Args:
            send_kwargs: Dict of keyword arguments for the Discord send
                call. Must include ``view=self``. V1 adds ``content``,
                ``embed``, ``embeds``; V2 passes only ``{"view": self}``.
            ephemeral: Whether the message should be ephemeral.

        Returns:
            The sent ``discord.Message`` on success, or ``None`` when a
            policy gate blocked the send.
        """
        # -- Pre-flight check: V2 placement validation --
        # Walks the assembled component tree and rejects placements
        # Discord would 400 on. Runs before any state mutation so a
        # rejected tree leaves zero side effects. The helper gates on
        # ``validate_placement`` so V1 views (which lack the attribute)
        # skip naturally.
        self._check_placement()

        # -- Stage 1: instance enforcement --
        try:
            await self._enforce_instance_limit()
        except InstanceLimitError as e:
            await self.on_instance_limit(e)
            self.stop()
            self.state_store._unsubscribe(self.id)
            self.state_store._undo_enabled_views.pop(self.id, None)
            return None

        # -- Stage 2+3: state registration and participant claiming --
        # Batched so SESSION_CREATED + VIEW_CREATED collapse into one
        # BATCH_COMPLETE. On participant-rejection rollback, the queued
        # VIEW_DESTROYED joins the same batch and the whole self-cancelling
        # sequence fires as a single notification -- subscribers never see
        # a transient "view exists" state. ``source_id`` threads this view
        # through so its initial ``on_state_changed`` awaits inline and the
        # first render lands flush with the send response.
        async with self.state_store.batch(source_id=self.id):
            self.state_store._register_view(self)
            await self._register_state()

            # Seed hook fires after registration so the view exists in
            # state, but inside the batch so any seeding dispatches join
            # the same BATCH_COMPLETE notification. Subscribers see the
            # seeded slot from frame one. Default is a no-op.
            await self.seed_initial_state(self.state_store.state)

            if type(self).auto_register_participants:
                if not await self._auto_register_participants():
                    self.stop()
                    self.task_manager.cancel_tasks(self.id)
                    self.state_store._unsubscribe(self.id)
                    self.state_store._unregister_view(self.id)
                    self.state_store._undo_enabled_views.pop(self.id, None)
                    await self.dispatch("VIEW_DESTROYED", ActionCreators.view_destroyed(self.id))
                    return None

        # -- Stage 4: ephemeral refresh-handoff derivation --
        # auto_refresh_ephemeral is Optional[bool]. None means "derive
        # from the declared timeout"; explicit True or False overrides
        # the derivation. The declared timeout is the sole source of
        # truth for longevity -- the library never rewrites it. The
        # 900s threshold is the webhook token cliff: any view that
        # wants to live past it needs the handoff, anything inside it
        # does not.
        if ephemeral:
            self._ephemeral = True
            if self.auto_refresh_ephemeral is None:
                if self.timeout is None or self.timeout > 900:
                    self.auto_refresh_ephemeral = True
                else:
                    self.auto_refresh_ephemeral = False

        # -- Stage 5: Discord send --
        # Capture any caller-supplied attachments before the send so the
        # rollback path can close their underlying file pointers if the
        # send raises before discord.py's own ``finally`` runs (e.g.
        # validation failures inside discord.py reject the payload before
        # the HTTP layer is reached, and the file objects opened by the
        # caller are otherwise leaked).
        files_to_close = []
        if send_kwargs.get("file") is not None:
            files_to_close.append(send_kwargs["file"])
        if send_kwargs.get("files"):
            files_to_close.extend(send_kwargs["files"])

        class_name = type(self).__name__
        try:
            if self.context and hasattr(self.context, "send"):
                if ephemeral:
                    send_kwargs["ephemeral"] = ephemeral
                message = await self.context.send(**send_kwargs)

            elif self.interaction:
                send_kwargs["ephemeral"] = ephemeral
                if not self.interaction.response.is_done():
                    await self.interaction.response.send_message(**send_kwargs)
                    message = await self.interaction.original_response()
                else:
                    message = await self.interaction.followup.send(**send_kwargs, wait=True)

            else:
                raise RuntimeError(
                    f"{class_name}.send() requires either 'context' or 'interaction' to be set."
                )
        except Exception:
            for f in files_to_close:
                # Defensive double-close: discord.py closes attachments in
                # its own finally when the HTTP layer is reached. Calling
                # close() on an already-closed File is a no-op, so this
                # only changes behavior on the pre-HTTP failure path.
                try:
                    f.close()
                except Exception:
                    pass
            self.stop()
            self.task_manager.cancel_tasks(self.id)
            self.state_store._unsubscribe(self.id)
            self.state_store._unregister_view(self.id)
            self.state_store._undo_enabled_views.pop(self.id, None)
            await self.dispatch("VIEW_DESTROYED", ActionCreators.view_destroyed(self.id))
            raise

        # -- Stage 6: message re-fetch for token-free editing --
        if not ephemeral and isinstance(
            message, (discord.InteractionMessage, discord.WebhookMessage)
        ):
            self._webhook_message = message
            try:
                self._message = await message.channel.fetch_message(message.id)
            except discord.HTTPException:
                self._message = message
        else:
            self._message = message

        # Record the render-hash baseline: the tree Discord has right
        # now is the tree just sent. Subsequent refresh() calls
        # compare against this and skip the REST edit when nothing has
        # changed.  For V1 views that include embed kwargs in send(),
        # embed content is outside the digest, which is fine -- the
        # digest only certifies the component tree, and refresh() only
        # short-circuits when the caller passes no kwargs.
        self._last_tree_digest = self._compute_tree_digest()

        await self._update_message_state(self._message)

        # -- Stage 7: cleanup listener + ephemeral refresh + parent attach --
        if not self.state_store._cleanup_listener_installed:
            bot = getattr(self.interaction, "client", None) or getattr(self.context, "bot", None)
            if isinstance(bot, discord.Client):
                self.state_store._install_message_cleanup(bot)

        if ephemeral and self.auto_refresh_ephemeral:
            self.create_task(self._schedule_ephemeral_refresh())

        if self._pending_parent is not None:
            self._pending_parent.attach_child(self)
            self._pending_parent = None

        return self._message

    def get_theme(self):
        """Get the theme for this view, falling back to the global default.

        Returns a Theme instance. If no per-view theme is set and no global
        default exists, returns a bare Theme with standard defaults.
        """
        if self.theme is not None:
            return self.theme
        from ..theming.core import Theme, get_default_theme

        return get_default_theme() or Theme("fallback")

    # // ==================( Interaction Hooks )================== // #

    async def interaction_check(self, interaction: Interaction) -> bool:
        """Called before every component callback to validate the interaction.

        Access control priority:

        1. ``allowed_users`` is not None -- only users in the set can interact.
        2. ``owner_only`` is True -- only the view creator can interact.
        3. Otherwise -- all users can interact.

        When ``allowed_users`` is set, it overrides ``owner_only`` completely.
        Set it in ``__init__`` for dynamic allowlists::

            self.allowed_users = {self.user_id, opponent.id}

        The ``owner_only`` check is skipped when ``self.user_id`` is None
        (e.g. restored PersistentViews with no originating user context).
        ``allowed_users`` is always enforced regardless of ``user_id``.

        Override this for custom access control (e.g. role-based checks),
        calling ``await super().interaction_check(interaction)`` to preserve
        the built-in checks.
        """
        if self.allowed_users is not None:
            allowed = interaction.user.id in self.allowed_users
        elif self.owner_only and self.user_id is not None:
            allowed = interaction.user.id == self.user_id
        else:
            return True

        if not allowed:
            await self.on_unauthorized(interaction)
            return False
        return True

    async def on_unauthorized(self, interaction: Interaction) -> None:
        """Called when a non-allowed user tries to interact with this view.

        Default implementation sends ``unauthorized_message`` as an
        ephemeral response.  Override for custom UX (logging the attempt,
        sending a custom embed, falling back to a read-only view, etc.).

        This is *only* called when the user fails the
        ``allowed_users`` / ``owner_only`` check inside
        ``interaction_check``.  The library has already decided to reject
        the interaction by the time this method runs -- overriding it
        does not allow the interaction to proceed, only customizes the
        response shown to the user.
        """
        try:
            await interaction.response.send_message(self.unauthorized_message, ephemeral=True)
        except discord.HTTPException as e:
            logger.debug(f"Could not send unauthorized response in {self.__class__.__name__}: {e}")

    async def on_instance_limit(self, error: "InstanceLimitError") -> None:
        """Called when ``send()`` is blocked by the session limit.

        Default implementation sends an ephemeral response using
        ``instance_limit_message`` (or ``error.default_message`` if unset)
        on the originating interaction or context.  Override for custom
        UX, e.g. dynamic phrasing tied to the user who already owns the
        existing session.

        If neither an interaction nor a context is available (e.g.
        ``send()`` was called via a raw Messageable), the error is
        re-raised so the caller can handle it.
        """
        message = self.instance_limit_message or error.default_message

        if self.interaction is not None:
            try:
                await self.respond(self.interaction, message, ephemeral=True)
            except discord.HTTPException as e:
                logger.debug(
                    f"Could not send instance limit response in {self.__class__.__name__}: {e}"
                )
            return

        if self.context is not None and hasattr(self.context, "send"):
            try:
                await self.context.send(message, ephemeral=True)
            except discord.HTTPException as e:
                logger.debug(
                    f"Could not send instance limit response in {self.__class__.__name__}: {e}"
                )
            return

        # No interaction and no context -- nothing to respond on.  Re-raise
        # so the caller (likely a background task or raw Messageable path)
        # can decide what to do.
        raise error

    async def on_participant_limit(
        self, user_id: int, interaction: Optional[Interaction] = None
    ) -> None:
        """Called when ``register_participant`` is blocked by ``participant_limit``.

        Default implementation sends an ephemeral response using
        ``participant_limit_message`` on the supplied interaction (if any
        and not already responded). Override for custom UX -- mention the
        joiner, log the rejection, redirect to a waitlist, etc.

        Unlike ``on_instance_limit``, this hook does *not* re-raise on
        missing interaction: capacity rejection is a routine lobby event,
        not an exceptional one. Callers that need to know whether the
        registration succeeded should check the bool return value of
        ``register_participant``.
        """
        if interaction is None:
            return
        try:
            await self.respond(interaction, self.participant_limit_message, ephemeral=True)
        except discord.HTTPException as e:
            logger.debug(
                f"Could not send participant limit response in {self.__class__.__name__}: {e}"
            )

    async def on_replaced(self) -> None:
        """Called when this view is about to be replaced by a new session.

        Fired by ``_enforce_instance_limit`` before ``exit()`` when
        ``instance_policy = "replace"`` evicts this view.  At this point
        the view is fully intact: ``_message``, ``_participants``, and
        channel access are all still live.

        Default implementation sends ``replaced_message`` to the channel
        when the attribute is set and the view has participants.
        Override for custom behavior (DMs, embeds, logging, conditional
        notification).  Errors raised here are logged but never block
        the new view's ``send()``.
        """
        if self.replaced_message and self._participants and self._message:
            try:
                await self._message.channel.send(self.replaced_message)
            except discord.HTTPException as e:
                logger.debug(
                    f"Could not send replaced notification in {self.__class__.__name__}: {e}"
                )

    async def on_error(self, interaction: Interaction, error: Exception, item: Item) -> None:
        """Called when a component callback raises an exception.

        Sends an ephemeral error embed using ``error_message`` as the
        description.  Override for fully custom UX (different embed
        layout, DM the bot owner, conditional logging, etc.).
        """
        logger.error(f"Error in {item!r} of view {self.__class__.__name__}: {error}", exc_info=True)

        embed = discord.Embed(
            title="Something went wrong",
            description=self.error_message,
            color=discord.Color.red(),
        )

        try:
            await self.respond(interaction, embed=embed, ephemeral=True)
        except discord.HTTPException as e:
            logger.debug(f"Could not send error response in {self.__class__.__name__}: {e}")

    async def on_reopen_failure(
        self, interaction: Interaction, error: Exception | None = None
    ) -> None:
        """Called when the ephemeral refresh button fails to spawn a replacement.

        Only fires for ephemeral views with ``auto_refresh_ephemeral = True``.
        When the user clicks the refresh button after the 15-minute
        interaction token expires, the library attempts to construct a new
        view instance. This hook fires if that construction fails.

        Two failure modes:

        - ``error`` is an ``Exception``: the reopen factory raised.
          Sends ``reopen_failure_message`` as an ephemeral.
        - ``error`` is ``None``: the reopen factory returned ``None``,
          signaling the session has ended. Sends a generic "session
          ended" ephemeral and calls ``exit()``.

        Override for custom recovery, logging, or localized messages.
        """
        if error is not None:
            msg = self.reopen_failure_message
        else:
            msg = "This session has ended."
        try:
            await self.respond(interaction, msg, ephemeral=True)
        except discord.HTTPException as e:
            logger.debug(
                f"Could not send reopen failure response in {self.__class__.__name__}: {e}"
            )
        if error is None:
            await self.exit()

    def clear_items(self):
        """Override that preserves ``_view`` on old items during rebuilds.

        discord.py's ``clear_items()`` calls ``_update_view(None)`` on every
        child (recursively for V2 Containers/ActionRows).  Those items are
        still registered in the ``ViewStore`` dispatch table.  During the
        async gap between ``build_ui()`` and ``message.edit()`` completing,
        any pending interaction finds the old item but sees ``_view is None``
        and is discarded with a "View interaction referencing unknown view"
        warning.

        This override restores ``_view`` on old items so they remain
        routable until ``add_view()`` snapshot-diffs them out of the
        dispatch table.  Only activates after the view has been sent
        (``_message`` is set); constructor-time ``clear_items()`` calls
        are unaffected.
        """
        old_children = list(self._children) if self._message else []
        result = super().clear_items()
        for child in old_children:
            child._update_view(self)
        from ..tracing import is_viewstore_trace_enabled

        if self._message and is_viewstore_trace_enabled() and logger.isEnabledFor(logging.DEBUG):
            try:
                summary = []
                for c in old_children:
                    items = list(c.walk_children()) if hasattr(c, "walk_children") else [c]
                    for it in items:
                        if hasattr(it, "_provided_custom_id"):
                            summary.append(
                                f"{type(it).__name__}(id={id(it):x}, "
                                f"cid={getattr(it, 'custom_id', None)!r}, "
                                f"label={getattr(it, 'label', None)!r}, "
                                f"_view_was={'None' if it._view is None else 'set'})"
                            )
                logger.debug(
                    f"[viewstore-trace] clear_items view={self.id[:8]} "
                    f"cls={type(self).__name__} restored={len(summary)} :: " + "; ".join(summary)
                )
            except Exception as e:
                logger.debug(f"[viewstore-trace] clear_items trace failed: {e}")
        return result

    def _stabilize_custom_ids(self):
        """Rewrite auto-generated ``custom_id`` values on interactive items.

        discord.py assigns ``custom_id = os.urandom(16).hex()`` to any
        Button or Select constructed without an explicit ``custom_id=``.
        Every ``build_ui()`` rebuild produces fresh UUIDs, so the
        ``ViewStore`` dispatch table churns on every edit. During the
        async gap between the component rebuild and ``message.edit()``
        completing, a user click carrying an older ``custom_id`` is
        routed to an item the store has already evicted, triggering the
        "View interaction referencing unknown view" warning and a
        silently discarded click.

        This method runs at two seams: the ``__init_subclass__`` wrapper
        around ``build_ui`` (covers views whose rebuild routes through
        ``build_ui``) and the top of :meth:`refresh` (covers pattern
        rebuild paths that bypass ``build_ui`` -- tab switches, paginated
        page flips, wizard/form step advances, menu category changes).
        Each auto-generated id is rewritten to a deterministic anchor:

        - **Unique content** (one item with this callback + label)
          gets a content-only id. Stable across rebuilds even when
          conditional rendering shifts the item's tree position
          (e.g. an alert row appearing above the action row).
        - **Colliding content** (many items share a callback family,
          like the 9 cells of a TicTacToe board) falls back to a
          position-anchored id using tree coordinates. Stable across
          rebuilds even when a single cell's label changes from
          ``""`` to ``"X"``, because each cell's coordinates in the
          component tree do not shift when its label changes.

        Items with ``_provided_custom_id = True`` (user passed
        ``custom_id=`` explicitly) are skipped -- the escape hatch wins.
        """
        prefix = self.id[:8]

        # First pass: collect (item, content_key, tree coords) for every
        # interactive item with an auto-generated custom_id. Coordinates
        # are (container_index, position_within_container) where
        # container_index is the top-level child index in self._children
        # and position_within_container is the walk_children order.
        entries: list[tuple[Any, str, int, int]] = []
        for container_idx, top in enumerate(self._children):
            if hasattr(top, "walk_children"):
                inner = list(top.walk_children())
            else:
                inner = [top]
            for pos, item in enumerate(inner):
                if not hasattr(item, "_provided_custom_id"):
                    continue
                if item._provided_custom_id:
                    continue
                callback = getattr(item, "original_callback", None)
                callback_name = callback.__qualname__ if callback else "none"
                label = getattr(item, "label", "") or ""
                content_key = f"{callback_name}:{label}"
                entries.append((item, content_key, container_idx, pos))

        # Count content-key collisions. Unique keys get content-only ids;
        # collisions use position as the disambiguator so label mutations
        # on one cell do not shift the ids of its neighbors.
        key_counts: dict[str, int] = {}
        for _, ck, _, _ in entries:
            key_counts[ck] = key_counts.get(ck, 0) + 1

        from ..tracing import is_viewstore_trace_enabled

        trace_on = is_viewstore_trace_enabled() and logger.isEnabledFor(logging.DEBUG)
        assigned: list[str] = []
        for item, ck, c_idx, p_idx in entries:
            if key_counts[ck] == 1:
                item.custom_id = f"{prefix}:{ck}"
            else:
                item.custom_id = f"{prefix}:{ck}@{c_idx}.{p_idx}"
            if trace_on:
                assigned.append(f"{type(item).__name__}({id(item):x})={item.custom_id}")
        if assigned:
            logger.debug(
                f"[viewstore-trace] _stabilize_custom_ids view={self.id[:8]} "
                f"cls={type(self).__name__} :: " + "; ".join(assigned)
            )

    def _compute_tree_digest(self) -> int:
        """Return a structural hash of the rendered component tree.

        The digest captures only the fields Discord compares server-side
        when an edit is applied: ``custom_id``, ``label``, ``style``,
        ``disabled``, ``url``, ``placeholder``, emoji string form, and
        (for ``TextDisplay``/``Container`` items) the visible text.
        Anything else -- internal python ids, callback identity, ephemeral
        view back-references -- is deliberately excluded. Two views that
        would render identical bytes on the wire must produce the same
        digest, and two views that differ in any user-visible way must
        not.

        Used by :meth:`refresh` to short-circuit the REST ``message.edit``
        call when the tree has not changed since the last send or refresh.
        Saves a Discord API round-trip and relieves rate-limit pressure
        on channels where many subscribers react to
        the same action (every Battleship shot wakes all 4 player views;
        3 of them render identical trees).

        O(n) in number of walkable items. Cheap tuple hashing dominates,
        no ``repr`` calls, no string concatenation in the hot path.
        """
        parts: list = []
        for item in self.walk_children():
            # TextDisplay carries raw markdown and is checked first because
            # discord.py injects a ``custom_id`` attribute on every item
            # once it is attached to a parent view (for ViewStore tracking),
            # so ``hasattr(item, "custom_id")`` is True for TextDisplay too.
            # ``isinstance`` is the only reliable discriminator here.
            if isinstance(item, TextDisplay):
                parts.append(("t", item.content))
            # Buttons and selects: record the wire-visible attributes.
            elif hasattr(item, "custom_id"):
                parts.append(
                    (
                        "i",
                        getattr(item, "custom_id", None),
                        getattr(item, "label", None),
                        # ButtonStyle is an IntEnum; its value is what ships.
                        getattr(getattr(item, "style", None), "value", None),
                        getattr(item, "disabled", None),
                        getattr(item, "url", None),
                        getattr(item, "placeholder", None),
                        str(getattr(item, "emoji", None)) if getattr(item, "emoji", None) else None,
                    )
                )
            # Other layout items (Separator, MediaGallery, Thumbnail) do
            # not currently carry mutable state the user can observe
            # changing between rebuilds. If they gain one later, extend
            # here with a dedicated branch.
        return hash(tuple(parts))

    def _freeze_components(self):
        """Disable all interactive components in this view.

        V2 LayoutViews nest buttons inside ActionRow/Container, so the
        full tree is walked to reach them. V1 Views have flat children.
        """
        items = self.walk_children() if self._is_layout() else self.children
        for item in items:
            if hasattr(item, "disabled"):
                item.disabled = True

    async def on_timeout(self) -> None:
        """Called when the view times out. Disables all components and cleans up state."""
        # Exit tracked child views first
        await self._cleanup_attached_children()

        self._freeze_components()

        if self._message:
            try:
                await self._message.edit(view=self)
            except discord.NotFound:
                pass  # Message was already deleted
            except Exception as e:
                hint = ""
                if self._ephemeral:
                    hint = (
                        " This is likely because the interaction token expired (15-minute limit). "
                        "Ephemeral messages cannot be edited after the token expires."
                    )
                logger.warning(f"Could not disable components on timeout: {e}.{hint}")

        # Cancel tasks and clean up state, mirroring exit()
        self.task_manager.cancel_tasks(self.id)
        self.state_store._unsubscribe(self.id)
        self.state_store._unregister_view(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)

        await self.dispatch("VIEW_DESTROYED", ActionCreators.view_destroyed(self.id))

    async def on_message_delete(self) -> None:
        """Called when the view's message is deleted externally.

        Triggered by Discord's ``MESSAGE_DELETE`` gateway event when the
        message this view is attached to is removed (admin delete, bulk
        purge, etc.). The default implementation calls
        ``exit(delete_message=False)`` since the message is already gone.

        Override for custom behavior (logging, re-sending to a new
        message, notifying the owner). If you override without calling
        ``exit()``, the view stays registered as a ghost in the state
        store -- call ``super().on_message_delete()`` or ``exit()``
        explicitly to clean up.
        """
        # The message is already gone -- null the reference so exit()
        # skips the edit/delete block entirely (no stale NotFound error).
        self._message = None
        await self.exit(delete_message=False)

    async def _handle_state_notification(self, state, action):
        """React to state changes with update coalescing.

        When multiple dispatches trigger this callback concurrently on the
        same view (e.g. two players clicking buttons at once in a shared
        game), the second notification sets a pending flag and returns
        immediately. The first notification re-runs ``on_state_changed``
        with the latest store state after completing, capturing both
        changes in a single rebuild + edit cycle.

        Once the ephemeral refresh button has been armed, subsequent
        notifications are dropped: the view is intentionally frozen on the
        refresh button so it stays clickable inside the 90-second
        pre-warning window. Allowing rebuilds to proceed would clobber the
        button and leave the user with no recovery path once the
        interaction token expires.
        """
        logger.debug(f"View '{self.id}' received state update for action '{action['type']}'")

        if self._refresh_armed:
            return

        if self._update_lock.locked():
            self._update_pending = True
            return

        async with self._update_lock:
            while True:
                self._update_pending = False
                await self.on_state_changed(self.state_store.state)
                if not self._update_pending:
                    break

    async def seed_initial_state(self, state):
        """Initialize per-view state slots before the first subscriber notification.

        Called once during :meth:`send`, inside the registration batch, after
        the view is registered but before participant claiming and before the
        batch's BATCH_COMPLETE notification fires. Subclasses override this to
        dispatch actions or write to ``state["application"]`` so subscribers
        see the seeded state from frame one instead of an empty slot followed
        by a separate seeding dispatch.

        The hook receives the live store state dict. Any dispatches issued
        from inside join the surrounding batch, so the seed work and the
        view's own SESSION_CREATED / VIEW_CREATED collapse into a single
        notification cycle.

        Default is a no-op. Args:
            state: The current application state dict.
        """
        return

    async def on_state_changed(self, state):
        """Update this view based on current state.

        The default implementation calls ``build_ui()`` (if the subclass
        defines it) followed by :meth:`refresh`.  Override this method
        for custom state-driven updates, or when using a different rebuild
        method.

        ``build_ui()`` may be sync or async. If it returns a ``dict``, the
        dict is splatted as keyword arguments into :meth:`refresh` -- this
        is how V1 views pass a freshly built embed (``return {"embed":
        self._build_embed()}``) without needing a custom override. A return
        value of ``None`` (the V2 idiom: mutate the component tree, return
        nothing) calls ``refresh()`` with no extra kwargs.

        Args:
            state: The current application state.
        """
        build = getattr(self, "build_ui", None)
        if build is not None:
            result = build()
            if inspect.isawaitable(result):
                result = await result
            kwargs = result if isinstance(result, dict) else {}
            await self.refresh(**kwargs)

    async def refresh(self, **kwargs) -> None:
        """Edit the view's message to reflect the current component state.

        Passes ``view=self`` along with any extra *kwargs* (``embed``,
        ``content``, etc.) to ``message.edit()``.  Silently handles the
        case where the message no longer exists (``discord.NotFound``).

        This does **not** rebuild components -- call your rebuild method
        (e.g. ``build_ui()``) before calling ``refresh()``.

        Calls landing inside an active cooldown window (from
        ``refresh_cooldown_ms`` or a prior 429) are deferred via a single
        scheduled task that re-enters :meth:`on_state_changed` once the
        window expires, so the deferred edit reflects the latest store
        state rather than kwargs captured at the deferred call's site.

        Args:
            **kwargs: Additional keyword arguments forwarded to
                ``message.edit()`` (e.g. ``embed=``, ``content=``).
        """
        if not self._message:
            return

        # Throttle gate. Checked before the digest + edit path so a view
        # in cooldown costs one clock read, not a digest hash + REST call.
        now = time.monotonic()
        wait = self._refresh_not_before - now
        if wait > 0:
            if self._deferred_refresh_task is None or self._deferred_refresh_task.done():
                self._deferred_refresh_task = self.create_task(self._deferred_refresh(wait))
            return

        store = self.state_store
        perf_on = getattr(store, "_perf_enabled", False)
        t0 = time.perf_counter() if perf_on else 0.0
        skipped = False

        try:
            # Stabilize custom_ids on every refresh so rebuild paths that
            # bypass ``build_ui`` (tab switches, paginated page flips,
            # wizard/form step advances, menu category changes) also get
            # deterministic anchors. Without this, fresh interactive items
            # constructed in those rebuild methods carry ``os.urandom(16).hex()``
            # ids, and any user click landing after ``message.edit`` completes
            # but before the client renders the new payload routes through the
            # evicted dispatch-table entry and silently fails. Idempotent:
            # re-running against already-stable ids produces the same ids.
            self._stabilize_custom_ids()

            # Render-hash short-circuit. Only valid when the caller is not
            # supplying fresh embed/content kwargs -- those affect bytes
            # outside the component tree, so the digest cannot certify
            # they are unchanged. When no kwargs are present, a digest
            # match means the exact same message body would ship as last
            # time, and the REST call is safe to skip entirely.
            if not kwargs and self._last_tree_digest is not None:
                current_digest = self._compute_tree_digest()
                if current_digest == self._last_tree_digest:
                    skipped = True
                    return

            # Pre-flight check on the assembled tree before any of the
            # three edit paths ships. Skipped refreshes (digest match
            # above) bypass this -- nothing changed, the previous send
            # already validated. Catches mid-session shape changes
            # (Wizard step swaps, Form section toggles, Tab body
            # rebuilds) that would otherwise surface as HTTP 400 from
            # Discord rather than a clear ``ValueError`` at the seam.
            self._check_placement()

            # Acting-view fast path. When the currently-handled interaction
            # targets this view's message and its response slot is still open,
            # the edit piggybacks onto the interaction ack packet via
            # ``interaction.response.edit_message()`` -- one REST round-trip
            # instead of two (ack + channel PATCH). The contextvar is bound
            # by ``StatefulComponent.create_stateful_callback`` for the
            # duration of the callback + dispatch sequence, so only the
            # subscriber that ran inline on the acting dispatch reads a live
            # value. Disqualified cases (modal interactions, cross-view
            # message mismatch, already-deferred response, missing message
            # ref) fall through to the existing webhook/channel paths.
            #
            # The fast path couples ack to edit in one HTTP call. A slow
            # edit response from Discord (latency spike, ephemeral backend
            # under load) would starve the ack past the 3s interaction
            # deadline. The ``wait_for`` guard caps the fast path below
            # ``auto_defer_delay`` so a stall cancels the fast path and falls through
            # to the channel endpoint -- the auto-defer timer then ships
            # a standalone ack at ``auto_defer_delay`` seconds, well inside
            # the 3s window. ``_response_type`` is set only after the await
            # in discord.py, so cancellation leaves the interaction "not
            # done" and the fall-through paths behave as if the fast path
            # was never attempted.
            interaction = _CURRENT_INTERACTION.get()
            if (
                interaction is not None
                and interaction.type == discord.InteractionType.component
                and interaction.message is not None
                and interaction.message.id == self._message.id
                and not interaction.response.is_done()
            ):
                fast_path_timeout = max(0.5, self.auto_defer_delay - 1.0)
                try:
                    await asyncio.wait_for(
                        interaction.response.edit_message(view=self, **kwargs),
                        timeout=fast_path_timeout,
                    )
                    self._last_tree_digest = self._compute_tree_digest()
                    if perf_on:
                        store._record_edit()
                    self._stamp_cooldown()
                    return
                except asyncio.TimeoutError:
                    logger.debug(
                        f"Acting-view fast path exceeded {fast_path_timeout:.2f}s "
                        f"in {type(self).__name__}; channel-endpoint fall-through "
                        f"skipped, auto-defer timer handles ack"
                    )
                    # Fast path was cancelled. Channel-endpoint fall-through
                    # would ship a second edit attempt (~500ms) on top of
                    # the cancelled fast path, draining the auto-defer
                    # timer's budget for its own ack call -- under genuine
                    # Discord-side latency the cumulative cost crosses the
                    # 3s deadline and the user sees an interaction-failed
                    # toast. Returning here lets the timer ack at
                    # ``auto_defer_delay`` seconds with the full remaining
                    # budget. Whether Discord processed the cancelled edit
                    # server-side is indeterminate (cancellation race), so
                    # invalidate the digest -- the next refresh ships
                    # unconditionally, a redundant edit (when Discord did
                    # process this one) is cheaper than a stuck UI (when
                    # it did not).
                    self._last_tree_digest = None
                    return
                except discord.HTTPException as e:
                    if self._handle_rate_limit(e):
                        return
                    # Any other HTTP error falls through to the channel path
                    # so a transient failure on the interaction endpoint does
                    # not lose the edit entirely.

            # Interaction-response messages ignore embed edits via the channel
            # endpoint (PATCH /channels/{id}/messages/{id}).  When the caller
            # passes embed/embeds kwargs, try the stored webhook message first
            # -- its .edit() routes through the interaction webhook which CAN
            # update embeds.  Falls back to the plain Message on token expiry.
            if ("embed" in kwargs or "embeds" in kwargs) and self._webhook_message:
                try:
                    await self._webhook_message.edit(view=self, **kwargs)
                    self._last_tree_digest = self._compute_tree_digest()
                    if perf_on:
                        store._record_edit()
                    self._stamp_cooldown()
                    return
                except discord.HTTPException as e:
                    if self._handle_rate_limit(e):
                        return
                    # Token expired (15-min lifetime) -- fall through to channel endpoint
                    self._webhook_message = None

            try:
                await self._message.edit(view=self, **kwargs)
                self._last_tree_digest = self._compute_tree_digest()
                if perf_on:
                    store._record_edit()
                self._stamp_cooldown()
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                if not self._handle_rate_limit(e):
                    raise
        finally:
            if perf_on:
                store._refresh_samples.append(
                    {
                        "view_id": self.id,
                        "view_class": type(self).__name__,
                        "refresh_ms": (time.perf_counter() - t0) * 1000,
                        "skipped": skipped,
                        "timestamp": datetime.now().isoformat(),
                    }
                )

    def _handle_rate_limit(self, error: "discord.HTTPException") -> bool:
        """Detect a 429 response and arm the reactive backoff window.

        Returns ``True`` when ``error`` is a rate-limit and the next-allowed
        timestamp has been stamped (caller should swallow the exception).
        Returns ``False`` for any other HTTP error (caller should re-raise
        or handle per its own contract).
        """
        if getattr(error, "status", None) != 429:
            return False
        retry = getattr(error, "retry_after", 1.0)
        self._refresh_not_before = time.monotonic() + retry
        return True

    def _check_placement(self) -> None:
        """Run the V2 placement validator on this view if enabled.

        Single helper consumed by every seam that ships a tree to
        Discord: ``_send_pipeline`` (initial send), ``refresh`` (in-place
        edits), and ``_apply_navigation_edit`` (push/pop edits). V1
        views lack the ``validate_placement`` attribute so the
        ``getattr`` default of ``False`` makes the check a no-op for
        them. The validator import is lazy to keep ``base.py``'s import
        graph thin -- the check fires often (one walk per edit) but
        loads its module once.
        """
        if getattr(self, "validate_placement", False):
            from ._placement import validate_placement

            validate_placement(self)

    def _stamp_cooldown(self) -> None:
        """Arm the proactive cooldown window after a successful edit.

        No-op when ``refresh_cooldown_ms`` is ``None``, so zero-config
        views never touch the throttle state on the hot path.
        """
        if self.refresh_cooldown_ms:
            self._refresh_not_before = time.monotonic() + (self.refresh_cooldown_ms / 1000)

    async def _deferred_refresh(self, wait: float) -> None:
        """Sleep until the cooldown boundary, then re-enter on_state_changed.

        Re-entering :meth:`on_state_changed` (not calling :meth:`refresh`
        directly) means ``build_ui()`` re-runs against the latest store
        state -- so the deferred edit ships whatever the view should look
        like *at the moment it fires*, not whatever it looked like at the
        point the cooldown kicked in.
        """
        try:
            await asyncio.sleep(wait)
            if self.is_finished() or not self._message:
                return
            await self.on_state_changed(self.state_store.state)
        finally:
            self._deferred_refresh_task = None

    # // ========================================( Dispatch )======================================== // #

    async def dispatch(self, action_type, payload=None):
        """Dispatch an action to the state store."""
        return await self.state_store.dispatch(action_type, payload, source_id=self.id)

    @property
    def message(self):
        """Get the message associated with this view."""
        return self._message

    @message.setter
    def message(self, value):
        """Set the message associated with this view."""
        self._message = value

        # Update state with new message info
        if value:
            payload = ActionCreators.view_updated(
                view_id=self.id,
                message_id=str(value.id),
                channel_id=str(value.channel.id) if value.channel else None,
            )
            self.create_task(self.dispatch("VIEW_UPDATED", payload))

    # // ========================================( Batching )======================================== // #

    def batch(self):
        """Start an atomic batch of dispatches from this view.

        All ``dispatch()`` calls made while the batch is active (direct or
        transitive) queue into the batch. One ``BATCH_COMPLETE``
        notification fires at the outermost exit.

        The batch carries this view's id as ``source_id`` so the resulting
        ``BATCH_COMPLETE`` rides the same acting-view inline-notification
        path as single-dispatch calls -- the batched refresh lands flush
        with the interaction's ack cycle instead of being deferred behind
        cross-view fan-out.

        Usage:
            async with self.batch():
                await self.dispatch("ACTION_A", payload1)
                await self.dispatch("ACTION_B", payload2)
                await self.update_session(x=1)  # transitively batched
        """
        return self.state_store.batch(source_id=self.id)

    # // ========================================( Session Data )======================================== // #

    @property
    def shared_data(self) -> Dict[str, Any]:
        """Read the current session's ``shared_data`` dict.

        Shared data lives on the session and is visible to every view
        attached to the same ``session_id``. Returns an empty dict when
        the session does not exist or has no shared data yet.
        """
        session = self.state_store.state.get("sessions", {}).get(self.session_id, {})
        return session.get("shared_data", {})

    async def update_session(self, **data) -> Any:
        """Merge key-value pairs into the current session's ``shared_data`` dict.

        Dispatches ``SESSION_UPDATED``, which shallow-merges ``data``
        into ``state["sessions"][session_id]["shared_data"]`` and
        updates ``updated_at``. Other views subscribing to
        ``SESSION_UPDATED`` are notified and can react to the change.

        Args:
            **data: Key-value pairs to merge into the session's shared data.
        """
        payload = ActionCreators.session_updated(self.session_id, **data)
        return await self.dispatch("SESSION_UPDATED", payload)

    # // ========================================( Scoped State )======================================== // #

    def _resolve_scope_target(self) -> Dict[str, Any]:
        """Return the identifiers kwargs for this view's ``state_scope``.

        Handles all four legal scope values (``"user"``, ``"guild"``,
        ``"user_guild"``, ``"global"``). Raises ``ValueError`` if
        ``state_scope`` is unset or a required identifier is missing.
        Single source of truth for ``scoped_state`` and ``dispatch_scoped``.
        """
        scope = self.state_scope
        if scope is None:
            raise ValueError("Cannot resolve scope target: view has no state_scope set")
        if scope == "user":
            if self.user_id is None:
                raise ValueError("Cannot resolve 'user' scope: view has no user_id")
            return {"user_id": self.user_id}
        if scope == "guild":
            if self.guild_id is None:
                raise ValueError("Cannot resolve 'guild' scope: view has no guild_id")
            return {"guild_id": self.guild_id}
        if scope == "user_guild":
            missing = []
            if self.user_id is None:
                missing.append("user_id")
            if self.guild_id is None:
                missing.append("guild_id")
            if missing:
                raise ValueError(
                    f"Cannot resolve 'user_guild' scope: view has no {' and '.join(missing)}"
                )
            return {"user_id": self.user_id, "guild_id": self.guild_id}
        if scope == "global":
            return {}
        raise ValueError(f"Unknown state_scope: {scope!r}")

    @property
    def _effective_scoped_slot(self) -> str:
        """Resolved bucket name for this view's scoped writes.

        Falls back to the shared ``"scoped"`` bucket when ``scoped_slot``
        is unset. Single source of truth so every scoped read/write on
        the view agrees on the bucket.
        """
        return self.scoped_slot or "scoped"

    @property
    def scoped_state(self) -> Dict[str, Any]:
        """Get the scoped state slice for this view based on its state_scope class var.

        Returns an empty dict if no state_scope is set or identifiers are missing.
        """
        if self.state_scope is None:
            return {}
        try:
            identifiers = self._resolve_scope_target()
        except ValueError:
            return {}
        return self.state_store.get_scoped(
            self.state_scope,
            slot_name=self._effective_scoped_slot,
            **identifiers,
        )

    def scoped_state_for(self, scope: str, **overrides: Any) -> Dict[str, Any]:
        """Read a scoped state slice by explicit scope value, ignoring ``state_scope``.

        Lets a view read slices from scopes other than its own ``state_scope``
        class attribute. Identifiers default to the view's ``user_id`` and
        ``guild_id``; pass explicit overrides to target a different user/guild.

        Useful for hub views that aggregate data from ``user``, ``user_guild``,
        and ``global`` slices at the same time -- see ``examples/v2_settings.py``
        for the canonical pattern.

        Args:
            scope: One of ``"user"``, ``"guild"``, ``"user_guild"``, ``"global"``.
            **overrides: Optional explicit ``user_id`` / ``guild_id`` values.
                When omitted, the view's own identifiers are used.

        Returns:
            The scoped state slice as a dict. Empty dict when required
            identifiers are missing (matches ``scoped_state`` semantics).
        """
        slot = self._effective_scoped_slot
        if scope == "global":
            return self.state_store.get_scoped("global", slot_name=slot)

        uid = overrides.get("user_id", self.user_id)
        gid = overrides.get("guild_id", self.guild_id)

        if scope == "user":
            if uid is None:
                return {}
            return self.state_store.get_scoped("user", slot_name=slot, user_id=uid)
        if scope == "guild":
            if gid is None:
                return {}
            return self.state_store.get_scoped("guild", slot_name=slot, guild_id=gid)
        if scope == "user_guild":
            if uid is None or gid is None:
                return {}
            return self.state_store.get_scoped(
                "user_guild", slot_name=slot, user_id=uid, guild_id=gid
            )
        raise ValueError(f"Unknown scope: {scope!r}")

    def user_scoped_state(self, user_id: Optional[int] = None) -> Dict[str, Any]:
        """Read the ``"user"`` scope slice for the given (or own) user id.

        Sugar over ``scoped_state_for("user", ...)`` that reads at the
        call site like plain attribute access. Defaults to the view's
        own ``user_id`` when no argument is passed; returns an empty
        dict when the identifier is missing (matches ``scoped_state``).
        """
        return self.scoped_state_for(
            "user", user_id=user_id if user_id is not None else self.user_id
        )

    def guild_scoped_state(self, guild_id: Optional[int] = None) -> Dict[str, Any]:
        """Read the ``"guild"`` scope slice for the given (or own) guild id."""
        return self.scoped_state_for(
            "guild", guild_id=guild_id if guild_id is not None else self.guild_id
        )

    def user_guild_scoped_state(
        self,
        user_id: Optional[int] = None,
        guild_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Read the ``"user_guild"`` composite scope slice."""
        return self.scoped_state_for(
            "user_guild",
            user_id=user_id if user_id is not None else self.user_id,
            guild_id=guild_id if guild_id is not None else self.guild_id,
        )

    def global_scoped_state(self) -> Dict[str, Any]:
        """Read the ``"global"`` scope slice (single shared slot)."""
        return self.scoped_state_for("global")

    def _resolve_scoped_identifiers(
        self, scope: Optional[str], overrides: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Return identifier kwargs for ``scope``, with overrides winning over self attrs.

        Shared resolver for ``dispatch_scoped`` and ``dispatch_scoped_as``.
        Pulls defaults from ``self.user_id`` / ``self.guild_id`` for the
        scopes that need them, then applies ``overrides`` on top so
        callers can target another user's scope by passing
        ``user_id=...``. Raises ``ValueError`` when a required identifier
        is missing after the merge.
        """
        if scope is None:
            raise ValueError(
                "Cannot dispatch scoped action: no scope given and view has no state_scope set"
            )
        if scope == "global":
            return {}
        ids: Dict[str, Any] = {}
        if scope in ("user", "user_guild"):
            ids["user_id"] = overrides.get("user_id", self.user_id)
        if scope in ("guild", "user_guild"):
            ids["guild_id"] = overrides.get("guild_id", self.guild_id)
        if scope not in ("user", "guild", "user_guild"):
            raise ValueError(f"Unknown scope: {scope!r}")
        missing = [k for k, v in ids.items() if v is None]
        if missing:
            raise ValueError(
                f"Cannot resolve {scope!r} scope: missing identifiers {missing}. "
                f"Pass them as kwargs or ensure they are set on the view."
            )
        return ids

    async def dispatch_scoped(
        self,
        data: Dict[str, Any],
        *,
        scope: Optional[str] = None,
        **identifiers: Any,
    ) -> Any:
        """Dispatch a ``SCOPED_UPDATE`` action targeting a scoped state slice.

        Args:
            data: Dict of key-value pairs to shallow-merge into the slice.
            scope: Override the view's ``state_scope``. Default falls back
                to ``self.state_scope``.
            **identifiers: Override identifier kwargs (``user_id``,
                ``guild_id``). Defaults to ``self.user_id`` / ``self.guild_id``
                for whichever keys the scope needs. Pass an explicit
                ``user_id=`` to write into another player's scope.
        """
        effective_scope = scope if scope is not None else self.state_scope
        effective_ids = self._resolve_scoped_identifiers(effective_scope, identifiers)
        payload = {
            "scope": effective_scope,
            "identifiers": effective_ids,
            "data": data,
            "slot_name": self._effective_scoped_slot,
        }
        return await self.dispatch("SCOPED_UPDATE", payload)

    async def dispatch_scoped_as(
        self,
        action_type: str,
        data: Dict[str, Any],
        *,
        scope: Optional[str] = None,
        **identifiers: Any,
    ) -> Any:
        """Dispatch a named scoped action with the same payload shape as SCOPED_UPDATE.

        For patterns that need a custom reducer name (for subscriber
        filtering, domain-specific side effects) but still want to write
        into scoped state. Emits the canonical
        ``{"scope", "identifiers", "data"}`` payload shape so custom
        reducers share the same decode code as built-in
        ``SCOPED_UPDATE``.

        Args:
            action_type: Reducer name to dispatch (e.g. ``"SETTINGS_UPDATED"``).
            data: Dict of key-value pairs the reducer should merge.
            scope: Override the view's ``state_scope``. Default falls
                back to ``self.state_scope``.
            **identifiers: Override identifier kwargs (``user_id``,
                ``guild_id``). See ``dispatch_scoped`` for details.
        """
        effective_scope = scope if scope is not None else self.state_scope
        effective_ids = self._resolve_scoped_identifiers(effective_scope, identifiers)
        payload = {
            "scope": effective_scope,
            "identifiers": effective_ids,
            "data": data,
            "slot_name": self._effective_scoped_slot,
        }
        return await self.dispatch(action_type, payload)

    # // ========================================( Session Limiting )======================================== // #

    async def _enforce_instance_limit(self):
        """Enforce session limiting before sending.

        Called by concrete ``send()`` implementations. Exits overflow views
        under replace policy, or raises ``InstanceLimitError`` under reject policy.
        """
        if self.instance_limit is None:
            return

        scope_key = self.state_store._build_instance_scope_key(self)
        if scope_key is None:
            return

        view_type = self._instance_root_class or type(self)._class_session_key()
        display_type = type(self).__name__
        existing = self.state_store._get_active_views(view_type, scope_key)
        overflow = len(existing) - self.instance_limit + 1

        if overflow <= 0:
            return

        if self.instance_policy == "reject":
            raise InstanceLimitError(display_type, self.instance_limit)

        # Replace policy: only replace views owned by this user. Views where
        # this user is a participant (owned by someone else) are not replaceable.
        # Views with other users' investment (participants or attached children
        # belonging to a different user) are excluded when protect_attached is set.
        replaceable = [
            v
            for v in existing
            if v.user_id == self.user_id
            and not (v.protect_attached and v._has_other_users_attached(self.user_id))
        ]
        to_replace = replaceable[:overflow]

        if len(to_replace) < overflow:
            # Not enough owned views to replace (some are participant entries)
            raise InstanceLimitError(display_type, self.instance_limit)

        # Pre-scan: check all candidates before exiting any, so the
        # replace path never destroys views and then raises on a later
        # protected one.
        if not self._persistent:
            for old_view in to_replace:
                if getattr(old_view, "_persistent", False):
                    raise InstanceLimitError(display_type, self.instance_limit)

        # Notify each view before tearing it down. on_replaced fires
        # while the view is fully intact (message, participants, channel).
        # Errors are logged but never block the new view's send().
        for old_view in to_replace:
            try:
                await old_view.on_replaced()
            except Exception as e:
                logger.warning(f"on_replaced raised in {old_view.__class__.__name__}: {e}")

        # Exit oldest owned views to make room. Each view's replace_policy
        # decides what happens to its message: "delete" (default) removes
        # it so the new view cleanly supplants the old one; "disable"
        # freezes the components in place, leaving the message as a
        # static record in the channel.
        for old_view in to_replace:
            await old_view.exit(delete_message=(old_view.replace_policy == "delete"))

    @classmethod
    def check_instance_available(
        cls,
        *,
        user_id: int | None = None,
        guild_id: int | None = None,
        session_origin: str | None = None,
        state_store=None,
    ) -> bool:
        """Check whether a new instance can be created without hitting the limit.

        A lightweight pre-check that avoids constructing the view. Useful
        when ``__init__`` does expensive work (database queries, API calls)
        and you want to fail fast.

        Counts both owner and participant occupancy: participants are
        tracked in the instance index under their own scope key, so a
        user who is a participant in someone else's game will correctly
        fail this check.

        Args:
            user_id: The Discord user ID for user-scoped limits.
            guild_id: The Discord guild ID for guild-scoped limits.
            session_origin: Fully-qualified session key of the root view
                in a navigation chain. Pass this when checking availability
                for a view that will be pushed onto an existing chain.
                Defaults to ``cls._class_session_key()``.
            state_store: Optional ``StateStore`` instance. Uses the
                singleton if not provided.

        Returns:
            ``True`` if an instance slot is available (or no limit is set).
        """
        if cls.instance_limit is None:
            return True

        from ..state.singleton import get_store

        store = state_store or get_store()

        scope = cls.instance_scope
        if scope == "user":
            scope_key = f"user:{user_id}" if user_id else None
        elif scope == "guild":
            scope_key = f"guild:{guild_id}" if guild_id else None
        elif scope == "user_guild":
            scope_key = f"user_guild:{user_id}:{guild_id}" if user_id and guild_id else None
        elif scope == "global":
            scope_key = "global"
        else:
            scope_key = None

        if scope_key is None:
            return True

        view_type = session_origin or cls._class_session_key()
        existing = store._get_active_views(view_type, scope_key)
        return len(existing) < cls.instance_limit

    # // ========================================( Participants )======================================== // #

    async def register_participant(
        self, user_id, *, interaction: Optional[Interaction] = None
    ) -> bool:
        """Register a non-owner user as a participant in this view's session.

        Participants are tracked in the session index so that session limiting
        applies to them. For example, in a two-player game, the opponent should
        not be able to join a second game while already in one.

        Returns ``True`` on success and ``False`` on rejection. Two rejection
        paths are checked, in order:

        1. **Per-user session overflow** -- if the participant already has an
           active session of this view type, ``on_instance_limit`` fires with
           a ``InstanceLimitError`` (default response: ephemeral message on the
           supplied interaction). Returns ``False``.
        2. **View capacity overflow** -- if ``participant_limit`` is set and
           adding this user would exceed it, ``on_participant_limit`` fires.
           Returns ``False``.

        The owner is counted toward ``participant_limit``: a view with
        ``participant_limit = 4`` and a non-None ``user_id`` accepts at most
        three additional participants. Calling ``register_participant`` with
        the owner's own ID is a no-op that returns ``True``.

        Args:
            user_id: The Discord user ID (or any ``Snowflake``-shaped
                object) to register as a participant.
            interaction: Optional interaction to respond on if the
                registration is rejected. The default ``on_instance_limit``
                and ``on_participant_limit`` hooks both prefer this
                interaction over ``self.interaction`` so the joiner --
                not the view owner -- sees the rejection ephemeral.

        Returns:
            ``True`` if the participant was registered (or is already the
            owner), ``False`` if either limit blocked the registration.

        Raises:
            TypeError: If *user_id* is not an ``int`` or ``Snowflake``-shaped
                object.
        """
        user_id = coerce_snowflake_id(user_id)
        if user_id == self.user_id:
            return True  # Owner is already tracked via register_view

        # 1. Per-user session overflow check.
        if self.instance_limit is not None:
            scope_key = self.state_store._build_instance_scope_key(self, user_id=user_id)
            owner_key = self.state_store._build_instance_scope_key(self)
            if scope_key is not None and scope_key != owner_key:
                view_type = self._instance_root_class or type(self)._class_session_key()
                existing = self.state_store._get_active_views(view_type, scope_key)
                if len(existing) >= self.instance_limit:
                    error = InstanceLimitError(
                        type(self).__name__, self.instance_limit, blocked_user_id=user_id
                    )
                    # Temporarily swap the bound interaction so the default
                    # ``on_instance_limit`` responds to the joiner, not the
                    # owner. Subclass overrides see the same swap.
                    saved_interaction = self.interaction
                    if interaction is not None:
                        self.interaction = interaction
                    try:
                        await self.on_instance_limit(error)
                    finally:
                        self.interaction = saved_interaction
                    return False

        # 2. View capacity overflow check (participant_limit). Owner counts.
        if self.participant_limit is not None:
            current = len(self._participants) + (1 if self.user_id is not None else 0)
            if current >= self.participant_limit:
                await self.on_participant_limit(user_id, interaction=interaction)
                return False

        self._participants.add(user_id)
        self.state_store._register_participant(self, user_id)
        return True

    async def _auto_register_participants(self) -> bool:
        """Auto-register every non-owner ID in ``allowed_users``.

        Called by ``send()`` when ``auto_register_participants = True``.
        Iterates the set, claiming each participant slot in turn. On the
        first rejection, every previously claimed slot is rolled back so
        the failure leaves zero side effects, and the method returns
        ``False``. The caller is responsible for unregistering the view
        and skipping the Discord send.

        Returns:
            ``True`` if every participant claimed successfully, ``False``
            if any single registration was rejected (and rollback ran).
        """
        if not self.allowed_users:
            return True

        claimed: list[int] = []
        for uid in self.allowed_users:
            if uid == self.user_id:
                continue
            ok = await self.register_participant(uid, interaction=self.interaction)
            if not ok:
                for already in claimed:
                    self.unregister_participant(already)
                return False
            claimed.append(uid)
        return True

    def unregister_participant(self, user_id: int) -> None:
        """Remove a participant from this view's session tracking.

        Args:
            user_id: The Discord user ID to unregister.
        """
        self._participants.discard(user_id)
        self.state_store._unregister_participant(self, user_id)

    # // ========================================( Lifecycle )======================================== // #

    async def exit(self, delete_message: bool | None = None):
        """Cleanly exit and clean up this view.

        When ``delete_message`` is ``None`` (the default), the view's
        ``exit_policy`` decides: ``"disable"`` (the default) freezes
        the existing components in place, ``"delete"`` removes the
        message. Pass an explicit ``True`` or ``False`` to override
        the policy entirely.
        """
        if delete_message is None:
            delete_message = self.exit_policy == "delete"
        # Exit tracked child views first
        await self._cleanup_attached_children()

        # Cancel all tasks owned by this view
        self.task_manager.cancel_tasks(self.id)

        # Stop this view
        self.stop()

        # Unsubscribe BEFORE dispatching VIEW_DESTROYED so the view's own
        # subscriber doesn't re-render after component removal.
        self.state_store._unsubscribe(self.id)
        self.state_store._unregister_view(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)

        # Clean up the message
        if self._message:
            try:
                if delete_message:
                    await self._message.delete()
                elif self._is_layout():
                    # V2 messages ARE their components -- edit(view=None) would
                    # produce an empty message (error 50006).  Freeze instead.
                    self._freeze_components()
                    await self._message.edit(view=self)
                else:
                    await self._message.edit(view=None)
            except discord.NotFound:
                # Expected lifecycle: user dismissed the ephemeral, an
                # admin deleted the message, or the channel was deleted.
                # Nothing left to clean up on Discord's side.
                pass
            except discord.HTTPException as e:
                if self._ephemeral and getattr(e, "status", None) == 401:
                    # Expected lifecycle for ephemerals past the 15-minute
                    # webhook cliff: the token is gone, the message is
                    # un-editable. Debug-log so verbose runs can confirm
                    # the cleanup reached this path without flagging an
                    # error to operators.
                    logger.debug(
                        f"Exit cleanup skipped: ephemeral webhook token "
                        f"expired for {type(self).__name__}."
                    )
                else:
                    logger.error(f"Error cleaning up message: {e}")
            except Exception as e:
                logger.error(f"Error cleaning up message: {e}")

        await self.dispatch("VIEW_DESTROYED", ActionCreators.view_destroyed(self.id))

        return True

    def make_exit_button(
        self,
        label="Exit",
        style=discord.ButtonStyle.secondary,
        emoji="\u274c",
        delete_message=False,
        custom_id=None,
        row=None,
    ):
        """Return an exit button without attaching it to the view.

        Useful when the button must be packed into a caller-owned
        container (``ActionRow``, ``Section`` accessory, tab builder
        return list, etc.) rather than appended to ``self.children``
        directly. ``add_exit_button`` is the attach-to-self convenience
        wrapper; reach for this helper whenever the layout needs to own
        the button's placement.

        For ``PersistentView``/``PersistentLayoutView`` subclasses, pass
        ``custom_id`` so the button survives a restart.
        """

        async def exit_callback(interaction):
            await self.exit(delete_message=delete_message)

        return StatefulButton(
            label=label,
            style=style,
            row=row,
            emoji=emoji,
            custom_id=custom_id,
            callback=exit_callback,
        )

    def add_exit_button(
        self,
        label="Exit",
        style=discord.ButtonStyle.secondary,
        row=None,
        emoji="\u274c",
        delete_message=False,
        custom_id=None,
    ):
        """Add a button that exits this view when clicked.

        Thin wrapper over :meth:`make_exit_button` that attaches the
        result to ``self``. For PersistentView subclasses, pass a
        custom_id (e.g. ``custom_id="exit"``).
        """
        button = self.make_exit_button(
            label=label,
            style=style,
            row=row,
            emoji=emoji,
            delete_message=delete_message,
            custom_id=custom_id,
        )
        self.add_item(button)
        return button

    def clear_row(self, row: int):
        """Remove all components on the given row number.

        Useful for dynamically rebuilding a specific section of the view
        without affecting other rows.
        """
        for item in [c for c in self.children if getattr(c, "row", None) == row]:
            self.remove_item(item)

    def __del__(self):
        """Drop the state subscriber so GC can collect this view."""
        if hasattr(self, "state_store") and hasattr(self, "id"):
            self.state_store._unsubscribe(self.id)
            self.state_store._unregister_view(self.id)
